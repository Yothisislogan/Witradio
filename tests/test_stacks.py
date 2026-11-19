"""The Stacks: the planner, the remix engine, the Halo producer, and the director playing a plan."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))  # flat, the way the modules import each other
sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_tools as at  # noqa: E402
import buffer_sync  # noqa: E402
import produce_monster_mix  # noqa: E402
import remix  # noqa: E402
import stacks_planner  # noqa: E402
from catalog import Catalog  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from playout_director import Director  # noqa: E402
from produce_miles import MilesProducer  # noqa: E402
from produce_stacks import StacksProducer, closing_text  # noqa: E402
from producer_daemon import Daemon  # noqa: E402
from producers import Context  # noqa: E402
from schedule import load_schedule  # noqa: E402
from script_writer import ScriptWriter  # noqa: E402
from test_producers import FakeLLM, voices_config  # noqa: E402
from voices import Renderer  # noqa: E402

ET = ZoneInfo("America/New_York")
HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_RSYNC = shutil.which("rsync") is not None
HAVE_LIBROSA = remix._librosa() is not None
SCHEDULE = PROJECT_ROOT / "config" / "wit_next_schedule.yaml"
CREDIT = "Citizen DJ Project, Library of Congress, National Jukebox."
THURSDAY = datetime(2026, 11, 19, 13, 0)  # The Stacks, 1-3 PM
SATURDAY = datetime(2026, 11, 21, 11, 0)  # Saturday Crate Dig, 11 AM-1 PM


def click(path: Path, seconds: float, bpm: float = 100.0, frequency: int = 880) -> Path:
    """A click on every beat, so tempo detection has something to find."""
    path.parent.mkdir(parents=True, exist_ok=True)
    period = 60.0 / bpm
    expr = f"if(lt(mod(t\\,{period:.5f})\\,0.03)\\,0.8*sin(2*PI*{frequency}*t)\\,0)"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"aevalsrc={expr}:s=44100:d={seconds}",
                    "-ac", "2", str(path)], check=True)
    return path


WORDS = ["Automobile", "Fox Trot", "Rag", "Banjo", "March", "Opera"]


def fill_shelf(catalog: Catalog, library: Path, records: int = 44) -> dict[str, list[int]]:
    """An approved Stacks shelf: records from 1905-1924, a few speeches, and some that may not air."""
    ids: dict[str, list[int]] = {"records": [], "spoken": [], "blocked": [], "clips": []}

    def add(group, title, seconds, year, tags=(), **extra):
        path = library / "air" / "stacks" / f"{group}-{len(ids[group])}.flac"
        work_id = catalog.add_work(
            kind="stacks", title=title, performer=f"Performer {len(ids[group])}", year=year,
            composition_year=year, source="loc", license="public_domain", credit_line=CREDIT, tags=["stacks", *tags],
            source_url=extra.pop("source_url", f"https://www.loc.gov/item/{group}{len(ids[group])}/"),
            files=[{"path": str(path), "duration_sec": seconds}], notes=extra.pop("notes", None), **extra)
        ids[group].append(work_id)
        return work_id

    for i in range(records):
        add("records", f"{WORDS[i % len(WORDS)]} Number {i}", 120 + (i * 37) % 200, 1905 + i % 20,
            notes=f"LOC: Victor {17000 + i}\nissued as Victor {17000 + i}")
    for seconds in (150, 200, 260, 400):
        add("spoken", f"An Address {seconds}", seconds, 1912, tags=["spoken"])
    add("blocked", "Too New Fox Trot", 180, 1926)  # not public domain until 2027
    add("blocked", "No Year Rag", 180, None)  # no year, no airplay
    add("clips", "Rag Number 2 (clip)", 6, 1907, tags=["remix-clip"], source_url="https://www.loc.gov/item/records2/")
    return ids


class StacksPlannerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.library = self.root / "library"
        self.schedule = load_schedule(SCHEDULE)
        self.catalog = Catalog(self.root / "catalog.db")
        self.addCleanup(self.catalog.close)
        self.ids = fill_shelf(self.catalog, self.library)

    def plan(self, when: datetime):
        block = self.schedule.program_block_at(when)
        return stacks_planner.plan_for_block(self.schedule, block, self.catalog, library=self.library)

    @staticmethod
    def hours(entries):
        hours, current = [], []
        for entry in entries:
            current.append(entry)
            if entry["role"] == "closing":
                hours.append(current)
                current = []
        return hours

    def test_each_hour_follows_the_clock(self):
        plan = self.plan(THURSDAY)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.problems, [])
        data = plan.to_json()
        self.assertEqual((data["program"], data["plan_id"]), ("stacks", "the_stacks@2026-11-19T1800Z"))
        hours = self.hours(data["entries"])
        self.assertEqual(len(hours), 2)
        for number, hour in enumerate(hours):
            roles = [e["role"] for e in hour if e["type"] != "gap"]
            self.assertEqual(roles[:6], ["pull_one", "shelf_intro", "shelf", "shelf_intro", "shelf", "remix"])
            self.assertEqual(roles[6], "call_number")
            self.assertIn("speech_sample", roles)
            then = roles.index("then_now_intro")
            self.assertEqual(roles[then:then + 3], ["then_now_intro", "then", "then_now"])
            self.assertEqual(roles[-1], "closing")
            self.assertEqual(hour[-1]["last_hour"], number == 1)
            self.assertTrue(hour[0]["theme"])
            seconds = sum(e["seconds"] for e in hour)
            self.assertLessEqual(seconds, 3600 + 60)
            self.assertGreaterEqual(seconds, 3600 - 300)

            # Every record gets a 30-second intro naming it, right before it.
            for i, entry in enumerate(hour):
                if entry["type"] == "work" and entry["role"] == "shelf":
                    self.assertEqual((hour[i - 1]["role"], hour[i - 1]["about"]), ("shelf_intro", [entry["work_id"]]))

        works = [e for e in data["entries"] if e["type"] == "work"]
        self.assertEqual(len({e["work_id"] for e in works}), len(works))  # nothing repeats
        allowed = set(self.ids["records"])
        self.assertTrue({e["work_id"] for e in works} <= allowed)  # never blocked, spoken or clip items
        first = works[0]
        self.assertEqual(first["library_rel"], [str(Path(first["files"][0]).relative_to(self.library))])
        self.assertEqual(first["credit"], CREDIT)
        self.assertIn(f"Victor {17000 + self.ids['records'].index(first['work_id'])}", first["facts"])

        remix_entry = next(e for e in data["entries"] if e["role"] == "remix")
        self.assertIn(remix_entry["bed_style"], stacks_planner.BED_STYLES)
        self.assertTrue(remix_entry["source"]["library_rel"].startswith("air/stacks/"))
        speech = next(e for e in data["entries"] if e["role"] == "speech_sample")
        self.assertIn(speech["source"]["work_id"], self.ids["spoken"])
        self.assertLessEqual(speech["seconds"], stacks_planner.SPEECH_LONGEST + 30)
        then = next(e for e in data["entries"] if e["role"] == "then")
        self.assertTrue(1910 <= then["year"] <= 1919)
        then_now = next(e for e in data["entries"] if e["role"] == "then_now")
        self.assertEqual(then_now["source"]["work_id"], then["work_id"])

    def test_crate_dig_keeps_one_theme_and_remixes_every_pair(self):
        plan = self.plan(SATURDAY)
        self.assertEqual(plan.block, "crate_dig")
        self.assertEqual(len(plan.themes), 2)
        self.assertEqual(len(set(plan.themes)), 1)
        weekday = [[e["role"] for e in hour] for hour in self.hours(self.plan(THURSDAY).to_json()["entries"])]
        for hour in self.hours(plan.to_json()["entries"]):
            roles = [e["role"] for e in hour if e["type"] != "gap"]
            remixes = [i for i, role in enumerate(roles) if role == "remix"]
            for i in remixes:  # a remix always follows a pair of records
                self.assertEqual(roles[i - 4:i], ["shelf_intro", "shelf", "shelf_intro", "shelf"])
            self.assertGreater(len(remixes), max(h.count("remix") for h in weekday))

    def test_rotation_keeps_recent_plays_off_the_air(self):
        aired = self.ids["records"][:10]
        for work_id in aired:
            path = self.catalog.work(work_id).files[0].path
            self.catalog.record_airplay(path, aired_at=datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc))
        works = {e["work_id"] for e in self.plan(THURSDAY).entries if e["type"] == "work"}
        self.assertFalse(works & set(aired))

    def test_a_citizen_dj_clip_is_the_remix_source_for_its_record(self):
        shelf = stacks_planner.Shelf(self.catalog, date(2026, 11, 19))
        record = self.catalog.work(self.ids["records"][2])
        clip = shelf.clip_for([record])
        self.assertEqual(clip.id, self.ids["clips"][0])
        self.assertIsNone(shelf.clip_for([self.catalog.work(self.ids["records"][3])]))

    def test_too_small_a_shelf_plans_nothing(self):
        with Catalog(self.root / "small.db") as small:
            fill_shelf(small, self.library, records=1)
            block = self.schedule.program_block_at(THURSDAY)
            plan = stacks_planner.plan_for_block(self.schedule, block, small, library=self.library)
        self.assertFalse(plan.ok)
        self.assertIn("not enough approved Stacks records for a full plan", plan.problems)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class RemixTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.beds = []

    def bed_maker(self, caption, out, seconds, *, short=None):
        self.beds.append((caption, seconds))
        bpm = float(re.search(r"(\d+) BPM", caption).group(1))
        click(Path(out), short or seconds, bpm, 220)
        return True

    def test_tempo_fit_and_style_choice(self):
        self.assertEqual(remix.fit_tempo(60, "house"), 120)  # doubled into the style's range
        self.assertEqual(remix.fit_tempo(180, "boom-bap"), 90)  # halved
        self.assertEqual(remix.fit_tempo(None, "lo-fi"), 80)  # unknown: the style's own tempo
        self.assertEqual(remix.choose_style(92, "boom-bap", stacks_planner.BED_STYLES), "boom-bap")
        self.assertEqual(remix.choose_style(122, "boom-bap", stacks_planner.BED_STYLES), "house")
        self.assertTrue(remix.bed_caption("lo-fi", 80, "A minor").endswith("instrumental only, no vocals"))

    def test_placements_sit_on_bar_lines(self):
        times = remix.placements(60, 4.8, 100)
        bar = 2.4
        self.assertEqual([round(t, 3) for t in times[:2]], [9.6, 14.4])  # a four-bar intro, then every two bars
        self.assertTrue(all(abs(t / bar - round(t / bar)) < 1e-6 for t in times))
        self.assertLessEqual(times[-1] + 4.8, 60 - 2 * bar)
        long = remix.placements(150, 12, 100)  # a five-bar phrase leaves five bars of beat between
        self.assertEqual([round(t, 3) for t in long[:3]], [9.6, 33.6, 57.6])

    def test_remix_keeps_the_bed_length_and_tempo(self):
        source = click(self.root / "record.wav", 30, 100)
        info = remix.remix(source, self.bed_maker, self.root / "remix.flac", style="boom-bap", bed_seconds=40)
        self.assertAlmostEqual(at.duration(self.root / "remix.flac"), 40, delta=0.5)
        self.assertIn("instrumental only, no vocals", info["caption"])
        self.assertGreaterEqual(info["placements"], 1)
        if HAVE_LIBROSA:
            self.assertAlmostEqual(info["phrase_tempo"], 100, delta=6)
            self.assertEqual(info["style"], "boom-bap")
            self.assertAlmostEqual(info["stretch"], 1.0, delta=0.08)

    def test_a_long_speech_fades_out_and_a_short_bed_loops(self):
        speech = at.tone(self.root / "speech.wav", 30, 300)
        maker = lambda caption, out, seconds: self.bed_maker(caption, out, seconds, short=8)  # noqa: E731
        info = remix.speech_over_bed(speech, maker, self.root / "speech.flac", longest=10)
        self.assertAlmostEqual(info["seconds"], 10, delta=0.2)
        self.assertAlmostEqual(at.duration(self.root / "speech.flac"), 22, delta=0.5)
        self.assertEqual(self.beds[0][1], info["seconds"] + 12)


def produced_plan(library: Path) -> dict:
    record = {"type": "work", "role": "shelf", "seconds": 30, "work_id": 7, "title": "Maple Leaf Rag",
              "performer": "Test Band", "composer": "Scott Joplin", "year": 1912, "credit": CREDIT,
              "license": "public_domain", "files": ["/server/library/air/stacks/rag.flac"],
              "facts": ["Victor 17033", "issued as Victor 17033"], "library_rel": ["air/stacks/rag.flac"]}
    source = {"work_id": 7, "title": "Maple Leaf Rag", "performer": "Test Band", "composer": "Scott Joplin",
              "year": 1912, "credit": CREDIT, "license": "public_domain", "seconds": 30,
              "path": "/server/library/air/stacks/rag.flac", "library_rel": "air/stacks/rag.flac",
              "tags": ["stacks", "ragtime", "loc-jukebox-ragtime"]}
    speech = {"work_id": 8, "title": "An Address", "performer": "A Speaker", "year": 1912, "credit": CREDIT,
              "license": "public_domain", "seconds": 20, "path": "/server/library/air/stacks/speech.flac",
              "library_rel": "air/stacks/speech.flac", "tags": ["stacks", "spoken"]}
    entries = [
        {"type": "talk", "role": "pull_one", "seconds": 20, "theme": "Ragtime Week"},  # 0
        {"type": "talk", "role": "shelf_intro", "seconds": 10, "about": [7]},  # 1
        record,  # 2
        {"type": "gap", "role": "gap", "seconds": 2},  # 3
        {"type": "talk", "role": "remix", "seconds": 60, "about": [7], "source": source, "bed_style": "boom-bap"},  # 4
        {"type": "talk", "role": "call_number", "seconds": 20, "about": [7]},  # 5
        {"type": "talk", "role": "speech_sample", "seconds": 50, "about": [8], "source": speech},  # 6
        {"type": "talk", "role": "then_now_intro", "seconds": 10, "about": [7]},  # 7
        {"type": "talk", "role": "then_now", "seconds": 30, "about": [7], "source": source},  # 8
        {"type": "talk", "role": "closing", "seconds": 20, "about": [7], "last_hour": True},  # 9
    ]
    return {"plan_id": "the_stacks@2026-11-19T1800Z", "show_id": "the_stacks", "block": "hour_clock",
            "title": "The Stacks with Steve", "program": "stacks", "starts_at": "2026-11-19T13:00:00-05:00",
            "ends_at": "2026-11-19T15:00:00-05:00", "entries": entries}


@unittest.skipUnless(HAVE_FFMPEG and HAVE_RSYNC, "ffmpeg and rsync are required")
class StacksProducerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.llm = FakeLLM()
        threading.Thread(target=self.llm.serve_forever, daemon=True).start()
        self.addCleanup(self.llm.server_close)
        self.addCleanup(self.llm.shutdown)
        renderer = Renderer.from_config(voices_config(self.root), self.root / "none.yaml")
        self.library = self.root / "library"
        click(self.library / "air" / "stacks" / "rag.flac", 30, 100)
        at.tone(self.library / "air" / "stacks" / "speech.flac", 20, 300)
        self.captions = []
        self.ctx = Context(
            schedule=load_schedule(SCHEDULE), renderer=renderer,
            writer=ScriptWriter.for_renderer(LLMClient(base_url=self.llm.base, retries=0), renderer, attempts=2),
            buffer_dir=self.root / "buffer", rehearsal_dir=self.root / "rehearsal", work_dir=self.root / "work",
            state_dir=self.root / "state", log=lambda m: None, services={"music": self.fake_music},
            library_dir=self.library)
        os.environ.pop("WRIT_REHEARSE", None)

    def fake_music(self, caption, out, seconds):
        self.captions.append(caption)
        match = re.search(r"(\d+) BPM", caption)
        click(Path(out), seconds, float(match.group(1)) if match else 100, 220)
        return True

    def write_plan(self, plan: dict) -> None:
        plans = self.ctx.buffer_dir / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        (plans / f"{plan['plan_id']}.json").write_text(json.dumps(plan))

    def test_every_talk_entry_is_rendered_with_its_credits(self):
        plan = produced_plan(self.library)
        self.write_plan(plan)
        producer = StacksProducer(self.ctx)
        (result,) = producer.produce_due(datetime(2026, 11, 19, 9, 0, tzinfo=ET))
        talk = self.ctx.buffer_dir / "talk" / plan["plan_id"]
        self.assertEqual(sorted(p.name for p in talk.glob("*.flac")),
                         ["000.flac", "001.flac", "004.flac", "005.flac", "006.flac", "007.flac", "008.flac", "009.flac"])
        self.assertEqual(result.status, "done")
        self.assertFalse(list(talk.glob(".*")))  # no half-written files left behind

        steve = json.loads((talk / "001.meta.json").read_text())
        self.assertEqual(steve, {"title": "The Stacks with Steve", "artist": "Steve"})
        intro = " ".join(line["text"] for line in json.loads((talk / "001.json").read_text())["lines"])
        self.assertIn("Maple Leaf Rag", intro)
        segment, _ = producer.steve_segment(plan, 1)  # Steve gets the Library's own notes on the record
        self.assertIn("Library of Congress notes: Victor 17033", segment.facts)

        remixed = json.loads((talk / "004.meta.json").read_text())
        self.assertEqual(remixed["title"], "Remix: Maple Leaf Rag")
        self.assertEqual(remixed["catalog_path"], "/server/library/air/stacks/rag.flac")  # logs the record's airplay
        self.assertTrue(remixed["credit"].startswith(f"Remix of {CREDIT}"))
        self.assertEqual(remixed["license"], "public_domain")
        self.assertTrue((talk / "004.json").exists())  # Steve's line ahead of the beat
        self.assertAlmostEqual(at.duration(talk / "004.flac"), 48 + 0.4 + 0.4 * 2, delta=1.5)

        speech = json.loads((talk / "006.meta.json").read_text())
        self.assertEqual((speech["title"], speech["catalog_path"]),
                         ("Speech Sample: An Address", "/server/library/air/stacks/speech.flac"))
        self.assertAlmostEqual(at.duration(talk / "006.flac"), 20 + 12 + 0.6 + 0.8, delta=1.5)

        then_now = json.loads((talk / "008.meta.json").read_text())
        self.assertEqual(then_now["license"], "generated")
        self.assertNotIn("catalog_path", then_now)
        self.assertIn("1912", then_now["caption"])
        self.assertAlmostEqual(at.duration(talk / "008.flac"), 30, delta=0.5)
        self.assertTrue(all(c.endswith("instrumental only, no vocals") for c in self.captions))
        self.assertEqual(float(at.measure(talk / "004.flac")["input_i"]) // 1, -16.0)

        closing = " ".join(line["text"] for line in json.loads((talk / "009.json").read_text())["lines"])
        self.assertIn("Maple Leaf Rag", closing)

        # A second pass renders nothing new.
        (again,) = producer.produce_due(datetime(2026, 11, 19, 9, 5, tzinfo=ET))
        self.assertEqual(again.segments, [])

    def test_an_inventing_model_falls_back_to_the_plan(self):
        self.llm.invent = True
        plan = produced_plan(self.library)
        plan["entries"] = plan["entries"][:3] + [plan["entries"][-1]]
        result = StacksProducer(self.ctx).produce_plan(plan)
        talk = self.ctx.buffer_dir / "talk" / plan["plan_id"]
        self.assertTrue(any("plain template" in p for p in result.problems))
        intro = json.loads((talk / "001.json").read_text())["lines"][0]["text"]
        self.assertEqual(intro, "From 1912, here's Test Band with Maple Leaf Rag.")
        closing = json.loads((talk / "003.json").read_text())["lines"][0]["text"]
        self.assertTrue(closing.endswith("Return your books on time, and keep digging."))
        self.assertEqual(closing.count("Courtesy of"), 1)

    def test_missing_recordings_are_asked_for_and_skipped(self):
        plan = produced_plan(self.library)
        (self.library / "air" / "stacks" / "speech.flac").unlink()
        self.write_plan(plan)
        producer = StacksProducer(self.ctx)
        now = datetime(2026, 11, 19, 9, 0, tzinfo=ET)
        self.assertEqual(producer.media_needed(now), ["air/stacks/speech.flac"])
        result = producer.produce_plan(plan)
        self.assertEqual(result.status, "partial")
        self.assertTrue(any("isn't on the Halo yet" in p for p in result.problems))
        self.assertFalse((self.ctx.buffer_dir / "talk" / plan["plan_id"] / "006.flac").exists())

    def test_without_a_music_engine_the_beat_segments_wait(self):
        self.ctx.services["music"] = lambda caption, out, seconds: False
        plan = produced_plan(self.library)
        result = StacksProducer(self.ctx).produce_plan(plan)
        talk = self.ctx.buffer_dir / "talk" / plan["plan_id"]
        self.assertEqual(result.status, "partial")
        for name in ("004", "006", "008"):
            self.assertFalse((talk / f"{name}.flac").exists())
        self.assertTrue((talk / "005.flac").exists())  # Steve's talk still renders

    def test_slow_segments_go_a_few_per_cycle_and_the_talk_never_waits(self):
        self.ctx.services["music"] = lambda caption, out, seconds: False  # the engine is down
        plan = produced_plan(self.library)
        self.write_plan(plan)
        producer = StacksProducer(self.ctx)
        producer.per_call = 1
        now = datetime(2026, 11, 19, 9, 0, tzinfo=ET)
        (first,) = producer.produce_due(now)
        talk = self.ctx.buffer_dir / "talk" / plan["plan_id"]
        self.assertEqual(sorted(p.name for p in talk.glob("*.flac")),
                         ["000.flac", "001.flac", "005.flac", "007.flac", "009.flac"])  # every line of Steve's
        self.assertEqual(first.status, "partial")
        self.assertEqual(sum("music engine" in p or "failed" in p for p in first.problems), 1)  # one try per cycle

    def test_the_daemon_pulls_recordings_and_fills_locked_slots(self):
        plan = produced_plan(self.library)
        plan["entries"][6]["source"]["library_rel"] = "air/stacks/elsewhere.flac"
        self.write_plan(plan)

        class FakeSync:
            def __init__(self):
                self.media = []

            def pull(self):
                pass

            def push(self):
                pass

            def pull_media(self, paths, library):
                self.media.append((list(paths), library))

        sync = FakeSync()
        status = self.ctx.buffer_dir / "plans" / "_status.json"
        status.write_text(json.dumps({"unlocked": {"the_stacks": True}}))
        daemon = Daemon(self.ctx, sync)
        with patch.object(produce_monster_mix.MonsterMixProducer, "lead_hours", 0.0), \
                patch.object(MilesProducer, "lead_hours", 0.0):  # not what this test is about
            results = daemon.cycle(datetime(2026, 11, 19, 9, 0))
        self.assertEqual(sync.media, [(["air/stacks/elsewhere.flac"], self.library)])
        self.assertIn(plan["plan_id"], [r.slot for r in results])

        airing = next(a for a in __import__("producers").upcoming_airings(self.ctx.schedule, THURSDAY, 1)
                      if a.show_id == "the_stacks")
        self.assertIsNone(daemon.resolve(airing))  # unlocked: the plan covers it
        status.write_text(json.dumps({"unlocked": {"the_stacks": False}}))
        target, program = daemon.resolve(airing)
        self.assertEqual((target.show_id, program, target.slot), ("monster_mix", "monster_mix", "2026-11-19_1300"))


class ClosingTextTests(unittest.TestCase):
    def test_credits_are_read_once(self):
        records = [{"title": "A", "performer": "P", "credit": CREDIT}, {"title": "B", "credit": CREDIT}]
        text = closing_text(records, last=False)
        self.assertEqual(text, "Back to the stacks. That hour: A, P; B. Courtesy of Citizen DJ Project, Library of "
                               "Congress, National Jukebox. Stay right there.")


@unittest.skipUnless(HAVE_RSYNC, "rsync is required")
class LibraryPullTests(unittest.TestCase):
    def test_pull_media_copies_only_what_was_asked_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            (server / "library" / "air" / "stacks").mkdir(parents=True)
            (server / "library" / "air" / "stacks" / "rag.flac").write_bytes(b"rag")
            (server / "library" / "air" / "stacks" / "other.flac").write_bytes(b"other")
            (server / "buffer").mkdir()
            sync = buffer_sync.Sync(root / "halo" / "buffer", str(server / "buffer"))
            self.assertEqual(sync.library_remote, str(server / "library"))
            pulled = sync.pull_media(["air/stacks/rag.flac", "../secrets.txt", "air/stacks/missing.flac"],
                                     root / "halo" / "library")
            self.assertEqual(pulled, ["air/stacks/missing.flac", "air/stacks/rag.flac"])
            self.assertEqual((root / "halo" / "library" / "air" / "stacks" / "rag.flac").read_bytes(), b"rag")
            self.assertFalse((root / "halo" / "library" / "air" / "stacks" / "other.flac").exists())

    def test_library_remote_sits_next_to_the_buffer(self):
        self.assertEqual(buffer_sync.library_remote_for("witfm@playout:/srv/wit-next/buffer/"),
                         "witfm@playout:/srv/wit-next/library")
        self.assertEqual(buffer_sync.library_remote_for("witfm@playout:/srv/elsewhere"), "")


class DirectorStacksTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        out = self.root / "out"
        self.station = SimpleNamespace(
            id="wit-next", call_sign="WIT-FM Next", output_dir=out, runtime_dir=out / "runtime",
            library_dir=out / "library", now_playing_file=out / "now_playing.json",
            stream=SimpleNamespace(mount="/next"))
        self.schedule = load_schedule(SCHEDULE)
        self.catalog = Catalog(self.root / "catalog.db")
        self.addCleanup(self.catalog.close)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ("WRIT_FORCE_STACKS", "WRIT_FORCE_CLASSICAL", "WRIT_SAFE_DIR", "WRIT_IDS_DIR"):
            os.environ.pop(name, None)

    def director(self) -> Director:
        return Director(self.station, schedule=self.schedule, catalog_path=self.root / "catalog.db")

    def test_the_stacks_waits_for_enough_approved_records(self):
        fill_shelf(self.catalog, self.station.library_dir, records=5)
        director = self.director()
        self.assertEqual(director._unlocked_fallback(self.schedule.shows["the_stacks"]), "monster_mix")
        item = director.next_item(datetime(2026, 11, 19, 13, 5, tzinfo=ET))
        self.assertEqual(item.show_id, "monster_mix")
        with patch.dict(os.environ, {"WRIT_FORCE_STACKS": "1"}):
            self.assertIsNone(director._unlocked_fallback(self.schedule.shows["the_stacks"]))

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
    def test_the_plan_plays_records_and_produced_segments_with_their_credits(self):
        fill_shelf(self.catalog, self.station.library_dir)
        director = self.director()
        status = director.plan_ahead(hours=6, now=datetime(2026, 11, 19, 10, 0, tzinfo=ET))
        plan_id = "the_stacks@2026-11-19T1800Z"
        self.assertTrue(status["unlocked"]["the_stacks"])
        self.assertIn(plan_id, status["planned"])
        plan = json.loads((director.plans_dir / f"{plan_id}.json").read_text())
        self.assertEqual(plan["program"], "stacks")

        entries = plan["entries"]
        talk = director.talk_dir / plan_id
        remix_index = next(i for i, e in enumerate(entries) if e["role"] == "remix")
        at.tone(talk / "000.flac", 3)
        (talk / "000.meta.json").write_text(json.dumps({"title": "The Stacks with Steve", "artist": "Steve"}))
        first_intro = next(i for i, e in enumerate(entries) if e["role"] == "shelf_intro")
        at.tone(talk / f"{first_intro:03d}.flac", 2)  # the first record's intro
        at.tone(talk / f"{remix_index:03d}.flac", 5)
        source = entries[remix_index]["source"]
        (talk / f"{remix_index:03d}.meta.json").write_text(json.dumps({
            "title": f"Remix: {source['title']}", "artist": "WIT-FM", "credit": f"Remix of {source['credit']}",
            "license": "public_domain", "catalog_path": source["path"]}))

        now = datetime(2026, 11, 19, 13, 0, tzinfo=ET)
        items = []
        for _ in range(8):
            item = director.next_item(now)
            director.aired(item.entry_id, item.path, now=now)
            items.append(item)
            now += timedelta(seconds=item.seconds)
            if item.role == "remix":
                break
        kinds = [(i.kind, i.role) for i in items if i.kind != "gap"]
        self.assertEqual(kinds, [("talk", "pull_one"), ("talk", "shelf_intro"), ("work", "shelf"),
                                 ("work", "shelf"), ("talk", "remix")])  # the second intro wasn't rendered
        self.assertEqual((items[0].title, items[0].artist), ("The Stacks with Steve", "Steve"))
        record = next(i for i in items if i.kind == "work")
        self.assertEqual((record.credit, record.show_id), (CREDIT, "the_stacks"))
        remixed = items[-1]
        self.assertEqual(remixed.title, f"Remix: {source['title']}")
        rows = self.catalog.db.execute("SELECT path, role, license FROM airplay WHERE role = 'remix'").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(source["path"], "remix", "public_domain")])


if __name__ == "__main__":
    unittest.main()
