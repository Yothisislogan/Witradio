#!/usr/bin/env python3
"""Decides what WIT-FM plays next, one item at a time, for Liquidsoap.

Liquidsoap (liquidsoap/wit_next.liq) is the playout master: it decodes,
applies each work's gain, encodes /lossless and the MP3 mount, and falls back
to the safe library and station IDs if this program ever fails. This program
is its brain:

    playout_director.py serve    the localhost service Liquidsoap talks to:
                                 GET /next, /aired, /alert, /health
    playout_director.py next     print one annotated request (for testing)
    playout_director.py aired    record that an item started (for testing)
    playout_director.py status   what it thinks is on and what comes next

Liquidsoap reaches it over HTTP rather than by running a command per track:
spawning processes from Liquidsoap's threads crashed 2.2.4 in rehearsal.

What it serves, in order of preference, for the time the item will start:

1. classical blocks: the planner's plan for the block, entry by entry:
   whole works (movement by movement, one gain), 2-4 s gaps, hourly IDs and
   the Count's links once the producers render them. Blocks below their
   unlock threshold run their fallback show instead.
2. finished segments the producers left in the buffer for the show's slot
   (buffer/<show_id>/<slot>/), the 48-hour buffer the Halo fills
3. the safe library: cleared, catalogued music (library/air/safe/, the catalog source "safe")
4. station IDs (library/ids/)
5. room tone, so there is never dead air

It never cuts a work: if the program runs late it drops links and fit
pieces, and a work that would overrun the next post by more than a minute
waits for another day. Times are real (UTC) seconds, so DST is harmless.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schedule import ProgramBlock, StationSchedule, load_schedule, slot_key  # noqa: E402

AUDIO_EXTENSIONS = {".flac", ".wav", ".mp3", ".ogg", ".opus", ".m4a", ".aif", ".aiff"}
LATE_WINDOW = timedelta(minutes=10)  # joining a block this late still starts at its top
POST_SLACK = 60.0  # a work may overrun the next post by at most this many seconds
SAFE_REPEAT_HOURS = 6.0
SERVED_KEEP = 40


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


@dataclass
class Item:
    """One request for Liquidsoap: a file plus everything shown and logged about it."""

    kind: str  # work | talk | id | gap | buffer | safe | filler | roomtone
    path: str
    seconds: float
    title: str = ""
    artist: str = ""  # performer, or the host for talk
    composer: str = ""
    credit: str = ""
    license: str = ""
    gain_db: float = 0.0
    show_id: str = ""
    show_title: str = ""
    host: str = ""
    lossless: bool = False  # only for cleared lossless works in a classical block
    entry_id: str = ""
    work_id: int | None = None
    plan_id: str = ""
    role: str = ""
    starts_at: str = ""  # when the director expects it to start

    def annotate(self) -> str:
        """Liquidsoap's annotate: URI, carrying gain and display metadata."""
        fields = {
            "title": self.display_title(),
            # The host is the artist of their own links, never of the music.
            "artist": self.artist or (self.host if self.kind == "talk" else ""),
            "album": self.show_title,
            "comment": self.credit,
            "liq_amplify": f"{self.gain_db:+.2f} dB",
            "witfm_entry": self.entry_id,
            "witfm_kind": self.kind,
            "witfm_show": self.show_id,
            "witfm_lossless": "true" if self.lossless else "false",
        }
        def quote(value: str) -> str:
            return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
        pairs = ",".join(f"{key}={quote(value)}" for key, value in fields.items() if value != "")
        return f"annotate:{pairs}:{self.path}"

    def display_title(self) -> str:
        if self.kind in ("work", "filler", "safe") and self.composer and self.title:
            return f"{self.composer}: {self.title}"
        return self.title


# ── the director ─────────────────────────────────────────────────────────────


class Director:
    def __init__(self, station, schedule: StationSchedule | None = None, catalog_path: Path | None = None,
                 rng: random.Random | None = None):
        self.station = station
        self.schedule = schedule or load_schedule(station.schedule_path)
        self.catalog_path = Path(catalog_path or station.catalog_db_path)
        self.library = station.library_dir
        self.buffer = station.output_dir / "buffer"
        self.plans_dir = self.buffer / "plans"
        self.talk_dir = self.buffer / "talk"
        # The station exports these for Liquidsoap too (station_config.env()).
        self.safe_dir = Path(os.environ.get("WRIT_SAFE_DIR") or self.library / "air" / "safe")
        self.ids_dir = Path(os.environ.get("WRIT_IDS_DIR") or self.library / "ids")
        self.state_dir = station.runtime_dir / "playout"
        self.state_path = self.state_dir / "state.json"
        self.air_log = self.state_dir / "airlog.jsonl"
        self.rng = rng or random.Random()
        self._catalog = None

    # ── state ──

    @contextmanager
    def locked_state(self) -> Iterator[dict]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
            except (OSError, json.JSONDecodeError):
                state = {}
            yield state
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1))
            tmp.replace(self.state_path)

    # ── catalog (optional on the playout host) ──

    def catalog(self):
        if self._catalog is None and self.catalog_path.exists():
            from catalog import Catalog

            self._catalog = Catalog(self.catalog_path)
        return self._catalog

    # ── deciding ──

    def next_item(self, now: datetime | None = None) -> Item:
        now = now or utc_now()
        with self.locked_state() as state:
            cursor = _parse(state.get("cursor"))
            # The cursor is when the next item will start: right after the one
            # Liquidsoap is playing. A stale or runaway cursor resyncs to now.
            if cursor is None or cursor < now or cursor > now + timedelta(hours=3):
                cursor = now
            item = self._choose(cursor, state)
            item.starts_at = _iso(cursor)
            state["cursor"] = _iso(cursor + timedelta(seconds=item.seconds))
            state["last_kind"] = item.kind
            state.setdefault("pending", []).append(item.entry_id)
            state["pending"] = state["pending"][-10:]
            served = state.setdefault("served", {})
            served[item.entry_id] = asdict(item)
            for key in list(served)[:-SERVED_KEEP]:
                served.pop(key, None)
            return item

    def _choose(self, cursor: datetime, state: dict) -> Item:
        block = self.schedule.program_block_at(cursor)
        show = self.schedule.shows[block.show_id]
        context = {"show_id": block.show_id, "show_title": block.title, "host": show.host}
        if show.program == "classical":
            fallback = self._classical_unlocked(show)
            if fallback is None:
                item = self._from_plan(block, cursor, state, context)
                if item is not None:
                    return item
                return self._filler(block, cursor, state, context)
            context = {"show_id": fallback, "show_title": self.schedule.shows[fallback].name,
                       "host": self.schedule.shows[fallback].host}
        return (
            self._from_buffer(block, cursor, state, context)
            or self._from_safe(block, cursor, state, context)
            or self._station_id(state, context)
            or self._roomtone(min(30.0, max(1.0, self._seconds_to(block, cursor))), context)
        )

    def _seconds_to(self, block: ProgramBlock, cursor: datetime) -> float:
        return (self.schedule.localize(block.end) - cursor).total_seconds()

    def _classical_unlocked(self, show) -> str | None:
        """None if the block may run; otherwise the show that runs instead.

        WRIT_FORCE_CLASSICAL=1 plays classical blocks before the library
        reaches their unlock hours, for staging on /next.
        """
        need = float(show.params.get("unlock_hours", 0) or 0)
        if os.environ.get("WRIT_FORCE_CLASSICAL") == "1":
            need = 0.0
        catalog = self.catalog()
        hours = catalog.cleared_hours("classical") if catalog else 0.0
        if hours >= need and need >= 0 and catalog is not None:
            return None
        return show.params.get("fallback_show", "monster_mix")

    # ── classical plans ──

    def plan_for(self, block: ProgramBlock) -> dict | None:
        from classical_planner import Plan, commit_plan, plan_for_block

        start = self.schedule.localize(block.start)
        plan_id = f"{block.show_id}@{start.astimezone(timezone.utc):%Y-%m-%dT%H%MZ}"
        path = self.plans_dir / f"{plan_id}.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                pass
        catalog = self.catalog()
        if catalog is None:
            return None
        plan: Plan = plan_for_block(self.schedule, block, catalog)
        if not plan.ok or not plan.entries:
            return None
        commit_plan(catalog, plan)
        data = plan.to_json()
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)
        return data

    def _from_plan(self, block: ProgramBlock, cursor: datetime, state: dict, context: dict) -> Item | None:
        plan = self.plan_for(block)
        if plan is None:
            return None
        plan_id = plan["plan_id"]
        entries = plan["entries"]
        block_end = self.schedule.localize(block.end)
        if state.get("plan_id") != plan_id:
            state["plan_id"] = plan_id
            state["file_index"] = 0
            block_start = self.schedule.localize(block.start)
            if cursor - block_start <= LATE_WINDOW:
                state["index"] = 0
            else:  # joining mid-block: never start a work in the middle
                state["index"] = next(
                    (i for i, e in enumerate(entries) if _parse(e["starts_at"]) >= cursor - timedelta(seconds=2)),
                    len(entries),
                )
        index = state.get("index", 0)
        while index < len(entries):
            entry = entries[index]
            late = (cursor - _parse(entry["starts_at"])).total_seconds()
            if entry["type"] == "work":
                files = entry.get("files") or []
                file_index = state.get("file_index", 0)
                if file_index >= len(files):
                    file_index = 0
                if file_index == 0:
                    end_at = cursor + timedelta(seconds=entry["seconds"])
                    optional = entry["role"] in ("fit", "encore") and late > 30
                    if not files or optional or (end_at - block_end).total_seconds() > POST_SLACK:
                        index += 1
                        continue  # running late: a work waits rather than overrun the post
                entry_index = index
                # Movements play back to back under the work's one gain.
                if file_index + 1 < len(files):
                    state["file_index"] = file_index + 1
                else:
                    index, state["file_index"] = index + 1, 0
                state["index"] = index
                path = files[file_index]
                return Item(
                    kind="work", path=path, seconds=self._duration(path), title=entry.get("title", ""),
                    artist=entry.get("performer") or "", composer=entry.get("composer") or "",
                    credit=entry.get("credit", ""), license=entry.get("license", ""),
                    gain_db=float(entry.get("gain_db") or 0.0), lossless=bool(entry.get("lossless")),
                    entry_id=f"{plan_id}#{entry_index}.{file_index}", work_id=entry.get("work_id"),
                    plan_id=plan_id, role=entry["role"], **context,
                )
            index += 1
            if entry["type"] == "gap":
                if state.get("last_kind") in (None, "gap"):
                    continue  # no double gap after a skipped link
                state["index"] = index
                return self._gap(entry["seconds"], plan_id, index - 1, context)
            if entry["type"] == "id":
                item = self._station_id(state, context)
                if item is not None:
                    state["index"] = index
                    return item
                continue
            if entry["type"] == "talk":
                if late > 30 and entry["role"] not in ("sign_off", "handoff", "closing"):
                    continue  # running late: drop links first
                path = self._talk_file(plan_id, index - 1)
                if path is not None:
                    state["index"] = index
                    return Item(kind="talk", path=str(path), seconds=self._duration(str(path)),
                                title=entry["role"].replace("_", " ").title(), entry_id=f"{plan_id}#{index - 1}",
                                plan_id=plan_id, role=entry["role"], **context)
                continue  # the producers haven't rendered this link yet
        state["index"] = index
        return None  # plan finished early: the filler covers the rest

    def _talk_file(self, plan_id: str, index: int) -> Path | None:
        folder = self.talk_dir / plan_id
        for ext in (".flac", ".wav", ".mp3"):
            path = folder / f"{index:03d}{ext}"
            if path.exists():
                return path
        return None

    def _filler(self, block: ProgramBlock, cursor: datetime, state: dict, context: dict) -> Item:
        """The plan ran short: a shelf piece that fits before the post, or room tone."""
        remaining = self._seconds_to(block, cursor)
        if remaining <= 30:
            return self._roomtone(max(1.0, remaining), context)
        catalog = self.catalog()
        if catalog is not None:
            recent = {p.work_id for p in catalog.plays_since(cursor - timedelta(days=7))}
            fits = [
                w for w in catalog.works(kind="classical")
                if len(w.files) == 1 and 60 <= w.duration_sec <= remaining - 5
                and w.id not in recent and not w.cycle and catalog.is_airable(w)
            ]
            if fits:
                work = max(fits, key=lambda w: (w.duration_sec, self.rng.random()))
                path = work.files[0].path
                return Item(kind="filler", path=path, seconds=work.files[0].duration_sec, title=work.title,
                            artist=work.performer or "", composer=work.composer or "", credit=work.credit_line,
                            license=work.license, gain_db=work.gain_db or 0.0, lossless=work.lossless,
                            entry_id=f"filler:{work.id}:{_iso(cursor)}", work_id=work.id, role="filler", **context)
        safe = self._from_safe(block, cursor, state, context, fit_seconds=remaining)
        return safe or self._roomtone(min(30.0, remaining), context)

    # ── buffer, safe library, IDs ──

    def _from_buffer(self, block: ProgramBlock, cursor: datetime, state: dict, context: dict) -> Item | None:
        slot = slot_key(self.schedule.airing_start(cursor))
        folder = self.buffer / context["show_id"] / slot
        if not folder.is_dir():
            return None
        played = set(state.setdefault("buffer_played", {}).get(f"{context['show_id']}/{slot}", []))
        for path in sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS):
            if path.name in played or path.name.startswith("."):
                continue  # dotfiles are still syncing
            meta = self._sidecar(path)
            state["buffer_played"].setdefault(f"{context['show_id']}/{slot}", []).append(path.name)
            return Item(kind="buffer", path=str(path), seconds=self._duration(str(path)),
                        title=meta.get("title", path.stem), artist=meta.get("artist", ""),
                        credit=meta.get("credit", ""), license=meta.get("license", ""),
                        gain_db=float(meta.get("gain_db", 0.0)), entry_id=f"buffer:{context['show_id']}/{slot}/{path.name}",
                        role=meta.get("kind", "segment"), **context)
        return None

    def _from_safe(self, block: ProgramBlock, cursor: datetime, state: dict, context: dict,
                   fit_seconds: float | None = None) -> Item | None:
        if not self.safe_dir.is_dir():
            return None
        files = [p for p in sorted(self.safe_dir.rglob("*")) if p.suffix.lower() in AUDIO_EXTENSIONS]
        if not files:
            return None
        recent = state.setdefault("safe_recent", {})
        horizon = cursor - timedelta(hours=SAFE_REPEAT_HOURS)
        for path in list(recent):
            if _parse(recent[path]) < horizon:
                recent.pop(path)
        fresh = [p for p in files if str(p) not in recent] or files
        if fit_seconds is not None:
            fitting = [p for p in fresh if self._duration(str(p)) <= fit_seconds]
            if not fitting:
                return None
            fresh = fitting
        path = self.rng.choice(fresh)
        recent[str(path)] = _iso(cursor)
        info = self._catalog_info(str(path)) or self._sidecar(path)
        return Item(kind="safe", path=str(path), seconds=self._duration(str(path)),
                    title=info.get("title", path.stem), artist=info.get("artist", ""),
                    composer=info.get("composer", ""), credit=info.get("credit", ""),
                    license=info.get("license", ""), gain_db=float(info.get("gain_db") or 0.0),
                    work_id=info.get("work_id"), entry_id=f"safe:{path.name}:{_iso(cursor)}", role="safe", **context)

    def _station_id(self, state: dict, context: dict) -> Item | None:
        if not self.ids_dir.is_dir():
            return None
        files = [p for p in sorted(self.ids_dir.iterdir()) if p.suffix.lower() in AUDIO_EXTENSIONS]
        if not files:
            return None
        turn = state.get("id_turn", 0) % len(files)
        state["id_turn"] = turn + 1
        path = files[turn]
        return Item(kind="id", path=str(path), seconds=self._duration(str(path)), title=self.station.call_sign,
                    entry_id=f"id:{path.name}:{state['id_turn']}", role="hourly_id", **context)

    # ── generated audio ──

    def _gap(self, seconds: float, plan_id: str, index: int, context: dict) -> Item:
        """Digital silence between works, like a concert hall."""
        path = self._generated("gap", seconds, "anullsrc=r=44100:cl=stereo")
        return Item(kind="gap", path=str(path), seconds=seconds, entry_id=f"{plan_id}#{index}",
                    plan_id=plan_id, role="gap", **context)

    def _roomtone(self, seconds: float, context: dict) -> Item:
        """Inaudible pink noise: the never-dead-air filler."""
        path = self._generated("roomtone", seconds, "anoisesrc=color=pink:amplitude=0.0003:sample_rate=44100")
        return Item(kind="roomtone", path=str(path), seconds=seconds, title=self.station.call_sign,
                    entry_id=f"roomtone:{seconds:.1f}:{self.rng.random():.6f}", role="roomtone", **context)

    def _generated(self, name: str, seconds: float, source: str) -> Path:
        seconds = round(max(0.5, seconds) * 2) / 2  # half-second steps keep the cache small
        folder = self.state_dir / "generated"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}_{int(seconds * 1000):07d}ms.flac"
        if not path.exists():
            tmp = path.with_suffix(".tmp.flac")
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", source, "-ac", "2",
                            "-sample_fmt", "s16", "-t", f"{seconds:.3f}", str(tmp)], check=True)
            tmp.replace(path)
        return path

    # ── metadata ──

    def _duration(self, path: str) -> float:
        catalog = self.catalog()
        if catalog is not None:
            row = catalog.db.execute("SELECT duration_sec FROM files WHERE path = ?", (path,)).fetchone()
            if row:
                return float(row[0])
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                 "csv=p=0", path], capture_output=True, text=True, timeout=30)
        try:
            return float(result.stdout.strip())
        except ValueError:
            return 0.0

    def _catalog_info(self, path: str) -> dict | None:
        catalog = self.catalog()
        if catalog is None:
            return None
        row = catalog.db.execute(
            """SELECT w.id, w.title, w.composer, w.performer, w.credit_line, w.license, w.gain_db
               FROM files f JOIN works w ON w.id = f.work_id WHERE f.path = ?""", (path,)).fetchone()
        if row is None:
            return None
        return {"work_id": row[0], "title": row[1], "composer": row[2] or "", "artist": row[3] or "",
                "credit": row[4], "license": row[5], "gain_db": row[6] or 0.0}

    @staticmethod
    def _sidecar(path: Path) -> dict:
        meta = path.with_suffix(".json")
        try:
            data = json.loads(meta.read_text()) if meta.exists() else {}
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}

    # ── on air ──

    def aired(self, entry_id: str, path: str, now: datetime | None = None) -> dict:
        """An item just started: now-playing JSON, the as-run log, and catalog airplay."""
        now = now or utc_now()
        with self.locked_state() as state:
            served = state.get("served", {}).get(entry_id)
            pending = state.get("pending", [])
            if entry_id in pending:
                # Liquidsoap prefetches; re-anchor the cursor on what really started.
                after = pending[pending.index(entry_id) + 1:]
                ahead = sum(state["served"].get(e, {}).get("seconds", 0.0) for e in after)
                state["cursor"] = _iso(now + timedelta(seconds=(served or {}).get("seconds", 0.0) + ahead))
                state["pending"] = after
        item = Item(**served) if served else Item(kind="external", path=path, seconds=self._duration(path))
        if not served:  # Liquidsoap's own fallback played this: describe it from the catalog
            info = self._catalog_info(path) or {}
            item.title = info.get("title", Path(path).stem)
            item.composer, item.artist = info.get("composer", ""), info.get("artist", "")
            item.credit, item.license, item.work_id = info.get("credit", ""), info.get("license", ""), info.get("work_id")
        now_playing = {
            "station_id": self.station.id,
            "station": self.station.call_sign,
            "mount": self.station.stream.mount,
            "lossless_mount": os.environ.get("WRIT_LOSSLESS_MOUNT", "/lossless"),
            "track": item.display_title() or item.title,
            "title": item.title,
            "composer": item.composer,
            "artist": item.artist,
            "credit": item.credit,
            "license": item.license,
            "type": item.kind,
            "lossless": item.lossless,
            "show_id": item.show_id,
            "show": item.show_title,
            "host": item.host,
            "started_at": _iso(now),
            "seconds": round(item.seconds, 2),
            "timestamp": _iso(now),
        }
        if item.kind not in ("gap",):
            path_out = self.station.now_playing_file
            path_out.parent.mkdir(parents=True, exist_ok=True)
            tmp = path_out.with_name(f".{path_out.name}.tmp")
            tmp.write_text(json.dumps(now_playing))
            tmp.replace(path_out)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.air_log.open("a") as log:
            log.write(json.dumps({**now_playing, "path": path, "entry": entry_id}) + "\n")
        catalog = self.catalog()
        if catalog is not None and item.kind in ("work", "filler", "safe", "buffer", "external"):
            if item.kind != "buffer" or item.license not in ("", "generated"):
                catalog.record_airplay(path, aired_at=now, station_id=self.station.id,
                                       show_id=item.show_id or None, role=item.role or item.kind)
        return now_playing


def make_server(director: Director, port: int, host: str = "127.0.0.1", alerter=None):
    """The localhost service Liquidsoap calls. One request at a time, so state
    updates never race. Port 0 picks a free port (server.server_address)."""
    import http.server
    import urllib.parse

    if alerter is None:
        from alerts import Alerter

        alerter = Alerter(director.state_dir, call_sign=director.station.call_sign)

    class Handler(http.server.BaseHTTPRequestHandler):
        def _reply(self, status: int, body: str, kind: str = "text/plain") -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                if url.path == "/next":
                    return self._reply(200, director.next_item().annotate())
                if url.path == "/aired":
                    info = director.aired(query.get("entry", ""), query.get("path", ""))
                    return self._reply(200, json.dumps(info), "application/json")
                if url.path == "/alert":
                    key, message = query.get("key", "playout"), query.get("message", "")
                    if query.get("action") == "clear":
                        outcome = alerter.clear(key, message)
                    else:
                        outcome = alerter.send(key, message)
                    return self._reply(200, outcome)
                if url.path == "/health":
                    return self._reply(200, "ok")
                return self._reply(404, "not found")
            except Exception as exc:  # Liquidsoap falls back to the safe library
                print(f"director error on {url.path}: {exc!r}", file=sys.stderr, flush=True)
                return self._reply(503, f"error: {exc}")

        def log_message(self, *args):
            pass

    return http.server.HTTPServer((host, port), Handler)


def serve(director: Director, port: int, host: str = "127.0.0.1") -> None:
    server = make_server(director, port, host)
    print(f"director for {director.station.id} on http://{host}:{port}", flush=True)
    server.serve_forever()


def main(argv: Sequence[str] | None = None) -> int:
    from station_config import load_station_config

    parser = argparse.ArgumentParser(description="WIT-FM playout director")
    parser.add_argument("--station", default=os.environ.get("WRIT_STATION_ID", "wit-next"))
    parser.add_argument("--db", type=Path, help="catalog database (default: the station's)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve", help="run the localhost service Liquidsoap calls")
    p_serve.add_argument("--port", type=int, help="default: the station's API port + 100")
    sub.add_parser("next", help="print the next annotated request for Liquidsoap")
    p_aired = sub.add_parser("aired", help="record that an item started playing")
    p_aired.add_argument("--entry", default="")
    p_aired.add_argument("--path", required=True)
    p_status = sub.add_parser("status", help="show the director's state")
    p_status.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    director = Director(load_station_config(args.station), catalog_path=args.db)
    if args.cmd == "serve":
        serve(director, args.port or director.station.director_port)
        return 0
    if args.cmd == "next":
        try:
            item = director.next_item()
        except Exception as exc:  # Liquidsoap falls back to the safe library
            print(f"director error: {exc}", file=sys.stderr)
            return 1
        print(item.annotate())
        return 0
    if args.cmd == "aired":
        info = director.aired(args.entry, args.path)
        print(json.dumps(info))
        return 0
    if args.cmd == "status":
        state = json.loads(director.state_path.read_text()) if director.state_path.exists() else {}
        if args.json:
            print(json.dumps(state, indent=1))
        else:
            print(f"cursor: {state.get('cursor')}  plan: {state.get('plan_id')} entry {state.get('index')}")
            np = director.station.now_playing_file
            if np.exists():
                print("now playing:", json.loads(np.read_text()).get("track"))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
