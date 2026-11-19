#!/usr/bin/env python3
"""Miles with Logan: tempo-locked sessions, coached by Logan, as one continuous mix.

For each airing the producer picks sessions from the show's menu
(mac/miles_sessions.py), then for each session:

1. Music. Every block needs music at one exact tempo. Tracks come from a
   cache of tempo-locked tracks; new ones come from the local music engine
   (ACE-Step), asked for that BPM, then measured, time-stretched onto the
   target and measured again (mac/tempo_lock.py), within 2 BPM or they're
   thrown away. A cached track at a nearby tempo can be re-stretched. At
   most config/miles.yaml's new_tracks_per_session are made per session
   once the cache can cover the rest.
2. The mix. Tracks start on their first beat; tracks at the same tempo
   crossfade over two bars, on the beat; a tempo change lands on the
   downbeat. Logan's cues sit on top, the music ducked under him, the bell
   10 seconds ahead of every interval change.
3. Files. The mix is cut into gapless files of about five minutes in the
   show's slot folder, so a late join or a restart loses minutes, not the
   session. Liquidsoap plays them back to back with no fades.
4. The gate. Each block is measured again in the finished audio; anything
   off by more than 2 BPM is reported (state_dir/miles/reports/).

Logan's voice: recorded cues from WRIT_MILES_CUES where they exist (see
config/miles_cues.yaml), his cloned voice for the rest, and for the lines
written from facts: the open, the Route of the Day narration and the Miles
Club shout-outs.

    python3 mac/produce_miles.py cues
    python3 mac/produce_miles.py report
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import random  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402
from typing import Any, Sequence  # noqa: E402

import audio_tools as at  # noqa: E402
import miles_sessions as ms  # noqa: E402
import tempo_lock as tl  # noqa: E402
from producers import TALK_LUFS, Airing, Producer, Result, SlotWriter, single  # noqa: E402
from script_writer import Segment  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CUES_PATH = PROJECT_ROOT / "config" / "miles_cues.yaml"
CONFIG_PATH = PROJECT_ROOT / "config" / "miles.yaml"
RATE = 44100
MUSIC_LUFS = -16.0
DUCK_DB = -9.0  # the music under Logan
XFADE_BARS = 2
TAIL_BARS = 4  # a generated track's last bars (its ending) aren't used
GATE = tl.TOLERANCE
LOGAN_NAMES = ["Logan", "WIT-FM", "Miles", "Miles Club", "Route", "Day", "Count", "Nocturne", "We Insure Things"]
OPEN_BRIEF = ("Open the session in Logan's voice, about 25 seconds: say hello, the time from FACTS, and today's "
              "session and its shape from FACTS. If FACTS names the show before this one, nod to it. End with "
              "\"Let's go.\" Don't mention safety; that line follows.")
ROUTE_BRIEFS = {
    "route_start": "Start today's route: where we are, from FACTS, and what's ahead. About 30 seconds, walking pace.",
    "route_middle": "Halfway along today's route: one or two details from FACTS. About 15 seconds.",
    "route_end": "Finishing today's route: wrap it up from FACTS. About 12 seconds.",
}
CLUB_BRIEF = ("Miles Club shout-outs: thank each listener in FACTS by first name and city with their miles, then "
              "give the station's running total from FACTS, and the milestone if FACTS has one. About 30 seconds.")


def _yaml(path: Path) -> dict:
    import yaml

    try:
        return yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        return {}


def cues_dir() -> Path:
    return Path(os.environ.get("WRIT_MILES_CUES") or Path.home() / ".writ" / "voices" / "logan_cues").expanduser()


def _np():
    import numpy as np

    return np


# ── the track cache ───────────────────────────────────────────────────────


@dataclass
class Track:
    path: Path
    bpm: float | None
    first_beat: float
    seconds: float
    caption: str = ""
    measured: float | None = None
    used: list[str] = field(default_factory=list)

    @property
    def meta_path(self) -> Path:
        return self.path.with_suffix(".json")

    def save(self) -> None:
        self.meta_path.write_text(json.dumps({
            "bpm": self.bpm, "first_beat": self.first_beat, "seconds": self.seconds, "caption": self.caption,
            "measured": self.measured, "used": self.used[-30:]}, indent=1))


class TrackCache:
    """Tempo-locked tracks, one folder per target BPM (and one for ambient beds)."""

    def __init__(self, root: Path):
        self.root = root

    def folder(self, bpm: float | None) -> Path:
        return self.root / ("ambient" if bpm is None else f"{bpm:g}")

    def tracks(self, bpm: float | None) -> list[Track]:
        found = []
        for meta in sorted(self.folder(bpm).glob("*.json")):
            audio = meta.with_suffix(".flac")
            if not audio.exists():
                continue
            try:
                data = json.loads(meta.read_text())
            except json.JSONDecodeError:
                continue
            found.append(Track(path=audio, bpm=data.get("bpm"), first_beat=float(data.get("first_beat") or 0),
                               seconds=float(data.get("seconds") or 0), caption=data.get("caption", ""),
                               measured=data.get("measured"), used=list(data.get("used") or [])))
        return found

    def pick(self, bpm: float | None, *, exclude: set[Path], day: date, rest_days: int) -> Track | None:
        """The least recently aired track at this tempo that's rested long enough."""
        horizon = (day - timedelta(days=rest_days)).isoformat()
        options = [t for t in self.tracks(bpm) if t.path not in exclude
                   and (not t.used or max(t.used) <= horizon or rest_days == 0)]
        options.sort(key=lambda t: (max(t.used) if t.used else "", t.path.name))
        return options[0] if options else None

    def nearest(self, bpm: float, *, exclude: set[Path], within: float) -> Track | None:
        """A track cached at another tempo within `within` (a ratio) of this one."""
        best = None
        for folder in self.root.iterdir() if self.root.is_dir() else []:
            try:
                other = float(folder.name)
            except ValueError:
                continue
            if other == bpm or abs(bpm / other - 1) > within:
                continue
            for track in self.tracks(other):
                if track.path in exclude:
                    continue
                score = (abs(bpm / other - 1), max(track.used) if track.used else "")
                if best is None or score < best[0]:
                    best = (score, track)
        return best[1] if best else None

    def add(self, audio: Path, *, bpm: float | None, first_beat: float, caption: str,
            measured: float | None) -> Track:
        folder = self.folder(bpm)
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(f"{caption}{audio.stat().st_size}{datetime.now().isoformat()}".encode()).hexdigest()[:10]
        dest = folder / f"{datetime.now():%Y%m%d}-{digest}.flac"
        hidden = folder / f".{dest.name}.partial.flac"
        at.normalize(audio, hidden, MUSIC_LUFS)
        track = Track(path=dest, bpm=bpm, first_beat=first_beat, seconds=at.duration(hidden), caption=caption,
                      measured=measured)
        hidden.replace(dest)
        track.save()
        return track

    def mark_used(self, track: Track, day: date) -> None:
        if day.isoformat() not in track.used:
            track.used.append(day.isoformat())
            track.save()


# ── placements and the mixer ──────────────────────────────────────────────


@dataclass
class Placement:
    track: Track
    start: float  # session seconds
    offset: float  # seconds into the track
    length: float
    fade_in: float
    fade_out: float
    target: float | None


@dataclass
class Spoken:
    path: Path
    start: float
    seconds: float
    duck: bool = True


def bell(dest: Path) -> Path:
    """A soft two-tone bell, made here (or WRIT_MILES_BELL)."""
    custom = os.environ.get("WRIT_MILES_BELL")
    if custom and Path(custom).exists():
        return Path(custom)
    if dest.exists():
        return dest
    np = _np()
    t = np.arange(int(1.6 * RATE)) / RATE
    tone = sum(g * np.sin(2 * np.pi * f * t) for f, g in ((880, 1.0), (1318.5, 0.35), (1760, 0.4), (2640, 0.2)))
    tone *= np.exp(-3.2 * t) * np.minimum(1.0, t / 0.004)
    tone = (tone / np.abs(tone).max() * 0.4).astype(np.float32)
    _write_flac(dest, np.repeat(tone[:, None], 2, axis=1))
    return dest


def _write_flac(dest: Path, samples) -> Path:
    np = _np()
    dest.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    result = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "-",
                             "-c:a", "flac", str(dest)], input=pcm, capture_output=True, timeout=600)
    if result.returncode != 0:
        raise at.AudioError(f"flac encode failed: {result.stderr.decode(errors='replace')[-200:]}")
    return dest


def _decode_stereo(path: Path):
    np = _np()
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(RATE),
                             "-"], capture_output=True, timeout=600)
    if result.returncode != 0:
        raise at.AudioError(f"can't decode {path}")
    return np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 2)


class Mixer:
    """Renders any stretch of a session from its placements: sample-exact, so consecutive
    stretches join with no gap and no click."""

    def __init__(self, work: Path, music: Sequence[Placement], spoken: Sequence[Spoken]):
        self.work, self.music, self.spoken = work, list(music), list(spoken)
        self._raw: dict[Path, Any] = {}
        self._clips: dict[Path, Any] = {}

    def _track_samples(self, path: Path):
        np = _np()
        if path not in self._raw:
            raw = self.work / f"{hashlib.sha1(str(path).encode()).hexdigest()[:12]}.s16"
            if not raw.exists():
                result = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "2",
                                         "-ar", str(RATE), str(raw)], capture_output=True, timeout=600)
                if result.returncode != 0:
                    raise at.AudioError(f"can't decode {path}")
            self._raw[path] = np.memmap(raw, dtype="<i2", mode="r").reshape(-1, 2)
        return self._raw[path]

    def _clip(self, path: Path):
        if path not in self._clips:
            self._clips[path] = _decode_stereo(path)
        return self._clips[path]

    @staticmethod
    def _ramp(n: int, rising: bool):
        np = _np()
        x = (np.arange(n, dtype=np.float32) + 0.5) / max(1, n)
        return np.sin(0.5 * np.pi * x) if rising else np.cos(0.5 * np.pi * x)

    def render(self, t0: float, t1: float):
        np = _np()
        s0, s1 = round(t0 * RATE), round(t1 * RATE)
        n = s1 - s0
        music = np.zeros((n, 2), dtype=np.float32)
        for p in self.music:
            p0 = round(p.start * RATE)
            pn = round(p.length * RATE)
            lo, hi = max(s0, p0), min(s1, p0 + pn)
            if hi <= lo:
                continue
            source = self._track_samples(p.track.path)
            first = round(p.offset * RATE) + (lo - p0)
            chunk = np.zeros((hi - lo, 2), dtype=np.float32)
            available = source[first:first + (hi - lo)]
            chunk[:len(available)] = available.astype(np.float32) / 32768.0
            fi, fo = round(p.fade_in * RATE), round(p.fade_out * RATE)
            for edge, n_fade, rising in ((p0, fi, True), (p0 + pn - fo, fo, False)):
                a, b = max(lo, edge), min(hi, edge + n_fade)
                if n_fade and b > a:  # only the stretch of the fade inside this window
                    chunk[a - lo:b - lo] *= self._ramp(n_fade, rising)[a - edge:b - edge, None]
            music[lo - s0:hi - s0] += chunk

        voice = np.zeros((n, 2), dtype=np.float32)
        weight = np.zeros(n, dtype=np.float32)  # how far the music is ducked, 0-1
        for clip in self.spoken:
            c0 = round(clip.start * RATE)
            samples = self._clip(clip.path)
            lo, hi = max(s0, c0), min(s1, c0 + len(samples))
            if hi > lo:
                voice[lo - s0:hi - s0] += samples[lo - c0:hi - c0]
            if not clip.duck:
                continue
            a, b = clip.start - 0.02, clip.start + clip.seconds + 0.1
            attack, release = 0.12, 0.45
            for edge0, edge1, rising in ((a - attack, a, True), (b, b + release, False)):
                e0, e1 = round(edge0 * RATE), round(edge1 * RATE)
                lo, hi = max(s0, e0), min(s1, e1)
                if hi > lo:
                    x = (np.arange(lo, hi) - e0) / max(1, e1 - e0)
                    w = (x if rising else 1 - x).astype(np.float32)
                    weight[lo - s0:hi - s0] = np.maximum(weight[lo - s0:hi - s0], w)
            lo, hi = max(s0, round(a * RATE)), min(s1, round(b * RATE))
            if hi > lo:
                weight[lo - s0:hi - s0] = 1.0
        duck = 1.0 - (1.0 - 10 ** (DUCK_DB / 20)) * weight
        mix = music * duck[:, None] + voice
        knee = 0.89  # soft-clip only what would clip
        over = np.abs(mix) > knee
        if over.any():
            mix[over] = np.sign(mix[over]) * (knee + (1 - knee) * np.tanh((np.abs(mix[over]) - knee) / (1 - knee)))
        return mix


# ── the producer ──────────────────────────────────────────────────────────


class SessionError(RuntimeError):
    pass


@dataclass
class BlockReport:
    label: str
    target: float | None
    tracks: list[str]
    measured: bool = False  # long enough to measure in the finished audio
    held: float | None = None  # what it measured; None if no steady beat was found

    def ok(self) -> bool:
        if not self.measured or self.target is None:
            return True
        return self.held is not None and abs(self.held - self.target) <= GATE


class MilesProducer(Producer):
    program = "miles"
    lead_hours = 20.0  # render overnight: music generation is the slow part

    def __init__(self, ctx):
        super().__init__(ctx)
        self.config = _yaml(Path(os.environ.get("WRIT_MILES_CONFIG") or CONFIG_PATH))
        self.cue_text: dict[str, str] = dict((_yaml(CUES_PATH).get("cues") or {}))
        self.cache = TrackCache(ctx.state_dir / "miles" / "tracks")
        self.voice_cache = ctx.state_dir / "miles" / "voice"

    # ── what to make ──

    def sessions_for(self, airing: Airing) -> list[ms.Session]:
        day = airing.start.date().toordinal()
        menu = list(airing.params.get("sessions") or ["walk_it_out"])
        sessions = []
        for k, (sid, minutes) in enumerate(ms.fill(menu, airing.seconds / 60, day)):
            rng = random.Random(f"{airing.show_id}{airing.slot}{k}")
            route = self.route_for(day) if sid == "route_of_the_day" else None
            sessions.append(ms.build(sid, rng=rng, minutes=minutes, route=route))
        return sessions

    def route_for(self, day: int) -> dict | None:
        routes = self.config.get("routes") or {}
        if not routes:
            return None
        route_id = sorted(routes)[day % len(routes)]
        return {"id": route_id, **routes[route_id]}

    @staticmethod
    def names(k: int, session: ms.Session, parts: int) -> list[str]:
        return [f"s{k + 1}_{session.id}_{part:02d}" for part in range(parts)]

    def produce(self, airing: Airing, now: datetime | None = None) -> Result:
        root = self.root_for(True)
        writer = SlotWriter(root, airing.show_id, airing.slot)
        result = Result(airing.show_id, airing.slot, "done", folder=writer.folder)
        sessions = self.sessions_for(airing)
        order = 0
        for k, session in enumerate(sessions):
            bounds = ms.chunks(session)
            names = self.names(k, session, len(bounds))
            if all(writer.has(name) for name in names):
                order += len(bounds)
                continue
            if result.segments:  # one session per call: the daemon comes back for the next
                result.status = "partial"
                break
            try:
                report = self.render_session(session, airing, k, len(sessions), writer, order, names, bounds)
            except (SessionError, at.AudioError, tl.TempoError) as exc:
                result.status = "failed"
                result.problems.append(f"{session.id}: {exc}")
                break
            result.segments += names
            result.problems += report["problems"]
            order += len(bounds)
        if os.environ.get("WRIT_REHEARSE") == "1" and result.status == "done":
            result.status = "rehearsed"
        return result

    # ── music ──

    def music_settings(self) -> dict:
        music = self.config.get("music") or {}
        return {"budget": int(music.get("new_tracks_per_session", 4)), "seconds": float(music.get("track_seconds", 210)),
                "rest_days": int(music.get("rest_days", 3))}

    def caption(self, bpm: float | None, route: dict | None) -> str:
        zone, genres = ms.zone_for(bpm)
        if bpm is None:
            return "soft ambient pads, slow swells, gentle and calm, no drums, instrumental only, no vocals"
        style = route.get("music") if route and route.get("music") else genres
        return (f"{style}, instrumental workout track, {bpm:g} BPM, steady tempo, strong clear beat, "
                f"16-bar intro and 16-bar outro, instrumental only, no vocals")

    def generate(self, bpm: float | None, route: dict | None, work: Path, problems: list[str]) -> Track | None:
        caption = self.caption(bpm, route)
        seconds = self.music_settings()["seconds"]
        for attempt in range(2):
            raw = work / f"gen-{bpm}-{attempt}-{random.random():.6f}.flac"
            if not self.make_music(caption, raw, seconds):
                problems.append(f"the music engine didn't return a track at {bpm} BPM")
                return None
            if bpm is None:
                return self.cache.add(raw, bpm=None, first_beat=0.0, caption=caption, measured=None)
            try:
                locked = tl.lock(raw, work / f"locked-{attempt}.wav", bpm)
            except tl.TempoError as exc:
                problems.append(f"discarded a new track: {exc}")
                continue
            return self.cache.add(locked.path, bpm=bpm, first_beat=locked.first_beat, caption=caption,
                                  measured=locked.measured)
        return None

    def restretch(self, track: Track, bpm: float, work: Path, problems: list[str]) -> Track | None:
        try:
            locked = tl.lock(track.path, work / f"restretch-{bpm:g}-{track.path.stem}.wav", bpm, max_stretch=0.13)
        except tl.TempoError as exc:
            problems.append(f"couldn't re-stretch {track.path.name}: {exc}")
            return None
        return self.cache.add(locked.path, bpm=bpm, first_beat=locked.first_beat,
                              caption=f"{track.caption} (re-stretched to {bpm:g} BPM)", measured=locked.measured)

    def acquire(self, bpm: float | None, route: dict | None, state: dict, work: Path) -> Track:
        """One track for this tempo: a rested cached one, a new one (budget allowing), a re-stretched
        one from a nearby tempo, a new one anyway, then anything cached at this tempo."""
        settings = self.music_settings()
        day: date = state["day"]
        used: set[Path] = state["used"]
        problems: list[str] = state["problems"]
        track = self.cache.pick(bpm, exclude=used, day=day, rest_days=settings["rest_days"])
        if track is None and state["new"] < settings["budget"]:
            track = self.generate(bpm, route, work, problems)
            state["new"] += track is not None
        if track is None and bpm is not None:
            near = self.cache.nearest(bpm, exclude=used, within=0.06)
            track = self.restretch(near, bpm, work, problems) if near else None
        if track is None:
            track = self.cache.pick(bpm, exclude=used, day=day, rest_days=0)
        if track is None and state["new"] >= settings["budget"]:
            track = self.generate(bpm, route, work, problems)
            state["new"] += track is not None
        if track is None:  # last resort: a track already in this session, taking turns
            previous = [p.track for p in state["placed"] if p.track.bpm == bpm]
            distinct = list({t.path: t for t in previous}.values())
            track = distinct[len(previous) % len(distinct)] if distinct else None
        if track is None:
            raise SessionError(f"no music at {bpm} BPM: the engine is down and nothing is cached near it")
        used.add(track.path)
        return track

    def place_music(self, session: ms.Session, state: dict, work: Path) -> list[Placement]:
        """Tracks for every block: same-tempo blocks share one run of crossfaded tracks."""
        placements: list[Placement] = state["placed"]
        spans: list[list[ms.Block]] = []
        for block in session.blocks:
            if spans and spans[-1][-1].bpm == block.bpm:
                spans[-1].append(block)
            else:
                spans.append([block])
        for i, span in enumerate(spans):
            bpm = span[0].bpm
            start, end = span[0].start, span[-1].end
            bar = 4 * 60.0 / bpm if bpm else 4.0
            xfade = XFADE_BARS * bar if bpm else 6.0
            t = start
            first = True
            while end - t > 0.05:
                track = self.acquire(bpm, session.route, state, work)
                usable = track.seconds - track.first_beat - (TAIL_BARS * bar if bpm else 4.0)
                if bpm:
                    usable = math.floor(usable / bar) * bar
                if usable < max(xfade * 2, 8.0):
                    raise SessionError(f"{track.path.name} is too short to use")
                last = usable >= end - t - 1e-6
                length = (end - t) if last else usable
                fade_in = (0.02 if bpm else 3.0) if first else xfade
                fade_out = (0.25 if i + 1 < len(spans) else 4.0) if last else xfade
                if not bpm and last:
                    fade_out = min(6.0, length / 3)
                placements.append(Placement(track=track, start=t, offset=track.first_beat, length=length,
                                            fade_in=fade_in, fade_out=fade_out, target=bpm))
                first = False
                t = t + length - (0 if last else xfade)
                if last:
                    break
        return placements

    # ── Logan ──

    def cue_clip(self, cue: str, problems: list[str]) -> Path | None:
        """A recorded cue if there is one, else the line in Logan's cloned voice (cached)."""
        text = self.cue_text.get(cue)
        for ext in (".wav", ".flac", ".mp3"):
            recorded = cues_dir() / f"{cue}{ext}"
            if recorded.exists():
                return self._normalized(recorded)
        if text is None:
            problems.append(f"no cue called {cue}")
            return None
        key = hashlib.sha1(f"{cue}|{text}".encode()).hexdigest()[:10]
        out = self.voice_cache / "cloned" / f"{cue}-{key}.wav"
        if out.exists():
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.ctx.renderer.render(single("logan", text, f"miles:{cue}"), out, target_lufs=TALK_LUFS)
        except Exception as exc:  # a voice engine failing drops the cue, not the session
            problems.append(f"cue {cue}: {exc}")
            return None
        return out

    def _normalized(self, recorded: Path) -> Path:
        stamp = f"{recorded.stat().st_mtime_ns}{recorded.stat().st_size}"
        out = self.voice_cache / "recorded" / f"{recorded.stem}-{hashlib.sha1(stamp.encode()).hexdigest()[:10]}.wav"
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            at.normalize(recorded, out, TALK_LUFS)
        return out

    def club(self) -> dict:
        try:
            data = json.loads((self.ctx.buffer_dir / "plans" / "_miles_club.json").read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def club_facts(self) -> list[str]:
        data = self.club()
        limit = int((self.config.get("miles_club") or {}).get("shout_outs", 6))
        lines = []
        for entry in [e for e in data.get("this_week") or [] if e.get("name") and e.get("miles")][:limit]:
            place = f" in {entry['city']}" if entry.get("city") else ""
            lines.append(f"Miles Club: {entry['name']}{place}, {float(entry['miles']):g} miles this week.")
        if data.get("total_miles"):
            lines.append(f"WIT-FM's Miles Club total: {data['total_miles']:,.1f} miles.".replace(".0 miles", " miles"))
        if data.get("milestone"):
            lines.append(f"Milestone: WIT-FM has now walked to {data['milestone']}.")
        return lines

    def write_line(self, name: str, session: ms.Session, airing: Airing, k: int, work: Path,
                   problems: list[str]) -> Path | None:
        """A line written from facts and voiced in Logan's cloned voice (or recorded, for routes)."""
        route = session.route or {}
        if name.startswith("route_") and route.get("id"):
            recorded = cues_dir() / "routes" / f"{route['id']}{'' if name == 'route_start' else '_' + name[6:]}.wav"
            if recorded.exists():
                return self._normalized(recorded)
        if name == "open":
            weekly = cues_dir() / "weekly" / f"{airing.start:%G}-W{airing.start:%V}.wav"
            if weekly.exists():
                return self._normalized(weekly)
        facts = [f"Show: {airing.title}."]
        if name == "open":
            when = airing.start
            facts.append(f"Time: {when:%-I:%M %p}, {when:%A %B} {when.day}.")
            facts.append(f"Today's session: {session.title}, {round(session.seconds / 60)} minutes: {ms.describe(session)}.")
            if session.blocks and session.blocks[0].bpm:
                facts.append(f"The warm-up music is at {session.blocks[0].bpm:g} beats a minute.")
            before = self._show_before(airing)
            if before:
                facts.append(f"The show before this one: {before}.")
            seconds, brief = 25.0, OPEN_BRIEF
        elif name.startswith("route_"):
            facts.append(f"Today's route: {route.get('name')}, in {route.get('place')}.")
            facts += [f"Route fact: {f}" for f in route.get("facts") or []]
            seconds, brief = {"route_start": 30.0, "route_middle": 15.0, "route_end": 12.0}[name], ROUTE_BRIEFS[name]
        elif name == "club":
            facts += self.club_facts()
            seconds, brief = 30.0, CLUB_BRIEF
        else:
            return None
        segment = Segment(id=f"miles/{airing.show_id}/{airing.slot}/{k}/{name}", brief=brief, speakers=["logan"],
                          seconds=seconds, facts="\n".join(facts), allow_names=LOGAN_NAMES)
        out = work / f"line-{name}.wav"
        _, path, line_problems = self.talk(segment, out, template=lambda: single(
            "logan", self.template(name, session, airing, route), segment.id))
        problems += line_problems
        return path

    def template(self, name: str, session: ms.Session, airing: Airing, route: dict) -> str:
        if name == "open":
            hour = airing.start.hour
            hello = "Morning" if hour < 12 else "Afternoon" if hour < 17 else "Evening"
            return (f"{hello}, it's Logan. Today is {session.title}: {round(session.seconds / 60)} minutes. "
                    f"Nobody's timing you. Let's go.")
        if name == "route_start":
            return f"Today's route: {route.get('name')}, in {route.get('place')}. Let's walk it."
        if name == "route_middle":
            return f"Still on {route.get('name')}. Keep looking around."
        if name == "route_end":
            return f"That's {route.get('name')}. Nice walk."
        if name == "club":
            data = self.club()
            names = "; ".join(f"{e['name']}" + (f" in {e['city']}" if e.get("city") else "")
                              + f", {float(e['miles']):g} miles"
                              for e in [e for e in data.get("this_week") or [] if e.get("name") and e.get("miles")][:6])
            total = data.get("total_miles")
            text = "Miles Club. " + (f"Shout-outs to {names}. " if names else "")
            return text + (f"WIT-FM has now walked {total:,.0f} miles." if total else "Log your miles and join us.")
        return ""

    def _show_before(self, airing: Airing) -> str:
        try:
            return self.ctx.schedule.resolve(airing.start - timedelta(minutes=1)).title
        except Exception:
            return ""

    # ── one session ──

    def render_session(self, session: ms.Session, airing: Airing, k: int, count: int, writer: SlotWriter,
                       order: int, names: list[str], bounds: list[tuple[float, float]]) -> dict:
        problems: list[str] = []
        day = airing.start.date()
        self.ctx.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="miles-", dir=self.ctx.work_dir) as tmp:
            work = Path(tmp)
            state = {"day": day, "used": set(), "problems": problems, "new": 0, "placed": []}
            music = self.place_music(session, state, work)

            events = list(session.events)
            lines: dict[str, Path] = {}
            opening = "line:open" if k == 0 else f"{_intro_cue(session.id)}"
            events.append(ms.Event(at=0.0, cue=opening, role="open", slack=0.0))
            events.append(ms.Event(at=0.0, cue="open_safety", role="safety", slack=180.0))
            if session.id == "route_of_the_day":
                events.append(ms.Event(at=40.0, cue="line:route_start", role="route", slack=120.0))
            if k == count - 1:  # the Miles Club, in the last session's cool-down
                club_at = max(0.0, session.seconds - 4.5 * 60)
                events.append(ms.Event(at=club_at, cue="line:club", role="club", slack=90.0))
                events.append(ms.Event(at=club_at, cue="club_invite", role="club", slack=150.0))
            events.append(ms.Event(at=session.seconds - 30, cue="next_session" if k + 1 < count else "sign_off",
                                   role="close", slack=25.0))

            clips: dict[str, Path] = {}
            for cue in sorted({e.cue for e in events}):
                if cue == "bell":
                    clips[cue] = bell(self.voice_cache / "bell.flac")
                elif cue.startswith("line:"):
                    if cue == "line:club" and not self.club_facts():
                        continue
                    path = self.write_line(cue[5:], session, airing, k, work, problems)
                    if path is not None:
                        lines[cue] = path
                        clips[cue] = path
                else:
                    path = self.cue_clip(cue, problems)
                    if path is not None:
                        clips[cue] = path
            durations = {cue: at.duration(path) for cue, path in clips.items()}
            placed = ms.place(events, durations, session.seconds)
            spoken = [Spoken(path=clips[e.cue], start=t, seconds=durations[e.cue], duck=e.cue != "bell")
                      for t, e in placed]

            mixer = Mixer(work, music, spoken)
            parts = []
            for i, (t0, t1) in enumerate(bounds):
                out = work / f"part{i:02d}.flac"
                _write_flac(out, mixer.render(t0, t1))
                parts.append(out)

            blocks = self.check(session, music, parts, bounds)
            for block in blocks:
                if not block.ok():
                    held = f"held {block.held:.2f} BPM" if block.held is not None else "no steady beat"
                    problems.append(f"{session.id} {block.label}: {held}, wanted {block.target:g}")
            for track in {p.track.path: p.track for p in music}.values():
                self.cache.mark_used(track, day)
            for i, (part, name, (t0, t1)) in enumerate(zip(parts, names, bounds)):
                tempos = sorted({b.bpm for b in session.blocks if b.bpm and b.start < t1 and b.end > t0})
                writer.add(order + i, name, part, {
                    "title": f"Miles with Logan: {session.title}", "artist": "Logan",
                    "credit": "Music made for WIT-FM", "license": "generated", "kind": "music",
                    "session": session.id, "part": i + 1, "of": len(parts), "bpm": tempos})
            report = {"show_id": airing.show_id, "slot": airing.slot, "session": session.id, "index": k,
                      "seconds": session.seconds, "new_tracks": state["new"],
                      "blocks": [vars(b) for b in blocks], "cues": [[round(t, 2), e.cue] for t, e in placed],
                      "problems": problems,
                      "made": datetime.now().isoformat(timespec="seconds")}
            reports = self.ctx.state_dir / "miles" / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            (reports / f"{airing.show_id}_{airing.slot}_{k + 1}_{session.id}.json").write_text(json.dumps(report, indent=1))
            return report

    def check(self, session: ms.Session, music: list[Placement], parts: list[Path],
              bounds: list[tuple[float, float]]) -> list[BlockReport]:
        """Measure each block's tempo in the finished files: the Phase 4 gate."""
        reports = []
        for block in session.blocks:
            tracks = sorted({p.track.path.name for p in music if p.start < block.end and p.start + p.length > block.start})
            report = BlockReport(label=block.label, target=block.bpm, tracks=tracks)
            if block.bpm:
                for part, (t0, t1) in zip(parts, bounds):
                    if t0 <= block.start + 1 < t1:
                        start = block.start - t0 + 1.0
                        span = min(block.end, t1) - block.start - 2.0
                        if span * block.bpm / 60 >= 16:  # enough beats to measure
                            found = tl.measure(part, block.bpm, seconds=min(span, 90.0), offset=start)
                            report.measured = True
                            report.held = found.bpm if found else None
                        break
            reports.append(report)
        return reports


def _intro_cue(session_id: str) -> str:
    return {"walk_it_out": "walk_it_out_intro", "intervals": "intervals_intro", "pyramid": "pyramid_intro",
            "route_of_the_day": "route_intro", "long_slow_saturday": "long_slow_intro",
            "recovery_sunday": "recovery_intro", "easy_miles": "easy_miles_intro"}.get(session_id, "open_morning")


# ── command line ──────────────────────────────────────────────────────────


def cue_status() -> list[tuple[str, str, str]]:
    cues = (_yaml(CUES_PATH).get("cues") or {})
    rows = []
    for cue, text in cues.items():
        recorded = any((cues_dir() / f"{cue}{ext}").exists() for ext in (".wav", ".flac", ".mp3"))
        rows.append((cue, "recorded" if recorded else "cloned", text))
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Miles with Logan")
    parser.add_argument("--station", default=os.environ.get("WRIT_STATION_ID", "wit-next"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cues", help="Logan's cue library: what's recorded and what's still cloned")
    p_report = sub.add_parser("report", help="the tempo gate for recent sessions")
    p_report.add_argument("--last", type=int, default=10)
    args = parser.parse_args(argv)

    if args.cmd == "cues":
        rows = cue_status()
        for cue, status, text in rows:
            print(f"{status:9s} {cue:22s} {text}")
        done = sum(1 for _, status, _ in rows if status == "recorded")
        print(f"\n{done} of {len(rows)} recorded in {cues_dir()}")
        return 0

    from station_config import load_station_config

    station = load_station_config(args.station)
    folder = station.runtime_dir / "producers" / "miles" / "reports"
    reports = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)[-args.last:] if folder.is_dir() else []
    if not reports:
        print("no Miles sessions made yet")
        return 0
    failures = 0
    for path in reports:
        data = json.loads(path.read_text())
        held = [b for b in data["blocks"] if b.get("target") and b.get("held") is not None]
        worst = max((abs(b["held"] - b["target"]) for b in held), default=0.0)
        failures += worst > GATE
        print(f"{data['slot']} {data['session']:20s} {len(held)} blocks measured, worst {worst:.2f} BPM off "
              f"{'FAIL' if worst > GATE else 'ok'}, {data['new_tracks']} new tracks")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
