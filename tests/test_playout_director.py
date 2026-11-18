"""The playout director's decisions, on a fixed clock."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mac.catalog import Catalog
from mac.playout_director import Director, Item
from mac.schedule import load_schedule

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
HAVE_FFMPEG = shutil.which("ffmpeg") is not None
BLOCK_START = datetime(2026, 11, 18, 9, 15, tzinfo=ET)  # Classical: Day Shift, a Wednesday
PLAN_ID = "classical_day_shift@2026-11-18T1415Z"


def tone(path: Path, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:sample_rate=44100:duration={seconds}", "-ac", "2", str(path)], check=True)
    return path


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class DirectorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        out = self.root / "out"
        self.station = SimpleNamespace(
            id="wit-next", call_sign="WIT-FM Next", output_dir=out, runtime_dir=out / "runtime",
            library_dir=out / "library", now_playing_file=out / "now_playing.json",
            stream=SimpleNamespace(mount="/next"),
        )
        self.schedule = load_schedule(PROJECT_ROOT / "config" / "wit_next_schedule.yaml")
        self.catalog = Catalog(self.root / "catalog.db")
        self.addCleanup(self.catalog.close)
        self.works = {}
        for key, seconds, gain, files in (("a", 100, 0.0, 2), ("b", 120, -3.0, 1), ("c", 200, 0.0, 1),
                                           ("d", 90, -1.0, 1)):
            paths = [f"/library/{key}{i}.flac" for i in range(files)]
            each = seconds / files
            work_id = self.catalog.add_work(
                kind="classical", title=f"Work {key.upper()}", composer=f"Composer {key.upper()}",
                composition_year=1800, performer="Ensemble", source="test", license="cc0",
                credit_line=f"Ensemble (credit {key})", gain_db=gain,
                files=[{"path": p, "duration_sec": each, "lossless": True} for p in paths])
            self.works[key] = self.catalog.work(work_id)
        env = patch.dict(os.environ, {"WRIT_FORCE_CLASSICAL": "1"})
        env.start()
        self.addCleanup(env.stop)

    def director(self) -> Director:
        return Director(self.station, schedule=self.schedule, catalog_path=self.root / "catalog.db")

    def write_plan(self, first_at: datetime, entries: list[tuple]) -> None:
        rows, t = [], first_at
        for spec in entries:
            kind, role, seconds = spec[:3]
            row = {"type": kind, "role": role, "starts_at": t.astimezone(timezone.utc).isoformat(),
                   "offset": 0, "seconds": seconds}
            if kind == "work":
                w = self.works[spec[3]]
                row.update({"work_id": w.id, "title": w.title, "composer": w.composer, "performer": w.performer,
                            "credit": w.credit_line, "license": w.license, "gain_db": w.gain_db,
                            "lossless": True, "files": [f.path for f in w.files]})
            rows.append(row)
            t += timedelta(seconds=seconds)
        plans = self.station.output_dir / "buffer" / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        (plans / f"{PLAN_ID}.json").write_text(json.dumps({"plan_id": PLAN_ID, "ok": True, "entries": rows}))

    def run_items(self, director: Director, now: datetime, count: int, air: bool = True) -> list[Item]:
        items = []
        for _ in range(count):
            item = director.next_item(now)
            items.append(item)
            if air:
                director.aired(item.entry_id, item.path, now=now)
            now += timedelta(seconds=item.seconds)
        return items

    STANDARD = [("work", "day_shift", 100, "a"), ("gap", "gap", 3), ("talk", "back_announce", 20),
                ("gap", "gap", 1), ("work", "day_shift", 120, "b"), ("gap", "gap", 2),
                ("id", "hourly_id", 8), ("gap", "gap", 1), ("work", "fit", 200, "c"), ("gap", "gap", 4),
                ("talk", "closing", 45)]

    def test_plan_entries_play_in_order_with_gain_and_gaps(self):
        start = datetime(2026, 11, 18, 10, 0, tzinfo=ET)
        self.write_plan(start, self.STANDARD)
        items = self.run_items(self.director(), start, 7)
        summary = [(i.kind, i.title, round(i.seconds, 1)) for i in items]
        self.assertEqual(summary, [
            ("work", "Work A", 50.0), ("work", "Work A", 50.0),  # movements back to back
            ("gap", "", 3.0),
            ("work", "Work B", 120.0),  # the unrendered link and its gap were skipped
            ("gap", "", 2.0),
            ("work", "Work C", 200.0),  # no station ID files yet: skipped
            ("gap", "", 4.0),
        ])
        self.assertEqual([i.entry_id for i in items[:2]], [f"{PLAN_ID}#0.0", f"{PLAN_ID}#0.1"])
        self.assertIn('liq_amplify="-3.00 dB"', items[3].annotate())
        self.assertIn('witfm_lossless="true"', items[0].annotate())
        self.assertTrue(all(i.show_id == "classical_day_shift" for i in items))

    def test_after_the_plan_a_fitting_unaired_work_fills(self):
        start = datetime(2026, 11, 18, 10, 0, tzinfo=ET)
        self.write_plan(start, self.STANDARD)
        items = self.run_items(self.director(), start, 8)
        filler = items[-1]
        self.assertEqual((filler.kind, filler.title), ("filler", "Work D"))  # A-C aired minutes ago
        self.assertEqual(filler.credit, "Ensemble (credit d)")

    def test_running_late_drops_fit_pieces_and_never_overruns_the_post(self):
        # Fifty minutes before the noon post; playback falls 90 s behind the plan.
        start = datetime(2026, 11, 18, 11, 10, tzinfo=ET)
        self.write_plan(start, [("work", "day_shift", 100, "a"), ("work", "fit", 200, "c"),
                                ("work", "day_shift", 3000, "b")])
        director = self.director()
        first = director.next_item(start)
        director.next_item(start)  # the second movement, prefetched
        director.aired(first.entry_id, first.path, now=start + timedelta(seconds=90))
        nxt = director.next_item(start + timedelta(seconds=100))
        # The fit piece is now 90 s late, so it is dropped, and the 50-minute
        # work would overrun noon: neither airs from the plan. A filler that
        # fits before the post takes the time.
        self.assertEqual(nxt.kind, "filler")
        post = datetime(2026, 11, 18, 12, 0, tzinfo=ET)
        starts = datetime.fromisoformat(nxt.starts_at)
        self.assertLessEqual(starts + timedelta(seconds=nxt.seconds), post)

    def test_joining_mid_block_starts_at_the_next_entry(self):
        start = datetime(2026, 11, 18, 10, 0, tzinfo=ET)
        self.write_plan(start, self.STANDARD)
        item = self.director().next_item(start + timedelta(seconds=110))  # A is under way
        self.assertEqual(item.title, "Work B")

    def test_aired_writes_now_playing_and_airplay_and_resyncs(self):
        start = datetime(2026, 11, 18, 10, 0, tzinfo=ET)
        self.write_plan(start, self.STANDARD)
        director = self.director()
        first = director.next_item(start)
        director.next_item(start)  # prefetched
        actual = start + timedelta(seconds=7)  # playback started late
        info = director.aired(first.entry_id, first.path, now=actual)
        self.assertEqual(info["credit"], "Ensemble (credit a)")
        self.assertTrue(info["lossless"])
        self.assertEqual(info["show"], "Classical: Day Shift")
        on_disk = json.loads(self.station.now_playing_file.read_text())
        self.assertEqual(on_disk["track"], "Composer A: Work A")
        state = json.loads(director.state_path.read_text())
        self.assertEqual(state["cursor"], (actual + timedelta(seconds=100)).astimezone(timezone.utc).isoformat(timespec="seconds"))
        rows = self.catalog.db.execute("SELECT path, license FROM airplay").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("/library/a0.flac", "cc0")])

    def test_locked_block_runs_the_fallback_show_from_buffer_then_safe_then_room_tone(self):
        os.environ.pop("WRIT_FORCE_CLASSICAL")  # 510 s of music is far under 200 h
        now = datetime(2026, 11, 18, 10, 0, tzinfo=ET)
        slot = "2026-11-18_0915"
        buffer = self.station.output_dir / "buffer" / "monster_mix" / slot
        tone(buffer / "01_track.flac", 2)
        (buffer / "01_track.json").write_text(json.dumps({"title": "Drift One", "credit": "Generated for WIT-FM",
                                                           "license": "generated", "gain_db": -2.0}))
        tone(self.station.library_dir / "air" / "safe" / "some-track" / "01-safe.flac", 3)
        director = self.director()
        items = self.run_items(director, now, 3)
        self.assertEqual([(i.kind, i.show_id) for i in items[:2]], [("buffer", "monster_mix"), ("safe", "monster_mix")])
        self.assertEqual(items[0].title, "Drift One")
        self.assertEqual(items[0].gain_db, -2.0)
        self.assertEqual(items[2].kind, "safe")  # the only safe file repeats rather than dead air

        shutil.rmtree(self.station.library_dir / "air" / "safe")
        roomtone = director.next_item(now + timedelta(hours=1))
        self.assertEqual(roomtone.kind, "roomtone")
        self.assertLessEqual(roomtone.seconds, 30)
        self.assertTrue(Path(roomtone.path).exists())

    def test_host_is_credited_on_talk_only(self):
        music = Item(kind="safe", path="/a.flac", seconds=1, title="Night Drive", host="wit", entry_id="e")
        talk = Item(kind="talk", path="/b.flac", seconds=1, title="Back announce", host="count", entry_id="f")
        self.assertNotIn("artist=", music.annotate())
        self.assertIn('artist="count"', talk.annotate())

    def test_annotation_escapes_quotes(self):
        item = Item(kind="work", path="/a b/c.flac", seconds=1, title='Say "hi" \\ there', composer="C",
                    credit="X", entry_id="e")
        self.assertIn('title="C: Say \\"hi\\" \\\\ there"', item.annotate())
        self.assertTrue(item.annotate().endswith(":/a b/c.flac"))


if __name__ == "__main__":
    unittest.main()
