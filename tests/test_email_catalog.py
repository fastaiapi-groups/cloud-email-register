import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import web_ui
from storage import Storage
from inbox_links import is_ready
from tests.auth_helpers import admin_client
from inbox_clear import clear_inbox


class EmailCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Storage(Path(self.temp.name)/'catalog.sqlite3')
        for p in [patch.object(web_ui,'email_storage',return_value=self.store),
                  patch.object(web_ui,'load_links',side_effect=self.store.load_links),
                  patch.object(web_ui._fastai_sync,'config',{'api_url':'https://sync.test'})]:
            p.start();self.addCleanup(p.stop)
        self.client=admin_client()

    def add(self,email,account='a1',ready=True,destination=None):
        self.store.add_email(email,account)
        self.store.update_link(email,{'email':email,'account_id':account,'configured':ready,
            'inbox_url':'https://webmail.fastaiapi.cloud/m/public',
            'forward_inbox_url':'https://webmail.fastaiapi.cloud/?jwt=VERY_PRIVATE_TOKEN',
            'destination':destination or email.split('@')[0]+'@fastaiapi.cloud'})

    def test_real_sql_paging_keyword_type_account_and_no_credential_leak(self):
        for n in range(125):self.add(f'one_{n}@icloud.com')
        self.add('other@icloud.com','a2');self.add('one_0+tag@icloud.com')
        with patch.object(self.store,'emails',side_effect=AssertionError('must not load all emails')), \
             patch.object(web_ui,'load_links',side_effect=AssertionError('must not load all links')):
            r=self.client.get('/api/emails?page=2&page_size=25&q=one_&account_id=a1&address_type=source')
        self.assertEqual(r.status_code,200)
        self.assertEqual((r.json['total'],r.json['pages'],r.json['page'],len(r.json['emails'])),(125,5,2,25))
        self.assertNotIn('VERY_PRIVATE_TOKEN',r.text)
        self.assertEqual(self.client.get('/api/emails?page=1&q=one%25').json['total'],0)
        for q in ['page=0','page=no','page_size=201','page=1&sort=email;DROP','page=1&address_type=wrong']:
            self.assertEqual(self.client.get('/api/emails?'+q).status_code,400)

    def test_sorting_and_page_clamp_are_server_side(self):
        self.add('new@icloud.com');self.add('old@icloud.com');self.add('missing@icloud.com')
        with self.store.connect() as db:
            db.execute("UPDATE email_records SET created_at=CASE email WHEN 'new@icloud.com' THEN '2026-10-27T00:00:00Z' WHEN 'old@icloud.com' THEN '2025-01-01T00:00:00Z' ELSE '' END")
        for direction,expected in [('asc',['old','new','missing']),('desc',['new','old','missing'])]:
            rows=self.client.get('/api/emails?page=1&direction='+direction).json['emails']
            self.assertEqual([x['email'].split('@')[0] for x in rows],expected)
        r=self.client.get('/api/emails?page=100&page_size=2').json
        self.assertEqual((r['page'],len(r['emails'])),(2,1))

    def test_snapshot_all_selection_excludes_new_rows_and_unchecked_rows_when_deleting(self):
        for email in ['one@icloud.com','one2@icloud.com','other@icloud.com']:self.add(email)
        data=self.client.get('/api/emails?page=1&q=one').json
        self.add('one-new@icloud.com')
        selection={'all_matching':True,'filters':{'q':'one'},'through_seq':data['snapshot_max_seq'],'excluded':['one2@icloud.com']}
        with patch.object(web_ui._email_delete_jobs,'start_emails',return_value='delete-job') as start:
            r=self.client.post('/api/emails/bulk',json={'action':'delete','selection':selection})
        self.assertEqual((r.status_code,r.json['selected']),(202,1))
        self.assertEqual([row['email'] for row in start.call_args.args[0]],['one@icloud.com'])
        self.assertEqual(len(self.store.emails()),4)  # Remote confirmation owns removal.

    def test_legacy_local_only_deletions_restore_once_without_enabling_sources(self):
        import email_catalog
        self.store.update_link('legacy@icloud.com',{'deleted':True,'configured':True,'inbox_url':'public','account_id':'a'})
        email_catalog.restore_legacy_deletions(self.store)
        email_catalog.restore_legacy_deletions(self.store)
        rows=email_catalog.page(self.store,{'status':'local_deleted'},'https://sync.test')['rows']
        self.assertEqual([r['email'] for r in rows],['legacy@icloud.com'])
        self.assertFalse(is_ready(self.store.load_links()['legacy@icloud.com']))

    def test_status_filters_apply_identically_to_page_and_all_selection(self):
        import email_catalog
        self.add('ready@icloud.com');self.add('pending@icloud.com',ready=False)
        self.add('verified@icloud.com');self.store.update_link('verified@icloud.com',{'verified':True})
        with self.store.connect() as db:
            db.execute("INSERT INTO fastai_sync VALUES(?,?,?,?,?,?,?)",('ready@icloud.com','https://sync.test','https://webmail.fastaiapi.cloud/m/public','synced',100,'','job'))
            # Same email with obsolete link must be pending for the current link.
            db.execute("INSERT INTO fastai_sync VALUES(?,?,?,?,?,?,?)",('verified@icloud.com','https://sync.test','old-link','synced',100,'','old'))
        for query,expected in [({'status':'ready'},{'ready@icloud.com','verified@icloud.com'}),
                               ({'status':'pending'},{'pending@icloud.com'}),
                               ({'status':'verified'},{'verified@icloud.com'}),
                               ({'status':'unverified','sync_status':'synced'},{'ready@icloud.com'}),
                               ({'status':'ready','sync_status':'pending'},{'verified@icloud.com'})]:
            page=email_catalog.page(self.store,{'page':1,**query},'https://sync.test')
            self.assertEqual({r['email'] for r in page['rows']},expected)
            with self.store.connect() as db:
                selected=email_catalog.selected(db,{'all_matching':True,'filters':query,'through_seq':page['snapshot_max_seq']},'https://sync.test')
            self.assertEqual({r['email'] for r in selected},expected)
        self.assertEqual(self.client.get('/api/emails?page=1&sync_status=invalid').status_code,400)

    def test_sync_bulk_records_history_and_never_imports_unselected(self):
        from fastai_sync import FastAISync
        from tests.test_fastai_sync import accepted
        self.add('one@icloud.com');self.add('two@icloud.com');self.add('pending@icloud.com',ready=False)
        self.add('other@icloud.com')
        http=Mock();http.post.return_value=accepted()
        sync=FastAISync(self.store,http_factory=lambda:http);sync.configure({'batch_size':1})
        with patch.object(web_ui,'_fastai_sync',sync):
            r=self.client.post('/api/emails/bulk',json={'action':'sync','selection':{'emails':['one@icloud.com','two@icloud.com','pending@icloud.com']}})
            self.assertEqual((r.status_code,r.json['selected'],r.json['skipped']),(202,2,1))
            sync.work_thread.join(3)
            job=self.client.get('/api/emails/bulk-jobs/'+r.json['job_id']).json['job']
            self.assertEqual((job['succeeded'],len(job['items'])),(2,2))
            history=self.client.get('/api/emails/bulk-jobs').json['history']
            self.assertEqual(history[0]['succeeded'],2)
            self.assertNotIn('VERY_PRIVATE_TOKEN',str(history))
        imported={call.kwargs['json']['text'].split('----')[0] for call in http.post.call_args_list}
        self.assertEqual(imported,{'one@icloud.com','two@icloud.com'})

    def test_selected_split_is_once_per_source_and_rejects_split_addresses(self):
        for email in ['one@icloud.com','two@icloud.com','one+tag@icloud.com']:self.add(email)
        with patch.object(web_ui._plus_creation_jobs,'sources',return_value={'one@icloud.com':{},'two@icloud.com':{}}), \
             patch.object(web_ui._plus_creation_jobs,'start',return_value='split-job') as start:
            r=self.client.post('/api/emails/bulk',json={'action':'split','selection':{'emails':['one@icloud.com','one@icloud.com','two@icloud.com']}})
            self.assertEqual(r.status_code,202);self.assertEqual(r.json['selected'],2)
            self.assertEqual(start.call_args.kwargs['count'],1)
            self.assertEqual(set(start.call_args.args[0]),{'one@icloud.com','two@icloud.com'})
            start.reset_mock()
            r=self.client.post('/api/emails/bulk',json={'action':'split','selection':{'emails':['one+tag@icloud.com']}})
            self.assertEqual(r.status_code,400);start.assert_not_called()

    def test_repair_only_selected_incomplete_addresses(self):
        self.add('ready@icloud.com');self.add('pending@icloud.com',ready=False);self.add('unselected@icloud.com',ready=False)
        with patch.object(web_ui._account_mgr,'get_account',return_value={'id':'a1'}), \
             patch.object(web_ui._creation_jobs,'start',return_value='repair-job') as start:
            r=self.client.post('/api/emails/bulk',json={'action':'repair','selection':{'emails':['ready@icloud.com','pending@icloud.com']}})
            self.assertEqual(r.status_code,202)
            self.assertEqual(start.call_args.kwargs['repair'],{'a1':['pending@icloud.com']})

    def test_clear_skips_split_addresses_and_deduplicates_physical_inboxes(self):
        for email in ['one@icloud.com','two@icloud.com','one+tag@icloud.com']:
            self.add(email,destination='shared@fastaiapi.cloud')
        with patch.object(web_ui._inbox_clear_jobs,'start_inboxes',return_value='clear-job') as start:
            r=self.client.post('/api/emails/bulk',json={'action':'clear','selection':{'emails':[x['email'] for x in self.store.emails()]}})
            self.assertEqual(r.status_code,202)
            records=start.call_args.args[0];self.assertEqual(len(records),1)
            self.assertNotIn('+',records[0]['email'])
            start.reset_mock()
            r=self.client.post('/api/emails/bulk',json={'action':'clear','selection':{'emails':['one+tag@icloud.com']}})
            self.assertEqual(r.json['selected'],0);self.assertIn('job_id',r.json);start.assert_not_called()

    def test_invalid_all_selection_and_empty_ids_cannot_become_select_all(self):
        self.add('one@icloud.com')
        for selection in [{},{'emails':[]},{'emails':'one@icloud.com'},{'all_matching':True},{'all_matching':True,'filters':{},'through_seq':True}]:
            self.assertEqual(self.client.post('/api/emails/bulk',json={'action':'delete','selection':selection}).status_code,400)
        self.assertEqual(len(self.store.emails()),1)


class ClearInboxTests(unittest.TestCase):
    def setUp(self):
        self.record={'email':'one@icloud.com','destination':'sink@fastaiapi.cloud',
                     'forward_inbox_url':'https://webmail.fastaiapi.cloud/?jwt=private'}
        self.http=Mock()

    def response(self,data,status=200):return Mock(status_code=status,json=lambda:data)

    def test_scope_checked_before_single_delete_and_no_apple_session_is_sent(self):
        self.http.request.side_effect=[self.response({'address':'sink@fastaiapi.cloud'}),self.response({'success':True})]
        self.assertTrue(clear_inbox(self.record,self.http))
        self.assertEqual([x.args[:2] for x in self.http.request.call_args_list],[('GET','https://mail-api.fastaiapi.cloud/api/settings'),('DELETE','https://mail-api.fastaiapi.cloud/api/clear_inbox')])
        self.assertFalse(any('Cookie' in x.kwargs['headers'] for x in self.http.request.call_args_list))

    def test_wrong_identity_and_split_address_never_delete(self):
        self.http.request.return_value=self.response({'address':'other@fastaiapi.cloud'})
        with self.assertRaisesRegex(RuntimeError,'归属'):clear_inbox(self.record,self.http)
        self.assertEqual(self.http.request.call_count,1)
        self.http.reset_mock()
        with self.assertRaises(ValueError):clear_inbox({**self.record,'email':'one+tag@icloud.com'},self.http)
        self.http.request.assert_not_called()

    def test_lost_delete_response_is_not_retried_or_reported_as_success(self):
        import requests
        self.http.request.side_effect=[self.response({'address':'sink@fastaiapi.cloud'}),requests.Timeout('private')]
        with self.assertRaisesRegex(RuntimeError,'不会自动重发'):clear_inbox(self.record,self.http)
        self.assertEqual(self.http.request.call_count,2)
