"""Verified Apple alias/rule cleanup with durable retry state."""
import json
import re
from creation_jobs import CreationJobs
from mail_provisioning import MailProvisioner


class EmailDeleteJobs(CreationJobs):
    document_kind = 'email_delete_job'
    target_label = '邮箱'

    def start_emails(self, records):
        with self._lock:
            if self.active: raise RuntimeError('已有删除任务运行中')
            self.records = {r['email']:dict(r) for r in records}
            self.contexts = {}
            return super().start(list(self.records), count=1, interval=0)

    def _context(self, account):
        if account not in self.contexts:
            try:
                client = self.manager.get_client(account)
                provisioner = MailProvisioner(client)
                provisioner.prepare_rules()
                expected = (self.manager.get_account(account).get('real_email') or '').lower()
                actual = (client.get_account_info().get('appleId') or '').lower()
                if expected and actual != expected:
                    raise RuntimeError('Cookie 所属 Apple 账号与记录不一致，未删除')
                self.contexts[account] = (client, provisioner)
            except Exception as error:
                self.contexts[account] = error
        result = self.contexts[account]
        if isinstance(result, Exception): raise result
        return result

    def create_item(self, target, label):
        row = self.records[target]
        account = row['account_id']
        with self.manager._workflow_lock(account):
            try:
                return self._delete(row)
            except Exception as error:
                message = str(error)[:250]
                from icloud_hme import ICloudSessionExpired
                status = re.search(r'HTTP (401|403|421|450)\b',message)
                # 收件服务的鉴权错误不能误判为 Apple Cookie 失效。
                expired = isinstance(error, ICloudSessionExpired) or bool(
                    status and status.group(1)=='421' and '短链接' not in message and '收件箱' not in message)
                if expired and status:
                    message = 'iCloud 会话不可用（HTTP '+status.group(1)+'），请更新所属账号 Cookie 后重试删除'
                if expired:
                    update = getattr(self.manager, 'update_account', None)
                    if update:
                        update(account, status='error', session_requires_login=True, last_error=message)
                self.storage.update_link(target, {'deletion_error':message})
                return {'ok':False, 'email':target, 'error':message, 'session_expired':expired}

    def _delete(self, row):
        email, account = row['email'], row['account_id']
        previous = self.storage.read_document('email_deletion', email) or {}
        # 旧版只删本地记录的邮箱仍需撤销远端短链接，从删除审计恢复清理凭据。
        link = {**previous.get('link', {}), **self.storage.load_links().get(email, {})}
        split = '+' in email.split('@',1)[0]
        if not split:
            client, p = self._context(account)
            rules = p.list_rules()
            own = [r for r in rules if p.matching_rule(r,email,link.get('destination',''))]
            saved = link.get('rule_id')
            if saved and any(str(r.get('ruleId'))==str(saved) and r not in own for r in rules):
                raise RuntimeError('保存的规则与邮箱归属不符，未删除，请检查配置')
            if any(r.get('prefix') in ('TO','RECP') and r.get('body','').lower()==email and r not in own for r in rules):
                raise RuntimeError('邮箱存在无法确认归属的规则，未删除，请检查配置')
            aliases = client.list_aliases(strict=True)
            alias = next((a for a in aliases if a['email'].lower()==email),None)
            if alias and not alias.get('anonymousId'): raise RuntimeError('Apple 邮箱标识缺失，未删除')
            # Preserve credentials and remote identifiers even across a crash.
            journal = self.storage.read_document('email_deletion',email) or {'email':email,'account_id':account,'link':link}
            journal.update(stage='deleting',rule_ids=[r['ruleId'] for r in own])
            self.storage.write_document('email_deletion',journal,email)
            self.storage.update_link(email,{'deleting':True,'deletion_error':'','stage':'deleting'})
            if alias:
                identifier = alias['anonymousId']
                if alias.get('active'):
                    self._mutate(client,'deactivate',identifier)
                    current = next((a for a in client.list_aliases(strict=True) if a['email'].lower()==email),None)
                    if current and current.get('active'): raise RuntimeError('Apple 未确认停用，保留记录供重试')
                self._mutate(client,'delete',identifier)
                if any(a['email'].lower()==email for a in client.list_aliases(strict=True)):
                    raise RuntimeError('Apple 未确认邮箱已删除，保留记录供重试')
            for rule in own:
                # No automatic replay after an ambiguous response; read-back decides.
                try:
                    client._request('POST',p.rules_url.rstrip('/')+'/batch',
                        json_data={'data':[{'operation':'DELETE','data':{'ruleId':rule['ruleId']}}]},max_attempts=1)
                except Exception:
                    pass
                if any(str(r.get('ruleId'))==str(rule['ruleId']) for r in p.list_rules()):
                    raise RuntimeError('Apple 未确认转发规则已删除，保留记录供重试')
            if any(p.matching_rule(r,email,link.get('destination','')) for r in p.list_rules()):
                raise RuntimeError('仍有对应转发规则，保留记录供重试')
            remaining = client.list_aliases(strict=True)
            with self.manager._lock:
                account_data = self.manager.accounts.get(account)
                if account_data is not None:
                    account_data['alias_total'] = len(remaining)
                    account_data['alias_active'] = sum(bool(a.get('active')) for a in remaining)
                    self.manager._save()
        if getattr(self, 'cleanup_inboxes', False):
            from public_inbox import revoke_inbox_link
            from mailbox_cleanup import delete_domain_inbox
            revoke_inbox_link({**link, 'email': email})
            if not split and not link.get('domain_inbox_deleted'):
                delete_domain_inbox({**link, 'email': email}, self.storage)
                # 物理邮箱删除已确认后立即保存，后续步骤重试不再使用已撤销的 JWT。
                self.storage.update_link(email, {'domain_inbox_deleted': True})
        # Plus rows only remove their own mapping; never touch their source rule.
        journal = self.storage.read_document('email_deletion',email) or {'email':email,'account_id':account,'link':link}
        journal.update(stage='completed',split=split,remote_cleanup_completed=bool(getattr(self, 'cleanup_inboxes', False)))
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not split:
                for child, raw in db.execute('SELECT email,data FROM inbox_links').fetchall():
                    record = json.loads(raw)
                    base = record.get('base_email') or child.split('+')[0]+'@'+child.split('@')[-1]
                    if '+' in child.split('@')[0] and base.lower()==email:
                        record.update(parent_deleted=True,configured=False,verified=False,stage='source_deleted',
                                      last_error='源邮箱已删除，无法继续接收邮件')
                        db.execute('UPDATE inbox_links SET data=? WHERE email=?',(json.dumps(record,ensure_ascii=False),child))
            db.execute('DELETE FROM email_records WHERE lower(email)=?',(email,))
            db.execute('DELETE FROM inbox_links WHERE email=?',(email,))
            db.execute('DELETE FROM fastai_sync WHERE email=?',(email,))
            db.execute('INSERT INTO documents VALUES(?,?,?) ON CONFLICT(kind,key) DO UPDATE SET data=excluded.data',
                       ('email_deletion',email,json.dumps(journal,ensure_ascii=False)))
        return {'ok':True,'email':email,'line':email+(' 分裂记录已删除' if split else ' Apple 邮箱及对应转发规则已确认删除')}

    @staticmethod
    def _mutate(client, operation, identifier):
        try:
            client._request('POST',client._service_url+'/v1/hme/'+operation,
                            json_data={'anonymousId':identifier},max_attempts=1)
        except Exception:
            # The next read confirms success or retains the local record for retry.
            pass
