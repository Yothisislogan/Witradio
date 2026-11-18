#!/usr/bin/env python3
"""SMS alerts for the playout chain, through Twilio (development plan, item 7).

Liquidsoap calls this on silence, and the monitor on a missing mount, a thin
buffer or an unreachable Halo. Each alert key has a cooldown so one outage
sends one text, not hundreds; when the problem clears, a "cleared" message
resets it.

Credentials come from the environment only (never commit them):

    TWILIO_ACCOUNT_SID              AC...
    TWILIO_AUTH_TOKEN               or TWILIO_API_KEY + TWILIO_API_SECRET
    TWILIO_MESSAGING_SERVICE_SID    MG... (recommended), or
    TWILIO_FROM                     your Twilio number, E.164 (+1...)
    WRIT_ALERT_SMS_TO               one or more E.164 numbers, comma-separated

Without them, alerts are still written to <runtime>/playout/alerts.jsonl and
printed, so nothing breaks before Twilio is set up. US numbers need A2P 10DLC
(or toll-free) registration, or carriers drop the texts (Twilio error 30034).

    python3 mac/alerts.py send --key silence --message "20 s of silence on /next"
    python3 mac/alerts.py clear --key silence --message "audio is back"
    python3 mac/alerts.py test
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

E164 = re.compile(r"^\+[1-9]\d{6,14}$")
DEFAULT_COOLDOWN = timedelta(minutes=30)
NO_RETRY_CODES = {21211, 21408, 21610, 30034}  # bad number, region off, opted out, unregistered


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Alerter:
    def __init__(self, state_dir: Path, call_sign: str = "WIT-FM", env: dict | None = None):
        self.state_dir = state_dir
        self.call_sign = call_sign
        self.env = os.environ if env is None else env
        self.state_path = state_dir / "alerts.json"
        self.log_path = state_dir / "alerts.jsonl"

    # ── configuration ──

    def recipients(self) -> list[str]:
        raw = self.env.get("WRIT_ALERT_SMS_TO", "")
        numbers = [n.strip() for n in raw.split(",") if n.strip()]
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

    # ── sending ──

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

    def _log(self, record: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def send(self, key: str, message: str, *, cooldown: timedelta = DEFAULT_COOLDOWN,
             force: bool = False, now: datetime | None = None) -> str:
        """Returns sent, cooldown, logged (Twilio not set up) or failed."""
        now = now or _now()
        body = f"{self.call_sign}: {message}"[:300]
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / ".alerts.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
            except json.JSONDecodeError:
                state = {}
            last = state.get(key, {}).get("sent_at")
            if not force and last and now - datetime.fromisoformat(last) < cooldown:
                outcome = "cooldown"
            elif not self.configured():
                outcome = "logged"
            else:
                results = [self._post(to, body) for to in self.recipients()]
                errors = [r for r in results if r.get("error_code")]
                outcome = "failed" if len(errors) == len(results) else "sent"
                for r in errors:
                    self._log({"at": now.isoformat(), "key": key, "error": r})
            if outcome in ("sent", "logged"):
                state[key] = {"sent_at": now.isoformat(), "message": message, "open": True}
                self.state_path.write_text(json.dumps(state, indent=1))
        self._log({"at": now.isoformat(), "key": key, "outcome": outcome, "body": body})
        print(f"[alert:{key}] {outcome}: {body}", file=sys.stderr)
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
    parser = argparse.ArgumentParser(description="WIT-FM SMS alerts")
    parser.add_argument("--station", default=os.environ.get("WRIT_STATION_ID", "wit-next"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_send = sub.add_parser("send")
    p_send.add_argument("--key", required=True)
    p_send.add_argument("--message", required=True)
    p_send.add_argument("--cooldown-minutes", type=float, default=30)
    p_clear = sub.add_parser("clear")
    p_clear.add_argument("--key", required=True)
    p_clear.add_argument("--message", required=True)
    sub.add_parser("test", help="send a test text now, ignoring the cooldown")
    args = parser.parse_args(argv)

    alerter = for_station(args.station)
    if args.cmd == "send":
        outcome = alerter.send(args.key, args.message, cooldown=timedelta(minutes=args.cooldown_minutes))
    elif args.cmd == "clear":
        outcome = alerter.clear(args.key, args.message)
    else:
        if not alerter.configured():
            print("Twilio is not configured: set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
                  "TWILIO_MESSAGING_SERVICE_SID or TWILIO_FROM, and WRIT_ALERT_SMS_TO")
            return 1
        outcome = alerter.send("test", "test alert from the playout chain", force=True)
    return 0 if outcome != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
