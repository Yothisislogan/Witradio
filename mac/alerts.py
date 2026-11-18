#!/usr/bin/env python3
"""Alerts for the playout chain: email, and optionally text messages.

Liquidsoap raises one on silence, and the monitor on a missing mount, a
stalled program, a thin buffer or an unreachable Halo. Each alert key has a
cooldown, so one outage sends one message rather than hundreds, and when the
problem clears, one "resolved" message follows.

Email goes through any SMTP server: your mail provider's (Gmail or
Microsoft 365 with an app password, Fastmail...), a relay such as SendGrid
or Mailgun, or a mail server on the playout host. Settings come from the
environment only; never commit them:

    WRIT_ALERT_EMAIL_TO     one or more addresses, comma-separated
    WRIT_SMTP_HOST          e.g. smtp.gmail.com
    WRIT_SMTP_PORT          587 (STARTTLS, the default), 465 (SSL) or 25
    WRIT_SMTP_USER          the account to sign in as, if the server asks
    WRIT_SMTP_PASSWORD      its password or app password
    WRIT_SMTP_FROM          the sender address (default: WRIT_SMTP_USER)
    WRIT_SMTP_SECURITY      starttls, ssl or none (default: from the port)

Text messages through Twilio still work alongside email, if these are set.
US carriers drop texts from unregistered numbers (Twilio error 30034), so
the number needs A2P 10DLC or toll-free registration first:

    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN (or TWILIO_API_KEY + TWILIO_API_SECRET),
    TWILIO_MESSAGING_SERVICE_SID or TWILIO_FROM, WRIT_ALERT_SMS_TO

With nothing set, alerts are still written to <runtime>/playout/alerts.jsonl
and printed, so nothing breaks before alerts are set up.

    python3 mac/alerts.py test
    python3 mac/alerts.py send --key silence --message "20 s of silence on /next"
    python3 mac/alerts.py clear --key silence --message "audio is back"
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

E164 = re.compile(r"^\+[1-9]\d{6,14}$")
EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")
DEFAULT_COOLDOWN = timedelta(minutes=30)
EASTERN = ZoneInfo("America/New_York")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _addresses(raw: str, name: str) -> list[str]:
    found = [a.strip() for a in raw.split(",") if a.strip()]
    bad = [a for a in found if not EMAIL.match(a)]
    if bad:
        raise ValueError(f"{name} must hold email addresses, got {bad}")
    return found


class EmailChannel:
    name = "email"

    def __init__(self, env: Mapping[str, str]):
        self.env = env

    def recipients(self) -> list[str]:
        return _addresses(self.env.get("WRIT_ALERT_EMAIL_TO", ""), "WRIT_ALERT_EMAIL_TO")

    def configured(self) -> bool:
        return bool(self.env.get("WRIT_SMTP_HOST") and self.recipients() and self.sender())

    def sender(self) -> str:
        return (self.env.get("WRIT_SMTP_FROM") or self.env.get("WRIT_SMTP_USER") or "").strip()

    def security(self) -> tuple[str, int]:
        mode = (self.env.get("WRIT_SMTP_SECURITY") or "").strip().lower()
        port = int(self.env.get("WRIT_SMTP_PORT") or 0)
        if not mode:
            mode = "ssl" if port == 465 else "none" if port == 25 else "starttls"
        if mode not in ("starttls", "ssl", "none"):
            raise ValueError("WRIT_SMTP_SECURITY must be starttls, ssl or none")
        return mode, port or {"ssl": 465, "none": 25}.get(mode, 587)

    def send(self, subject: str, body: str) -> str | None:
        """None on success, else what went wrong."""
        sender = self.sender()
        if not EMAIL.match(sender):
            return "WRIT_SMTP_FROM (or WRIT_SMTP_USER) must be an email address"
        message = EmailMessage()
        message["From"] = sender
        message["To"] = ", ".join(self.recipients())
        message["Subject"] = subject
        message["Date"] = format_datetime(_now())
        message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
        message.set_content(body)
        mode, port = self.security()
        host = self.env["WRIT_SMTP_HOST"]
        try:
            if mode == "ssl":
                client = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
            else:
                client = smtplib.SMTP(host, port, timeout=20)
            with client:
                client.ehlo()
                if mode == "starttls":
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                if self.env.get("WRIT_SMTP_USER"):
                    client.login(self.env["WRIT_SMTP_USER"], self.env.get("WRIT_SMTP_PASSWORD", ""))
                client.send_message(message)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            return f"{type(exc).__name__}: {exc}"[:300]
        return None


class SmsChannel:
    name = "sms"

    def __init__(self, env: Mapping[str, str]):
        self.env = env

    def recipients(self) -> list[str]:
        numbers = [n.strip() for n in self.env.get("WRIT_ALERT_SMS_TO", "").split(",") if n.strip()]
        bad = [n for n in numbers if not E164.match(n)]
        if bad:
            raise ValueError(f"WRIT_ALERT_SMS_TO must hold E.164 numbers like +15551234567, got {bad}")
        return numbers

    def configured(self) -> bool:
        has_auth = self.env.get("TWILIO_AUTH_TOKEN") or (
            self.env.get("TWILIO_API_KEY") and self.env.get("TWILIO_API_SECRET")
        )
        has_sender = self.env.get("TWILIO_MESSAGING_SERVICE_SID") or self.env.get("TWILIO_FROM")
        return bool(self.env.get("TWILIO_ACCOUNT_SID") and has_auth and has_sender and self.recipients())

    def _post(self, to: str, body: str) -> dict:
        sid = self.env["TWILIO_ACCOUNT_SID"]
        user, secret = (
            (self.env["TWILIO_API_KEY"], self.env["TWILIO_API_SECRET"])
            if self.env.get("TWILIO_API_KEY") else (sid, self.env["TWILIO_AUTH_TOKEN"])
        )
        form = {"To": to, "Body": body}
        if self.env.get("TWILIO_MESSAGING_SERVICE_SID"):
            form["MessagingServiceSid"] = self.env["TWILIO_MESSAGING_SERVICE_SID"]
        else:
            sender = self.env["TWILIO_FROM"]
            if not E164.match(sender):
                raise ValueError("TWILIO_FROM must be an E.164 number like +15551234567")
            form["From"] = sender
        base = self.env.get("TWILIO_API_BASE", "https://api.twilio.com").rstrip("/")
        request = urllib.request.Request(
            f"{base}/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json",
            data=urllib.parse.urlencode(form).encode(),
            headers={
                "Authorization": "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode() or "{}")
            except json.JSONDecodeError:
                detail = {}
            return {"error_code": detail.get("code", exc.code), "error_message": detail.get("message", str(exc))}
        except (urllib.error.URLError, OSError) as exc:
            return {"error_code": "network", "error_message": str(exc)}

    def send(self, subject: str, body: str) -> str | None:
        results = [self._post(to, subject[:300]) for to in self.recipients()]
        errors = [r for r in results if r.get("error_code")]
        if errors and len(errors) == len(results):
            return "; ".join(f"{e['error_code']}: {e.get('error_message', '')}" for e in errors)[:300]
        return None


class Alerter:
    def __init__(self, state_dir: Path, call_sign: str = "WIT-FM", env: Mapping[str, str] | None = None):
        self.state_dir = state_dir
        self.call_sign = call_sign
        self.env = os.environ if env is None else env
        self.state_path = state_dir / "alerts.json"
        self.log_path = state_dir / "alerts.jsonl"
        self.channels = [EmailChannel(self.env), SmsChannel(self.env)]
        self.config_errors: dict[str, str] = {}

    def active_channels(self) -> list:
        """Channels that are set up. A mistyped setting skips that channel and is
        reported, rather than stopping an alert in the middle of an outage."""
        active = []
        for channel in self.channels:
            try:
                if channel.configured():
                    active.append(channel)
            except ValueError as exc:
                self.config_errors[channel.name] = str(exc)
        return active

    def configured(self) -> bool:
        return bool(self.active_channels())

    def _log(self, record: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def _compose(self, key: str, message: str, now: datetime) -> tuple[str, str]:
        resolved = key.endswith(":cleared")
        subject = f"[{self.call_sign}] {'Resolved: ' if resolved else ''}{message}"[:200]
        local = now.astimezone(EASTERN)
        body = "\n".join([
            message,
            "",
            f"Station: {self.call_sign}",
            f"When: {local:%a %b %-d, %-I:%M %p} Eastern ({now:%H:%M} UTC)",
            f"Alert: {key}",
            "",
            "One message per problem at most every 30 minutes, and another when it clears.",
            "History: runtime/playout/alerts.jsonl on the playout server.",
        ])
        return subject, body

    def send(self, key: str, message: str, *, cooldown: timedelta = DEFAULT_COOLDOWN,
             force: bool = False, now: datetime | None = None) -> str:
        """Returns sent, cooldown, logged (no channel set up) or failed."""
        now = now or _now()
        subject, body = self._compose(key, message, now)
        errors: dict[str, str] = {}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / ".alerts.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
            except json.JSONDecodeError:
                state = {}
            last = state.get(key, {}).get("sent_at")
            channels = self.active_channels()
            if not force and last and now - datetime.fromisoformat(last) < cooldown:
                outcome = "cooldown"
            elif not channels:
                outcome = "logged"
            else:
                for channel in channels:
                    problem = channel.send(subject, body)
                    if problem:
                        errors[channel.name] = problem
                outcome = "failed" if len(errors) == len(channels) else "sent"
            if outcome in ("sent", "logged"):
                state[key] = {"sent_at": now.isoformat(), "message": message, "open": True}
                self.state_path.write_text(json.dumps(state, indent=1))
        record = {"at": now.isoformat(), "key": key, "outcome": outcome, "subject": subject}
        if errors:
            record["errors"] = errors
        if self.config_errors:
            record["config_errors"] = dict(self.config_errors)
        self._log(record)
        print(f"[alert:{key}] {outcome}: {subject}" + (f" ({errors})" if errors else ""), file=sys.stderr)
        return outcome

    def clear(self, key: str, message: str, now: datetime | None = None) -> str:
        """Send an all-clear, once, if this key had an open alert."""
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if not state.get(key, {}).get("open"):
            return "nothing open"
        outcome = self.send(f"{key}:cleared", message, force=True, now=now)
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        state.pop(key, None)
        self.state_path.write_text(json.dumps(state, indent=1))
        return outcome


def for_station(station_id: str | None = None) -> Alerter:
    from station_config import load_station_config

    station = load_station_config(station_id)
    return Alerter(station.runtime_dir / "playout", call_sign=station.call_sign)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WIT-FM playout alerts (email, optionally SMS)")
    parser.add_argument("--station", default=os.environ.get("WRIT_STATION_ID", "wit-next"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_send = sub.add_parser("send")
    p_send.add_argument("--key", required=True)
    p_send.add_argument("--message", required=True)
    p_send.add_argument("--cooldown-minutes", type=float, default=30)
    p_clear = sub.add_parser("clear")
    p_clear.add_argument("--key", required=True)
    p_clear.add_argument("--message", required=True)
    sub.add_parser("test", help="send a test alert now, ignoring the cooldown")
    args = parser.parse_args(argv)

    alerter = for_station(args.station)
    if args.cmd == "send":
        outcome = alerter.send(args.key, args.message, cooldown=timedelta(minutes=args.cooldown_minutes))
    elif args.cmd == "clear":
        outcome = alerter.clear(args.key, args.message)
    else:
        channels = alerter.active_channels()
        for name, problem in alerter.config_errors.items():
            print(f"{name} settings: {problem}")
        if not channels:
            print("No alert channel is set up. For email set WRIT_ALERT_EMAIL_TO, WRIT_SMTP_HOST, "
                  "WRIT_SMTP_USER and WRIT_SMTP_PASSWORD (see mac/alerts.py).")
            return 1
        print("Sending a test alert by " + " and ".join(c.name for c in channels))
        outcome = alerter.send("test", "Test alert from the playout chain", force=True)
        print(outcome)
    return 0 if outcome != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
