import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac'))
import api_server
from feedback import FeedbackStore


class ReleaseAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        station = SimpleNamespace(id='wit', home_dir=Path(self.tmp.name), stream=SimpleNamespace(api_port=8001))
        self.station_patch = patch.object(api_server, 'STATION', station)
        self.station_patch.start()
        self.addCleanup(self.station_patch.stop)
        self.server = api_server.ReusableTCPServer(('127.0.0.1', 0), api_server.NowPlayingHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.store = FeedbackStore(station.home_dir/'feedback.db')
        self.identity = 'a'*64
        self.store.register(self.identity, 'Verified current track')
        self.listener = '00000000-0000-4000-8000-000000000001'

    def request(self, body, path='/feedback', headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            raw = json.dumps(body) if not isinstance(body, str) else body
            conn.request('POST', path, body=raw, headers=headers or {'Content-Type': 'application/json'})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_feedback_request_reaches_persistent_store(self):
        payload = {'track_id': self.identity, 'listener_id': self.listener, 'vote': 'like'}
        status, data = self.request(payload)
        self.assertEqual((status, data['status']), (200, 'saved'))
        self.request(payload)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM votes').fetchone()[0], 1)

    def test_unknown_track_does_not_claim_success(self):
        status, data = self.request({'track_id': 'b'*64, 'listener_id': self.listener, 'vote': 'like'})
        self.assertEqual(status, 400)
        self.assertIn('error', data)

    def test_body_errors_are_bounded_and_rejected(self):
        for payload, status in [('x'*4097, 413), ('{bad', 400), ('[]', 400)]:
            with self.subTest(payload=payload[:10]):
                self.assertEqual(self.request(payload)[0], status)
        self.assertEqual(self.request('{}', headers={'Content-Length': '-1'})[0], 413)
        self.assertEqual(self.request('{}', headers={'Content-Length': 'bad'})[0], 400)

    def test_station_prefixed_feedback_uses_the_same_handler(self):
        with patch.object(api_server, 'load_station_config', return_value=api_server.STATION):
            status, data = self.request({'track_id': self.identity, 'listener_id': self.listener, 'vote': 'like'}, '/wit/feedback')
        self.assertEqual((status, data['status']), (200, 'saved'))


class AudioHealthTests(unittest.TestCase):
    def test_running_process_with_silent_audio_is_degraded(self):
        state = {'audio': {'status': 'silent', 'checked_at': time.time(), 'failures': 2}}
        with patch.object(api_server, '_track_info', state), patch.object(api_server, '_api_mode', 'stream'), patch.object(api_server, '_encoder_getter', lambda: SimpleNamespace(poll=lambda: None)), patch.object(api_server, 'check_url', return_value=True), patch.object(api_server, 'check_process', return_value=False):
            data = api_server.get_health_status()
        self.assertEqual(data['status'], 'degraded')
        self.assertEqual(data['components']['streamer']['status'], 'up')
        self.assertEqual(data['components']['audio']['status'], 'silent')

    def test_stale_audio_sample_does_not_report_healthy(self):
        state = {'audio': {'status': 'audible', 'checked_at': time.time()-100, 'failures': 0}}
        with patch.object(api_server, '_track_info', state), patch.object(api_server, '_api_mode', 'stream'), patch.object(api_server, '_encoder_getter', lambda: SimpleNamespace(poll=lambda: None)), patch.object(api_server, 'check_url', return_value=True), patch.object(api_server, 'check_process', return_value=True):
            self.assertEqual(api_server.get_health_status()['status'], 'degraded')
