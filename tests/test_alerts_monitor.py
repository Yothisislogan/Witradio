"""Alerts (against a fake SMTP server and a fake Twilio) and the outside
monitor (against a fake Icecast)."""

import base64
import email
import email.policy
import http.server
import json
import socketserver
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mac.alerts import Alerter
from mac.playout_monitor import Monitor


class FakeSMTP(socketserver.ThreadingTCPServer):
    """Just enough SMTP for smtplib: EHLO, AUTH PLAIN, MAIL, RCPT, DATA, QUIT."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeSMTPHandler)
        self.messages: list[dict] = []
        self.refuse_recipients = False


class FakeSMTPHandler(socketserver.StreamRequestHandler):
    def reply(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())

    def handle(self):
        server: FakeSMTP = self.server
        envelope = {"from": None, "to": [], "auth": None}
        self.reply("220 fake ESMTP")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode().rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            if verb == "EHLO":
                self.wfile.write(b"250-fake\r\n250-AUTH PLAIN\r\n250 8BITMIME\r\n")
            elif verb == "AUTH":
                _, user, password = base64.b64decode(line.split()[2]).split(b"\0")
                envelope["auth"] = (user.decode(), password.decode())
                self.reply("235 accepted")
            elif verb == "MAIL":
                envelope["from"] = line.split(":", 1)[1].split()[0].strip("<>")
                self.reply("250 ok")
            elif verb == "RCPT":
                if server.refuse_recipients:
                    self.reply("550 no such user")
                    continue
                envelope["to"].append(line.split(":", 1)[1].strip().strip("<>"))
                self.reply("250 ok")
            elif verb == "DATA":
                self.reply("354 go ahead")
                lines = []
                while (data := self.rfile.readline()) not in (b".\r\n", b""):
                    lines.append(data)
                server.messages.append({**envelope, "message": email.message_from_bytes(
                    b"".join(lines), policy=email.policy.default)})
                self.reply("250 queued")
            elif verb == "QUIT":
                self.reply("221 bye")
                return
            else:
                self.reply("250 ok")


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
                                "Body": ["[WIT-FM Next] 20 s of silence"]})
        self.assertEqual(len(self.server.posts), 3)
        self.assertEqual(self.server.posts[-1][2]["Body"], ["[WIT-FM Next] Resolved: audio is back"])

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

    def test_bad_numbers_skip_the_channel_and_are_reported(self):
        alerter = Alerter(self.dir, env={**self.env, "WRIT_ALERT_SMS_TO": "555-0100"})
        self.assertEqual(alerter.send("x", "hello"), "logged")  # never raises mid-outage
        self.assertIn("E.164", alerter.config_errors["sms"])


class EmailTests(Base):
    def setUp(self):
        super().setUp()
        self.smtp = FakeSMTP()
        threading.Thread(target=self.smtp.serve_forever, daemon=True).start()
        self.addCleanup(self.smtp.server_close)
        self.addCleanup(self.smtp.shutdown)
        self.mail_env = {
            "WRIT_ALERT_EMAIL_TO": "ops@example.com, oncall@example.com",
            "WRIT_SMTP_HOST": "127.0.0.1", "WRIT_SMTP_PORT": str(self.smtp.server_address[1]),
            "WRIT_SMTP_SECURITY": "none", "WRIT_SMTP_USER": "alerts@example.com", "WRIT_SMTP_PASSWORD": "app-password",
        }

    def test_email_alert_and_all_clear(self):
        alerter = Alerter(self.dir, call_sign="WIT-FM Next", env=self.mail_env)
        t0 = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(alerter.send("silence", "20 s of silence on air", now=t0), "sent")
        self.assertEqual(alerter.send("silence", "20 s of silence on air", now=t0 + timedelta(minutes=5)), "cooldown")
        self.assertEqual(alerter.clear("silence", "audio is back on air", now=t0 + timedelta(minutes=6)), "sent")

        first, resolved = self.smtp.messages
        self.assertEqual(first["auth"], ("alerts@example.com", "app-password"))
        self.assertEqual(first["from"], "alerts@example.com")
        self.assertEqual(first["to"], ["ops@example.com", "oncall@example.com"])
        self.assertEqual(first["message"]["Subject"], "[WIT-FM Next] 20 s of silence on air")
        body = first["message"].get_content()
        self.assertIn("20 s of silence on air", body)
        self.assertIn("Tue Nov 17, 10:00 PM Eastern (03:00 UTC)", body)
        self.assertEqual(resolved["message"]["Subject"], "[WIT-FM Next] Resolved: audio is back on air")
        self.assertEqual(self.server.posts, [])  # no Twilio settings: no texts

    def test_email_and_text_together_and_partial_failure(self):
        alerter = Alerter(self.dir, call_sign="WIT-FM Next", env={**self.env, **self.mail_env})
        self.assertEqual(alerter.send("mount", "/lossless is off Icecast"), "sent")
        self.assertEqual(len(self.smtp.messages), 1)
        self.assertEqual(len(self.server.posts), 1)
        self.smtp.refuse_recipients = True  # email fails, the text still goes out
        self.assertEqual(alerter.send("buffer", "only 3 h in the buffer"), "sent")
        log = [json.loads(line) for line in (self.dir / "alerts.jsonl").read_text().splitlines()]
        self.assertIn("email", log[-1]["errors"])

    def test_all_channels_failing_reports_failure(self):
        self.smtp.refuse_recipients = True
        self.assertEqual(Alerter(self.dir, env=self.mail_env).send("x", "hello"), "failed")

    def test_starttls_is_used_on_port_587(self):
        env = {**self.mail_env, "WRIT_SMTP_PORT": "587", "WRIT_SMTP_SECURITY": ""}
        with patch("smtplib.SMTP") as smtp:
            client = smtp.return_value
            self.assertEqual(Alerter(self.dir, env=env).send("x", "hello"), "sent")
        smtp.assert_called_once_with("127.0.0.1", 587, timeout=20)
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("alerts@example.com", "app-password")
        client.send_message.assert_called_once()

    def test_bad_address_skips_email_and_is_reported(self):
        alerter = Alerter(self.dir, env={**self.mail_env, "WRIT_ALERT_EMAIL_TO": "not an address"})
        self.assertFalse(alerter.configured())
        self.assertIn("email addresses", alerter.config_errors["email"])


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
