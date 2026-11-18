"""The format-hours grid from the show bible, and the schedule features it needs."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from mac.schedule import ScheduleError, load_schedule

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WIT_NEXT = PROJECT_ROOT / "config" / "wit_next_schedule.yaml"
ET = ZoneInfo("America/New_York")

# A week with no DST change: Monday Nov 23, 2026.
QUIET_MONDAY = datetime(2026, 11, 23, 0, 0)

CLASSICAL = {"nocturne", "classical_day_shift", "evening_concert", "saturday_matinee", "sunday_concert_hall"}

WEEKDAY_GRID = [
    ("05:00", "miles_dawn"),
    ("07:00", "monster_mix"),
    ("09:00", "market_opening_bell"),
    ("09:15", "classical_day_shift"),
    ("12:00", "miles_lunch_walk"),
    ("13:00", "the_stacks"),
    ("15:00", "classical_day_shift"),
    ("16:30", "market_closing_tape"),
    ("17:00", "ying_yang"),
    ("18:00", "office_hours"),
    ("19:00", "monster_mix"),
    ("19:30", "bedtime_early"),
    ("20:00", "evening_concert"),
    ("22:00", "bedtime_late"),
    ("23:00", "nocturne"),
]


def _at(day: datetime, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return day.replace(hour=hour, minute=minute)


class FormatHoursTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schedule = load_schedule(WIT_NEXT)

    def test_grid_is_pinned_to_eastern_time(self):
        self.assertEqual(self.schedule.timezone, "America/New_York")

    def test_weekday_grid_matches_the_bible(self):
        for offset in range(5):  # Monday..Friday
            day = QUIET_MONDAY + timedelta(days=offset)
            for hhmm, show_id in WEEKDAY_GRID:
                with self.subTest(day=day.strftime("%a"), at=hhmm):
                    self.assertEqual(self.schedule.resolve(_at(day, hhmm)).show_id, show_id)

    def test_weekend_grids_match_the_bible(self):
        saturday = QUIET_MONDAY + timedelta(days=5)
        sunday = QUIET_MONDAY + timedelta(days=6)
        expected = {
            saturday: [
                ("05:00", "nocturne"), ("07:00", "miles_long_slow_saturday"),
                ("08:30", "monster_mix"), ("10:00", "ying_yang_receipts"),
                ("11:00", "stacks_crate_dig"), ("13:00", "exam_cram"),
                ("14:00", "saturday_matinee"), ("19:30", "bedtime_early"),
                ("20:00", "evening_concert"), ("22:00", "bedtime_late"),
                ("23:00", "nocturne"),
            ],
            sunday: [
                ("05:00", "nocturne"), ("08:00", "miles_recovery_sunday"),
                ("08:30", "monster_mix"), ("11:00", "sunday_concert_hall"),
                ("17:00", "market_week_ahead"), ("17:30", "monster_mix"),
                ("19:30", "bedtime_early"), ("20:00", "evening_concert"),
                ("22:00", "bedtime_late"), ("23:00", "nocturne"),
            ],
        }
        for day, grid in expected.items():
            for hhmm, show_id in grid:
                with self.subTest(day=day.strftime("%a"), at=hhmm):
                    self.assertEqual(self.schedule.resolve(_at(day, hhmm)).show_id, show_id)

    def test_weekly_hours(self):
        hours = self.schedule.weekly_hours(QUIET_MONDAY)
        self.assertAlmostEqual(sum(hours.values()), 168.0)
        classical = sum(h for show_id, h in hours.items() if show_id in CLASSICAL)
        self.assertAlmostEqual(classical, 93.75)  # 56% of the week
        self.assertAlmostEqual(hours["monster_mix"], 18.5)
        self.assertAlmostEqual(hours["classical_day_shift"], 21.25)  # 4h15 x 5

    def test_every_classical_show_is_a_classical_program(self):
        for show_id in CLASSICAL:
            show = self.schedule.shows[show_id]
            self.assertEqual(show.program, "classical", show_id)
            self.assertEqual(show.host, "count", show_id)
            self.assertEqual(show.params["fallback_show"], "monster_mix", show_id)
        self.assertEqual(self.schedule.shows["nocturne"].params["unlock_hours"], 100)
        self.assertEqual(self.schedule.shows["evening_concert"].params["unlock_hours"], 100)
        self.assertEqual(self.schedule.shows["classical_day_shift"].params["unlock_hours"], 200)
        self.assertEqual(self.schedule.shows["saturday_matinee"].params["unlock_hours"], 300)
        self.assertEqual(self.schedule.shows["sunday_concert_hall"].params["unlock_hours"], 300)

    def test_weekend_nocturnes_merge_into_one_block(self):
        friday_night = QUIET_MONDAY + timedelta(days=4, hours=23)
        block = self.schedule.program_block_at(friday_night + timedelta(hours=7))  # Sat 06:00
        self.assertEqual(block.show_id, "nocturne")
        self.assertEqual(block.start, friday_night)
        self.assertEqual(block.end, friday_night + timedelta(hours=8))  # Sat 07:00
        self.assertEqual(block.hours, 8.0)

        saturday_night = friday_night + timedelta(days=1)
        block = self.schedule.program_block_at(saturday_night + timedelta(hours=1))
        self.assertEqual(block.end, saturday_night + timedelta(hours=9))  # Sun 08:00

        weekday = self.schedule.program_block_at(QUIET_MONDAY + timedelta(days=1, hours=2))
        self.assertEqual(weekday.hours, 6.0)

    def test_block_titles(self):
        sunday = QUIET_MONDAY + timedelta(days=6)
        self.assertEqual(self.schedule.resolve(_at(sunday, "06:00")).title, "Nocturne: Sunday Sacred")
        self.assertEqual(self.schedule.resolve(_at(sunday, "09:00")).title, "Monster Mix: Sunday Brunch")
        self.assertEqual(self.schedule.resolve(_at(sunday, "18:00")).title, "Monster Mix")

    def test_spring_forward_night_is_an_hour_shorter(self):
        # DST starts 2:00 AM Sunday March 14, 2027, inside Saturday's Nocturne.
        saturday_night = datetime(2027, 3, 13, 23, 0)
        block = self.schedule.program_block_at(saturday_night)
        self.assertEqual(block.start, saturday_night)
        self.assertEqual(block.end, datetime(2027, 3, 14, 8, 0))
        self.assertEqual(block.hours, 8.0)  # 9 hours on the clock

    def test_fall_back_night_is_an_hour_longer(self):
        # DST ends 2:00 AM Sunday November 7, 2027.
        block = self.schedule.program_block_at(datetime(2027, 11, 6, 23, 30))
        self.assertEqual(block.hours, 10.0)

    def test_aware_times_are_converted_to_eastern(self):
        # 22:00 UTC on Nov 18, 2026 is 17:00 Eastern: Ying & Yang.
        utc = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(self.schedule.resolve(utc).show_id, "ying_yang")
        self.assertEqual(self.schedule.to_wall(utc), datetime(2026, 11, 18, 17, 0))

    def test_next_airings_catches_short_blocks(self):
        airings = self.schedule.next_airings(now=datetime(2026, 11, 18, 8, 58), count=3)
        self.assertEqual(
            airings,
            [
                ("monster_mix", datetime(2026, 11, 18, 7, 0)),
                ("market_opening_bell", datetime(2026, 11, 18, 9, 0)),
                ("classical_day_shift", datetime(2026, 11, 18, 9, 15)),
            ],
        )


class ScheduleValidationTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        tmp.write(body)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    SHOWS = """
shows:
  a: {name: A, description: A show}
  b: {name: B, description: B show}
"""

    def test_overlapping_overrides_are_rejected(self):
        path = self._write(self.SHOWS + """
schedule:
  base:
    - {start: "00:00", end: "12:00", show: a}
    - {start: "12:00", end: "00:00", show: a}
  overrides:
    - {days: [sat], start: "10:00", end: "12:00", show: b}
    - {days: [weekend], start: "11:00", end: "13:00", show: a}
""")
        with self.assertRaises(ScheduleError) as ctx:
            load_schedule(path)
        self.assertIn("sat at 11:00", str(ctx.exception))

    def test_unknown_timezone_is_rejected(self):
        path = self._write("timezone: Mars/Olympus_Mons\n" + self.SHOWS + """
schedule:
  base:
    - {start: "00:00", end: "12:00", show: a}
    - {start: "12:00", end: "00:00", show: b}
""")
        with self.assertRaises(ScheduleError):
            load_schedule(path)

    def test_local_timezone_keeps_naive_behaviour(self):
        path = self._write("timezone: local\n" + self.SHOWS + """
schedule:
  base:
    - {start: "00:00", end: "12:00", show: a}
    - {start: "12:00", end: "00:00", show: b}
""")
        schedule = load_schedule(path)
        self.assertIsNone(schedule.tz)
        self.assertEqual(schedule.resolve(datetime(2026, 1, 1, 13, 0)).show_id, "b")
        self.assertEqual(schedule.shows["a"].segment_types, ["deep_dive"])

    def test_live_schedules_still_load(self):
        for name in ("schedule.yaml", "cdex_schedule.yaml"):
            with self.subTest(name=name):
                load_schedule(PROJECT_ROOT / "config" / name)


if __name__ == "__main__":
    unittest.main()
