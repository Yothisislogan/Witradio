#!/usr/bin/env python3
"""
WRIT-FM Weekly Scheduling

Loads `config/schedule.yaml` and resolves the currently-active show based on:
- day of week (mon..sun)
- wall-clock time (HH:MM) in the schedule's `timezone`

`timezone: local` (the default) uses the server's local time. An IANA name
such as `America/New_York` pins the grid to that zone no matter where the
server runs. Times passed in without tzinfo are read as wall-clock times in
the schedule's zone; aware datetimes are converted.

The streamer can use this to:
- pick the host persona and topic focus
- determine segment types for the show
- select bumper music style for breaks
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone, tzinfo
import os
from pathlib import Path
import re
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_TO_INDEX = {k: i for i, k in enumerate(DAY_KEYS)}
INDEX_TO_DAY = {i: k for k, i in DAY_TO_INDEX.items()}

VALID_SEGMENT_TYPES = {
    "deep_dive", "news_analysis", "interview", "panel", "story",
    "listener_mailbag", "listener_response", "music_essay",
    "station_id", "show_intro", "show_outro",
}


class ScheduleError(RuntimeError):
    pass


LOCAL_TIMEZONE = "local"


def _parse_timezone(value: Any) -> tuple[str, tzinfo | None]:
    """Return (name, tzinfo). tzinfo is None for the server's local time."""
    if value is None:
        return LOCAL_TIMEZONE, None
    name = str(value).strip()
    if not name or name.lower() == LOCAL_TIMEZONE:
        return LOCAL_TIMEZONE, None
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"Unknown timezone {name!r} (use an IANA name or 'local')") from exc


def _parse_time_hhmm(value: str) -> int:
    if not isinstance(value, str):
        raise ScheduleError(f"Invalid time (expected HH:MM string): {value!r}")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not m:
        raise ScheduleError(f"Invalid time (expected HH:MM): {value!r}")
    hour = int(m.group(1))
    minute = int(m.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ScheduleError(f"Invalid time (out of range): {value!r}")
    return hour * 60 + minute


def _normalize_day_token(token: str) -> str:
    t = token.strip().lower()
    aliases = {
        "monday": "mon",
        "tuesday": "tue",
        "wednesday": "wed",
        "thursday": "thu",
        "friday": "fri",
        "saturday": "sat",
        "sunday": "sun",
    }
    return aliases.get(t, t)


def _parse_days(value: Any) -> set[int]:
    if value is None:
        raise ScheduleError("Missing required field: days")
    if not isinstance(value, list) or not value:
        raise ScheduleError(f"Invalid days (expected non-empty list): {value!r}")

    expanded: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise ScheduleError(f"Invalid day token: {raw!r}")
        tok = _normalize_day_token(raw)
        if tok in ("daily", "all"):
            expanded.extend(list(DAY_KEYS))
            continue
        if tok == "weekday":
            expanded.extend(["mon", "tue", "wed", "thu", "fri"])
            continue
        if tok == "weekend":
            expanded.extend(["sat", "sun"])
            continue
        expanded.append(tok)

    days: set[int] = set()
    for tok in expanded:
        if tok not in DAY_TO_INDEX:
            raise ScheduleError(f"Invalid day token: {tok!r}")
        days.add(DAY_TO_INDEX[tok])
    return days


def _expand_minutes(start_minute: int, end_minute: int) -> list[tuple[int, int]]:
    if start_minute == end_minute:
        raise ScheduleError("Schedule block start and end cannot be the same")
    if 0 <= start_minute < 1440 and 0 <= end_minute < 1440:
        if end_minute > start_minute:
            return [(start_minute, end_minute)]
        # Cross-midnight: split into two ranges
        return [(start_minute, 1440), (0, end_minute)]
    raise ScheduleError("Schedule block times out of range")


@dataclass(frozen=True)
class Show:
    show_id: str
    name: str
    description: str
    host: str = "liminal_operator"
    topic_focus: str = ""
    segment_types: list[str] = field(default_factory=lambda: ["deep_dive"])
    bumper_style: str = "ambient"
    voices: dict[str, str] = field(default_factory=dict)
    # Legacy fields (optional, unused in the current music-forward mode)
    segment_after_tracks: int = 1
    podcasts_enabled: bool = False
    music: dict[str, Any] = field(default_factory=dict)
    # Format-hours shows: which producer runs the show (classical, monster_mix,
    # miles, ...) and that producer's settings.
    program: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScheduleBlock:
    start_minute: int
    end_minute: int
    show_id: str
    days: set[int] | None = None  # None => every day (base)
    title: str | None = None  # on-air title for this slot, e.g. "Nocturne: Sunday Sacred"

    def is_cross_midnight(self) -> bool:
        return self.end_minute < self.start_minute

    def matches(self, now: datetime) -> bool:
        minute = now.hour * 60 + now.minute
        day = now.weekday()  # mon=0
        prev_day = (day - 1) % 7

        if self.days is None:
            # Base clock: day-agnostic
            if self.end_minute > self.start_minute:
                return self.start_minute <= minute < self.end_minute
            return minute >= self.start_minute or minute < self.end_minute

        # Overrides: day-aware, including cross-midnight behavior.
        if self.end_minute > self.start_minute:
            return day in self.days and self.start_minute <= minute < self.end_minute

        # Cross-midnight: belongs to the start day; continues into next day.
        return (day in self.days and minute >= self.start_minute) or (
            prev_day in self.days and minute < self.end_minute
        )


@dataclass(frozen=True)
class ResolvedShow:
    show_id: str
    name: str
    description: str
    host: str
    topic_focus: str
    segment_types: list[str]
    bumper_style: str
    voices: dict[str, str]
    # Legacy (kept for backward compat)
    segment_after_tracks: int = 1
    podcasts_enabled: bool = False
    podcast_hours: set[int] = field(default_factory=set)
    music_profile: dict[str, Any] = field(default_factory=dict)
    title: str = ""  # block title if the slot has one, else the show name
    program: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgramBlock:
    """A contiguous run of one show, merged across schedule-block boundaries.

    Friday's 23:00-05:00 Nocturne and Saturday's 05:00-07:00 extension are one
    ProgramBlock from Friday 23:00 to Saturday 07:00. `start` and `end` are
    wall-clock times in the schedule's zone; `seconds` is real elapsed time,
    so a block that spans a DST change is an hour shorter or longer than its
    wall-clock span.
    """

    show_id: str
    start: datetime
    end: datetime
    seconds: float
    title: str

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0


@dataclass
class StationSchedule:
    shows: dict[str, Show]
    base: list[ScheduleBlock]
    overrides: list[ScheduleBlock]
    podcast_hours: set[int] = field(default_factory=set)
    timezone: str = LOCAL_TIMEZONE
    tz: tzinfo | None = None

    # ── time helpers ────────────────────────────────────────────

    def now(self) -> datetime:
        """Current wall-clock time in the schedule's zone (naive)."""
        if self.tz is None:
            return datetime.now()
        return datetime.now(self.tz).replace(tzinfo=None)

    def to_wall(self, when: datetime | None = None) -> datetime:
        """Naive wall-clock time in the schedule's zone for `when`."""
        if when is None:
            return self.now()
        if when.tzinfo is None:
            return when
        if self.tz is None:
            return when.astimezone().replace(tzinfo=None)
        return when.astimezone(self.tz).replace(tzinfo=None)

    def localize(self, wall: datetime) -> datetime:
        """Attach the schedule's zone to a naive wall-clock time."""
        if wall.tzinfo is not None:
            return wall
        if self.tz is None:
            return wall.astimezone()
        return wall.replace(tzinfo=self.tz)

    def real_seconds(self, start_wall: datetime, end_wall: datetime) -> float:
        """Elapsed seconds between two wall-clock times, honouring DST."""
        a = self.localize(start_wall).astimezone(dt_timezone.utc)
        b = self.localize(end_wall).astimezone(dt_timezone.utc)
        return (b - a).total_seconds()

    def validate(self) -> None:
        if not self.base:
            raise ScheduleError("schedule.base is empty")

        # Base coverage: every minute must be covered exactly once.
        coverage = [0] * 1440
        for block in self.base:
            for a, b in _expand_minutes(block.start_minute, block.end_minute):
                for m in range(a, b):
                    coverage[m] += 1

        uncovered = [i for i, c in enumerate(coverage) if c == 0]
        if uncovered:
            first = uncovered[0]
            raise ScheduleError(
                f"schedule.base does not cover the full day (first gap at {first // 60:02d}:{first % 60:02d})"
            )

        overlapped = [i for i, c in enumerate(coverage) if c > 1]
        if overlapped:
            first = overlapped[0]
            raise ScheduleError(
                f"schedule.base overlaps itself (first overlap at {first // 60:02d}:{first % 60:02d})"
            )

        # Overrides on the same day must not overlap: the first match would
        # silently win and hide the other block.
        week = [[None] * 1440 for _ in range(7)]
        for block in self.overrides:
            for day in sorted(block.days or ()):
                spans: list[tuple[int, int, int]] = []
                if block.end_minute > block.start_minute:
                    spans.append((day, block.start_minute, block.end_minute))
                else:
                    spans.append((day, block.start_minute, 1440))
                    spans.append(((day + 1) % 7, 0, block.end_minute))
                for d, a, b in spans:
                    for m in range(a, b):
                        if week[d][m] is not None:
                            raise ScheduleError(
                                f"schedule.overrides overlap on {INDEX_TO_DAY[d]} at "
                                f"{m // 60:02d}:{m % 60:02d} ({week[d][m]} and {block.show_id})"
                            )
                        week[d][m] = block.show_id

        # Show references exist
        for block in self.base + self.overrides:
            if block.show_id not in self.shows:
                raise ScheduleError(f"Schedule references unknown show: {block.show_id!r}")

        # Validate show configs
        for show in self.shows.values():
            if show.host:
                # Validate host exists in persona system (soft check - just verify non-empty)
                pass
            for st in show.segment_types:
                if st not in VALID_SEGMENT_TYPES:
                    raise ScheduleError(
                        f"Show {show.show_id}: unknown segment type {st!r}. "
                        f"Valid: {sorted(VALID_SEGMENT_TYPES)}"
                    )

    def _matching_block(self, now: datetime) -> ScheduleBlock:
        for block in self.overrides:
            if block.matches(now):
                return block
        for block in self.base:
            if block.matches(now):
                return block
        raise ScheduleError("No matching schedule block for current time (base clock may be invalid)")

    def resolve(self, now: datetime | None = None) -> ResolvedShow:
        now = self.to_wall(now)
        block = self._matching_block(now)
        show = self.shows[block.show_id]
        return ResolvedShow(
            show_id=show.show_id,
            name=show.name,
            description=show.description,
            host=show.host,
            topic_focus=show.topic_focus,
            segment_types=list(show.segment_types),
            bumper_style=show.bumper_style,
            voices=dict(show.voices),
            title=block.title or show.name,
            program=show.program,
            params=dict(show.params),
        )

    def airing_start(self, now: datetime | None = None) -> datetime:
        """Return the datetime when the currently-matching block began airing."""
        now = self.to_wall(now)
        block = self._matching_block(now)
        start_h, start_m = divmod(block.start_minute, 60)
        minute = now.hour * 60 + now.minute

        # Non-cross-midnight block: airing started today at start time
        if block.end_minute > block.start_minute:
            return now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)

        # Cross-midnight block
        if minute >= block.start_minute:
            # We're in the start-day portion
            return now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        # We're past midnight, in the continuation portion
        yesterday = now - timedelta(days=1)
        return yesterday.replace(hour=start_h, minute=start_m, second=0, microsecond=0)

    def next_airings(
        self,
        now: datetime | None = None,
        count: int = 4,
        horizon_hours: int = 48,
    ) -> list[tuple[str, datetime]]:
        """Return the next `count` airings as (show_id, start_datetime).

        The currently-active airing is the first entry. Only schedule-block
        boundaries and midnights can change the airing, so it checks those
        instead of stepping through the clock.
        """
        now = self.to_wall(now)
        airings: list[tuple[str, datetime]] = []
        last_key: tuple[str, datetime] | None = None
        end = now + timedelta(hours=horizon_hours)
        for t, _ in self._change_points(now, end):
            if len(airings) >= count:
                break
            block = self._matching_block(t)
            key = (block.show_id, self.airing_start(t))
            if key != last_key:
                airings.append(key)
                last_key = key
        return airings

    def _boundary_minutes(self) -> list[int]:
        minutes = {0}
        for block in self.base + self.overrides:
            minutes.add(block.start_minute)
            minutes.add(block.end_minute % 1440)
        return sorted(minutes)

    def _change_points(self, start: datetime, end: datetime) -> Iterator[tuple[datetime, ScheduleBlock]]:
        """Yield (time, block) at `start` and at every boundary in (start, end).

        Between two consecutive change points the matching block cannot change.
        """
        start = start.replace(second=0, microsecond=0)
        yield start, self._matching_block(start)
        minutes = self._boundary_minutes()
        day = start.replace(hour=0, minute=0)
        while day < end:
            for minute in minutes:
                t = day + timedelta(minutes=minute)
                if start < t < end:
                    yield t, self._matching_block(t)
            day += timedelta(days=1)

    def program_blocks(self, start: datetime, end: datetime) -> list[ProgramBlock]:
        """Contiguous runs of each show between `start` and `end` (wall clock).

        Runs are clipped to the window, so pass a window wider than the blocks
        you care about. Adjacent schedule blocks with the same show merge.
        """
        start = self.to_wall(start)
        end = self.to_wall(end)
        if end <= start:
            return []
        runs: list[list[Any]] = []  # [show_id, start, end, title]
        for t, block in self._change_points(start, end):
            title = block.title or self.shows[block.show_id].name
            if runs and runs[-1][0] == block.show_id:
                continue
            if runs:
                runs[-1][2] = t
            runs.append([block.show_id, t, end, title])
        return [
            ProgramBlock(
                show_id=show_id,
                start=a,
                end=b,
                seconds=self.real_seconds(a, b),
                title=title,
            )
            for show_id, a, b, title in runs
        ]

    def program_block_at(self, when: datetime | None = None) -> ProgramBlock:
        """The full merged block airing at `when` (looks up to 2 days each way)."""
        when = self.to_wall(when)
        for block in self.program_blocks(when - timedelta(days=2), when + timedelta(days=2)):
            if block.start <= when < block.end:
                return block
        raise ScheduleError("No program block found around the requested time")

    def weekly_hours(self, monday: datetime | None = None) -> dict[str, float]:
        """Real hours per show_id over one Monday-00:00 to Monday-00:00 week."""
        if monday is None:
            today = self.now()
            monday = (today - timedelta(days=today.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        totals: dict[str, float] = {}
        for block in self.program_blocks(monday, monday + timedelta(days=7)):
            totals[block.show_id] = totals.get(block.show_id, 0.0) + block.hours
        return totals


def slot_key(airing_start: datetime) -> str:
    """Canonical slot folder name for an airing."""
    return airing_start.strftime("%Y-%m-%d_%H%M")


SLOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}$")


def parse_slot_key(key: str) -> datetime:
    """Inverse of slot_key; raises ValueError if malformed."""
    if not SLOT_RE.match(key):
        raise ValueError(f"Not a slot key: {key!r}")
    return datetime.strptime(key, "%Y-%m-%d_%H%M")


def load_schedule(path: Path) -> StationSchedule:
    try:
        payload = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise ScheduleError(f"Failed to read schedule YAML: {exc}") from exc

    if not isinstance(payload, dict):
        raise ScheduleError("Schedule YAML must be a mapping at the top level")

    shows_raw = payload.get("shows")
    if not isinstance(shows_raw, dict) or not shows_raw:
        raise ScheduleError("Missing or invalid `shows` section")

    shows: dict[str, Show] = {}
    for show_id, cfg in shows_raw.items():
        if not isinstance(show_id, str) or not show_id.strip():
            raise ScheduleError(f"Invalid show id: {show_id!r}")
        if not isinstance(cfg, dict):
            raise ScheduleError(f"Show {show_id}: config must be a mapping")
        name = str(cfg.get("name", "")).strip()
        description = str(cfg.get("description", "")).strip()
        if not name or not description:
            raise ScheduleError(f"Show {show_id}: missing name/description")

        # Format-hours shows name the producer that runs them.
        program = str(cfg.get("program", "") or "").strip()
        params_raw = cfg.get("params", {})
        if params_raw is None:
            params_raw = {}
        if not isinstance(params_raw, dict):
            raise ScheduleError(f"Show {show_id}: params must be a mapping")

        # Talk-show fields
        host = str(cfg.get("host", "liminal_operator")).strip()
        topic_focus = str(cfg.get("topic_focus", "")).strip()
        segment_types_raw = cfg.get("segment_types", [] if program else ["deep_dive"])
        if not isinstance(segment_types_raw, list):
            raise ScheduleError(f"Show {show_id}: segment_types must be a list")
        segment_types = [str(s).strip() for s in segment_types_raw]
        bumper_style = str(cfg.get("bumper_style", "ambient")).strip()

        # Voice config
        voices = cfg.get("voices") if isinstance(cfg.get("voices"), dict) else {}

        # Legacy fields (optional)
        segment_after_tracks = int(cfg.get("segment_after_tracks", 1))
        podcasts_enabled = bool(cfg.get("podcasts_enabled", False))
        music = cfg.get("music") if isinstance(cfg.get("music"), dict) else {}

        shows[show_id] = Show(
            show_id=show_id,
            name=name,
            description=description,
            host=host,
            topic_focus=topic_focus,
            segment_types=segment_types,
            bumper_style=bumper_style,
            voices={str(k): str(v) for k, v in voices.items()},
            segment_after_tracks=segment_after_tracks,
            podcasts_enabled=podcasts_enabled,
            music=dict(music) if music else {},
            program=program,
            params=dict(params_raw),
        )

    # Legacy podcasts config (optional)
    podcasts_cfg = payload.get("podcasts") if isinstance(payload.get("podcasts"), dict) else {}
    hours_raw = podcasts_cfg.get("hours", [])
    if hours_raw is None:
        hours_raw = []
    if not isinstance(hours_raw, list):
        raise ScheduleError("podcasts.hours must be a list of integers")
    podcast_hours: set[int] = set()
    for item in hours_raw:
        if not isinstance(item, int):
            raise ScheduleError(f"podcasts.hours contains non-int value: {item!r}")
        podcast_hours.add(item)

    sched = payload.get("schedule")
    if not isinstance(sched, dict):
        raise ScheduleError("Missing or invalid `schedule` section")

    base_raw = sched.get("base")
    if not isinstance(base_raw, list) or not base_raw:
        raise ScheduleError("schedule.base must be a non-empty list")

    overrides_raw = sched.get("overrides", [])
    if overrides_raw is None:
        overrides_raw = []
    if not isinstance(overrides_raw, list):
        raise ScheduleError("schedule.overrides must be a list")

    def _parse_block(cfg: Any, *, day_aware: bool) -> ScheduleBlock:
        if not isinstance(cfg, dict):
            raise ScheduleError(f"Schedule block must be a mapping: {cfg!r}")
        start = _parse_time_hhmm(str(cfg.get("start", "")))
        end = _parse_time_hhmm(str(cfg.get("end", "")))
        show_id = str(cfg.get("show", "")).strip()
        if not show_id:
            raise ScheduleError("Schedule block missing `show`")
        days = _parse_days(cfg.get("days")) if day_aware else None
        title = str(cfg.get("title", "") or "").strip() or None
        return ScheduleBlock(
            start_minute=start, end_minute=end, show_id=show_id, days=days, title=title
        )

    base_blocks = [_parse_block(item, day_aware=False) for item in base_raw]
    override_blocks = [_parse_block(item, day_aware=True) for item in overrides_raw]

    tz_name, tz = _parse_timezone(payload.get("timezone"))

    schedule = StationSchedule(
        shows=shows,
        base=base_blocks,
        overrides=override_blocks,
        podcast_hours=podcast_hours,
        timezone=tz_name,
        tz=tz,
    )
    schedule.validate()
    return schedule


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="WRIT-FM schedule tools")
    parser.add_argument(
        "--schedule",
        default=os.environ.get(
            "WRIT_SCHEDULE_PATH",
            str(Path(__file__).resolve().parents[1] / "config" / "schedule.yaml"),
        ),
        help="Path to schedule.yaml",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="Validate schedule file")

    p_now = sub.add_parser("now", help="Print current show")
    p_now.add_argument("--at", help="Override time (YYYY-MM-DD HH:MM)")

    sub.add_parser("shows", help="List all shows")

    sub.add_parser("week", help="Print the merged weekly grid and hours per show")

    args = parser.parse_args()

    schedule = load_schedule(Path(args.schedule).expanduser())
    if args.cmd == "validate":
        print("OK")
        return 0

    if args.cmd == "shows":
        for sid, show in schedule.shows.items():
            print(f"  {sid:25s} host={show.host:20s} {show.name}")
        return 0

    if args.cmd == "week":
        today = schedule.now()
        monday = (today - timedelta(days=today.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        print(f"Timezone: {schedule.timezone}")
        for block in schedule.program_blocks(monday, monday + timedelta(days=7)):
            print(
                f"  {INDEX_TO_DAY[block.start.weekday()]} {block.start:%H:%M}-{block.end:%H:%M}"
                f"  {block.hours:5.2f}h  {block.title}"
            )
        print("Hours per show:")
        for show_id, hours in sorted(schedule.weekly_hours(monday).items(), key=lambda kv: -kv[1]):
            print(f"  {show_id:30s} {hours:6.2f}")
        return 0

    when = schedule.now()
    if args.cmd == "now" and args.at:
        try:
            when = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
        except Exception as exc:
            raise ScheduleError(f"Invalid --at format: {exc}") from exc

    resolved = schedule.resolve(when)
    print(f"{INDEX_TO_DAY[when.weekday()]} {when:%H:%M} -- {resolved.name} ({resolved.show_id})")
    print(f"  Host: {resolved.host}")
    print(f"  Focus: {resolved.topic_focus}")
    print(f"  Segments: {', '.join(resolved.segment_types)}")
    print(f"  Bumper: {resolved.bumper_style}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
