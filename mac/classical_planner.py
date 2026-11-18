#!/usr/bin/env python3
"""Classical fill planner: whole works into fixed blocks, landing on the post.

From the show bible, "How classical fits a radio clock":

1. Works are never cut. Hard posts exist only at block edges. The hourly ID
   floats to the nearest gap between works.
2. A fill planner sizes each block: works whose lengths add up to the block
   minus talk, then a final fit piece from a shelf of 2-8 minute works to land
   within 30 seconds of the post. The Count's closing link absorbs the rest.
3. Rotation: no work repeats within 7 days, no composer twice in one hour, and
   a Main Event work airs at most once every 30 days.
4. Nothing interrupts: classical blocks carry the hourly ID and nothing else.

Each block type follows its template (Nocturne's night structure, the Evening
Concert program, Day Shift back-announces, one intro per work at the Matinee
and Concert Hall). Only cleared, lossless works are eligible. When the
library is too small for the rotation rules, the planner relaxes them one
step at a time and says so in the plan's warnings; it never relaxes the
lossless, licensing or whole-work rules.

    python mac/classical_planner.py --at "2026-11-18 23:00"
    python mac/classical_planner.py --at "2026-11-18 23:00" --json --commit
    python mac/classical_planner.py --week 2026-11-23
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from catalog import Catalog, Play, Work  # noqa: E402
from schedule import ProgramBlock, ScheduleError, StationSchedule, load_schedule  # noqa: E402

MINUTE = 60.0
HOUR = 3600.0

NOCTURNE_THEMES = {
    0: ("composers_i_have_known", "Composers I Have Known"),
    1: ("keyboard", "Keyboard Night"),
    2: ("strings", "Strings"),
    3: ("symphony", "Symphony Night"),
    4: ("requiems_and_dark_things", "Requiems and Dark Things"),
    5: ("brass_and_marches", "Brass and Marches"),
    6: ("sacred_and_choral", "Sacred and Choral"),
}

# Words that mark a work as fitting a theme or segment, matched against its
# form and tags. Matching is a preference, never a requirement: the library
# fills the themes as it grows.
THEME_WORDS = {
    "composers_i_have_known": set(),
    "keyboard": {"keyboard", "piano", "solo_piano", "harpsichord", "organ", "clavier"},
    "strings": {"strings", "string_quartet", "quartet", "violin", "cello", "viola", "string_orchestra"},
    "symphony": {"symphony"},
    "requiems_and_dark_things": {"requiem", "dark", "lament", "funeral"},
    "brass_and_marches": {"brass", "march", "band", "fanfare"},
    "sacred_and_choral": {"sacred", "choral", "mass", "oratorio", "cantata", "motet", "requiem", "hymn"},
}
FIRST_SET_WORDS = {"overture", "sonata", "chamber", "quartet", "trio", "quintet", "suite", "prelude", "serenade"}
SMALL_HOURS_WORDS = {"solo_piano", "piano", "keyboard", "strings", "violin", "cello", "guitar", "quiet", "nocturne", "lullaby"}
DAY_SHIFT_WORDS = {"calm", "focus", "sonata", "concerto", "chamber", "quartet", "suite"}
BIG_WORDS = {"variations", "oratorio", "opera", "mass", "symphony", "requiem", "passion", "cycle"}
CONCERT_HALL_WORDS = {"sacred", "choral", "mass", "oratorio", "cantata", "motet", "requiem", "symphony", "symphonic"}
OPENER_FORMS = {"overture", "prelude", "fanfare", "opener"}
CHAMBER_SOLO_WORDS = {"chamber", "solo", "solo_piano", "sonata", "quartet", "trio", "quintet", "suite", "partita"}


@dataclass(frozen=True)
class Rules:
    work_repeat_days: float = 7.0
    composer_spacing_min: float = 60.0
    main_event_repeat_days: float = 30.0

    def describe(self) -> str:
        return (
            f"work repeat {self.work_repeat_days:g} d, composer spacing "
            f"{self.composer_spacing_min:g} min, main event {self.main_event_repeat_days:g} d"
        )


STRICT = Rules()
# Tried in order when the library can't satisfy the rules. Two works by one
# composer are never placed back to back, whatever the level.
RELAXATIONS = (STRICT, Rules(3, 60, 30), Rules(1, 30, 30), Rules(0, 0, 14))


@dataclass(frozen=True)
class PlannerConfig:
    gap_min: float = 2.0  # silence between works, like a concert hall
    gap_max: float = 4.0
    talk_gap: float = 1.0  # between a link and the music
    id_seconds: float = 8.0
    shelf_min: float = 2 * MINUTE
    shelf_max: float = 8 * MINUTE
    tolerance: float = 30.0
    backtrack: int = 4


@dataclass(frozen=True)
class TalkPolicy:
    open_role: str
    open_seconds: float
    link_seconds: float  # before each work after the first (0 = none)
    close_role: str
    close_seconds: float  # nominal; the closing link absorbs the slack
    back_announce_every: float | None = None  # Day Shift: talk every N seconds


POLICIES = {
    "day_shift": TalkPolicy("intro", 20, 0, "closing", 20, back_announce_every=30 * MINUTE),
    "evening_concert": TalkPolicy("program", 180, 75, "handoff", 45),
    "nocturne": TalkPolicy("recollection", 120, 40, "sign_off", 45),
    "matinee": TalkPolicy("open", 60, 60, "closing", 45),
    "concert_hall": TalkPolicy("open", 60, 60, "closing", 45),
}


# ── plan ─────────────────────────────────────────────────────────────────────


@dataclass
class PlanEntry:
    type: str  # talk | work | gap | id
    role: str
    offset: float  # seconds from block start
    seconds: float
    work: Work | None = None
    about: list[int] = field(default_factory=list)

    @property
    def end(self) -> float:
        return self.offset + self.seconds


@dataclass
class Plan:
    show_id: str
    block: str
    title: str
    host: str
    start: datetime  # aware
    seconds: float
    entries: list[PlanEntry]
    theme: str | None = None
    rules: Rules = STRICT
    warnings: list[str] = field(default_factory=list)
    ok: bool = True
    fallback_show: str | None = None

    @property
    def end(self) -> datetime:
        return _after(self.start, self.seconds)

    @property
    def plan_id(self) -> str:
        return f"{self.show_id}@{self.start.astimezone(timezone.utc):%Y-%m-%dT%H%MZ}"

    def works(self) -> list[PlanEntry]:
        return [e for e in self.entries if e.type == "work"]

    def at(self, entry: PlanEntry) -> datetime:
        return _after(self.start, entry.offset)

    @property
    def closing(self) -> PlanEntry | None:
        talks = [e for e in self.entries if e.type == "talk"]
        return talks[-1] if talks else None

    def to_json(self) -> dict:
        def entry_json(e: PlanEntry) -> dict:
            data = {
                "type": e.type,
                "role": e.role,
                "starts_at": self.at(e).isoformat(timespec="seconds"),
                "offset": round(e.offset, 2),
                "seconds": round(e.seconds, 2),
            }
            if e.work is not None:
                w = e.work
                data.update({
                    "work_id": w.id,
                    "title": w.title,
                    "composer": w.composer,
                    "performer": w.performer,
                    "credit": w.credit_line,
                    "license": w.license,
                    "gain_db": w.gain_db or 0.0,
                    "lossless": w.lossless,
                    "files": [f.path for f in w.files],
                })
            if e.about:
                data["about"] = e.about
            return data

        return {
            "plan_id": self.plan_id,
            "show_id": self.show_id,
            "block": self.block,
            "title": self.title,
            "host": self.host,
            "theme": self.theme,
            "starts_at": self.start.isoformat(timespec="seconds"),
            "ends_at": self.end.isoformat(timespec="seconds"),
            "seconds": round(self.seconds, 2),
            "ok": self.ok,
            "fallback_show": self.fallback_show,
            "rules": self.rules.describe(),
            "warnings": self.warnings,
            "entries": [entry_json(e) for e in self.entries],
        }


def _after(start: datetime, seconds: float) -> datetime:
    """`start` plus elapsed seconds, in start's zone.

    Adding a timedelta to an aware datetime in Python moves the wall clock,
    which is wrong across a DST change; go through UTC instead.
    """
    moved = start.astimezone(timezone.utc) + timedelta(seconds=seconds)
    return moved.astimezone(start.tzinfo)


# ── history ──────────────────────────────────────────────────────────────────


class History:
    """Past and scheduled airings, indexed for the rotation rules."""

    def __init__(self, plays: Iterable[Play]):
        self.by_work: dict[int, list[datetime]] = defaultdict(list)
        self.main_events: dict[int, list[datetime]] = defaultdict(list)
        self.by_composer: dict[str, list[datetime]] = defaultdict(list)
        self.last_by_work: dict[int, datetime] = {}
        self.cycle_positions: dict[str, int] = {}
        for play in plays:
            at = play.at if play.at.tzinfo else play.at.replace(tzinfo=timezone.utc)
            self.by_work[play.work_id].append(at)
            if play.role == "main_event":
                self.main_events[play.work_id].append(at)
            if play.composer:
                self.by_composer[play.composer].append(at)
            if play.work_id not in self.last_by_work or at > self.last_by_work[play.work_id]:
                self.last_by_work[play.work_id] = at

    @staticmethod
    def _near(times: list[datetime], at: datetime, seconds: float) -> bool:
        return any(abs((t - at).total_seconds()) < seconds for t in times)

    def work_recent(self, work_id: int, at: datetime, days: float) -> bool:
        return days > 0 and self._near(self.by_work.get(work_id, []), at, days * 86400)

    def main_event_recent(self, work_id: int, at: datetime, days: float) -> bool:
        return days > 0 and self._near(self.main_events.get(work_id, []), at, days * 86400)

    def composer_near(self, composer: str | None, at: datetime, minutes: float) -> bool:
        if not composer or minutes <= 0:
            return False
        return self._near(self.by_composer.get(composer, []), at, minutes * MINUTE)

    def last_played(self, work_id: int) -> float:
        when = self.last_by_work.get(work_id)
        return when.timestamp() if when else float("-inf")


# ── helpers ──────────────────────────────────────────────────────────────────


def words(work: Work) -> set[str]:
    found = {t.lower() for t in work.tags}
    if work.form:
        found.add(work.form.lower())
        found.update(work.form.lower().replace("-", "_").split("_"))
    return found


def matches(work: Work, wanted: set[str]) -> bool:
    return bool(wanted) and bool(words(work) & wanted)


def nocturne_theme(start_local: datetime) -> tuple[str, str]:
    return NOCTURNE_THEMES[start_local.weekday()]


def block_unlocked(show_params: dict, cleared_hours: float) -> bool:
    return cleared_hours >= float(show_params.get("unlock_hours", 0) or 0)


# ── the planner ──────────────────────────────────────────────────────────────


class _NoLanding(Exception):
    pass


class BlockPlanner:
    def __init__(
        self,
        *,
        block_type: str,
        start: datetime,
        seconds: float,
        library: Sequence[Work],
        history: History,
        rules: Rules,
        config: PlannerConfig,
        seed: str,
        tz=None,
        params: dict | None = None,
        requested: Sequence[int] = (),
        cycle_positions: dict[str, int] | None = None,
    ):
        if start.tzinfo is None:
            raise ValueError("Block start must be timezone-aware")
        if block_type != "matinee":
            # Cycles air in order at the Matinee; a sonata played elsewhere
            # would make the cycle skip ahead.
            library = [w for w in library if not w.cycle]
        self.cycle_positions = dict(cycle_positions or {})
        self.block_type = block_type
        self.start = start
        self.seconds = seconds
        self.library = list(library)
        self.history = history
        self.rules = rules
        self.config = config
        self.policy = POLICIES[block_type]
        self.params = params or {}
        self.requested = set(requested)
        self.tz = tz or start.tzinfo
        self.rng = random.Random(seed)
        self.gap_rng = random.Random(seed + ":gaps")
        self._gaps: list[float] = []
        self.entries: list[PlanEntry] = []
        self.used: set[int] = set()
        self.placed: list[tuple[float, Work]] = []  # (offset, work)
        self.last_talk_end = 0.0
        self.warnings: list[str] = []
        self.tiebreak = {w.id: self.rng.random() for w in self.library}
        self.shelf = sorted(
            (w for w in self.library if config.shelf_min <= w.duration_sec <= config.shelf_max),
            key=lambda w: w.duration_sec,
        )
        self.shelf_durations = [w.duration_sec for w in self.shelf]
        self.id_marks = self._hour_marks()
        # IDs are inserted after planning; reserve their time (and the short
        # gap after each) now.
        self.id_cost = config.id_seconds + config.talk_gap
        self.post = seconds - len(self.id_marks) * self.id_cost

    # ── time ──

    @property
    def clock(self) -> float:
        return self.entries[-1].end if self.entries else 0.0

    def abs_time(self, offset: float) -> datetime:
        return _after(self.start, offset)

    def local(self, offset: float) -> datetime:
        return self.abs_time(offset).astimezone(self.tz)

    def offset_of_local(self, local_wall: datetime) -> float:
        aware = local_wall.replace(tzinfo=self.tz) if local_wall.tzinfo is None else local_wall
        return (aware - self.start).total_seconds()

    def _hour_marks(self) -> list[float]:
        marks = []
        t = self.start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        if t < self.start:
            t += timedelta(hours=1)
        end = _after(self.start, self.seconds)
        while t < end:
            if t.astimezone(self.tz).minute == 0:
                marks.append((t - self.start).total_seconds())
            t += timedelta(hours=1)
        return marks

    def gap(self, index: int) -> float:
        while len(self._gaps) <= index:
            steps = int((self.config.gap_max - self.config.gap_min) / 0.5)
            self._gaps.append(self.config.gap_min + 0.5 * self.gap_rng.randint(0, steps))
        return self._gaps[index]

    # ── placing ──

    def _work_count(self) -> int:
        return sum(1 for e in self.entries if e.type == "work")

    def _talk_before(self, at: float) -> tuple[str, float] | None:
        """The link that would precede a work placed now, if any."""
        if not self.entries:
            return None
        last = self.entries[-1]
        if last.type == "talk":
            return None
        if self.policy.back_announce_every is not None:
            if at - self.last_talk_end >= self.policy.back_announce_every:
                return ("back_announce", 20.0)
            return None
        if self.policy.link_seconds > 0:
            return ("link", self.policy.link_seconds)
        return None

    def _overhead(self, talk: tuple[str, float] | None) -> float:
        """Seconds between now and the start of the next work."""
        if not self.entries:
            return 0.0
        last = self.entries[-1]
        if talk is not None:
            return self.gap(self._work_count()) + talk[1] + self.config.talk_gap
        if last.type == "talk":
            return self.config.talk_gap
        return self.gap(self._work_count())

    def add_talk(self, role: str, seconds: float, about: Iterable[int] = ()) -> PlanEntry:
        if self.entries and self.entries[-1].type == "work":
            self._add_gap(self.gap(self._work_count()))
        entry = PlanEntry("talk", role, self.clock, seconds, about=list(about))
        self.entries.append(entry)
        self.last_talk_end = entry.end
        return entry

    def _add_gap(self, seconds: float) -> None:
        self.entries.append(PlanEntry("gap", "gap", self.clock, seconds))

    def place(self, work: Work, role: str, *, talk_role: str | None = None,
              talk_seconds: float | None = None) -> PlanEntry:
        talk = self._talk_before(self.clock)
        if talk_role is not None:
            talk = (talk_role, talk_seconds or self.policy.link_seconds)
        if talk is not None:
            self.add_talk(talk[0], talk[1], about=[work.id])
            self._add_gap(self.config.talk_gap)
        elif self.entries:
            self._add_gap(self.config.talk_gap if self.entries[-1].type == "talk" else self.gap(self._work_count()))
        entry = PlanEntry("work", role, self.clock, work.duration_sec, work=work)
        self.entries.append(entry)
        self.used.add(work.id)
        self.placed.append((entry.offset, work))
        return entry

    def _unplace_last(self) -> Work | None:
        """Remove the last work and the link/gaps that led into it."""
        while self.entries and self.entries[-1].type != "work":
            if self.entries[-1].type == "talk":
                return None  # never unwind past the block's opening talk
            self.entries.pop()
        if not self.entries:
            return None
        entry = self.entries.pop()
        self.used.discard(entry.work.id)
        self.placed.pop()
        while self.entries and self.entries[-1].type in ("gap", "talk"):
            if self.entries[-1].type == "talk" and self.entries[-1].role not in ("link", "back_announce"):
                break
            self.entries.pop()
        self.last_talk_end = max((e.end for e in self.entries if e.type == "talk"), default=0.0)
        return entry.work

    # ── rules ──

    def allowed(self, work: Work, start: float, role: str) -> bool:
        if work.id in self.used:
            return False
        at = self.abs_time(start)
        if self.history.work_recent(work.id, at, self.rules.work_repeat_days):
            return False
        if role == "main_event" and self.history.main_event_recent(work.id, at, self.rules.main_event_repeat_days):
            return False
        if work.composer:
            if self.placed and self.placed[-1][1].composer == work.composer:
                return False  # never back to back
            spacing = self.rules.composer_spacing_min * MINUTE
            for offset, other in self.placed:
                if other.composer == work.composer and abs(start - offset) < spacing:
                    return False
            if self.history.composer_near(work.composer, at, self.rules.composer_spacing_min):
                return False
        return True

    def _rank(self, work: Work, prefer: set[str] | None, target_len: float | None = None) -> tuple:
        pref = 1 if prefer and matches(work, prefer) else 0
        requested = 1 if work.id in self.requested else 0
        # Template slots ("a concerto, about 30 minutes") prefer works near
        # the target length, in 2-minute steps so rotation still decides ties.
        size = int(abs(work.duration_sec - target_len) // (2 * MINUTE)) if target_len else 0
        return (-requested, -pref, size, self.history.last_played(work.id), self.tiebreak[work.id])

    def pick(
        self,
        pool: Iterable[Work],
        role: str,
        *,
        until: float,
        prefer: set[str] | None = None,
        min_len: float = 0.0,
        max_len: float = float("inf"),
        target_len: float | None = None,
        require: Callable[[Work], bool] | None = None,
    ) -> Work | None:
        start = self.clock + self._overhead(self._talk_before(self.clock))
        best, best_rank = None, None
        for work in pool:
            d = work.duration_sec
            if d < min_len or d > max_len or start + d > until:
                continue
            if require is not None and not require(work):
                continue
            if not self.allowed(work, start, role):
                continue
            rank = self._rank(work, prefer, target_len)
            if best_rank is None or rank < best_rank:
                best, best_rank = work, rank
        return best

    def fill(self, until: float, role: str, *, prefer: set[str] | None = None,
             max_len: float = float("inf"), min_len: float = 0.0) -> int:
        count = 0
        while True:
            work = self.pick(self.library, role, until=until, prefer=prefer, max_len=max_len, min_len=min_len)
            if work is None:
                return count
            self.place(work, role)
            count += 1

    # ── landing on the post ──

    def _closing_room(self) -> float:
        """Latest end of the last work: the post minus the gap and closing link."""
        return self.post - self.policy.close_seconds - self.config.gap_max

    def land(self, role: str, *, prefer: set[str] | None = None, final_role: str = "fit",
             final_prefer: set[str] | None = None, max_len: float = float("inf"),
             pool_require: Callable[[Work], bool] | None = None) -> None:
        """Greedy fill, then a filler plus a shelf piece that lands on the post."""
        cfg = self.config
        # Leave room for a final piece of about half the longest allowed plus
        # a shelf piece: the middle of what the tail can reach, so it has
        # many combinations to land with.
        reserve = min(max_len, 30 * MINUTE) / 2 + cfg.shelf_max + 2 * MINUTE
        self.fill(self._closing_room() - reserve, role, prefer=prefer, max_len=max_len)
        popped: list[Work] = []
        for attempt in range(cfg.backtrack + 1):
            try:
                self._land_tail(role, prefer, final_role, final_prefer, max_len, pool_require)
                return
            except _NoLanding:
                work = self._unplace_last()
                if work is None:
                    break
                popped.append(work)
        raise _NoLanding()

    def _land_tail(self, role, prefer, final_role, final_prefer, max_len, pool_require) -> None:
        """Place at most one filler (A) and one or two shelf pieces (B) to land."""
        cfg = self.config
        target = self._closing_room()
        if target - self.clock < 0:
            raise _NoLanding()
        if 0 <= target - self.clock <= cfg.tolerance and self.placed:
            return  # already on the post
        # Candidate fillers: None first, then pool works sorted by rank.
        fillers: list[Work | None] = [None]
        start_a = self.clock + self._overhead(self._talk_before(self.clock))
        pool = [
            w for w in self.library
            if w.duration_sec <= max_len and start_a + w.duration_sec <= target
            and (pool_require is None or pool_require(w)) and self.allowed(w, start_a, role)
        ]
        pool.sort(key=lambda w: self._rank(w, prefer))
        fillers.extend(pool[:200])

        for filler in fillers:
            snapshot = (len(self.entries), set(self.used), list(self.placed), self.last_talk_end)
            if filler is not None:
                self.place(filler, role)
            if self._try_shelf(target, final_role, final_prefer):
                return
            if filler is None and self._try_shelf_pair(target, final_role, final_prefer):
                return
            # roll back the filler
            del self.entries[snapshot[0]:]
            self.used, self.placed, self.last_talk_end = snapshot[1], snapshot[2], snapshot[3]
        raise _NoLanding()

    def _shelf_candidates(self, lo: float, hi: float) -> list[Work]:
        i = bisect.bisect_left(self.shelf_durations, lo)
        j = bisect.bisect_right(self.shelf_durations, hi)
        return self.shelf[i:j]

    def _try_shelf(self, target: float, final_role: str, final_prefer: set[str] | None) -> bool:
        overhead = self._overhead(self._talk_before(self.clock))
        start = self.clock + overhead
        lo, hi = target - self.config.tolerance - start, target - start
        options = [w for w in self._shelf_candidates(lo, hi) if self.allowed(w, start, final_role)]
        if not options:
            return False
        options.sort(key=lambda w: self._rank(w, final_prefer))
        self.place(options[0], final_role)
        return True

    def _try_shelf_pair(self, target: float, final_role: str, final_prefer: set[str] | None) -> bool:
        overhead = self._overhead(self._talk_before(self.clock))
        start = self.clock + overhead
        for first in sorted(self.shelf, key=lambda w: self._rank(w, final_prefer))[:120]:
            if not self.allowed(first, start, "fit"):
                continue
            if start + first.duration_sec + overhead + self.config.shelf_min > target:
                continue
            mark = len(self.entries)
            self.place(first, "fit")
            if self._try_shelf(target, final_role, final_prefer):
                return True
            del self.entries[mark:]
            self.used.discard(first.id)
            self.placed.pop()
        return False

    # ── finishing ──

    def close(self) -> None:
        """Closing link: its length absorbs whatever is left before the post."""
        if self.entries and self.entries[-1].type == "work":
            self._add_gap(self.config.gap_max)  # the longest pause before the farewell
        about = [self.placed[-1][1].id] if self.placed else []
        remaining = self.post - self.clock
        entry = PlanEntry("talk", self.policy.close_role, self.clock, max(0.0, remaining), about=about)
        self.entries.append(entry)
        nominal = self.policy.close_seconds
        if remaining < nominal - 0.5 or remaining > nominal + self.config.tolerance + 0.5:
            self.warnings.append(
                f"closing link is {remaining:.0f} s (nominal {nominal:.0f} s, "
                f"tolerance {self.config.tolerance:.0f} s)"
            )

    def insert_ids(self) -> None:
        """Put each hourly ID in the work gap nearest its hour mark.

        After a work: work, silence, ID, short gap, next item. At a block that
        starts on the hour: ID, short gap, opening link. Never between a link
        and its music, never inside a work.
        """
        cfg = self.config
        taken: set[int] = set()
        placed = 0
        for mark in self.id_marks:
            candidates = []
            if mark == 0.0:
                candidates.append(0)
            for i, entry in enumerate(self.entries):
                if (entry.type == "work" and i + 2 < len(self.entries)
                        and self.entries[i + 1].type == "gap"):
                    candidates.append(i + 2)
            candidates = [c for c in candidates if c not in taken]
            if not candidates:
                self.warnings.append(f"no gap for the hourly ID near offset {mark:.0f} s")
                continue
            best = min(candidates, key=lambda i: abs(self.entries[i].offset - mark))
            at = self.entries[best].offset
            self.entries[best:best] = [
                PlanEntry("id", "hourly_id", at, cfg.id_seconds),
                PlanEntry("gap", "gap", at + cfg.id_seconds, cfg.talk_gap),
            ]
            self._shift_from(best + 2, self.id_cost)
            taken = {t + 2 if t >= best else t for t in taken}
            taken.add(best)
            placed += 1
        # IDs that found no slot leave their reserved time to the closing link.
        missing = len(self.id_marks) - placed
        if missing and self.entries and self.entries[-1].type == "talk":
            self.entries[-1].seconds += missing * self.id_cost

    def _shift_from(self, index: int, seconds: float) -> None:
        for entry in self.entries[index:]:
            entry.offset += seconds


# ── block templates ──────────────────────────────────────────────────────────


def _plan_day_shift(bp: BlockPlanner) -> None:
    max_len = float(bp.params.get("max_work_minutes", 30)) * MINUTE
    bp.add_talk(bp.policy.open_role, bp.policy.open_seconds)
    bp.land("day_shift", prefer=DAY_SHIFT_WORDS, max_len=max_len - 1)


def _plan_evening_concert(bp: BlockPlanner) -> None:
    program = bp.add_talk(bp.policy.open_role, bp.policy.open_seconds)
    slots = [  # role, shortest, longest, about, forms
        ("overture", 5 * MINUTE, 15 * MINUTE, 10 * MINUTE, OPENER_FORMS),
        ("concerto", 18 * MINUTE, 42 * MINUTE, 30 * MINUTE, {"concerto"}),
        ("symphony", 22 * MINUTE, 58 * MINUTE, 40 * MINUTE, {"symphony"}),
    ]
    for role, lo, hi, about, forms in slots:
        talk_role, talk_seconds = None, None
        if role == "symphony":
            talk_role, talk_seconds = "intermission", 180.0  # the Count's Recollection
        until = bp._closing_room() - 12 * MINUTE  # leave room for the second half
        pick = (
            bp.pick(bp.library, role, until=until, min_len=lo, max_len=hi, target_len=about,
                    require=lambda w, f=forms: matches(w, f))
            or bp.pick(bp.library, role, until=until, min_len=lo, max_len=hi, target_len=about)
        )
        if pick is None:
            bp.warnings.append(f"no {role} available; the second half absorbs the time")
            continue
        if bp.placed:
            bp.place(pick, role, talk_role=talk_role, talk_seconds=talk_seconds)
        else:
            bp.place(pick, role)  # the program reading introduces the opener
    bp.land("second_half", prefer=CHAMBER_SOLO_WORDS, final_role="encore", max_len=40 * MINUTE)
    program.about = [w.id for _, w in bp.placed]


def _plan_nocturne(bp: BlockPlanner) -> str:
    theme_key, theme_name = nocturne_theme(bp.local(0))
    theme_words = THEME_WORDS[theme_key]
    # The night structure assumes a full night (Main Event at 1 AM, about
    # two hours in). A shorter Nocturne scales down and may skip it.
    main_target = min(2 * HOUR, bp.seconds / 3)
    main_max = min(100 * MINUTE, bp.seconds - main_target - max(1.5 * HOUR, bp.seconds / 4))

    # Choose the Main Event first: the Recollection is about its composer.
    start_main = main_target
    main = None
    for prefer in ((theme_words, None) if main_max >= 40 * MINUTE else ()):
        candidates = [
            w for w in bp.library
            if 40 * MINUTE <= w.duration_sec <= main_max
            and bp.allowed(w, start_main, "main_event")
            and (prefer is None or matches(w, prefer))
        ]
        if candidates:
            main = min(candidates, key=lambda w: bp._rank(w, theme_words))
            break
    opening = bp.add_talk(bp.policy.open_role, bp.policy.open_seconds,
                          about=[main.id] if main else [])
    if main is not None:
        bp.used.add(main.id)  # hold it for 1 AM
    elif main_max < 40 * MINUTE:
        bp.warnings.append("block too short for a Main Event")
    else:
        bp.warnings.append("no Main Event candidate (40+ minutes, not aired in 30 days)")

    bp.fill(main_target - 90 - 10, "first_set", prefer=FIRST_SET_WORDS | theme_words, max_len=30 * MINUTE)
    if main is not None:
        bp.used.discard(main.id)
        if bp.allowed(main, bp.clock + 100, "main_event"):
            bp.place(main, "main_event", talk_role="link", talk_seconds=90)
        else:
            bp.warnings.append("held Main Event no longer fits the composer spacing; skipped")

    # The Small Hours run to the end; on Sunday morning the last stretch
    # (05:00 onward) is Sunday Sacred.
    end_local = bp.local(bp.seconds)
    sacred_from = None
    if end_local.weekday() == 6 and end_local.hour >= 6:
        sunday_five = end_local.replace(hour=5, minute=0, second=0, microsecond=0).replace(tzinfo=None)
        sacred_from = bp.offset_of_local(sunday_five)
    if sacred_from is not None and sacred_from > bp.clock:
        bp.fill(sacred_from, "small_hours", prefer=SMALL_HOURS_WORDS, max_len=30 * MINUTE)
        bp.land("sunday_sacred", prefer=THEME_WORDS["sacred_and_choral"], final_role="before_the_dawn",
                final_prefer=SMALL_HOURS_WORDS | THEME_WORDS["sacred_and_choral"])
    else:
        bp.land("small_hours", prefer=SMALL_HOURS_WORDS | theme_words, final_role="before_the_dawn",
                final_prefer=SMALL_HOURS_WORDS, max_len=30 * MINUTE)
    if main is None and bp.placed:
        opening.about = [bp.placed[0][1].id]
    return theme_name


def _plan_matinee(bp: BlockPlanner) -> None:
    bp.add_talk(bp.policy.open_role, bp.policy.open_seconds)
    # Cycles air in order across the weeks: each cycle's next unaired work
    # gets first claim on every slot the composer spacing allows.
    by_cycle: dict[str, list[Work]] = defaultdict(list)
    for work in bp.library:
        if work.cycle and work.cycle_no is not None:
            by_cycle[work.cycle].append(work)
    queue: list[Work] = []
    for cycle, works in sorted(by_cycle.items()):
        works.sort(key=lambda w: w.cycle_no)
        position = bp.cycle_positions.get(cycle, 0)
        if position >= works[-1].cycle_no:
            position = 0  # cycle complete: start again
        queue.extend(w for w in works if w.cycle_no > position)
    queued = {w.id for w in queue}
    others = [w for w in bp.library if w.id not in queued and not w.cycle]
    # Big works and the cycle run until the last half hour or so, then the
    # usual landing (one larger piece and a shelf piece) takes over.
    limit = bp._closing_room() - (15 * MINUTE + bp.config.shelf_max + 2 * MINUTE)
    while True:
        nxt = next((w for w in queue if w.id not in bp.used), None)
        start = bp.clock + bp._overhead(bp._talk_before(bp.clock))
        if nxt is not None and start + nxt.duration_sec <= limit and bp.allowed(nxt, start, "cycle"):
            bp.place(nxt, "cycle")
            continue
        other = bp.pick(others, "big", until=limit, prefer=BIG_WORDS, min_len=20 * MINUTE)
        if other is None:
            break
        bp.place(other, "big")
    bp.library = others  # the tail never pulls a cycle work out of order
    bp.land("matinee", prefer=BIG_WORDS)


def _plan_concert_hall(bp: BlockPlanner) -> None:
    bp.add_talk(bp.policy.open_role, bp.policy.open_seconds)
    bp.land("concert_hall", prefer=CONCERT_HALL_WORDS)


TEMPLATES: dict[str, Callable[[BlockPlanner], str | None]] = {
    "day_shift": _plan_day_shift,
    "evening_concert": _plan_evening_concert,
    "nocturne": _plan_nocturne,
    "matinee": _plan_matinee,
    "concert_hall": _plan_concert_hall,
}


# ── entry points ─────────────────────────────────────────────────────────────


def plan_block(
    *,
    block_type: str,
    show_id: str,
    title: str,
    host: str,
    start: datetime,
    seconds: float,
    library: Sequence[Work],
    plays: Iterable[Play] = (),
    params: dict | None = None,
    config: PlannerConfig = PlannerConfig(),
    tz=None,
    requested: Sequence[int] = (),
    cycle_positions: dict[str, int] | None = None,
) -> Plan:
    """Plan one classical block from an already-cleared library."""
    if block_type not in TEMPLATES:
        raise ValueError(f"Unknown classical block {block_type!r}")
    history = History(plays)
    lossless = [w for w in library if w.lossless and w.kind == "classical"]
    if len(lossless) < len(library):
        dropped = len(library) - len(lossless)
        # The caller should only pass cleared works; this is the last guard.
        library = lossless
    else:
        dropped = 0
    seed = f"{show_id}|{start.astimezone(timezone.utc).isoformat()}"
    last_error = None
    for level, rules in enumerate(RELAXATIONS):
        bp = BlockPlanner(
            block_type=block_type, start=start, seconds=seconds, library=library,
            history=history, rules=rules, config=config, seed=seed, tz=tz,
            params=params, requested=requested, cycle_positions=cycle_positions,
        )
        try:
            theme = TEMPLATES[block_type](bp)
        except _NoLanding:
            last_error = "could not land within the tolerance"
            continue
        bp.close()
        bp.insert_ids()
        warnings = list(bp.warnings)
        if dropped:
            warnings.insert(0, f"ignored {dropped} work(s) that are not lossless classical")
        if level:
            warnings.insert(0, f"library too small for the strict rules; relaxed to: {rules.describe()}")
        return Plan(
            show_id=show_id, block=block_type, title=title, host=host, start=start,
            seconds=seconds, entries=bp.entries, theme=theme, rules=rules,
            warnings=warnings, ok=True,
        )
    return Plan(
        show_id=show_id, block=block_type, title=title, host=host, start=start,
        seconds=seconds, entries=[], rules=RELAXATIONS[-1],
        warnings=[f"no plan: {last_error or 'library is empty'}"], ok=False,
    )


def plan_for_block(
    schedule: StationSchedule,
    block: ProgramBlock,
    catalog: Catalog,
    *,
    config: PlannerConfig = PlannerConfig(),
    requested: Sequence[int] = (),
    today: date | None = None,
) -> Plan:
    """Plan a scheduled classical block, honouring the library unlock thresholds."""
    show = schedule.shows[block.show_id]
    if show.program != "classical":
        raise ValueError(f"{block.show_id} is not a classical show")
    params = dict(show.params)
    start = schedule.localize(block.start)
    library = [w for w in catalog.works(kind="classical") if catalog.is_airable(w, today)]
    hours = sum(w.duration_sec for w in library) / HOUR
    if not block_unlocked(params, hours):
        fallback = params.get("fallback_show", "monster_mix")
        return Plan(
            show_id=block.show_id, block=params.get("block", ""), title=block.title,
            host=show.host, start=start, seconds=block.seconds, entries=[],
            warnings=[f"locked: library has {hours:.1f} h of cleared lossless classical, "
                      f"needs {params.get('unlock_hours')} h; the slot runs {fallback}"],
            ok=True, fallback_show=fallback,
        )
    since = start - timedelta(days=31)
    return plan_block(
        block_type=params["block"], show_id=block.show_id, title=block.title, host=show.host,
        start=start, seconds=block.seconds, library=library,
        plays=catalog.plays_since(since), params=params, config=config,
        tz=schedule.tz, requested=requested, cycle_positions=catalog.cycle_positions(),
    )


def commit_plan(catalog: Catalog, plan: Plan) -> None:
    """Record the plan's works as scheduled so later plans respect rotation."""
    catalog.clear_plan(plan.plan_id)
    for entry in plan.works():
        catalog.schedule_work(entry.work.id, plan.at(entry), show_id=plan.show_id,
                              role=entry.role, plan_id=plan.plan_id)


def format_plan(plan: Plan, tz=None) -> str:
    lines = [f"{plan.title} ({plan.show_id}) {plan.start.astimezone(tz):%a %Y-%m-%d %H:%M}"
             f" - {plan.end.astimezone(tz):%H:%M}"]
    if plan.theme:
        lines.append(f"Theme: {plan.theme}")
    if plan.fallback_show:
        lines.append(f"Runs {plan.fallback_show} instead")
    for e in plan.entries:
        if e.type == "gap":
            continue
        when = plan.at(e).astimezone(tz)
        label = e.work.display() if e.work else ("Station ID" if e.type == "id" else e.role)
        role = e.role if e.work else e.type
        lines.append(f"  {when:%H:%M:%S}  {e.seconds / 60:6.1f}m  {role:16s} {label}")
    for w in plan.warnings:
        lines.append(f"  ! {w}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    from station_config import load_station_config

    parser = argparse.ArgumentParser(description="Plan classical blocks")
    parser.add_argument("--station", default="wit-next")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--at", help="a time inside the block, 'YYYY-MM-DD HH:MM' station time")
    parser.add_argument("--week", help="dry-run every classical block in the week starting this Monday")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--commit", action="store_true", help="record planned works for rotation")
    args = parser.parse_args(argv)

    station = load_station_config(args.station)
    schedule = load_schedule(station.schedule_path)
    with Catalog(args.db or station.catalog_db_path) as catalog:
        if args.week:
            monday = datetime.strptime(args.week, "%Y-%m-%d")
            blocks = [
                b for b in schedule.program_blocks(monday, monday + timedelta(days=7, hours=12))
                if schedule.shows[b.show_id].program == "classical"
                and monday <= b.start < monday + timedelta(days=7)
            ]
            for block in blocks:
                plan = plan_for_block(schedule, block, catalog)
                closing = plan.closing.seconds if plan.closing else 0
                print(f"{block.start:%a %H:%M} {block.title:34s} works={len(plan.works()):3d} "
                      f"closing={closing:5.1f}s warnings={len(plan.warnings)}")
                if args.commit and plan.entries:
                    commit_plan(catalog, plan)
            return 0

        when = datetime.strptime(args.at, "%Y-%m-%d %H:%M") if args.at else schedule.now()
        try:
            block = schedule.program_block_at(when)
        except ScheduleError as exc:
            parser.error(str(exc))
        plan = plan_for_block(schedule, block, catalog)
        if args.commit and plan.entries:
            commit_plan(catalog, plan)
        if args.json:
            print(json.dumps(plan.to_json(), indent=2))
        else:
            print(format_plan(plan, schedule.tz))
        return 0 if plan.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
