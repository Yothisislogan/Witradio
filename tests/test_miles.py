"""Miles with Logan: tempo lock, session timelines, the mixer, the producer and the Miles Club."""

import http.server
import json
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))  # flat, the way the modules import each other
sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_tools as at  # noqa: E402
import miles_club  # noqa: E402
import miles_sessions as ms  # noqa: E402
import tempo_lock as tl  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from produce_miles import Mixer, MilesProducer, Placement, SessionError, Spoken, Track  # noqa: E402
from producers import Airing, Context, SlotWriter  # noqa: E402
from schedule import load_schedule  # noqa: E402
from script_writer import ScriptWriter  # noqa: E402
from test_producers import FakeLLM, voices_config  # noqa: E402
from voices import Renderer  # noqa: E402

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

HAVE_TOOLS = shutil.which("ffmpeg") is not None and np is not None
SCHEDULE = PROJECT_ROOT / "config" / "wit_next_schedule.yaml"


def drums(path: Path, bpm: float, seconds: float, *, seed: int = 0, pattern: str = "four", lead_in: float = 0.2,
          sixteenths: bool = False, rate: int = 44100) -> Path:
    """A drum loop at an exact tempo: kick, snare, hats, and a quiet pad."""
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    out = np.zeros(n, dtype=np.float32)
    beat = 60.0 / bpm
    t = np.arange(int(0.25 * rate)) / rate
    kick = (np.sin(2 * np.pi * (50 + 70 * np.exp(-t * 30)) * t) * np.exp(-t * 12)).astype(np.float32)
    snare = (rng.standard_normal(len(t)) * np.exp(-t * 25) * 0.5).astype(np.float32)
    hat = (np.diff(rng.standard_normal(len(t) + 1)) * np.exp(-t * 80) * 0.25).astype(np.float32)
    division = 4 if sixteenths else 2
    k = 0
    while lead_in + k * beat / division < seconds:
        i = max(0, int((lead_in + k * beat / division + rng.normal(0, 0.004)) * rate))

        def put(sound, gain=1.0):
            j = min(n, i + len(sound))
            out[i:j] += gain * sound[:j - i]

        put(hat, 0.6)
        if k % division == 0:
            beat_no = (k // division) % 4
            if pattern == "four" or beat_no in (0, 2):
                put(kick)
            if beat_no in (1, 3):
                put(snare)
        k += 1
    out += 0.05 * np.sin(2 * np.pi * 220 * np.arange(n) / rate).astype(np.float32)
    out /= np.abs(out).max() / 0.8
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.repeat((out * 32767).astype(np.int16)[:, None], 2, axis=1).tobytes())
    return path


@unittest.skipUnless(HAVE_TOOLS, "ffmpeg and numpy are required")
class TempoLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_measures_running_tempos_to_a_fraction_of_a_bpm(self):
        for bpm, target, pattern in ((157.3, 160, "four"), (121.0, 120, "rock"), (172.4, 170, "rock"),
                                     (104.2, 105, "four"), (84.5, 170, "rock")):  # the last: half-time
            found = tl.measure(drums(self.root / f"{bpm}.wav", bpm, 60, seed=int(bpm), pattern=pattern), target)
            self.assertIsNotNone(found, bpm)
            self.assertAlmostEqual(found.bpm, tl.octave_near(bpm, target), delta=0.1)
            self.assertAlmostEqual(found.first_beat, 0.2, delta=0.04)

    def test_no_false_beat_from_hi_hats(self):
        # 140 BPM with 16th-note hats: every third 16th makes a tidy grid at 186.7, but the kick says 140.
        self.assertIsNone(tl.measure(drums(self.root / "a.wav", 140, 60, sixteenths=True, pattern="rock"), 170))
        self.assertIsNone(tl.measure(drums(self.root / "b.wav", 100, 60, pattern="rock"), 120))  # nothing near 120

    def test_lock_stretches_onto_the_target(self):
        locked = tl.lock(drums(self.root / "off.wav", 155, 60, seed=3), self.root / "locked.wav", 160)
        self.assertAlmostEqual(locked.measured, 155, delta=0.1)
        self.assertLessEqual(locked.error, 0.1)
        self.assertAlmostEqual(locked.factor, 160 / 155, delta=0.002)
        self.assertAlmostEqual(tl.measure(locked.path, 160).bpm, 160, delta=0.1)
        with self.assertRaises(tl.TempoError):  # 140 is too far from 160 to stretch
            tl.lock(drums(self.root / "far.wav", 140, 60), self.root / "x.wav", 160)


class SessionTests(unittest.TestCase):
    def test_the_menu_matches_the_bible(self):
        for sid, minutes in ms.LENGTHS.items():
            session = ms.build(sid, rng=random.Random(1))
            self.assertAlmostEqual(sum(b.seconds for b in session.blocks), minutes * 60, delta=0.5)
        intervals = ms.build("intervals")
        self.assertEqual([b.bpm for b in intervals.blocks if b.label == "jog"], [160] * 7)
        self.assertEqual([b.seconds for b in intervals.blocks if b.label == "jog"], [120] * 7)
        self.assertEqual([b.bpm for b in intervals.blocks if b.label == "walk"], [120] * 7)
        self.assertEqual((intervals.blocks[0].seconds, intervals.blocks[-1].seconds), (300, 240))
        pyramid = ms.build("pyramid")
        climb = [b for b in pyramid.blocks if b.label == "climb"]
        self.assertEqual((climb[0].bpm, climb[-1].bpm, sum(b.seconds for b in climb)), (115, 170, 1200))
        self.assertEqual([(b.bpm, b.seconds) for b in pyramid.blocks if b.label == "hold"], [(170, 300)])
        self.assertEqual(sum(b.seconds for b in pyramid.blocks if b.label == "descend"), 900)
        walk = ms.build("walk_it_out", rng=random.Random(2))
        self.assertTrue(all(115 <= b.bpm <= 125 for b in walk.blocks if b.label in ("walk", "warm-up")))
        self.assertEqual([(b.bpm, b.seconds) for b in walk.blocks if b.label == "push"], [(130, 300)])
        strength = [b for b in walk.blocks if b.label == "strength"]
        self.assertEqual([(b.bpm, b.seconds) for b in strength], [(120.0, 60)])
        counts = [e for e in walk.events if e.cue.startswith("count_")]
        self.assertEqual(counts[1].at - counts[0].at, 2.0)  # one rep every two seconds at 120 BPM
        long_slow = ms.build("long_slow_saturday")
        self.assertTrue(all(150 <= b.bpm <= 160 for b in long_slow.blocks))
        self.assertFalse([e for e in long_slow.events if e.role in ("encourage", "bell")])  # minimal talk
        self.assertEqual(sorted(e.at for e in long_slow.events if e.role == "time"), [900, 1800, 3600, 4500])
        recovery = ms.build("recovery_sunday")
        self.assertEqual([(b.bpm, b.seconds) for b in recovery.blocks], [(110, 1200), (None, 600)])

    def test_bells_and_countdowns_land_on_the_changes(self):
        session = ms.build("intervals")
        changes = [b.start for b in session.blocks if b.interval]
        bells = sorted(e.at for e in session.events if e.cue == "bell")
        self.assertEqual(bells, [c - 10 for c in changes])
        jogs = [b.start for b in session.blocks if b.label == "jog"]
        countdowns = [e for e in session.events if e.cue == "countdown"]
        self.assertEqual([e.at for e in countdowns], jogs)  # the jogs speed up; the walks don't
        self.assertTrue(all(e.align == "end" for e in countdowns))
        halfway = [e.at for e in session.events if e.cue == "halfway"]
        self.assertEqual(halfway, [900])
        self.assertNotIn("time_15", [e.cue for e in session.events])  # halfway covers it

    def test_placement_never_talks_over_itself(self):
        session = ms.build("intervals", rng=random.Random(4))
        durations = {e.cue: 2.5 for e in session.events}
        durations.update(bell=1.6, countdown=3.2)
        placed = ms.place(session.events, durations, session.seconds)
        spans = sorted((t, t + durations[e.cue]) for t, e in placed)
        self.assertTrue(all(b0 >= a1 + ms.CUE_GAP - 1e-9 for (_, a1), (b0, _) in zip(spans, spans[1:])))
        cues = {e.cue for _, e in placed}
        self.assertIn("countdown", cues)
        self.assertEqual(sum(1 for _, e in placed if e.cue == "bell"), 15)  # every change keeps its bell
        for t, e in placed:
            if e.cue == "countdown":
                self.assertAlmostEqual(t + 3.2, e.at)  # finishes right on the change

    def test_an_airing_is_filled_and_the_lead_session_rotates(self):
        dawn = ["walk_it_out", "intervals", "pyramid", "route_of_the_day"]
        leads = set()
        for day in range(8):
            plan = ms.fill(dawn, 120, day)
            self.assertAlmostEqual(sum(m for _, m in plan), 120)
            leads.add(plan[0][0])
        self.assertEqual(leads, set(dawn))
        self.assertEqual(ms.fill(["walk_it_out", "route_of_the_day"], 60, 0), [("walk_it_out", 45.0), ("easy_miles", 15.0)])
        self.assertEqual(ms.fill(["long_slow_saturday"], 90, 3), [("long_slow_saturday", 90.0)])

    def test_chunks_are_contiguous_and_short(self):
        for sid in ms.LENGTHS:
            session = ms.build(sid)
            bounds = ms.chunks(session)
            self.assertEqual(bounds[0][0], 0.0)
            self.assertEqual(bounds[-1][1], session.seconds)
            self.assertTrue(all(a[1] == b[0] for a, b in zip(bounds, bounds[1:])))
            self.assertTrue(all(b - a <= 360 for a, b in bounds))


@unittest.skipUnless(HAVE_TOOLS, "ffmpeg and numpy are required")
class MixerTests(unittest.TestCase):
    def test_pieces_join_sample_exact_and_the_music_ducks_under_logan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music = at.tone(root / "music.flac", 20, 440)
            voice = root / "voice.wav"
            at.silence(voice, 2.0)
            track = Track(path=music, bpm=120, first_beat=0.0, seconds=20)
            placements = [Placement(track, 0.0, 0.0, 10.0, 0.02, 1.0, 120),
                          Placement(track, 9.0, 3.0, 8.0, 1.0, 0.25, 120)]
            mixer = Mixer(root, placements, [Spoken(voice, 5.0, 2.0)])
            whole = mixer.render(0, 15)
            parts = np.vstack([mixer.render(0, 4.3), mixer.render(4.3, 9.5), mixer.render(9.5, 15)])
            self.assertEqual(whole.shape, parts.shape)
            self.assertTrue(np.array_equal(whole, parts))

            def rms(a, b):
                return float(np.sqrt(np.mean(whole[int(a * 44100):int(b * 44100)] ** 2)))

            self.assertAlmostEqual(20 * np.log10(rms(5.3, 6.8) / rms(2.0, 3.5)), -9.0, delta=0.3)
            self.assertAlmostEqual(rms(8.0, 8.8), rms(2.0, 3.5), delta=0.01)  # back up after the cue


def tiny_session() -> ms.Session:
    session = ms.Session(id="intervals", title="Intervals", seconds=60)
    b = ms._Builder(session)
    b.block(20 / 60, 105, "warm-up", cue="warmup")
    b.block(20 / 60, 160, "jog", interval=True, cue="jog_go")
    b.block(20 / 60, 120, "walk", interval=True, cue="walk_go")
    ms._add_common_events(session, random.Random(1))
    return session


def fake_engine(calls: list):
    """A music engine that misses the tempo it's asked for by 2.5%, as real ones do."""
    def make(caption, out, seconds):
        calls.append(caption)
        bpm = re.search(r"([\d.]+) BPM", caption)
        raw = Path(out).with_suffix(".src.wav")
        if bpm:
            drums(raw, float(bpm.group(1)) * 1.025, 36, seed=len(calls))
        else:
            at.tone(raw, 36, 196)
        at.to_wav(raw, Path(out))  # converts to the extension asked for; the cache normalizes loudness
        return True
    return make


@unittest.skipUnless(HAVE_TOOLS, "ffmpeg and numpy are required")
class MilesProducerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """One cache of tempo-locked tracks (105, 160 and 120 BPM) for the tests that don't test making them."""
        cls.seed = tempfile.TemporaryDirectory()
        root = Path(cls.seed.name)
        ctx = Context(schedule=load_schedule(SCHEDULE), renderer=None, writer=None, buffer_dir=root / "buffer",
                      rehearsal_dir=root / "rehearsal", work_dir=root / "work", state_dir=root / "state",
                      log=lambda m: None, services={"music": fake_engine([])})
        producer = MilesProducer(ctx)
        producer.config["music"] = {"new_tracks_per_session": 9, "track_seconds": 36, "rest_days": 0}
        (root / "work").mkdir()
        for bpm in (105.0, 160.0, 120.0):
            for _ in range(2):
                assert producer.generate(bpm, None, root / "work", []) is not None
        cls.seed_tracks = root / "state" / "miles" / "tracks"

    @classmethod
    def tearDownClass(cls):
        cls.seed.cleanup()

    def seed_cache(self):
        shutil.copytree(self.seed_tracks, self.ctx.state_dir / "miles" / "tracks")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.llm = FakeLLM()
        threading.Thread(target=self.llm.serve_forever, daemon=True).start()
        self.addCleanup(self.llm.server_close)
        self.addCleanup(self.llm.shutdown)
        renderer = Renderer.from_config(voices_config(self.root), self.root / "none.yaml")
        self.calls = []
        self.ctx = Context(
            schedule=load_schedule(SCHEDULE), renderer=renderer,
            writer=ScriptWriter.for_renderer(LLMClient(base_url=self.llm.base, retries=0), renderer, attempts=2),
            buffer_dir=self.root / "buffer", rehearsal_dir=self.root / "rehearsal", work_dir=self.root / "work",
            state_dir=self.root / "state", log=lambda m: None, services={"music": self.fake_music})
        env = patch.dict(os.environ, {"WRIT_MILES_CUES": str(self.root / "cues")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WRIT_REHEARSE", None)
        self.producer = MilesProducer(self.ctx)
        self.producer.config["music"] = {"new_tracks_per_session": 4, "track_seconds": 36, "rest_days": 3}
        self.airing = Airing(show_id="miles_dawn", start=datetime(2026, 11, 20, 5, 0), end=datetime(2026, 11, 20, 7, 0),
                             program="miles", params={"sessions": ["intervals"]}, host="logan",
                             title="Miles with Logan: Dawn Miles")

    def fake_music(self, caption, out, seconds):
        return fake_engine(self.calls)(caption, out, seconds)

    def render(self, session, k=0, count=1):
        writer = SlotWriter(self.ctx.buffer_dir, self.airing.show_id, self.airing.slot)
        bounds = ms.chunks(session, longest=40)
        names = self.producer.names(k, session, len(bounds))
        report = self.producer.render_session(session, self.airing, k, count, writer, 0, names, bounds)
        return writer, report, bounds

    def test_a_session_holds_its_tempos_end_to_end(self):
        session = tiny_session()
        writer, report, bounds = self.render(session)
        files = sorted(writer.folder.glob("*.flac"))
        self.assertEqual([f.name for f in files], [f"{i:02d}_s1_intervals_{i:02d}.flac" for i in range(len(bounds))])
        self.assertAlmostEqual(sum(at.duration(f) for f in files), session.seconds, delta=0.01)
        held = {b["label"]: b["held"] for b in report["blocks"]}
        for block in session.blocks:
            self.assertAlmostEqual(held[block.label], block.bpm, delta=tl.TOLERANCE)  # the Phase 4 gate
        self.assertEqual(report["problems"], [])
        self.assertTrue(all("instrumental only, no vocals" in c and "BPM" in c for c in self.calls))
        self.assertTrue(any("160 BPM" in c for c in self.calls))

        cues = [c for _, c in report["cues"]]
        self.assertEqual(cues[:2], ["line:open", "open_safety"])  # every open says it
        self.assertIn("countdown", cues)
        self.assertEqual(cues.count("bell"), 2)
        self.assertLess(cues.index("session_done"), cues.index("sign_off"))
        times = dict((c, t) for t, c in report["cues"])
        jog_start = session.blocks[1].start
        self.assertLess(times["countdown"], jog_start)
        meta = json.loads(files[0].with_suffix(".json").read_text())
        self.assertEqual((meta["artist"], meta["license"], meta["kind"]), ("Logan", "generated", "music"))
        self.assertEqual(meta["title"], "Miles with Logan: Intervals")
        saved = json.loads((self.ctx.state_dir / "miles" / "reports" / "miles_dawn_2026-11-20_0500_1_intervals.json").read_text())
        self.assertEqual(saved["session"], "intervals")

        # The next session reuses the cache: no new music.
        made = len(self.calls)
        self.producer.config["music"] = {"new_tracks_per_session": 0, "track_seconds": 36, "rest_days": 0}
        _, again, _ = self.render(tiny_session(), k=1, count=2)
        self.assertEqual(len(self.calls), made)
        self.assertEqual(again["new_tracks"], 0)
        self.assertEqual([c for _, c in again["cues"]][0], "intervals_intro")  # not the first session: no written open

    def test_recorded_cues_win_and_a_nearby_tempo_is_restretched(self):
        cues = self.root / "cues"
        cues.mkdir()
        at.tone(cues / "open_safety.wav", 1.2, 600)  # Logan recorded this one
        clip = self.producer.cue_clip("open_safety", [])
        self.assertIn("recorded", str(clip))
        self.assertAlmostEqual(float(at.measure(clip)["input_i"]), -16.0, delta=1.0)

        # The engine goes down: 163 BPM comes from a 160 BPM track, re-stretched.
        self.seed_cache()
        self.ctx.services["music"] = lambda caption, out, seconds: False
        self.producer.config["music"] = {"new_tracks_per_session": 4, "track_seconds": 36, "rest_days": 3}
        state = {"day": date(2026, 11, 21), "used": set(), "problems": [], "new": 0, "placed": []}
        track = self.producer.acquire(163.0, None, state, self.root)
        self.assertEqual(track.bpm, 163.0)
        self.assertIn("re-stretched", track.caption)
        self.assertAlmostEqual(tl.measure(track.path, 163).bpm, 163, delta=tl.TOLERANCE)

    def test_no_engine_and_no_cache_fails_the_session_cleanly(self):
        self.ctx.services["music"] = lambda caption, out, seconds: False
        with self.assertRaises(SessionError):
            self.render(tiny_session())
        result = self.producer.produce(self.airing, datetime(2026, 11, 19, 20, 0))
        self.assertEqual(result.status, "failed")
        self.assertFalse(list((self.ctx.buffer_dir).rglob("*.flac")))

    def test_produce_makes_one_session_per_call(self):
        sessions = [tiny_session(), tiny_session()]
        sessions[1].id = "walk_it_out"
        self.seed_cache()
        self.producer.config["music"] = {"new_tracks_per_session": 0, "track_seconds": 36, "rest_days": 0}
        with patch.object(MilesProducer, "sessions_for", lambda self, airing: sessions), \
                patch("miles_sessions.chunks", lambda session, longest=300.0: [(0.0, 50.0), (50.0, session.seconds)]):
            first = self.producer.produce(self.airing)
            self.assertEqual((first.status, first.segments), ("partial", ["s1_intervals_00", "s1_intervals_01"]))
            second = self.producer.produce(self.airing)
            self.assertEqual((second.status, second.segments), ("done", ["s2_walk_it_out_00", "s2_walk_it_out_01"]))
            third = self.producer.produce(self.airing)
            self.assertEqual((third.status, third.segments), ("done", []))
        names = sorted(p.name for p in first.folder.glob("*.flac"))
        self.assertEqual(names, ["00_s1_intervals_00.flac", "01_s1_intervals_01.flac",
                                 "02_s2_walk_it_out_00.flac", "03_s2_walk_it_out_01.flac"])

    def test_the_last_session_reads_the_miles_club(self):
        plans = self.ctx.buffer_dir / "plans"
        plans.mkdir(parents=True)
        (plans / "_miles_club.json").write_text(json.dumps({
            "this_week": [{"name": "Dana", "city": "Norfolk", "miles": 12.5}, {"name": "Sam", "city": "", "miles": 3}],
            "total_miles": 1234.0, "milestone": "Marshall, North Carolina"}))
        facts = self.producer.club_facts()
        self.assertEqual(facts, ["Miles Club: Dana in Norfolk, 12.5 miles this week.", "Miles Club: Sam, 3 miles this week.",
                                 "WIT-FM's Miles Club total: 1,234 miles.",
                                 "Milestone: WIT-FM has now walked to Marshall, North Carolina."])
        self.seed_cache()
        self.producer.config["music"] = {"new_tracks_per_session": 0, "track_seconds": 36, "rest_days": 0}
        _, report, _ = self.render(tiny_session())
        cues = [c for _, c in report["cues"]]
        self.assertIn("line:club", cues)
        self.assertIn("club_invite", cues)
        self.assertEqual(self.calls, [])

    def test_sessions_for_a_real_airing(self):
        airing = next(a for a in __import__("producers").upcoming_airings(self.ctx.schedule, datetime(2026, 11, 20, 4, 0), 3)
                      if a.show_id == "miles_dawn")
        sessions = self.producer.sessions_for(airing)
        self.assertAlmostEqual(sum(s.seconds for s in sessions), airing.seconds, delta=1)
        self.assertEqual(sessions, self.producer.sessions_for(airing))  # the same every time
        route = next((s for s in sessions if s.id == "route_of_the_day"), None)
        if route is not None:
            self.assertIn(route.route["id"], ("elizabeth_river_trail", "french_broad_marshall", "las_vegas_strip_sunrise"))


class DaemonOrderTests(unittest.TestCase):
    def test_short_lead_shows_go_before_miles(self):
        from producer_daemon import Daemon
        from producers import Result

        order = []

        def recorder(name):
            def produce(self, airing, now=None):
                order.append((name, airing.show_id))
                return Result(airing.show_id, airing.slot, "done")
            return produce

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = Context(schedule=load_schedule(SCHEDULE), renderer=None, writer=None, buffer_dir=root / "buffer",
                          rehearsal_dir=root / "rehearsal", work_dir=root / "work", state_dir=root / "state",
                          log=lambda m: None)
            daemon = Daemon(ctx)
            patches = [patch.object(type(p), "produce", recorder(name)) for name, p in daemon.producers.items()]
            for patcher in patches:
                patcher.start()
            try:
                daemon.cycle(datetime(2026, 11, 19, 15, 0))  # Thursday: the Closing Tape at 4:30, Miles at 5 AM Friday
            finally:
                for patcher in patches:
                    patcher.stop()
        names = [name for name, _ in order]
        self.assertIn("market", names)
        self.assertIn(("miles", "miles_dawn"), order)
        self.assertLess(names.index("market"), names.index("miles"))
        self.assertLess(names.index("ying_yang"), names.index("monster_mix"))


class MilesClubTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.club = miles_club.MilesClub(self.root / "club.db")
        self.addCleanup(self.club.close)

    def test_entries_are_checked(self):
        ok = {"name": "Dana", "city": "Norfolk", "miles": "3.25", "age_13_plus": True}
        self.assertEqual(miles_club.clean_entry(ok), {"name": "Dana", "city": "Norfolk", "miles": 3.2})
        for bad, message in (({**ok, "age_13_plus": False}, "13"), ({**ok, "age_13_plus": "yes"}, "13"),
                             ({**ok, "name": "Dana Q Public"}, "first name"), ({**ok, "name": "visit www.x.com"}, "first name"),
                             ({**ok, "name": "R2D2"}, "first name"), ({**ok, "miles": 0}, "between"),
                             ({**ok, "miles": 250}, "between"), ({**ok, "miles": "lots"}, "number"),
                             ({**ok, "city": "x" * 41}, "city")):
            with self.assertRaises(miles_club.ClubError) as caught:
                miles_club.clean_entry(bad)
            self.assertIn(message, str(caught.exception))
        self.assertEqual(miles_club.clean_entry({**ok, "name": "  Mary-Jo  ", "city": ""})["name"], "Mary-Jo")

    def test_only_approved_names_reach_air_and_the_total_counts(self):
        monday = datetime(2026, 11, 16, 12, 0)
        ids = [self.club.submit({"name": n, "city": c, "miles": m, "age_13_plus": True}, now=monday)
               for n, c, m in (("Dana", "Norfolk", 3), ("dana", "norfolk", 2.5), ("Sam", "", 10), ("Troll", "", 5))]
        old = self.club.submit({"name": "Ari", "city": "Marshall", "miles": 40, "age_13_plus": True},
                               now=datetime(2026, 11, 2, 9, 0))
        self.assertEqual(len(self.club.pending()), 5)
        self.club.review(ids[:3] + [old], approve=True)
        self.club.review(ids[3:], approve=False)
        summary = self.club.summary(date(2026, 11, 19), milestones=[{"name": "Marshall", "miles": 50},
                                                                    {"name": "Las Vegas", "miles": 2000}])
        self.assertEqual(summary["this_week"], [{"name": "Sam", "city": "", "miles": 10.0},
                                                {"name": "Dana", "city": "Norfolk", "miles": 5.5}])
        self.assertEqual((summary["total_miles"], summary["milestone"]), (55.5, "Marshall"))
        dest = self.club.export(self.root / "plans" / "_miles_club.json", date(2026, 11, 19))
        self.assertEqual(json.loads(dest.read_text())["total_miles"], 55.5)

    def test_the_station_api_takes_entries(self):
        import api_server

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), api_server.NowPlayingHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/miles-club"

        def post(data):
            request = urllib.request.Request(url, data=json.dumps(data).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        with patch.dict(os.environ, {"WRIT_MILES_CLUB_DB": str(self.root / "api.db")}), \
                patch.dict(api_server.miles_club_times, clear=True):
            self.assertEqual(post({"name": "Dana", "city": "Norfolk", "miles": 3, "age_13_plus": True})[0], 200)
            status, body = post({"name": "Dana", "miles": 3, "age_13_plus": False})
            self.assertEqual((status, body["error"]), (400, "The Miles Club is for listeners 13 and older."))
            for _ in range(4):
                post({"name": "Dana", "miles": 1, "age_13_plus": True})
            self.assertEqual(post({"name": "Dana", "miles": 1, "age_13_plus": True})[0], 429)
        with miles_club.MilesClub(self.root / "api.db") as club:
            rows = club.pending()
        self.assertEqual(len(rows), 5)
        self.assertEqual(set(rows[0].keys()), {"id", "logged_at", "name", "city", "miles", "status", "reviewed_at"})


if __name__ == "__main__":
    unittest.main()
