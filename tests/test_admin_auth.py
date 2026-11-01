import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_ui
from admin_auth import AuthStore, Throttled
from auth_web import COOKIE


class AdminAuthTest(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.now=1000
        self.store=AuthStore(Path(folder.name)/'auth.sqlite3', clock=lambda:self.now)
        self.store.create_admin('admin','initial-password-123')
        p=patch.dict(web_ui.app.extensions, {'admin_auth':self.store});p.start();self.addCleanup(p.stop)
        self.client=web_ui.app.test_client()

    def login(self, client=None):
        response=(client or self.client).post('/api/auth/login',json={'username':'admin','password':'initial-password-123'})
        self.assertEqual(response.status_code,200)
        return response.json['csrf_token'],response

    def test_cookie_refresh_requires_csrf_and_never_returns_cookie(self):
        csrf, _ = self.login()
        path = '/api/accounts/a/cookies'
        with patch.object(web_ui._account_mgr, 'replace_session', return_value={
                'real_email': 'a@example.com', 'status': 'active', 'cookies': {'token': 'private'}}) as replace:
            self.assertEqual(self.client.post(path, json={'cookie_input': 'token=private'}).status_code, 403)
            replace.assert_not_called()
            response = self.client.post(path, json={'cookie_input': 'token=private'},
                                        headers={'X-CSRF-Token': csrf})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('private', response.text)
            self.assertNotIn('cookies', response.json)
            replace.return_value = {'status':'error','last_error':'Apple 邮件规则服务暂时异常',
                                    'cookies':{'token':'private'}}
            response = self.client.post(path,json={'cookie_input':'token=private'},
                                        headers={'X-CSRF-Token':csrf})
            self.assertEqual(response.status_code,503)
            self.assertTrue(response.json['session_saved'])
            self.assertNotIn('private',response.text)
            replace.side_effect = RuntimeError('private-session-response')
            response = self.client.post(path, json={'cookie_input': 'token=private'},
                                        headers={'X-CSRF-Token': csrf})
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('private-session-response', response.text)

    def test_all_existing_management_routes_fail_closed(self):
        # Covers new routes as they are added; only public/auth bootstrap are exempt.
        for rule in web_ui.app.url_map.iter_rules():
            if not rule.rule.startswith('/api/') or rule.rule.startswith('/api/public/') or rule.rule=='/api/auth/login':continue
            path=rule.rule.replace('<acc_id>','x').replace('<job_id>','x').replace('<key_id>','x').replace('<msg_id>','x').replace('<alias_email>','x')
            method='GET' if 'GET' in rule.methods else next(m for m in rule.methods if m not in ['HEAD','OPTIONS'])
            self.assertEqual(self.client.open(path,method=method).status_code,401,path)
        self.assertEqual(self.client.get('/').location,'/login')
        self.assertEqual(self.client.get('/static/auto_plus.html').status_code,302)
        self.assertEqual(self.client.get('/healthz').json,{'ok':True})

    def test_month_absolute_expiry_cookie_security_and_logout_revocation(self):
        csrf,response=self.login()
        cookie=response.headers['Set-Cookie']
        for attr in ['HttpOnly','Secure','SameSite=Lax','Max-Age=2592000']:self.assertIn(attr,cookie)
        token=self.client.get_cookie(COOKIE).value
        self.assertNotIn(token,self.store.path.read_bytes().decode('latin1'))
        self.now+=29*86400
        self.assertEqual(self.client.get('/api/auth/session').status_code,200)
        self.assertEqual(self.client.get('/api/auth/session').json['expires_at'],1000+30*86400)
        self.now+=86400
        self.assertEqual(self.client.get('/api/auth/session').status_code,401)
        self.now=1000;csrf,_=self.login();token=self.client.get_cookie(COOKIE).value
        self.assertEqual(self.client.post('/api/auth/logout',headers={'X-CSRF-Token':csrf}).status_code,200)
        self.client.set_cookie(COOKIE,token)
        self.assertEqual(self.client.get('/api/auth/session').status_code,401)

    def test_csrf_enforced_and_frames_receive_session_helper(self):
        csrf,_=self.login()
        self.assertEqual(self.client.post('/api/admin/api-keys',json={'name':'x','scopes':['emails:read']}).status_code,403)
        response=self.client.post('/api/admin/api-keys',json={'name':'x','scopes':['emails:read']},headers={'X-CSRF-Token':csrf})
        self.assertEqual(response.status_code,201)
        for path in ['/','/auto-panel','/plus-panel','/admin/security']:
            with self.client.get(path) as r:self.assertIn(b'/static/admin_session.js',r.data,path)

    def test_keys_are_scoped_revocable_hashed_and_not_admin_sessions(self):
        csrf,_=self.login()
        r=self.client.post('/api/admin/api-keys',json={'name':'integration','scopes':['emails:read']},headers={'X-CSRF-Token':csrf})
        token,key_id=r.json['api_key'],r.json['id']
        self.assertNotIn(token,self.store.path.read_bytes().decode('latin1'))
        self.assertNotIn(token,json.dumps(self.client.get('/api/admin/api-keys').json))
        caller=web_ui.app.test_client();header={'Authorization':'Bearer '+token}
        with patch.object(web_ui,'email_storage') as storage,patch.object(web_ui,'load_links',return_value={}),patch.object(web_ui._fastai_sync,'records',return_value={}):
            storage.return_value.emails.return_value=[]
            self.assertEqual(caller.get('/api/public/emails',headers=header).status_code,200)
        self.assertEqual(caller.get('/api/public/inbox-links/export',headers=header).status_code,403)
        self.assertEqual(caller.get('/api/accounts',headers=header).status_code,401)
        self.assertEqual(caller.post('/api/accounts/add',headers=header).status_code,401)
        self.assertEqual(caller.get('/api/public/emails?api_key='+token).status_code,401)
        self.assertEqual(self.client.get('/api/public/emails').status_code,401)
        self.assertEqual(caller.post('/api/public/emails',headers=header).status_code,404)
        self.client.delete('/api/admin/api-keys/'+key_id,headers={'X-CSRF-Token':csrf})
        self.assertEqual(caller.get('/api/public/emails',headers=header).status_code,401)

    def test_password_change_revokes_every_session_and_old_password_fails(self):
        csrf,_=self.login();other=web_ui.app.test_client();self.login(other)
        r=self.client.post('/api/auth/password',json={'old_password':'initial-password-123','new_password':'changed-password-456'},headers={'X-CSRF-Token':csrf})
        self.assertEqual(r.status_code,200)
        self.assertEqual(other.get('/api/auth/session').status_code,401)
        self.assertEqual(self.client.post('/api/auth/login',json={'username':'admin','password':'initial-password-123'}).status_code,401)
        self.assertEqual(self.client.post('/api/auth/login',json={'username':'admin','password':'changed-password-456'}).status_code,200)

    def test_login_throttle_and_no_cross_origin_login(self):
        self.assertEqual(self.client.post('/api/auth/login',headers={'Origin':'https://evil.example'},json={}).status_code,403)
        for _ in range(8):
            self.assertEqual(self.client.post('/api/auth/login',json={'username':'admin','password':'wrong'}).status_code,401)
        self.assertEqual(self.client.post('/api/auth/login',json={'username':'admin','password':'initial-password-123'}).status_code,429)
        self.now+=901;self.login()

    def test_missing_admin_has_no_remote_bootstrap(self):
        with self.store.connect() as db:db.execute('DELETE FROM admin')
        self.assertEqual(self.client.post('/api/auth/login',json={'username':'admin','password':'initial-password-123'}).status_code,401)
        self.assertEqual(self.client.post('/api/auth/setup',json={}).status_code,401)

    def test_public_export_and_mail_use_key_scopes_and_sessions_cannot_be_keys(self):
        csrf,_=self.login()
        session_token=self.client.get_cookie(COOKIE).value
        caller=web_ui.app.test_client()
        self.assertEqual(caller.get('/api/public/emails',headers={'Authorization':'Bearer '+session_token}).status_code,401)
        _,key=self.store.create_key('reader',['inbox-links:read','mail:read'])
        header={'X-API-Key':key}
        with patch.object(web_ui,'export_lines',return_value=['example@icloud.com----https://example.test/inbox']), \
                patch.object(web_ui,'email_storage') as storage:
            storage.return_value.emails.return_value=[{'email':'example@icloud.com','account_id':'a'}]
            response=caller.get('/api/public/inbox-links/export',headers=header)
            self.assertEqual(response.status_code,200)
            self.assertIn('example@icloud.com----',response.text)
        with patch.object(web_ui._account_mgr,'list_accounts',return_value=[{'id':'a','real_email':'owner@icloud.com'}]), \
                patch.object(web_ui._account_mgr,'check_alias_mail',return_value=[{'subject':'test'}]):
            response=caller.get('/api/public/mail?email=owner@icloud.com&alias=example@icloud.com',headers=header)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['count'],1)

    def test_limits_persist_across_instances_and_return_retry_after(self):
        self.store.limit('sample',2)
        self.store.limit('sample',2)
        reopened=AuthStore(self.store.path,clock=lambda:self.now)
        with self.assertRaises(Throttled) as failure:reopened.limit('sample',2)
        self.assertEqual(failure.exception.retry_after,60)
        self.now+=60;reopened.limit('sample',2)

    def test_untrusted_forwarded_headers_cannot_evade_login_ip_limit(self):
        for number in range(10):
            r=self.client.post('/api/auth/login',json={'username':'nonexistent-'+str(number),'password':'wrong'},
                               headers={'X-Forwarded-For':'198.51.100.'+str(number)},
                               environ_overrides={'REMOTE_ADDR':'203.0.113.9'})
            self.assertEqual(r.status_code,401)
        r=self.client.post('/api/auth/login',json={'username':'other','password':'wrong'},
                           headers={'X-Forwarded-For':'192.0.2.99'},environ_overrides={'REMOTE_ADDR':'203.0.113.9'})
        self.assertEqual(r.status_code,429)
        self.assertEqual(r.headers['Retry-After'],'60')

    def test_trusted_proxy_clients_have_separate_limits(self):
        with patch.dict(web_ui.app.config,{'AUTH_TRUSTED_PROXIES':{'45.78.71.86'}}):
            for number in range(10):
                self.client.post('/api/auth/login',json={'username':'wrong-'+str(number),'password':'wrong'},
                                 headers={'X-Forwarded-For':'198.51.100.1'},environ_overrides={'REMOTE_ADDR':'45.78.71.86'})
            r=self.client.post('/api/auth/login',json={'username':'another','password':'wrong'},
                               headers={'X-Forwarded-For':'198.51.100.2'},environ_overrides={'REMOTE_ADDR':'45.78.71.86'})
            self.assertEqual(r.status_code,401)

    def test_account_lockout_covers_distributed_password_attempts(self):
        for number in range(12):
            r=self.client.post('/api/auth/login',json={'username':'admin','password':'wrong'},
                               environ_overrides={'REMOTE_ADDR':'198.51.100.'+str(number)})
            self.assertEqual(r.status_code,401)
        r=self.client.post('/api/auth/login',json={'username':'admin','password':'initial-password-123'},
                           environ_overrides={'REMOTE_ADDR':'203.0.113.1'})
        self.assertEqual(r.status_code,429)
        self.assertEqual(r.headers['Retry-After'],'900')

    def test_public_api_limits_invalid_keys_and_valid_key_traffic(self):
        for _ in range(120):
            self.assertEqual(self.client.get('/api/public/emails',headers={'X-API-Key':'invalid'}).status_code,401)
        r=self.client.get('/api/public/emails',headers={'X-API-Key':'different-invalid'})
        self.assertEqual(r.status_code,429)
        self.assertIn('Retry-After',r.headers)
        self.now+=61
        _,token=self.store.create_key('reader',['emails:read'])
        with patch.object(web_ui,'email_storage') as storage,patch.object(web_ui,'load_links',return_value={}),patch.object(web_ui._fastai_sync,'records',return_value={}):
            storage.return_value.emails.return_value=[]
            for _ in range(60):
                self.assertEqual(self.client.get('/api/public/emails',headers={'X-API-Key':token}).status_code,200)
            self.assertEqual(self.client.get('/api/public/emails',headers={'X-API-Key':token}).status_code,429)
