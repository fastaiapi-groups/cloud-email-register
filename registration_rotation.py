"""按主账号执行容量轮换：旧地址清理、新源地址创建及分裂，逐项隔离失败。"""
import copy
import json
import re
import secrets
import time
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from creation_jobs import CreationJobs
from email_catalog import stamp
from email_delete import EmailDeleteJobs
from icloud_hme import ICloudSessionExpired
from mail_provisioning import MAX_MAIL_RULES, MailProvisioner
from plus_addressing import safe_error
from auto_plus import PlusCreationJobs


from registration_policy import ROTATION_DEFAULTS


class RegistrationJobs(CreationJobs):
    document_kind = 'registration_rotation_job'

    def start_registration(self, ids, config):
        with self._lock:
            if self.active:
                raise RuntimeError('已有自动注册任务执行中')
            if getattr(self, 'busy', lambda: False)():
                raise RuntimeError('已有其他邮箱任务执行中，请等待完成')
            self.config = copy.deepcopy(config)
            return super().start(ids, config['count'], config['label'],
                                 item_interval=config['item_interval_seconds'])

    def _event(self, job_id, account, action, result):
        result = {**result, 'account_id': account, 'action': action, 'finished_at': time.time()}
        with self._lock:
            job = self._jobs[job_id]
            job['events'].append(result)
            job['results'].setdefault(account, []).append(result)
            if result.get('skipped'):
                job['total_skipped'] += 1
            elif not result.get('ok'):
                job['total_errors'] += 1
            elif action in ('create_source', 'create_split'):
                key = 'total_created' if action == 'create_source' else 'total_split'
                job[key] = job.get(key, 0) + 1
                job['lines'].append(result['line'])
            elif action in ('delete_source', 'delete_split'):
                key = 'total_deleted' if action == 'delete_source' else 'total_deleted_split'
                job[key] = job.get(key, 0) + 1
            job['progress'] = result.get('message') or result.get('error') or action
            self._persist(job_id)
        if action == 'create_source' and self.on_result:
            self.on_result(result)
        return result

    @staticmethod
    def _error(error):
        return safe_error(error) if re.search(r'HTTP \d{3}', str(error)) else str(error)[:250]

    def _expired(self, account, error=None):
        # 域名收件服务的 401/403 不等于 Apple Cookie 失效。
        return (isinstance(error, ICloudSessionExpired)
                or bool((self.manager.get_account(account) or {}).get('session_requires_login')))

    def _quota_state(self, db, now, account):
        """统计滑动窗口，释放时间沿用相同去重规则，避免成功与尝试重复计额。"""
        window = self.config['quota_window_minutes'] * 60
        db.create_function('mail_stamp', 1, stamp)
        sources = {}
        for email, created in db.execute(
                "SELECT email,mail_stamp(created_at) FROM email_records WHERE account_id=? "
                "AND instr(substr(email,1,instr(email,'@')-1),'+')=0 AND mail_stamp(created_at)>?",
                (account, now - window)):
            email = email.lower()
            sources[email] = max(sources.get(email, created), created)
        attempts = [json.loads(raw) for raw, in db.execute(
            "SELECT data FROM documents WHERE kind='registration_attempt' "
            "AND json_extract(data,'$.account_id')=? AND json_extract(data,'$.started_at')>?",
            (account, now - window))]

        def usage(at):
            recent = {email for email, created in sources.items() if created + window > at}
            return len(recent) + sum(row['started_at'] + window > at and
                (not row.get('email') or row['email'].lower() not in recent) for row in attempts)

        used = usage(now)
        # 邮箱创建时间可能晚于预留时间，不能直接把最早尝试时间当释放时间。
        expiries = sorted({t + window for t in sources.values()} |
                          {row['started_at'] + window for row in attempts})
        release = next((at for at in expiries if usage(at) < used), None)
        return {'used': used, 'remaining': max(0, self.config['hourly_create_limit'] - used),
                'limit': self.config['hourly_create_limit'],
                'window_minutes': self.config['quota_window_minutes'], 'next_release_at': release}

    def quota_status(self, account):
        with self.storage.connect() as db:
            return self._quota_state(db, time.time(), account)

    def _log_quota(self, job_id, account, phase):
        quota = self.quota_status(account)
        release = (datetime.fromtimestamp(math.ceil(quota['next_release_at']), ZoneInfo('Asia/Shanghai'))
                   .strftime('%Y-%m-%d %H:%M:%S') + '（北京时间）'
                   if quota['next_release_at'] else '无占用，无待释放额度')
        self._event(job_id, account, 'quota', {'ok': True, 'quota': quota,
            'message': f"{phase}额度：最近 {quota['window_minutes']} 分钟最多 {quota['limit']} 个源邮箱；"
                       f"已用 {quota['used']}，剩余 {quota['remaining']}；下次释放：{release}（无新增创建时）"})

    def _reserve_quota(self, account, reserve=False):
        now = time.time()
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            available = self._quota_state(db, now, account)['remaining']
            if not reserve:
                return available
            if not available:
                return None
            key = secrets.token_hex(12)
            db.execute('INSERT INTO documents VALUES(?,?,?)', ('registration_attempt', key,
                json.dumps({'account_id': account, 'started_at': now})))
            return key

    def _oldest(self, account, aliases, rules=None):
        links = self.storage.load_links()
        local = {row['email'].lower(): row for row in self.storage.emails() if row['account_id'] == account}
        candidates = []
        for alias in aliases:
            email = alias.get('email', '').lower()
            record = local.get(email)
            if not record or '+' in email.split('@')[0]:
                continue
            if links.get(email, {}).get('account_id') not in (None, account):
                continue
            # 规则容量不足时，只删除确实能释放本项目转发规则的源地址。
            if rules is not None and not any(MailProvisioner.matching_rule(
                    rule, email, links.get(email, {}).get('destination', '')) for rule in rules):
                continue
            created = stamp(alias.get('createdAt')) or stamp(record.get('created_at'))
            if created is not None:
                candidates.append((created, email))
        # 无法确认创建时间的地址不猜测为最老地址；只清理本项目明确管理的源地址。
        return [email for _, email in sorted(candidates)[:self.config['rotation_count']]]

    def _children(self, base, account):
        links = self.storage.load_links()
        emails = {row['email'].lower() for row in self.storage.emails() if row['account_id'] == account}
        emails.update(links)
        # 兼容历史上只删除本地分裂记录、尚未撤销远端收件映射的地址。
        with self.storage.connect() as db:
            emails.update(email for email, in db.execute(
                "SELECT key FROM documents WHERE kind='email_deletion' "
                "AND json_extract(data,'$.account_id')=? "
                "AND COALESCE(json_extract(data,'$.remote_cleanup_completed'),0)=0", (account,)))
        local, domain = base.split('@')
        return sorted(email for email in emails if email.startswith(local + '+') and email.endswith('@' + domain))

    def _delete(self, deleter, job_id, account, email, split, cancel):
        action = 'delete_split' if split else 'delete_source'
        result = None
        for attempt in range(self.config['delete_retries'] + 1):
            if cancel.is_set():
                return None
            journal = self.storage.read_document('email_deletion', email) or {}
            record = {**journal.get('link', {}), **self.storage.load_links().get(email, {})}
            if journal.get('account_id') not in (None, account):
                record['account_id'] = journal['account_id']
            if record.get('account_id') not in (None, account):
                result = {'ok': False, 'email': email, 'error': '收件配置所属账号不一致，未删除'}
                break
            deleter.records[email] = {'email': email, 'account_id': account}
            result = deleter.create_item(email, '')
            result['attempts'] = attempt + 1
            if result.get('ok') or result.get('session_expired') or self._expired(account):
                break
            # 非会话问题允许重试一次；重试仍先回读远端，避免盲目重发删除。
            if isinstance(deleter.contexts.get(account), Exception):
                deleter.contexts.pop(account, None)
        if result is not None:
            result['message'] = email + (' 分裂收件配置已撤销' if split else ' 源邮箱、规则和收件配置已删除') if result.get('ok') else None
            return self._event(job_id, account, action, result)

    def _account(self, job_id, account, cancel):
        config = self.config
        if self._expired(account):
            self._event(job_id, account, 'skip', {'ok': False, 'skipped': True, 'error': '主账号 Cookie 已失效，跳过本轮'})
            return
        # 检查额度在删除之前，避免本小时已无创建额度却仍清理旧地址。
        budget = min(config['count'], self._reserve_quota(account))
        if not budget:
            self._event(job_id, account, 'skip', {'ok': False, 'skipped': True, 'error': '本主账号已达到创建窗口限额，本轮不删除也不创建'})
            return
        client = self.manager.get_client(account)
        aliases = client.list_aliases(strict=True)
        deleter = EmailDeleteJobs(self.manager)
        deleter.records, deleter.contexts, deleter.cleanup_inboxes = {}, {}, True
        rules = None
        rule_rotation_needed = False
        if config['rotation_enabled']:
            # 两种容量独立计算；源地址未达阈值时，转发规则也可能已满。
            # 预先核验 Apple 身份和规则权限，失败时不先删除分裂地址。
            _, provisioner = deleter._context(account)
            rules = provisioner.list_rules()
            rule_rotation_needed = (len(rules) >= config['rotation_threshold']
                                    or max(0, MAX_MAIL_RULES - len(rules)) < budget)
        if config['rotation_enabled'] and (len(aliases) >= config['rotation_threshold'] or rule_rotation_needed):
            selected = self._oldest(account, aliases, rules if rule_rotation_needed else None)[:budget]
            self._event(job_id, account, 'plan', {'ok': True,
                'message': f'主账号当前有 {len(aliases)} 个隐私地址、{len(rules)} 条转发规则，'
                           f'选中最老的 {len(selected)} 个源地址及其全部分裂地址进行轮换'})
            if not selected:
                self._event(job_id, account, 'skip', {'ok': False, 'skipped': True,
                    'error': '没有可确认归属和创建时间的源地址，本轮不删除也不创建'})
                return
            with self._lock:
                self._jobs[job_id].setdefault('rotation_plans', {})[account] = {
                    'alias_total': len(aliases), 'sources': selected,
                    'children': {base: self._children(base, account) for base in selected}}
                self._persist(job_id)
            released = 0
            for base in selected:
                if cancel.is_set():
                    return
                children_ok = True
                for child in self._children(base, account):
                    result = self._delete(deleter, job_id, account, child, True, cancel)
                    if result is None:
                        return
                    children_ok = children_ok and bool(result.get('ok'))
                    if result.get('session_expired') or self._expired(account):
                        return
                if not children_ok:
                    self._event(job_id, account, 'skip', {'ok': False, 'skipped': True, 'email': base,
                        'error': '关联分裂地址未全部清理成功，保留该源地址，继续其他源地址'})
                    continue
                result = self._delete(deleter, job_id, account, base, False, cancel)
                if result is None:
                    return
                if result.get('session_expired') or self._expired(account):
                    return
                released += bool(result.get('ok'))
            # 只补建完整释放的名额，删除失败不会导致超量新建。
            budget = min(budget, released)
        splitter = PlusCreationJobs(self.manager)
        for index in range(budget):
            if cancel.is_set() or self._expired(account):
                return
            if index and cancel.wait(config['item_interval_seconds']):
                return
            reservation = self._reserve_quota(account, reserve=True)
            if reservation is None:
                self._event(job_id, account, 'skip', {'ok': False, 'skipped': True, 'error': '创建窗口额度已用尽，剩余项跳过'})
                return
            try:
                result = self.manager.create_aliases_for_account(account, 1, config['label'])[0]
            except Exception as error:
                result = {'ok': False, 'error': self._error(error), 'session_expired': self._expired(account, error)}
            saved = self.storage.read_document('registration_attempt', reservation)
            saved['email'] = result.get('email')
            self.storage.write_document('registration_attempt', saved, reservation)
            result['error'] = self._error(RuntimeError(result.get('error', ''))) if not result.get('ok') else ''
            self._event(job_id, account, 'create_source', {**result,
                'message': result.get('email', '') + ' 新源地址及收件配置完成' if result.get('ok') else None})
            if result.get('session_expired') or self._expired(account):
                return
            if not result.get('ok'):
                continue
            for _ in range(config['split_per_source']):
                if cancel.is_set():
                    return
                child = splitter.create_item(result['email'], 'rotate')
                self._event(job_id, account, 'create_split', {**child,
                    'message': (child.get('email') or '') + ' 新分裂地址及收件配置完成' if child.get('ok') else None})

    def _run(self, job_id, account_ids, count, label, interval, repair, item_interval=0):
        cancel = self._cancel_events[job_id]
        try:
            with self._lock:
                self._jobs[job_id].update(total_split=0, total_deleted=0, total_deleted_split=0)
                self._persist(job_id)
            for account in account_ids:
                if cancel.is_set():
                    break
                try:
                    # 全流程持有账号工作锁，防止其他线程在选择旧地址后插入分裂或创建。
                    with self.manager._workflow_lock(account):
                        self._log_quota(job_id, account, '执行前')
                        try:
                            self._account(job_id, account, cancel)
                        finally:
                            self._log_quota(job_id, account, '本账号结束时')
                except Exception as error:
                    self._event(job_id, account, 'account_error', {'ok': False,
                        'error': '主账号 Cookie 失效，跳过该账号' if self._expired(account, error) else self._error(error)})
            with self._lock:
                job = self._jobs[job_id]
                job['status'] = 'cancelled' if cancel.is_set() else 'completed'
                job['progress'] = (f"本轮结束：新源 {job['total_created']}，分裂 {job.get('total_split', 0)}，"
                                   f"删除源 {job.get('total_deleted', 0)}，失败 {job['total_errors']}，跳过 {job['total_skipped']}")
                job['output'] = '\n'.join(job['lines'])
                job['ok'] = not job['total_errors'] and not job['total_skipped']
        except Exception as error:
            self._jobs[job_id].update(status='failed', error=self._error(error))
        finally:
            with self._lock:
                self._jobs[job_id]['finished_at'] = time.time()
                self._persist(job_id)
                self._active = None
                self._cancel_events.pop(job_id, None)
