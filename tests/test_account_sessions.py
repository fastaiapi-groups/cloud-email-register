import json
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch, Mock
import account_manager as module
from creation_jobs import CreationJobs


class AccountSessionsTest(unittest.TestCase):
    def test_session_replacement_checks_identity_and_preserves_other_account(self):
        manager = module.AccountManager()
        with patch('icloud_hme.ICloudHME') as factory, patch('mail_provisioning.MailProvisioner'):
            client = factory.return_value
            client.cookies = {'token': 'rotated'}
            client.get_account_info.return_value = {'appleId': 'wrong@example.com'}
            with self.assertRaises(ValueError):
                manager.replace_session('acc_a', 'token=new')
            self.assertEqual(manager.accounts['acc_a']['cookies'], {'token': 'A'})
            client.get_account_info.return_value = {'appleId': 'a@example.com'}
            manager.replace_session('acc_a', 'token=new')
        reloaded = module.AccountManager()
        self.assertEqual(reloaded.accounts['acc_a']['cookies'], {'token': 'rotated'})
        self.assertEqual(reloaded.accounts['acc_b'], self.records['acc_b'])
        self.assertFalse(reloaded.accounts['acc_a']['session_requires_login'])

    def test_dashboard_and_keepalive_both_require_mail_rule_access(self):
        from icloud_hme import ICloudMailSessionExpired
        manager = module.AccountManager()
        validation = {'webservices': {'mcc': {'url': 'https://p1-mcc.icloud.com'}}}
        client = Mock(cookies={'token': 'A'})
        client.validate_session.return_value = validation
        client.get_account_info.return_value = {'appleId': 'a@example.com'}
        client.list_aliases.return_value = []
        with patch.object(manager, 'get_client', return_value=client), \
                patch('mail_provisioning.MailProvisioner') as provisioner:
            provisioner.return_value.prepare_rules.side_effect = ICloudMailSessionExpired('401')
            for validate in (manager.validate_account, manager.keep_session_alive):
                result = validate('acc_a')
                self.assertEqual(result['status'], 'error')
                self.assertTrue(result['session_requires_login'])
                self.assertIn('邮件规则', result['last_error'])
                self.assertIn('HTTP 401', result['last_error'])
            provisioner.return_value.prepare_rules.side_effect = None
            for validate in (manager.validate_account, manager.keep_session_alive):
                result = validate('acc_a')
                self.assertEqual(result['status'], 'active')
                self.assertFalse(result['session_requires_login'])
            self.assertTrue(all(call.kwargs == {'validation': validation}
                                for call in provisioner.return_value.prepare_rules.call_args_list))

    def test_new_cookie_without_mail_access_cannot_be_reported_as_valid(self):
        from icloud_hme import ICloudMailSessionExpired
        manager = module.AccountManager()
        with patch('icloud_hme.ICloudHME') as factory, \
                patch('mail_provisioning.MailProvisioner') as provisioner:
            factory.return_value.get_account_info.return_value = {'appleId': 'a@example.com'}
            provisioner.return_value.prepare_rules.side_effect = ICloudMailSessionExpired('401')
            with self.assertRaises(ICloudMailSessionExpired):
                manager.replace_session('acc_a', 'token=new')
        self.assertEqual(manager.accounts['acc_a']['cookies'], {'token': 'A'})

    def test_keepalive_marks_relogin_without_replaying_expired_cookie(self):
        from icloud_hme import ICloudSessionExpired
        manager = module.AccountManager()
        client = Mock(cookies={'token': 'A'})
        client.validate_session.side_effect = ICloudSessionExpired('HTTP 421')
        with patch.object(manager, 'get_client', return_value=client):
            result = manager.keep_session_alive('acc_a')
        self.assertTrue(result['session_requires_login'])
        self.assertEqual(result['status'], 'error')
        client.validate_session.assert_called_once_with(max_attempts=1)
        self.assertEqual(module.AccountManager().accounts['acc_a']['cookies'], {'token': 'A'})

    def test_old_keepalive_cannot_mark_new_cookie_as_expired(self):
        from icloud_hme import ICloudSessionExpired
        manager = module.AccountManager()
        client = Mock(cookies={'token': 'A'})
        def replace_while_waiting(**kwargs):
            manager.update_account('acc_a', cookies={'token': 'new'}, status='active')
            raise ICloudSessionExpired('HTTP 421')
        client.validate_session.side_effect = replace_while_waiting
        with patch.object(manager, 'get_client', return_value=client):
            result = manager.keep_session_alive('acc_a')
        self.assertEqual(result['status'], 'active')
        self.assertEqual(result['cookies'], {'token': 'new'})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.multiple(module, ACCOUNTS_FILE=self.root / 'accounts.json',
            RESULTS_DIR=self.root / 'results', OLD_COOKIES_FILE=self.root / 'legacy.json')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.records = {'acc_a': {'id': 'acc_a', 'real_email': 'a@example.com', 'cookies': {'token': 'A'}},
                        'acc_b': {'id': 'acc_b', 'real_email': 'b@example.com', 'cookies': {'token': 'B'}}}
        (self.root / 'accounts.json').write_text(json.dumps({'accounts': self.records}))

    def test_migration_persists_two_private_database_rows_and_reloads(self):
        original = (self.root / 'accounts.json').read_bytes()
        manager = module.AccountManager()
        self.assertEqual((self.root / 'accounts.json').read_bytes(), original)
        with manager.storage.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM account_sessions').fetchone()[0], 2)
            self.assertNotIn('cookies', json.loads(db.execute('SELECT data FROM accounts WHERE id=?', ('acc_a',)).fetchone()[0]))
        self.assertEqual(manager.storage.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(module.AccountManager().accounts['acc_b']['cookies'], {'token': 'B'})

    def test_cookie_rotation_and_stale_client_cannot_change_other_or_new_session(self):
        manager = module.AccountManager()
        a, b = manager.get_client('acc_a'), manager.get_client('acc_b')
        a.on_cookie_update({'token': 'A2'})
        self.assertEqual(manager.accounts['acc_b']['cookies'], {'token': 'B'})
        self.assertEqual(b.session.cookies.get('token'), 'B')
        manager.update_account('acc_a', cookies={'token': 'A-login-again'})
        a.on_cookie_update({'token': 'stale'})
        reloaded = module.AccountManager()
        self.assertEqual(reloaded.accounts['acc_a']['cookies']['token'], 'A-login-again')
        self.assertEqual(reloaded.accounts['acc_b']['cookies']['token'], 'B')

    def test_legacy_files_do_not_overwrite_database_after_migration(self):
        manager = module.AccountManager()
        manager.update_account('acc_a', cookies={'token': 'new-session'})
        (self.root / 'accounts.json').write_text('{broken')
        reloaded = module.AccountManager()
        self.assertEqual(reloaded.accounts['acc_a']['cookies'], {'token': 'new-session'})
        self.assertEqual(reloaded.accounts['acc_b']['cookies'], {'token': 'B'})

    def test_clients_do_not_share_mutable_cookie_jars(self):
        manager = module.AccountManager()
        a, b = manager.get_client('acc_a'), manager.get_client('acc_b')
        a.cookies['token'] = 'changed'
        a.session.cookies.set('token', 'jar-change')
        self.assertEqual(b.session.cookies.get('token'), 'B')
        self.assertEqual(manager.accounts['acc_a']['cookies']['token'], 'A')

    def test_failed_account_does_not_block_five_creations_for_other_account(self):
        class Manager:
            def create_aliases_for_account(self, acc_id, count, label):
                if acc_id == 'acc_a':
                    return [{'ok': False, 'error': 'Session expired', 'account_id': acc_id}]
                return [{'ok': True, 'line': 'email----link', 'account_id': acc_id}]
        jobs = CreationJobs(Manager())
        key = jobs.start(['acc_a', 'acc_b'], count=5, interval=0)
        deadline = time.monotonic() + 2
        while jobs.get(key)['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.005)
        result = jobs.get(key)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['total_created'], 5)
        self.assertEqual(result['total_errors'], 1)
        self.assertEqual(len(result['results']['acc_b']), 5)
