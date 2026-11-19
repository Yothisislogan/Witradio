#!/usr/bin/env python3
"""Plans The Stacks (show bible: "The Stacks with Steve") from the catalog.

Each hour follows the bible's clock, built from approved Library of Congress
records (kind stacks and airable: through the content screen, with a
publication year in the public domain):

  :00  Pull One                 Steve sets the hour's theme
  :03  Straight From the Shelf  two records, a 30-second intro each
  :12  The Remix                one of those records re-cut over a new beat
  :17  Call Number              the story behind one record (60 s)
  :20  Shelf + Remix            the same pattern again, until :40
  :40  Speech Sample            a public-domain speech or field recording over an ambient bed
  :45  Then & Now               a 1910s original, then a new track in the same feel
  :55  Back to the Stacks       credits recap and a tease (absorbs the slack)

Records come from one theme cart per hour (Saturday's Crate Dig keeps one
for both hours, with a remix after every pair), matched by title and tags,
and fall back to the whole shelf. No record repeats within 14 days,
counting what's already planned; a small shelf relaxes that step by step.
The records play straight from the library. Steve's talk, the remixes, the
speech samples and the Then & Now tracks are talk entries the Halo renders
into talk/<plan_id>/NNN.flac.

    python3 mac/stacks_planner.py --at "2026-11-19 13:00"
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from catalog import Catalog, Work  # noqa: E402
from schedule import ProgramBlock, StationSchedule  # noqa: E402

ROTATION_DAYS = (14, 7, 3, 0)
CLIP_TAG = "remix-clip"
SPOKEN_TAGS = {"spoken", "spoken word", "speech", "speeches", "loc-jukebox-spoken", "monologue", "recitation",
               "vaudeville sketch", "oratory", "lecture"}
BED_STYLES = ("boom-bap", "lo-fi", "house", "electro-swing", "trip-hop")
SPEECH_LONGEST = 240.0  # the :40 slot runs to :45; longer speeches are faded out here
CLOSING_SECONDS = (60.0, 150.0)  # Back to the Stacks: at least a minute, at most two and a half
THEMES: dict[str, tuple[str, ...]] = {
    "Songs About Cars (Before Anyone Insured Them)": ("automobile", "auto", "motor", "motoring", "car", "cars",
                                                      "jitney", "gasoline", "taxi", "limousine", "speed"),
    "Dance Crazes of the 1910s": ("fox trot", "fox-trot", "one-step", "one step", "two-step", "tango", "turkey trot",
                                  "maxixe", "hesitation", "castle walk", "dance", "dancing"),
    "Ragtime Week": ("rag", "ragtime", "rags"),
    "Opera Before Radio": ("opera", "aria", "operatic"),
    "Vaudeville Comedy": ("vaudeville", "monologue", "sketch", "comic", "humorous"),
    "Banjo Hour": ("banjo",),
    "Women of the Phonograph": ("women",),  # tag records "women" in the catalog to fill this cart
    "Brass Band Battles": ("band", "march", "marches", "brass"),
}
GENERAL_THEME = "From the Stacks"


def _text(work: Work) -> str:
    return " ".join([work.title, work.form or "", *work.tags]).lower()


def matches(work: Work, theme: str) -> bool:
    words = THEMES.get(theme, ())
    text = _text(work)
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) for w in words)


def duration(work: Work) -> float:
    return sum(f.duration_sec or 0 for f in work.files)


@dataclass
class Shelf:
    """The airable Stacks records, minus what rotation keeps off."""

    catalog: Catalog
    today: date
    rest: set[int] = field(default_factory=set)
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self):
        works = [w for w in self.catalog.works(kind="stacks") if w.files and self.catalog.is_airable(w, self.today)]
        self.clips = [w for w in works if CLIP_TAG in w.tags]
        playable = [w for w in works if CLIP_TAG not in w.tags and 45 <= duration(w) <= 480]
        self.spoken = [w for w in playable if SPOKEN_TAGS & {t.lower() for t in w.tags}]
        self.records = [w for w in playable if w not in self.spoken]
        self.used: set[int] = set()

    def _fresh(self, pool: Iterable[Work]) -> list[Work]:
        return [w for w in pool if w.id not in self.used and w.id not in self.rest]

    def take(self, count: int, theme: str, *, era: tuple[int, int] | None = None) -> list[Work]:
        picked: list[Work] = []
        for strict_theme in (True, False):
            pool = self._fresh(self.records)
            if strict_theme and theme in THEMES:
                pool = [w for w in pool if matches(w, theme)]
            if era:
                pool = [w for w in pool if w.year and era[0] <= w.year <= era[1]] or pool
            self.rng.shuffle(pool)
            for work in pool:
                if len(picked) == count:
                    break
                if work not in picked:
                    picked.append(work)
                    self.used.add(work.id)
            if len(picked) == count:
                break
        return picked

    def take_spoken(self) -> Work | None:
        pool = self._fresh(self.spoken)
        if not pool:
            return None
        short = [w for w in pool if duration(w) <= SPEECH_LONGEST]
        work = self.rng.choice(short or pool)
        self.used.add(work.id)
        return work

    def take_fitting(self, theme: str, seconds: float) -> Work | None:
        """One record no longer than `seconds`, on the theme if one fits."""
        for strict_theme in (True, False):
            pool = [w for w in self._fresh(self.records) if duration(w) <= seconds
                    and (not strict_theme or theme not in THEMES or matches(w, theme))]
            if pool:
                work = self.rng.choice(pool)
                self.used.add(work.id)
                return work
        return None

    def clip_for(self, records: Sequence[Work]) -> Work | None:
        """A Citizen DJ clip cut from one of these records, if the catalog has one."""
        urls = {w.source_url for w in records if w.source_url}
        options = [c for c in self.clips if c.source_url in urls]
        return self.rng.choice(options) if options else None

    def viable_themes(self, minimum: int = 4) -> list[str]:
        fresh = self._fresh(self.records)
        return [t for t in THEMES if sum(1 for w in fresh if matches(w, t)) >= minimum]


@dataclass
class StacksPlan:
    show_id: str
    block: str
    title: str
    host: str
    start: datetime  # aware
    seconds: float
    themes: list[str] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return any(e["type"] == "work" for e in self.entries)

    @property
    def plan_id(self) -> str:
        return f"{self.show_id}@{self.start.astimezone(timezone.utc):%Y-%m-%dT%H%MZ}"

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.seconds)

    def to_json(self) -> dict[str, Any]:
        offset, entries = 0.0, []
        for entry in self.entries:
            entries.append({**entry, "offset": round(offset, 2),
                            "starts_at": (self.start + timedelta(seconds=offset)).isoformat(timespec="seconds")})
            offset += float(entry["seconds"])
        return {"plan_id": self.plan_id, "show_id": self.show_id, "block": self.block, "title": self.title,
                "host": self.host, "theme": " / ".join(dict.fromkeys(self.themes)), "program": "stacks",
                "starts_at": self.start.isoformat(timespec="seconds"),
                "ends_at": self.end.isoformat(timespec="seconds"), "seconds": round(self.seconds, 2),
                "entries": entries}


def _rel(path: str, library: Path | None) -> str:
    if library is None:
        return ""
    try:
        return str(Path(path).resolve().relative_to(library.resolve()))
    except ValueError:
        return ""


def record_facts(work: Work) -> list[str]:
    facts = [line[len("LOC: "):] for line in (work.notes or "").splitlines() if line.startswith("LOC: ")]
    facts += [line for line in (work.notes or "").splitlines()
              if line.startswith(("issued as", "published ", "publication year"))]
    return facts


def record_entry(work: Work, role: str, library: Path | None) -> dict[str, Any]:
    paths = [f.path for f in work.files]
    return {"type": "work", "role": role, "seconds": round(duration(work), 2), "work_id": work.id,
            "title": work.title, "composer": work.composer, "performer": work.performer, "credit": work.credit_line,
            "license": work.license, "gain_db": work.gain_db or 0.0, "lossless": False, "files": paths,
            "year": work.year, "facts": record_facts(work), "library_rel": [_rel(p, library) for p in paths]}


def source_of(work: Work, library: Path | None) -> dict[str, Any]:
    path = work.files[0].path
    return {"work_id": work.id, "title": work.title, "performer": work.performer, "composer": work.composer,
            "year": work.year, "credit": work.credit_line, "license": work.license, "path": path,
            "library_rel": _rel(path, library), "seconds": round(duration(work), 2), "tags": list(work.tags)}


def talk(role: str, seconds: float, **extra: Any) -> dict[str, Any]:
    return {"type": "talk", "role": role, "seconds": float(seconds), **extra}


def gap(seconds: float = 2.0) -> dict[str, Any]:
    return {"type": "gap", "role": "gap", "seconds": seconds}


def build_hour(shelf: Shelf, theme: str, hour_seconds: float, library: Path | None, *, crate_dig: bool,
               rng: random.Random, last_hour: bool) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    clock = 0.0

    def add(*items: dict[str, Any]) -> None:
        nonlocal clock
        for item in items:
            entries.append(item)
            clock += float(item["seconds"])

    def shelf_pair(remix: bool) -> bool:
        pair = shelf.take(2, theme)
        if not pair:
            return False
        for work in pair:
            add(talk("shelf_intro", 30, about=[work.id]), record_entry(work, "shelf", library), gap())
        if remix:
            clip = shelf.clip_for(pair)
            source = clip or max(pair, key=duration)
            add(talk("remix", 180, about=[w.id for w in pair], source=source_of(source, library),
                     bed_style=rng.choice(BED_STYLES)), gap())
        return True

    add(talk("pull_one", 60, theme=theme), gap(1))
    if not shelf_pair(remix=True):
        return []
    first_records = [e["work_id"] for e in entries if e["type"] == "work"]
    add(talk("call_number", 60, about=[rng.choice(first_records)]), gap(1))
    speech_at = min(40 * 60.0, hour_seconds - 20 * 60.0)  # :40 in a full hour
    pair_seconds = 2 * (32 + 200) + 182
    pairs = 0
    while clock + pair_seconds <= speech_at and shelf_pair(remix=crate_dig or pairs % 2 == 0):
        pairs += 1

    def singles_until(mark: float) -> None:
        while True:
            room = mark - clock - 32
            extra = shelf.take_fitting(theme, room) if room >= 45 else None
            if extra is None:
                return
            add(talk("shelf_intro", 30, about=[extra.id]), record_entry(extra, "shelf", library), gap())

    singles_until(speech_at)
    spoken = shelf.take_spoken()
    if spoken is not None:  # an intro, the recording (faded at SPEECH_LONGEST) and the bed's tails
        add(talk("speech_sample", round(min(duration(spoken), SPEECH_LONGEST) + 30), about=[spoken.id],
                 source=source_of(spoken, library), bed_style="ambient"), gap())
    era = shelf.take(1, theme, era=(1910, 1919))
    if era:
        original = era[0]
        add(talk("then_now_intro", 20, about=[original.id]), record_entry(original, "then", library), gap(1),
            talk("then_now", 180, about=[original.id], source=source_of(original, library)), gap())
    shortest_close, longest_close = CLOSING_SECONDS
    singles_until(hour_seconds - shortest_close)  # toward :55; Back to the Stacks takes the rest
    played = [e["work_id"] for e in entries if e["type"] == "work"]
    add(talk("closing", max(shortest_close, min(longest_close, hour_seconds - clock)), about=played,
             last_hour=last_hour))
    return entries


def plan_for_block(schedule: StationSchedule, block: ProgramBlock, catalog: Catalog, *,
                   library: Path | None = None, today: date | None = None) -> StacksPlan:
    show = schedule.shows[block.show_id]
    start = schedule.localize(block.start)
    today = today or start.date()
    plan = StacksPlan(show_id=block.show_id, block=str(show.params.get("format", "hour_clock")), title=block.title,
                      host=show.host, start=start, seconds=block.seconds)
    rng = random.Random(plan.plan_id)
    crate_dig = plan.block == "crate_dig"
    hours = max(1, round(block.seconds / 3600))
    for days in ROTATION_DAYS:
        since = start - timedelta(days=days)
        rest = {p.work_id for p in catalog.plays_since(since) if p.work_id} if days else set()
        shelf = Shelf(catalog, today, rest=rest, rng=random.Random(plan.plan_id))
        entries, themes = [], []
        theme = None
        for hour in range(hours):
            if theme is None or not crate_dig:
                options = shelf.viable_themes()
                theme = rng.choice(options) if options else GENERAL_THEME
            hour_entries = build_hour(shelf, theme, min(3600.0, block.seconds - hour * 3600), library,
                                      crate_dig=crate_dig, rng=rng, last_hour=hour == hours - 1)
            if not hour_entries:
                break
            entries += hour_entries
            themes.append(theme)
        if entries and len(themes) == hours:
            plan.entries, plan.themes = entries, themes
            if days != ROTATION_DAYS[0]:
                plan.problems.append(f"small shelf: rotation relaxed to {days} days")
            return plan
    plan.problems.append("not enough approved Stacks records for a full plan")
    return plan


def commit_plan(catalog: Catalog, plan: StacksPlan) -> None:
    """Record the plan's records as scheduled, so later plans respect rotation."""
    catalog.clear_plan(plan.plan_id)
    data = plan.to_json()
    for entry in data["entries"]:
        if entry["type"] == "work":
            catalog.schedule_work(entry["work_id"], datetime.fromisoformat(entry["starts_at"]),
                                  show_id=plan.show_id, role=entry["role"], plan_id=plan.plan_id)


def airable_records(catalog: Catalog, today: date | None = None) -> int:
    today = today or date.today()
    return sum(1 for w in catalog.works(kind="stacks")
               if w.files and CLIP_TAG not in w.tags and catalog.is_airable(w, today))


def main(argv: Sequence[str] | None = None) -> int:
    from catalog import default_db_path
    from schedule import load_schedule
    from station_config import load_station_config

    parser = argparse.ArgumentParser(description="Plan a Stacks block")
    parser.add_argument("--station", default="wit-next")
    parser.add_argument("--at", required=True, help="a time inside the block, wall clock")
    args = parser.parse_args(argv)
    station = load_station_config(args.station)
    schedule = load_schedule(station.schedule_path)
    block = schedule.program_block_at(datetime.strptime(args.at, "%Y-%m-%d %H:%M"))
    with Catalog(default_db_path(args.station)) as catalog:
        plan = plan_for_block(schedule, block, catalog, library=station.library_dir)
    print(f"{plan.title} {plan.start:%a %Y-%m-%d %H:%M}, themes: {', '.join(plan.themes) or '-'}")
    for entry in plan.to_json()["entries"]:
        if entry["type"] == "gap":
            continue
        label = entry.get("title") or (entry.get("source") or {}).get("title") or entry.get("theme") or ""
        print(f"  {entry['starts_at'][11:16]}  {entry['role']:15s} {round(entry['seconds']):4d}s  {label}")
    for problem in plan.problems:
        print(f"  note: {problem}")
    return 0 if plan.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
