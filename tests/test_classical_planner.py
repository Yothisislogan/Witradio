"""Classical fill planner: whole works, landing on the post, rotation rules."""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from mac import classical_planner as cp
from mac.catalog import Catalog, Play
from mac.schedule import load_schedule
from tests.classical_fixtures import make_library, make_work

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = cp.PlannerConfig()

# block type, show id, start (Eastern), real seconds
CASES = [
    ("nocturne", "nocturne", datetime(2026, 11, 18, 23, 0, tzinfo=ET), 6 * 3600),
    ("nocturne", "nocturne", datetime(2026, 11, 20, 23, 0, tzinfo=ET), 8 * 3600),
    ("nocturne", "nocturne", datetime(2026, 11, 21, 23, 0, tzinfo=ET), 9 * 3600),
    ("day_shift", "classical_day_shift", datetime(2026, 11, 18, 9, 15, tzinfo=ET), 2.75 * 3600),
    ("day_shift", "classical_day_shift", datetime(2026, 11, 18, 15, 0, tzinfo=ET), 1.5 * 3600),
    ("evening_concert", "evening_concert", datetime(2026, 11, 18, 20, 0, tzinfo=ET), 2 * 3600),
    ("matinee", "saturday_matinee", datetime(2026, 11, 21, 14, 0, tzinfo=ET), 5.5 * 3600),
    ("concert_hall", "sunday_concert_hall", datetime(2026, 11, 22, 11, 0, tzinfo=ET), 6 * 3600),
]


def plan(block, show_id, start, seconds, library, **kwargs):
    return cp.plan_block(block_type=block, show_id=show_id, title=show_id, host="count",
                         start=start, seconds=seconds, library=library, tz=ET, **kwargs)


def local(plan_, entry):
    return plan_.at(entry).astimezone(ET)


class PlannerInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.library = make_library()
        cls.plans = {(b, s.isoformat()): plan(b, show, s, secs, cls.library) for b, show, s, secs in CASES}

    def check_invariants(self, p, seconds):
        self.assertTrue(p.ok, p.warnings)
        self.assertEqual(p.warnings, [])  # strict rules held on a 300-hour library
        entries = p.entries
        # Back to back from 0 to the post, exactly.
        self.assertAlmostEqual(entries[0].offset, 0.0)
        for before, after in zip(entries, entries[1:]):
            self.assertAlmostEqual(before.end, after.offset, places=6)
        self.assertAlmostEqual(entries[-1].end, seconds, places=6)

        # The closing link absorbs at most the tolerance beyond its nominal length.
        policy = cp.POLICIES[p.block]
        closing = entries[-1]
        self.assertEqual(closing.type, "talk")
        self.assertEqual(closing.role, policy.close_role)
        self.assertGreaterEqual(closing.seconds, policy.close_seconds - 0.5)
        self.assertLessEqual(closing.seconds, policy.close_seconds + CONFIG.tolerance + 0.5)

        works = p.works()
        ids = [e.work.id for e in works]
        self.assertEqual(len(ids), len(set(ids)), "a work repeats inside the block")
        for e in works:
            self.assertEqual(e.seconds, e.work.duration_sec, "a work was cut")
            self.assertTrue(e.work.lossless)

        # No composer twice in an hour, never back to back.
        for a, b in zip(works, works[1:]):
            self.assertNotEqual(a.work.composer, b.work.composer)
        for i, a in enumerate(works):
            for b in works[i + 1:]:
                if a.work.composer == b.work.composer:
                    self.assertGreaterEqual(b.offset - a.offset, 3600 - 1e-6,
                                            f"{a.work.composer} twice within the hour")

        # Silence between works is 2-4 s; a link leads into its music after 1 s.
        for i, e in enumerate(entries):
            if e.type != "gap":
                continue
            prev, nxt = entries[i - 1], entries[i + 1]
            if prev.type in ("talk", "id"):
                self.assertAlmostEqual(e.seconds, CONFIG.talk_gap)
            elif prev.type == "work":
                self.assertTrue(CONFIG.gap_min <= e.seconds <= CONFIG.gap_max, e.seconds)

        # One hourly ID per top of the hour, in a gap after a work (or at a
        # block that starts on the hour), never between a link and its music.
        marks = cp.BlockPlanner(block_type=p.block, start=p.start, seconds=seconds, library=[],
                                history=cp.History([]), rules=cp.STRICT, config=CONFIG,
                                seed="x", tz=ET).id_marks
        id_positions = [i for i, e in enumerate(entries) if e.type == "id"]
        self.assertEqual(len(id_positions), len(marks))
        for i in id_positions:
            if i == 0:
                self.assertEqual(entries[0].offset, 0.0)
                continue
            self.assertEqual(entries[i - 1].type, "gap")
            self.assertEqual(entries[i - 2].type, "work")

    def test_every_block_type_lands_on_the_post(self):
        for block, show, start, seconds in CASES:
            with self.subTest(block=block, start=start.strftime("%a %H:%M")):
                self.check_invariants(self.plans[(block, start.isoformat())], seconds)

    def test_nocturne_night_structure(self):
        start = datetime(2026, 11, 18, 23, 0, tzinfo=ET)
        p = self.plans[("nocturne", start.isoformat())]
        talks = [e for e in p.entries if e.type == "talk"]
        self.assertEqual(talks[0].role, "recollection")
        self.assertEqual(talks[0].seconds, 120)
        main = [e for e in p.works() if e.role == "main_event"]
        self.assertEqual(len(main), 1)
        self.assertEqual(talks[0].about, [main[0].work.id])  # tonight's main composer
        main_at = local(p, main[0])
        self.assertLess(abs((main_at - start.replace(day=19, hour=1)).total_seconds()), 10 * 60)
        self.assertGreaterEqual(main[0].seconds, 40 * 60)
        roles = [e.role for e in p.works()]
        self.assertEqual(roles[-1], "before_the_dawn")
        first_main = roles.index("main_event")
        self.assertTrue(all(r == "first_set" for r in roles[:first_main]))
        self.assertTrue(all(r == "small_hours" for r in roles[first_main + 1:-1]))
        for e in p.works():
            if e.role in ("first_set", "small_hours"):
                self.assertLessEqual(e.seconds, 30 * 60)
        # The Recollection opens the night; between works he talks 90 s at most.
        self.assertTrue(all(e.seconds <= 90 for e in talks[1:-1]), "the Count talks 90 s at most")
        self.assertEqual(p.theme, "Strings")  # Wednesday night

    def test_nocturne_theme_follows_the_night(self):
        self.assertEqual(cp.nocturne_theme(datetime(2026, 11, 17, 23, 0))[1], "Keyboard Night")  # Tuesday
        self.assertEqual(cp.nocturne_theme(datetime(2026, 11, 20, 23, 0))[1], "Requiems and Dark Things")

    def test_sunday_morning_is_sunday_sacred(self):
        start = datetime(2026, 11, 21, 23, 0, tzinfo=ET)
        p = self.plans[("nocturne", start.isoformat())]
        five = datetime(2026, 11, 22, 5, 0, tzinfo=ET)
        sacred = [e for e in p.works() if e.role == "sunday_sacred"]
        self.assertTrue(sacred)
        self.assertTrue(all(local(p, e) >= five for e in sacred))
        for e in p.works():
            if e.role == "small_hours":
                self.assertLessEqual(p.at(e) + timedelta(seconds=e.seconds), five)
        self.assertEqual(local(p, p.entries[-1]).hour, 7)  # signs off just before 8:00

    def test_evening_concert_follows_the_template(self):
        start = datetime(2026, 11, 18, 20, 0, tzinfo=ET)
        p = self.plans[("evening_concert", start.isoformat())]
        roles = [e.role for e in p.entries if e.type in ("talk", "work")]
        self.assertEqual(roles[:2], ["program", "overture"])
        self.assertEqual(roles[-1], "handoff")
        for role, minute, slack in (("concerto", 15, 6), ("symphony", 50, 6)):
            entry = next(e for e in p.works() if e.role == role)
            target = start.replace(hour=20, minute=minute)
            self.assertLess(abs((local(p, entry) - target).total_seconds()), slack * 60, role)
        talk_before_symphony = p.entries[p.entries.index(next(e for e in p.works() if e.role == "symphony")) - 2]
        self.assertEqual(talk_before_symphony.role, "intermission")
        self.assertEqual(talk_before_symphony.seconds, 180)
        self.assertEqual(p.works()[-1].role, "encore")
        program = p.entries[[e.role for e in p.entries].index("program")]
        self.assertEqual(program.about, [e.work.id for e in p.works()])

    def test_day_shift_keeps_works_short_and_back_announces(self):
        for start in (datetime(2026, 11, 18, 9, 15, tzinfo=ET), datetime(2026, 11, 18, 15, 0, tzinfo=ET)):
            p = self.plans[("day_shift", start.isoformat())]
            self.assertTrue(all(e.seconds < 30 * 60 for e in p.works()))
            talks = [e for e in p.entries if e.type == "talk"]
            self.assertTrue(all(t.role in ("intro", "back_announce", "closing") for t in talks))
            for a, b in zip(talks, talks[1:]):
                # About every 30 minutes: never more than 30 min plus one work.
                self.assertLess(b.offset - a.end, 60 * 60)


class RotationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.library = make_library()
        cls.start = datetime(2026, 11, 18, 23, 0, tzinfo=ET)

    def test_works_aired_in_the_last_week_are_skipped(self):
        recent = [w for w in self.library if w.id % 3 == 0]
        plays = [Play(w.id, self.start - timedelta(days=6), w.composer, None) for w in recent]
        p = plan("nocturne", "nocturne", self.start, 6 * 3600, self.library, plays=plays)
        self.assertEqual(p.warnings, [])
        aired = {w.id for w in recent}
        self.assertFalse(aired & {e.work.id for e in p.works()})

    def test_planned_future_airings_count_too(self):
        planned = [w for w in self.library if w.id % 2 == 0]
        plays = [Play(w.id, self.start + timedelta(days=2), w.composer, "planned") for w in planned]
        p = plan("nocturne", "nocturne", self.start, 6 * 3600, self.library, plays=plays)
        self.assertFalse({w.id for w in planned} & {e.work.id for e in p.works()})

    def test_main_event_waits_thirty_days(self):
        large = [w for w in self.library if 40 * 60 <= w.duration_sec <= 100 * 60 and not w.cycle]
        keep = large[0]
        plays = [Play(w.id, self.start - timedelta(days=20), w.composer, "main_event")
                 for w in large if w.id != keep.id]
        p = plan("nocturne", "nocturne", self.start, 6 * 3600, self.library, plays=plays)
        main = next(e for e in p.works() if e.role == "main_event")
        self.assertEqual(main.work.id, keep.id)

    def test_two_recordings_of_one_composition_rotate_as_one_work(self):
        def recording(work_id, performer):
            w = make_work(work_id, 12.5, "Frederic Chopin", "ballade")
            return w.__class__(**{**w.__dict__, "title": "Ballade No. 1 in G minor, Op. 23", "performer": performer})

        first, second = recording(9101, "Pianist A"), recording(9102, "Pianist B")
        library = list(self.library) + [first, second]
        self.assertEqual(first.key, second.key)
        aired = [Play(first.id, self.start - timedelta(days=2), first.composer, None)]
        for _ in range(3):
            p = plan("nocturne", "nocturne", self.start, 6 * 3600, library, plays=aired)
            self.assertFalse({9101, 9102} & {e.work.id for e in p.works()})
        p = plan("nocturne", "nocturne", self.start, 6 * 3600, library)
        self.assertLessEqual(len({9101, 9102} & {e.work.id for e in p.works()}), 1)

    def test_composer_heard_just_before_the_block_waits(self):
        p0 = plan("nocturne", "nocturne", self.start, 6 * 3600, self.library)
        composer = p0.works()[0].work.composer
        plays = [Play(-1, self.start - timedelta(minutes=20), composer, None)]
        p = plan("nocturne", "nocturne", self.start, 6 * 3600, self.library, plays=plays)
        first_hour = [e for e in p.works() if e.offset < 40 * 60]
        self.assertTrue(all(e.work.composer != composer for e in first_hour))

    def test_small_library_relaxes_rules_and_says_so(self):
        small = make_library(scale=0.12, beethoven_cycle=False)
        plays = [Play(w.id, self.start - timedelta(days=2), w.composer, None) for w in small[::2]]
        p = plan("nocturne", "nocturne", self.start, 6 * 3600, small, plays=plays)
        self.assertTrue(p.ok)
        self.assertTrue(any("relaxed" in w for w in p.warnings), p.warnings)
        self.assertAlmostEqual(p.entries[-1].end, 6 * 3600, places=6)

    def test_empty_library_fails_cleanly(self):
        p = plan("nocturne", "nocturne", self.start, 6 * 3600, [])
        self.assertFalse(p.ok)
        self.assertEqual(p.entries, [])

    def test_lossy_and_non_classical_works_never_air(self):
        library = list(self.library) + [
            make_work(9001, 12, "Test Composer", "sonata", lossless=False),
            make_work(9002, 12, "Test Composer", "sonata", kind="generated"),
        ]
        p = plan("nocturne", "nocturne", self.start, 6 * 3600, library)
        self.assertFalse({9001, 9002} & {e.work.id for e in p.works()})
        self.assertIn("ignored 2 work(s)", p.warnings[0])

    def test_plans_are_deterministic(self):
        a = plan("concert_hall", "sunday_concert_hall", datetime(2026, 11, 22, 11, 0, tzinfo=ET),
                 6 * 3600, self.library)
        b = plan("concert_hall", "sunday_concert_hall", datetime(2026, 11, 22, 11, 0, tzinfo=ET),
                 6 * 3600, self.library)
        self.assertEqual([(e.type, e.role, e.offset, e.work and e.work.id) for e in a.entries],
                         [(e.type, e.role, e.offset, e.work and e.work.id) for e in b.entries])

    def test_spring_forward_night(self):
        # Saturday March 13, 2027: 23:00 EST to 08:00 EDT is 8 real hours.
        start = datetime(2027, 3, 13, 23, 0, tzinfo=ET)
        p = plan("nocturne", "nocturne", start, 8 * 3600, self.library)
        self.assertTrue(p.ok)
        self.assertEqual(p.end.astimezone(ET), datetime(2027, 3, 14, 8, 0, tzinfo=ET))
        # 23, 0, 1, 3, 4, 5, 6, 7: there is no 2 AM that night.
        self.assertEqual(sum(1 for e in p.entries if e.type == "id"), 8)


class MatineeCycleTests(unittest.TestCase):
    def test_cycle_resumes_in_order_and_stays_out_of_other_blocks(self):
        library = make_library()
        p = plan("matinee", "saturday_matinee", datetime(2026, 11, 21, 14, 0, tzinfo=ET), 5.5 * 3600,
                 library, cycle_positions={"beethoven_piano_sonatas": 4})
        numbers = [e.work.cycle_no for e in p.works() if e.work.cycle]
        self.assertGreaterEqual(len(numbers), 2)
        self.assertEqual(numbers, list(range(5, 5 + len(numbers))))

        day = plan("day_shift", "classical_day_shift", datetime(2026, 11, 18, 9, 15, tzinfo=ET),
                   2.75 * 3600, library)
        self.assertFalse(any(e.work.cycle for e in day.works()))

    def test_finished_cycle_starts_again(self):
        library = make_library()
        p = plan("matinee", "saturday_matinee", datetime(2026, 11, 21, 14, 0, tzinfo=ET), 5.5 * 3600,
                 library, cycle_positions={"beethoven_piano_sonatas": 32})
        numbers = [e.work.cycle_no for e in p.works() if e.work.cycle]
        self.assertEqual(numbers[0], 1)


class CatalogIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.schedule = load_schedule(PROJECT_ROOT / "config" / "wit_next_schedule.yaml")
        self.catalog = Catalog(":memory:")
        self.addCleanup(self.catalog.close)

    def stock(self, hours_wanted: float) -> None:
        total = 0.0
        for work in make_library(beethoven_cycle=False):
            if total >= hours_wanted * 3600:
                break
            self.catalog.add_work(
                kind="classical", title=work.title, source=work.source, license=work.license,
                credit_line=work.credit_line, composer=work.composer, performer=work.performer,
                form=work.form, composition_year=1800, tags=work.tags, gain_db=work.gain_db,
                files=[{"path": f.path, "duration_sec": f.duration_sec, "lossless": True} for f in work.files],
            )
            total += work.duration_sec

    def block(self, wall: datetime):
        return self.schedule.program_block_at(wall)

    def test_blocks_unlock_as_the_library_grows(self):
        self.stock(150)
        night = cp.plan_for_block(self.schedule, self.block(datetime(2026, 11, 18, 23, 30)), self.catalog)
        day = cp.plan_for_block(self.schedule, self.block(datetime(2026, 11, 18, 10, 0)), self.catalog)
        self.assertIsNone(night.fallback_show)
        self.assertTrue(night.works())
        self.assertEqual(day.fallback_show, "monster_mix")  # Day Shift needs 200 h
        self.assertEqual(day.entries, [])
        self.assertIn("needs 200 h", day.warnings[0])

    def test_committed_plans_steer_the_next_night(self):
        self.stock(320)
        first = cp.plan_for_block(self.schedule, self.block(datetime(2026, 11, 18, 23, 30)), self.catalog)
        cp.commit_plan(self.catalog, first)
        second = cp.plan_for_block(self.schedule, self.block(datetime(2026, 11, 19, 23, 30)), self.catalog)
        self.assertTrue(first.works() and second.works())
        self.assertFalse({e.work.id for e in first.works()} & {e.work.id for e in second.works()})
        # Re-committing the same plan replaces it rather than doubling it.
        cp.commit_plan(self.catalog, first)
        rows = self.catalog.db.execute("SELECT COUNT(*) FROM scheduled WHERE plan_id = ?",
                                       (first.plan_id,)).fetchone()[0]
        self.assertEqual(rows, len(first.works()))

    def test_plan_json_carries_what_playout_needs(self):
        self.stock(320)
        p = cp.plan_for_block(self.schedule, self.block(datetime(2026, 11, 18, 21, 0)), self.catalog)
        data = p.to_json()
        self.assertEqual(data["show_id"], "evening_concert")
        self.assertEqual(data["starts_at"], "2026-11-18T20:00:00-05:00")
        work = next(e for e in data["entries"] if e["type"] == "work")
        for key in ("files", "gain_db", "credit", "license", "work_id", "starts_at"):
            self.assertIn(key, work)


if __name__ == "__main__":
    unittest.main()
