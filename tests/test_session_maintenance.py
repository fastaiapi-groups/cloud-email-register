import unittest
from unittest.mock import Mock

import requests
from icloud_hme import ICloudHME, ICloudSessionExpired
from session_maintenance import SessionMaintenance


class SessionMaintenanceTest(unittest.TestCase):
    def test_mail_internal_error_is_not_treated_as_revoked_main_session(self):
        from mail_provisioning import MailProvisioner
        from icloud_hme import ICloudMailServiceUnavailable
        from plus_addressing import safe_error
        client = ICloudHME({}, verbose=False)
        response = requests.Response()
        response.status_code = 401
        response._content = b'{"errorCode":"INTERNAL_SERVER_ERROR","errorDescription":"private-response"}'
        client.session.request = Mock(return_value=response)
        provisioner = MailProvisioner(client, publisher=lambda r:r)
        provisioner.rules_url = 'https://p1-mcc.icloud.com/cc/mail/v1/account/123/rules/'
        with self.assertRaises(ICloudMailServiceUnavailable) as caught:
            provisioner.list_rules()
        self.assertNotIsInstance(caught.exception, ICloudSessionExpired)
        self.assertIn('INTERNAL_SERVER_ERROR', safe_error(caught.exception))
        self.assertNotIn('private-response', safe_error(caught.exception))
        self.assertEqual(client.session.request.call_count, 1)
        # 同样的状态出现在主登录接口时仍要求重新登录，不放宽主会话鉴权。
        with self.assertRaises(ICloudSessionExpired):
            client.validate_session(max_attempts=1)

    def test_cookie_rotation_replaces_duplicate_and_persists_before_next_request(self):
        saved = Mock()
        client = ICloudHME({'token': 'old'}, on_cookie_update=saved, verbose=False)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{}'
        response.cookies.set('token', 'new', domain='.icloud.com', path='/')
        client.session.request = Mock(return_value=response)
        client._request('POST', client.setup_url + '/validate')
        self.assertEqual([(c.name, c.value) for c in client.session.cookies], [('token', 'new')])
        self.assertEqual(client.cookies['token'], 'new')
        saved.assert_called_once_with({'token': 'new'})

    def test_expired_cookie_does_not_retry_or_expose_response(self):
        client = ICloudHME({}, verbose=False)
        response = requests.Response()
        response.status_code = 421
        response._content = b'private-session-secret'
        client.session.request = Mock(return_value=response)
        with self.assertRaises(ICloudSessionExpired) as caught:
            client.validate_session()
        self.assertNotIn('private-session-secret', str(caught.exception))
        self.assertEqual(client.session.request.call_count, 1)

    def test_transient_failure_retries_other_accounts_continue_and_login_failure_pauses(self):
        now = [0]
        accounts = [{'id': 'a', 'cookies': {'token': 'a'}}, {'id': 'b', 'cookies': {'token': 'b'}}]
        manager = Mock()
        manager.list_accounts.return_value = accounts
        manager.keep_session_alive.side_effect = [
            {'status': 'error'}, {'status': 'active'},
            {'status': 'error', 'session_requires_login': True}, {'status': 'active'},
            {'status': 'active'}]
        emit = Mock()
        keeper = SessionMaintenance(manager, emit, interval=300, clock=lambda: now[0])
        keeper.tick()
        self.assertEqual(manager.keep_session_alive.call_count, 2)
        now[0] = 299
        keeper.tick()
        self.assertEqual(manager.keep_session_alive.call_count, 2)
        now[0] = 300
        keeper.tick()
        accounts[0]['session_requires_login'] = True
        now[0] = 900
        keeper.tick()
        self.assertEqual(manager.keep_session_alive.call_args.args, ('b',))

    def test_failure_of_one_account_does_not_terminate_loop(self):
        manager = Mock()
        manager.list_accounts.return_value = [{'id': 'a', 'cookies': {'t': 'a'}},
                                               {'id': 'b', 'cookies': {'t': 'b'}}]
        manager.keep_session_alive.side_effect = [RuntimeError('private'), {'status': 'active'}]
        emit = Mock()
        SessionMaintenance(manager, emit).tick()
        self.assertEqual(manager.keep_session_alive.call_count, 2)
        self.assertNotIn('private', str(emit.call_args_list))

    def test_random_interval_is_redrawn_after_each_success_and_revoked_account_stays_paused(self):
        now = [0]
        account = {'id': 'a', 'cookies': {'token': 'old'}}
        manager = Mock()
        manager.list_accounts.return_value = [account]
        manager.keep_session_alive.return_value = {'status': 'active'}
        random_interval = Mock(side_effect=[600, 1200, 900])
        keeper = SessionMaintenance(manager, Mock(), clock=lambda: now[0], random_interval=random_interval)
        keeper.tick()
        self.assertEqual(keeper.pending['a'][0], 600)
        now[0] = 599
        keeper.tick()
        self.assertEqual(manager.keep_session_alive.call_count, 1)
        now[0] = 600
        keeper.tick()
        self.assertEqual(keeper.pending['a'][0], 1800)
        for call in random_interval.call_args_list:
            self.assertEqual(call.args, (600, 1200))
        account['session_requires_login'] = True
        now[0] = 100000
        keeper.tick()
        self.assertEqual(manager.keep_session_alive.call_count, 2)
        # 只有成功更新会话并清除撤销标记后才恢复。
        account.update(cookies={'token': 'new'}, session_requires_login=False)
        keeper.tick()
        self.assertEqual(manager.keep_session_alive.call_count, 3)
