"""日志页面不能占满生产 HTTP 工作线程。所有数据和认证均在临时目录。"""
import http.client
import json
import queue
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import web_ui
from admin_auth import AuthStore
from auth_web import COOKIE


class NonBlockingQueue(queue.Queue):
    def get(self, block=True, timeout=None):
        if block:
            raise AssertionError("日志请求不应等待新日志")
        return super().get(block=False)


class LogStreamTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = AuthStore(Path(folder.name) / 'auth.sqlite3')
        self.store.create_admin('admin', 'test-log-stream-password')
        self.token, _ = self.store.login('admin', 'test-log-stream-password', '127.0.0.1')
        self.queue = NonBlockingQueue()
        for replacement in (patch.dict(web_ui.app.extensions, {'admin_auth': self.store}),
                            patch.object(web_ui, '_log_queue', self.queue)):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = web_ui.app.test_client()
        self.client.set_cookie(COOKIE, self.token)

    def test_empty_log_response_finishes_without_waiting(self):
        response = self.client.get('/api/log-stream')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/event-stream')
        self.assertIn('retry: 5000\n\n', response.text)
        self.assertIn(': heartbeat\n\n', response.text)
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_batch_is_bounded_and_remaining_logs_survive(self):
        for number in range(101):
            self.queue.put({'level': 'info', 'msg': '日志 ' + str(number)})
        first = self.client.get('/api/log-stream').text
        events = [json.loads(line[6:]) for line in first.splitlines() if line.startswith('data: ')]
        self.assertEqual([event['msg'] for event in events], ['日志 ' + str(i) for i in range(100)])
        self.assertEqual(self.queue.qsize(), 1)
        self.assertIn('日志 100', self.client.get('/api/log-stream').text)
        self.assertTrue(self.queue.empty())

    def test_anonymous_request_cannot_read_or_consume_logs(self):
        self.queue.put({'msg': 'private-log'})
        response = web_ui.app.test_client().get('/api/log-stream')
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('private-log', response.text)
        self.assertEqual(self.queue.qsize(), 1)

    def test_many_log_pages_leave_real_waitress_workers_available(self):
        from waitress import create_server
        server = create_server(web_ui.app, host='127.0.0.1', port=0, threads=2)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        def get(path):
            connection = http.client.HTTPConnection('127.0.0.1', server.effective_port, timeout=3)
            try:
                connection.request('GET', path, headers={'Cookie': COOKIE + '=' + self.token})
                response = connection.getresponse()
                return path, response.status, response.read()
            finally:
                connection.close()

        try:
            paths = ['/api/log-stream'] * 12 + ['/healthz', '/login']
            with ThreadPoolExecutor(max_workers=14) as pool:
                results = list(pool.map(get, paths))
            self.assertTrue(all(status == 200 for _, status, _ in results))
            self.assertEqual(json.loads(next(body for path, _, body in results if path == '/healthz')), {'ok': True})
            self.assertEqual(get('/api/auth/session')[1], 200)
        finally:
            server.close()
            server.task_dispatcher.shutdown()
            thread.join(timeout=3)
