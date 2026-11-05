"""自动注册容量轮换：最老地址、子地址先删、失败隔离、创建额度和配置。"""
import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from storage import Storage
from registration_rotation import RegistrationJobs
from registration_policy import ROTATION_DEFAULTS
from auto_registration import AutoRegistration
from icloud_hme import ICloudHME, ICloudSessionExpired, ICloudCreationLimitError


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Storage(Path(self.tmp.name)/'state.sqlite3')
        self.calls = []
        self.aliases = {}
        self.rules = {}
        self.accounts = {'a': {}, 'b': {}}
        self.manager = SimpleNamespace(storage=self.store, accounts=self.accounts,
            get_account=lambda key: self.accounts.get(key), _workflow_lock=lambda _:threading.RLock(),
            get_client=lambda key:SimpleNamespace(list_aliases=lambda **_:copy.deepcopy(self.aliases.get(key, []))),
            create_aliases_for_account=self.create)
        self.jobs = RegistrationJobs(self.manager)
        self.config = {**AutoRegistration.defaults, 'account_ids':['a'], 'item_interval_seconds':0,
                       'rotation_threshold':5}
        outer = self
        class Deleter:
            def __init__(self, manager):
                self.records, self.contexts = {}, {}
            def _context(self, account):
                if outer.accounts[account].get('session_requires_login'):
                    raise ICloudSessionExpired('HTTP 421')
                return outer.manager.get_client(account), SimpleNamespace(
                    list_rules=lambda: copy.deepcopy(outer.rules.get(account, [])))
            def create_item(self, email, label):
                outer.calls.append(('delete', email))
                outcome = outer.failures.get(email, [])
                if outcome:
                    result = outcome.pop(0)
                    if result is not None:
                        return result
                with outer.store.connect() as db:
                    db.execute('DELETE FROM inbox_links WHERE email=?',(email,))
                    db.execute('DELETE FROM email_records WHERE email=?',(email,))
                return {'ok':True,'email':email,'line':email}
        self.failures = {}
        self.patches = [patch('registration_rotation.EmailDeleteJobs', Deleter),
                        patch('registration_rotation.PlusCreationJobs.create_item', side_effect=self.split)]
        for item in self.patches:
            item.start();self.addCleanup(item.stop)

    def sources(self, count=5, account='a'):
        for index in range(count):
            email=f'{account}{index}@icloud.com'
            self.store.add_email(email,account)
            with self.store.connect() as db:
                db.execute("UPDATE email_records SET created_at='2000-01-01T00:00:00Z' WHERE email=?",(email,))
            destination=f'{account}{index}@fastaiapi.cloud'
            self.store.update_link(email,{'email':email,'account_id':account,'configured':True,
                                         'inbox_url':'public','destination':destination})
            self.rules.setdefault(account,[]).append({'prefix':'TO','body':email,
                                                      'action':'FRWD','target':destination})
            self.aliases.setdefault(account,[]).append({'email':email,'createdAt':1000+index,'active':True})
            child=f'{account}{index}+old@icloud.com'
            self.store.add_email(child,account)
            self.store.update_link(child,{'email':child,'account_id':account,'base_email':email})
        # 时间顺序与接口顺序相反，确保不是按接口返回顺序误删。
        self.aliases[account].reverse()

    def create(self, account, count, label):
        self.calls.append(('create',account))
        email=f'{account}new{len(self.calls)}@icloud.com'
        self.store.add_email(email,account)
        return [{'ok':True,'email':email,'inbox_url':'public','line':email+'----public','alias_created':True}]

    def split(self, base, label):
        self.calls.append(('split',base))
        return {'ok':True,'email':base.replace('@','+new@'),'line':base+'----split','inbox_url':'split'}

    def run_job(self, config=None, ids=None):
        key=self.jobs.start_registration(ids or ['a'],config or self.config)
        deadline=time.monotonic()+3
        while self.jobs.active and time.monotonic()<deadline:
            time.sleep(.005)
        self.assertFalse(self.jobs.active)
        return self.jobs.get(key)

    def test_threshold_deletes_all_children_first_then_five_sources_creates_and_splits(self):
        self.sources(7)
        job=self.run_job()
        deleted=[email for action,email in self.calls if action=='delete']
        self.assertEqual(deleted,[value for index in range(5) for value in [f'a{index}+old@icloud.com',f'a{index}@icloud.com']])
        self.assertEqual((job['total_deleted'],job['total_created'],job['total_split']),(5,5,5))
        self.assertIn('a5@icloud.com',self.store.load_links())
        # 所有删除先完成，再进入新建阶段。
        self.assertEqual([action for action,_ in self.calls[:10]],['delete']*10)

    def test_single_delete_retries_once_and_other_sources_continue(self):
        self.sources()
        email='a0+old@icloud.com'
        self.failures[email]=[{'ok':False,'email':email,'error':'temporary'},None]
        job=self.run_job()
        self.assertEqual(self.calls.count(('delete',email)),2)
        self.assertEqual(job['total_created'],5)

    def test_permanent_child_failure_preserves_parent_and_other_four_pairs_continue(self):
        self.sources()
        child='a0+old@icloud.com'
        self.failures[child]=[{'ok':False,'email':child,'error':'failed'}]*2
        job=self.run_job()
        self.assertNotIn(('delete','a0@icloud.com'),self.calls)
        self.assertEqual((job['total_created'],job['total_split'],job['total_deleted']),(4,4,4))

    def test_cookie_expired_skips_account_before_deleting_children_other_account_continues(self):
        self.sources(account='a');self.sources(account='b')
        self.accounts['a']['session_requires_login']=True
        job=self.run_job(ids=['a','b'])
        self.assertFalse(any(action=='delete' and email.startswith('a') for action,email in self.calls))
        self.assertEqual(job['total_created'],5)

    def test_failed_source_creation_and_split_do_not_abort_following_items(self):
        self.sources(1)
        original=self.manager.create_aliases_for_account
        attempts=0
        def create(*args):
            nonlocal attempts
            attempts+=1
            return [{'ok':False,'error':'one failed'}] if attempts==1 else original(*args)
        self.manager.create_aliases_for_account=create
        with patch('registration_rotation.PlusCreationJobs.create_item',return_value={'ok':False,'error':'split failed'}):
            job=self.run_job()
        self.assertEqual(attempts,5)
        self.assertEqual(job['total_created'],4)
        self.assertEqual(job['total_errors'],5)

    def test_apple_creation_limit_stops_retries_and_keeps_error_code(self):
        for description in ('Error Description not available', 'Please try again later'):
            with self.subTest(description=description):
                client = ICloudHME({}, verbose=False)
                client._service_url = 'https://example.icloud.com'
                client.generate = Mock(return_value='candidate@icloud.com')
                client._request = Mock(return_value={'success': False, 'error': {
                    'errorCode': '-41015', 'errorMessage': description, 'retryAfter': 2}})
                with self.assertRaisesRegex(ICloudCreationLimitError, '-41015'):
                    client.create_alias(max_retries=5)
                self.assertEqual(client.generate.call_count, 1)
                self.assertEqual(client._request.call_count, 1)
        client._request.return_value['error']['errorCode'] = '-41007'
        with self.assertRaisesRegex(RuntimeError, '-41007'):
            client.reserve('candidate@icloud.com')

    def test_creation_limit_persists_across_reload_and_clears_only_after_success(self):
        import account_manager as module
        root = Path(self.tmp.name)
        with patch.multiple(module, ACCOUNTS_FILE=root / 'accounts.json',
                            RESULTS_DIR=root, LATEST_EMAILS=root / 'latest.txt'):
            manager = module.AccountManager()
            manager.accounts = {'a': {'id': 'a', 'name': 'test', 'cookies': {}, 'status': 'active'}}
            manager._save()
            client = Mock()
            client.create_alias.side_effect = ICloudCreationLimitError()
            with patch.object(manager, 'get_client', return_value=client), \
                    patch('mail_provisioning.MailProvisioner') as factory:
                result = manager.create_aliases_for_account('a', count=5)[0]
            self.assertTrue(result['creation_limited'])
            self.assertEqual(client.create_alias.call_count, 1)
            reloaded = module.AccountManager()
            with patch.object(reloaded, 'get_client') as get_client:
                result = reloaded.create_aliases_for_account('a')[0]
                self.assertTrue(result['creation_limited'])
                get_client.assert_not_called()
            self.assertEqual(reloaded.accounts['a']['status'], 'active')
            client.create_alias.side_effect = None
            client.create_alias.return_value = {'email': 'new@icloud.com'}
            with patch('account_manager.time.time', return_value=result['retry_at'] + 1), \
                    patch.object(reloaded, 'get_client', return_value=client), \
                    patch('mail_provisioning.MailProvisioner') as factory:
                factory.return_value.provision.return_value = {'inbox_url': 'https://example.com/inbox'}
                self.assertTrue(reloaded.create_aliases_for_account('a')[0]['ok'])
            self.assertEqual(reloaded.storage.read_document('hme_creation_limit', 'a'), {})

    def test_creation_limit_stops_account_batch_but_other_account_continues(self):
        self.sources(1, account='a')
        self.sources(1, account='b')
        original = self.manager.create_aliases_for_account
        create = Mock(side_effect=lambda account, *args: (
            [{'ok': False, 'creation_limited': True, 'error': 'Apple -41015'}]
            if account == 'a' else original(account, *args)))
        self.manager.create_aliases_for_account = create
        job = self.run_job(ids=['a', 'b'])
        self.assertEqual(sum(call.args[0] == 'a' for call in create.call_args_list), 1)
        self.assertEqual((job['total_created'], job['total_errors']), (5, 1))

    def test_creation_limit_cooldown_skips_before_deleting_or_spending_quota(self):
        self.sources()
        self.store.write_document('hme_creation_limit', {
            'error_code': '-41015', 'error': 'Apple -41015', 'retry_at': time.time() + 3600}, 'a')
        job = self.run_job()
        self.assertEqual(self.calls, [])
        self.assertEqual(job['total_skipped'], 1)
        self.assertEqual(self.jobs.quota_status('a')['used'], 0)

    def test_expired_cooldown_does_not_allow_deletion_before_creation_recovers(self):
        self.sources()
        self.store.write_document('hme_creation_limit', {
            'error_code': '-41015', 'error': 'Apple -41015', 'retry_at': time.time() - 1}, 'a')
        job = self.run_job()
        self.assertEqual(self.calls, [])
        self.assertEqual(job['total_skipped'], 1)
        self.assertEqual(len(self.store.emails()), 10)

    def test_quota_release_uses_later_creation_time_and_is_per_account(self):
        from datetime import datetime, timezone
        self.jobs.config = {**self.config, 'hourly_create_limit': 2, 'quota_window_minutes': 10}
        # 整秒基准可精确往返 ISO 时间，避免微秒舍入使窗口边界断言偶发失败。
        now = int(time.time())
        self.store.add_email('new@icloud.com', 'a')
        self.store.add_email('new+child@icloud.com', 'a')
        self.store.add_email('other@icloud.com', 'b')
        with self.store.connect() as db:
            db.execute('UPDATE email_records SET created_at=? WHERE email=?',
                (datetime.fromtimestamp(now-100, timezone.utc).isoformat(), 'new@icloud.com'))
        self.store.write_document('registration_attempt', {'account_id':'a','started_at':now-110,
            'email':'new@icloud.com'}, 'confirmed')
        self.store.write_document('registration_attempt', {'account_id':'a','started_at':now-50}, 'unknown')
        with patch('registration_rotation.time.time', return_value=now):
            quota = self.jobs.quota_status('a')
            self.assertEqual((quota['used'], quota['remaining']), (2, 0))
            self.assertAlmostEqual(quota['next_release_at'], now+500)
            self.assertEqual(self.jobs.quota_status('b')['used'], 1)
        with patch('registration_rotation.time.time', return_value=now+500):
            quota = self.jobs.quota_status('a')
            self.assertEqual((quota['used'], quota['remaining']), (1, 1))
            self.assertAlmostEqual(quota['next_release_at'], now+550)

    def test_each_account_logs_initial_and_final_quota_with_custom_settings(self):
        job=self.run_job({**self.config, 'hourly_create_limit':2,
                          'quota_window_minutes':10, 'count':1, 'split_per_source':0}, ['a','b'])
        quota_events=[r for r in job['events'] if r['action']=='quota']
        self.assertEqual(len(quota_events),4)
        for account in ('a','b'):
            before, after=[r for r in quota_events if r['account_id']==account]
            self.assertEqual(before['quota']['used'],0)
            self.assertIsNone(before['quota']['next_release_at'])
            self.assertEqual((after['quota']['used'],after['quota']['remaining']), (1,1))
            self.assertIn('最近 10 分钟最多 2 个源邮箱',after['message'])
            self.assertIn('北京时间',after['message'])

    def test_hourly_quota_prevents_repeated_deletions_and_survives_restart(self):
        self.sources()
        self.run_job()
        self.calls.clear()
        self.jobs=RegistrationJobs(self.manager)
        job=self.run_job()
        self.assertFalse(self.calls)
        self.assertEqual(job['total_skipped'],1)

    def test_below_threshold_creates_without_deleting_and_custom_split_count(self):
        self.sources(2)
        job=self.run_job({**self.config,'count':2,'split_per_source':2})
        self.assertEqual(job['total_created'],2)
        self.assertEqual(job['total_split'],4)
        self.assertFalse(any(action=='delete' for action,_ in self.calls))

    def test_full_rules_rotate_owned_oldest_sources_below_alias_threshold(self):
        self.sources(7)
        # 最老的 a0 没有本项目可确认的规则，删除它无法释放规则容量。
        self.rules['a'] = self.rules['a'][1:] + [{} for _ in range(494)]
        job = self.run_job({**self.config, 'rotation_threshold':495})
        parents = [email for action,email in self.calls if action=='delete' and '+' not in email]
        self.assertEqual(parents, [f'a{index}@icloud.com' for index in range(1,6)])
        self.assertEqual((job['total_deleted'],job['total_created'],job['total_split']), (5,5,5))
        self.assertIn('a0@icloud.com',self.store.load_links())

    def test_rule_capacity_must_fit_the_whole_next_batch(self):
        self.sources(7)
        self.rules['a'] += [{} for _ in range(489)]
        job = self.run_job({**self.config, 'rotation_threshold':1000})
        self.assertEqual((job['total_deleted'],job['total_created']), (5,5))

    def test_aliases_and_rules_share_the_configured_rotation_threshold(self):
        for account, rule_count, deleted in [('a',494,0),('b',495,5)]:
            with self.subTest(rule_count=rule_count):
                self.sources(7,account)
                self.rules[account] += [{} for _ in range(rule_count-7)]
                job = self.run_job({**self.config, 'rotation_threshold':495}, [account])
                self.assertEqual((job['total_deleted'],job['total_created']), (deleted,5))

    def test_full_unowned_rules_do_not_delete_sources_or_create_more(self):
        self.sources(7)
        self.rules['a'] = [{} for _ in range(500)]
        job = self.run_job({**self.config, 'rotation_threshold':495})
        self.assertFalse(self.calls)
        self.assertEqual((job['total_deleted'],job['total_created'],job['total_skipped']), (0,0,1))

    def test_children_include_legacy_deleted_short_links_until_remote_cleanup_confirmed(self):
        self.sources(1)
        email='a0+legacy@icloud.com'
        self.store.write_document('email_deletion',{'account_id':'a','email':email,
            'link':{'base_email':'a0@icloud.com'},'stage':'completed'},email)
        self.assertIn(email,self.jobs._children('a0@icloud.com','a'))
        self.store.write_document('email_deletion',{'account_id':'a','email':email,
            'remote_cleanup_completed':True},email)
        self.assertNotIn(email,self.jobs._children('a0@icloud.com','a'))

    def test_parent_delete_failure_retries_once_without_stopping_other_pairs(self):
        self.sources()
        parent='a0@icloud.com'
        self.failures[parent]=[{'ok':False,'email':parent,'error':'temporary'}]*2
        job=self.run_job()
        self.assertEqual(self.calls.count(('delete',parent)),2)
        self.assertEqual((job['total_deleted'],job['total_created'],job['total_split']),(4,4,4))

    def test_recent_manual_creations_reduce_budget_before_selecting_old_sources(self):
        self.sources(7)
        for index in range(3):
            email=f'manual{index}@icloud.com'
            self.store.add_email(email,'a')
            self.store.update_link(email,{'account_id':'a','inbox_url':'public','configured':True})
            self.aliases['a'].append({'email':email,'createdAt':time.time()})
        job=self.run_job()
        self.assertEqual((job['total_deleted'],job['total_created']),(2,2))
        self.assertEqual([email for action,email in self.calls if action=='delete' and '+' not in email],
                         ['a0@icloud.com','a1@icloud.com'])

    def test_invalid_rotation_config_does_not_persist(self):
        auto=AutoRegistration(self.jobs,self.manager,Path(self.tmp.name)/'state.json')
        before=copy.deepcopy(auto.config)
        for data in [{'rotation_threshold':0},{'rotation_count':True},{'delete_retries':4},
                     {'split_per_source':-1},{'hourly_create_limit':0},{'quota_window_minutes':0}]:
            with self.assertRaises(ValueError):
                auto.configure(data)
        self.assertEqual(auto.config,before)
