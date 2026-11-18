"""SMS alerts (against a fake Twilio) and the outside monitor (against a fake Icecast)."""

import base64
import http.server
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from mac.alerts import Alerter
from mac.playout_monitor import Monitor


class FakeServer(http.server.ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.posts = []
        self.status = {"icestats": {"source": []}}
        self.reply_code = 201

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        self.server.posts.append((self.path, dict(self.headers), urllib.parse.parse_qs(body)))
        code = self.server.reply_code
        payload = {"sid": "SM123", "status": "queued"} if code < 400 else {"code": 21610, "message": "opted out"}
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        data = json.dumps(self.server.status).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.server = FakeServer()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {
            "TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "secret", "TWILIO_FROM": "+15550001111",
            "WRIT_ALERT_SMS_TO": "+15550002222", "TWILIO_API_BASE": self.server.base,
        }


class AlertTests(Base):
    def test_sends_once_per_cooldown_and_clears(self):
        alerter = Alerter(self.dir, call_sign="WIT-FM Next", env=self.env)
        t0 = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(alerter.send("silence", "20 s of silence", now=t0), "sent")
        self.assertEqual(alerter.send("silence", "20 s of silence", now=t0 + timedelta(minutes=5)), "cooldown")
        self.assertEqual(alerter.send("silence", "still silent", now=t0 + timedelta(minutes=31)), "sent")
        self.assertEqual(alerter.clear("silence", "audio is back", now=t0 + timedelta(minutes=32)), "sent")
        self.assertEqual(alerter.clear("silence", "audio is back"), "nothing open")

        path, headers, form = self.server.posts[0]
        self.assertEqual(path, "/2010-04-01/Accounts/ACtest/Messages.json")
        self.assertEqual(headers["Authorization"], "Basic " + base64.b64encode(b"ACtest:secret").decode())
        self.assertEqual(form, {"To": ["+15550002222"], "From": ["+15550001111"],
                                "Body": ["WIT-FM Next: 20 s of silence"]})
        self.assertEqual(len(self.server.posts), 3)
        self.assertEqual(self.server.posts[-1][2]["Body"], ["WIT-FM Next: audio is back"])

    def test_messaging_service_is_preferred(self):
        env = {**self.env, "TWILIO_MESSAGING_SERVICE_SID": "MGtest"}
        Alerter(self.dir, env=env).send("x", "hello")
        form = self.server.posts[0][2]
        self.assertEqual(form["MessagingServiceSid"], ["MGtest"])
        self.assertNotIn("From", form)

    def test_without_twilio_alerts_are_logged_not_lost(self):
        alerter = Alerter(self.dir, env={})
        self.assertEqual(alerter.send("silence", "20 s of silence"), "logged")
        log = [json.loads(line) for line in (self.dir / "alerts.jsonl").read_text().splitlines()]
        self.assertEqual(log[-1]["outcome"], "logged")
        self.assertEqual(self.server.posts, [])

    def test_twilio_errors_report_failure(self):
        self.server.reply_code = 400
        self.assertEqual(Alerter(self.dir, env=self.env).send("x", "hello"), "failed")

    def test_bad_numbers_are_refused(self):
        with self.assertRaises(ValueError):
            Alerter(self.dir, env={**self.env, "WRIT_ALERT_SMS_TO": "555-0100"}).recipients()


class MonitorTests(Base):
    def station(self):
        host, port = self.server.server_address
        out = self.dir / "out"
        return SimpleNamespace(
            id="wit-next", call_sign="WIT-FM Next", output_dir=out, runtime_dir=out / "runtime",
            now_playing_file=out / "now_playing.json",
            stream=SimpleNamespace(mount="/next", icecast_host=host, icecast_port=port,
                                   status_url=f"{self.server.base}/status-json.xsl"),
        )

    def monitor(self):
        station = self.station()
        alerter = Alerter(station.runtime_dir / "playout", call_sign="WIT-FM Next", env=self.env)
        return Monitor(station, alerter, lossless_mount="/lossless", sample_audio=False), station

    def test_missing_mount_alerts_then_clears(self):
        monitor, _ = self.monitor()
        self.server.status = {"icestats": {"source": {"listenurl": "http://x:8000/next", "listeners": 3}}}
        report = monitor.check()
        self.assertEqual(report["problems"], ["/lossless is off Icecast"])
        self.assertIn("/lossless is off Icecast", self.server.posts[-1][2]["Body"][0])

        self.server.status = {"icestats": {"source": [{"listenurl": "http://x:8000/next", "listeners": 3},
                                                      {"listenurl": "http://x:8000/lossless", "listeners": 1}]}}
        report = monitor.check()
        self.assertEqual(report["problems"], [])
        self.assertIn("back on Icecast", self.server.posts[-1][2]["Body"][0])

    def test_stalled_program_is_flagged(self):
        monitor, station = self.monitor()
        now = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)
        station.now_playing_file.parent.mkdir(parents=True)
        station.now_playing_file.write_text(json.dumps({
            "track": "Old", "started_at": (now - timedelta(minutes=10)).isoformat(), "seconds": 60, "show_id": "nocturne"}))
        report = monitor.check(now=now)
        self.assertTrue(report["stalled"])
        self.assertTrue(any("nothing new on air" in p for p in report["problems"]))

    def test_listener_counts_are_logged_per_show(self):
        monitor, station = self.monitor()
        station.now_playing_file.parent.mkdir(parents=True)
        station.now_playing_file.write_text(json.dumps({"track": "x", "show_id": "nocturne",
                                                        "started_at": datetime.now(timezone.utc).isoformat(),
                                                        "seconds": 600}))
        self.server.status = {"icestats": {"source": [{"listenurl": "http://x:8000/next", "listeners": 4},
                                                      {"listenurl": "http://x:8000/lossless", "listeners": 2}]}}
        monitor.check()
        with sqlite3.connect(station.runtime_dir / "playout" / "listeners.db") as db:
            rows = db.execute("SELECT mount, show_id, listeners FROM samples ORDER BY mount").fetchall()
        self.assertEqual(rows, [("/lossless", "nocturne", 2), ("/next", "nocturne", 4)])


if __name__ == "__main__":
    unittest.main()
