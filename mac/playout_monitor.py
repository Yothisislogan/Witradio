#!/usr/bin/env python3
"""Watches the playout from outside it (development plan, items 7 and 9).

Liquidsoap raises the silence alert itself, but that path runs through the director.
This monitor is the safety net when Liquidsoap or the director is the
problem, and it keeps the listener log the schedule will be tuned with:

- mounts:     the MP3 and lossless mounts are on Icecast
- stalled:    a new item started when the last one should have ended
- silence:    an 8-second sample of the MP3 mount is not silent
- buffer:     hours of finished segments waiting in the buffer
              (alerts under WRIT_BUFFER_ALERT_HOURS, default 12, once the
              producers have filled it at least once)
- halo:       the Halo's OpenAI-compatible endpoint answers
              (when WRIT_LLM_BASE_URL is set on this host)
- listeners:  Icecast listener counts per mount and show, kept in
              <runtime>/playout/listeners.db

    python3 mac/playout_monitor.py once          one pass, prints a report
    python3 mac/playout_monitor.py loop          every 60 s (writ runs this)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alerts import Alerter  # noqa: E402

STALL_GRACE = timedelta(minutes=2)
SILENCE_DB = -60.0
AUDIO_EXTENSIONS = {".flac", ".wav", ".mp3", ".ogg", ".opus", ".m4a"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Monitor:
    def __init__(self, station, alerter: Alerter | None = None, *, lossless_mount: str | None = None,
                 sample_audio: bool = True):
        self.station = station
        self.state_dir = station.runtime_dir / "playout"
        self.alerter = alerter or Alerter(self.state_dir, call_sign=station.call_sign)
        self.mounts = [station.stream.mount, lossless_mount or os.environ.get("WRIT_LOSSLESS_MOUNT", "/lossless")]
        self.sample_audio = sample_audio
        self.listeners_db = self.state_dir / "listeners.db"

    # ── probes ──

    def icecast_sources(self) -> list[dict] | None:
        try:
            with urllib.request.urlopen(self.station.stream.status_url, timeout=5) as response:
                data = json.load(response)
        except Exception:
            return None
        sources = data.get("icestats", {}).get("source") or []
        return sources if isinstance(sources, list) else [sources]

    @staticmethod
    def _mount_of(source: dict) -> str:
        return urllib.parse.urlparse(str(source.get("listenurl", ""))).path

    def now_playing(self) -> dict | None:
        try:
            return json.loads(self.station.now_playing_file.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def max_volume(self, url: str, seconds: float = 8.0) -> float | None:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-t", str(seconds), "-i", url,
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=seconds + 20,
        )
        match = re.search(r"max_volume: (-?[\d.]+|-inf) dB", result.stderr)
        if not match:
            return None
        return float("-inf") if match.group(1) == "-inf" else float(match.group(1))

    def buffer_hours(self) -> float:
        root = self.station.output_dir / "buffer"
        if not root.is_dir():
            return 0.0
        played = {}
        state = self.state_dir / "state.json"
        if state.exists():
            try:
                played = json.loads(state.read_text()).get("buffer_played", {})
            except json.JSONDecodeError:
                played = {}
        seconds = 0.0
        for path in root.glob("*/*/*"):
            if path.suffix.lower() not in AUDIO_EXTENSIONS or path.parent.parent.name in ("plans", "talk"):
                continue
            key = f"{path.parent.parent.name}/{path.parent.name}"
            if path.name in played.get(key, []):
                continue
            sidecar = path.with_suffix(".json")
            try:
                seconds += float(json.loads(sidecar.read_text()).get("seconds", 0)) if sidecar.exists() else 0
            except (OSError, ValueError):
                pass
            if not sidecar.exists():
                seconds += path.stat().st_size / (1411200 / 8 * 0.55)  # rough FLAC rate
        return seconds / 3600

    def halo_ok(self) -> bool | None:
        if not os.environ.get("WRIT_LLM_BASE_URL"):
            return None
        from llm_client import LLMClient

        return LLMClient.from_station(self.station).healthy()

    # ── a pass ──

    def check(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or _now()
        report: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "problems": []}

        sources = self.icecast_sources()
        live = {self._mount_of(s): s for s in sources or []}
        report["icecast"] = sources is not None
        for mount in self.mounts:
            up = mount in live
            report[f"mount {mount}"] = up
            self._flag(f"mount{mount.replace('/', '_')}", not up, f"{mount} is off Icecast",
                       f"{mount} is back on Icecast", report)

        playing = self.now_playing()
        if playing:
            ends = datetime.fromisoformat(playing["started_at"]) + timedelta(seconds=float(playing.get("seconds") or 0))
            stalled = now - ends > STALL_GRACE
            report["now_playing"] = playing.get("track")
            report["stalled"] = stalled
            self._flag("stalled", stalled, f"nothing new on air since {playing['started_at'][11:16]} UTC",
                       "the program is moving again", report)

        if self.sample_audio and self.station.stream.mount in live:
            url = f"http://{self.station.stream.icecast_host}:{self.station.stream.icecast_port}{self.station.stream.mount}"
            peak = self.max_volume(url)
            report["max_volume_db"] = peak
            if peak is not None:
                self._flag("silence_probe", peak < SILENCE_DB, f"{self.station.stream.mount} is silent ({peak} dB)",
                           "audio is back", report)

        hours = self.buffer_hours()
        report["buffer_hours"] = round(hours, 1)
        threshold = float(os.environ.get("WRIT_BUFFER_ALERT_HOURS", "12"))
        seen = self.state_dir / ".buffer_seen"
        if hours >= threshold:
            seen.parent.mkdir(parents=True, exist_ok=True)
            seen.touch()
        if seen.exists():  # only once the producers have filled it
            self._flag("buffer", hours < threshold, f"only {hours:.1f} h in the buffer (alert under {threshold:g} h)",
                       f"buffer back to {hours:.1f} h", report)

        halo = self.halo_ok()
        if halo is not None:
            report["halo"] = halo
            self._flag("halo", not halo, "the Halo's LLM endpoint is not answering", "the Halo answers again", report)

        if sources is not None:
            self.log_listeners(now, live, (playing or {}).get("show_id", ""))
        return report

    def _flag(self, key: str, bad: bool, message: str, cleared: str, report: dict) -> None:
        if bad:
            report["problems"].append(message)
            self.alerter.send(key, message)
        else:
            self.alerter.clear(key, cleared)

    def log_listeners(self, now: datetime, live: dict[str, dict], show_id: str) -> None:
        self.listeners_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.listeners_db) as db:
            db.execute("CREATE TABLE IF NOT EXISTS samples (at TEXT, mount TEXT, show_id TEXT, listeners INTEGER)")
            for mount in self.mounts:
                if mount in live:
                    db.execute("INSERT INTO samples VALUES (?, ?, ?, ?)",
                               (now.isoformat(timespec="seconds"), mount, show_id,
                                int(live[mount].get("listeners") or 0)))


def main(argv: Sequence[str] | None = None) -> int:
    from station_config import load_station_config

    parser = argparse.ArgumentParser(description="Watch the WIT-FM playout")
    parser.add_argument("--station", default=os.environ.get("WRIT_STATION_ID", "wit-next"))
    parser.add_argument("--no-audio", action="store_true", help="skip the 8-second silence sample")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("once")
    p_loop = sub.add_parser("loop")
    p_loop.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args(argv)

    monitor = Monitor(load_station_config(args.station), sample_audio=not args.no_audio)
    if args.cmd == "once":
        report = monitor.check()
        print(json.dumps(report, indent=1))
        return 1 if report["problems"] else 0
    while True:
        try:
            report = monitor.check()
            status = "; ".join(report["problems"]) or "ok"
            print(f"[{report['at'][11:19]}] {status}", flush=True)
        except Exception as exc:  # keep watching
            print(f"monitor error: {exc!r}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
