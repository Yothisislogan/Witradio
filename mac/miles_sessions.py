"""Miles with Logan: the session menu as timelines (show bible: Session menu, How the show runs).

A session is a run of music blocks, each at one exact tempo, plus the cues
Logan says over them:

- a bell 10 seconds before every interval change, and "Pick it up in
  three... two... one." ending right on the change when the pace goes up;
- a time call every 5 minutes (every 15 in Long Slow Saturday, which keeps
  the talk minimal) and a halfway call;
- every open says "Headphones low, eyes up, and listen to your body.";
- a Strength Minute (squats or lunges to a 120 BPM count) inside the walks.

Cues are placed by priority: a lower-priority cue that would talk over a
higher one waits for a gap or is dropped, so Logan never talks over himself.
An airing longer than its sessions gets an Easy Miles walk to fill the rest.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

MINUTE = 60.0
ZONES = (  # name, low BPM, high BPM, the genres that live there (the bible's tempo-zone table)
    ("easy", 90, 110, "lo-fi, neo-soul, chill funk"),
    ("walk", 110, 125, "funk, disco, soul, pop"),
    ("power", 125, 135, "house, nu-disco, dance-pop"),
    ("jog", 150, 165, "pop-punk, electro, uptempo rock"),
    ("run", 165, 180, "drum and bass, punk, fast electro"),
)
BELL_LEAD = 10.0  # seconds before an interval change
CUE_GAP = 0.4  # air between two cues

# Who wins when two cues want the same moment.
PRIORITY = {"open": 100, "safety": 99, "bell": 90, "countdown": 85, "change": 80, "round": 75, "count": 70,
            "halfway": 60, "time": 50, "route": 45, "club": 40, "close": 38, "encourage": 20}


def zone_for(bpm: float | None) -> tuple[str, str]:
    """(zone name, genres) for a tempo: the zone it's in, or the nearest one."""
    if bpm is None:
        return "ambient", "soft ambient pads, slow swells, no drums"

    def distance(zone):
        _, low, high, _ = zone
        return 0.0 if low <= bpm <= high else min(abs(bpm - low), abs(bpm - high))

    name, _, _, genres = min(ZONES, key=distance)
    return name, genres


@dataclass
class Block:
    start: float  # seconds from the session's start
    seconds: float
    bpm: float | None  # None: an ambient bed with no beat (the stretch)
    label: str
    interval: bool = False  # an interval change: the bell rings 10 seconds before it
    cue: str | None = None  # the cue that names it, at its start

    @property
    def end(self) -> float:
        return self.start + self.seconds


@dataclass
class Event:
    at: float  # seconds from the session's start
    cue: str  # a cue id from config/miles_cues.yaml, "bell", or "line:<name>" for a written line
    role: str  # a PRIORITY key
    align: str = "start"  # "end": the cue finishes at `at` (the countdown lands on the change)
    slack: float = 20.0  # how far later it may move to find a gap

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.role, 10)


@dataclass
class Session:
    id: str
    title: str
    seconds: float
    blocks: list[Block] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    route: dict | None = None
    minimal_talk: bool = False

    def tempos(self) -> list[float]:
        return [b.bpm for b in self.blocks if b.bpm]


TITLES = {"walk_it_out": "Walk It Out", "intervals": "Intervals", "pyramid": "Pyramid",
          "route_of_the_day": "Route of the Day", "long_slow_saturday": "Long Slow Saturday",
          "recovery_sunday": "Recovery Sunday", "easy_miles": "Easy Miles"}
LENGTHS = {"walk_it_out": 45, "intervals": 30, "pyramid": 40, "route_of_the_day": 45, "long_slow_saturday": 90,
           "recovery_sunday": 30}
STRETCHES = ["stretch_calf", "stretch_quad", "stretch_hamstring", "stretch_hip", "stretch_glute",
             "stretch_shoulders", "stretch_chest", "stretch_back", "stretch_neck"]


class _Builder:
    def __init__(self, session: Session):
        self.session = session
        self.t = 0.0

    def block(self, minutes: float, bpm: float | None, label: str, *, interval: bool = False,
              cue: str | None = None) -> Block:
        block = Block(start=self.t, seconds=minutes * MINUTE, bpm=bpm, label=label, interval=interval, cue=cue)
        self.session.blocks.append(block)
        self.t = block.end
        return block

    def event(self, at: float, cue: str, role: str, **kw) -> None:
        self.session.events.append(Event(at=at, cue=cue, role=role, **kw))


def _strength(b: _Builder, kind: str) -> None:
    """The Strength Minute: a 120 BPM count, one rep every two seconds."""
    block = b.block(1, 120.0, "strength", interval=True, cue=f"strength_{kind}")
    first = block.start + 6.0  # after Logan names it
    reps = int((block.seconds - 9.0) // 2.0)
    for rep in range(reps):
        b.event(first + 2.0 * rep, f"count_{rep % 10 + 1}", "count", slack=0.0)
    b.event(block.end, "strength_done", "change")


def build(session_id: str, *, rng: random.Random | None = None, minutes: float | None = None,
          route: Mapping | None = None) -> Session:
    """One session from the menu. `minutes` sizes Easy Miles; the others have the bible's lengths."""
    rng = rng or random.Random(0)
    title = TITLES.get(session_id, session_id.replace("_", " ").title())
    length = minutes if session_id == "easy_miles" else LENGTHS.get(session_id)
    if length is None:
        raise ValueError(f"unknown session {session_id!r}")
    session = Session(id=session_id, title=title, seconds=length * MINUTE, route=dict(route) if route else None,
                      minimal_talk=session_id == "long_slow_saturday")
    b = _Builder(session)
    strength = rng.choice(["squats", "lunges"])

    if session_id == "walk_it_out":  # steady 115-125 with one 5-minute power-walk push
        b.block(5, 115, "warm-up", cue="warmup")
        b.block(15, 120, "walk")
        b.block(5, 130, "push", interval=True, cue="push_power")
        b.block(7, 122, "walk", interval=True, cue="push_done")
        _strength(b, strength)
        b.block(8, 122, "walk", interval=True)  # "That's the minute" names it
        b.block(4, 112, "cool-down", cue="cooldown")
    elif session_id == "intervals":  # 5 min warm-up; 7 x (2 min jog at 160 + 1 min walk at 120); 4 min cool-down
        b.block(5, 105, "warm-up", cue="warmup")
        for round_no in range(1, 8):
            jog = b.block(2, 160, "jog", interval=True, cue="jog_go")
            b.event(jog.start + 2.5, "last_round" if round_no == 7 else f"round_{round_no}", "round", slack=15.0)
            b.block(1, 120, "walk", interval=True, cue="walk_go")
        b.block(4, 100, "cool-down", interval=True, cue="cooldown")
    elif session_id == "pyramid":  # climbs 115 to 170 over 20 min, holds 5, steps down over 15
        for i, bpm in enumerate((115, 123, 131, 139, 147, 155, 163, 170)):
            b.block(2.5, bpm, "climb", interval=i > 0, cue="pyramid_up" if i == 0 else "step_up")
        b.block(5, 170, "hold", cue="pyramid_hold")
        for i, bpm in enumerate((160, 150, 140, 130, 120, 115)):
            b.block(2.5, bpm, "descend", interval=True, cue="pyramid_down" if i == 0 else "step_down")
    elif session_id == "route_of_the_day":  # a real route, music themed to the place
        b.block(5, 112, "warm-up", cue="warmup")
        b.block(15, 118, "walk")
        b.block(10, 122, "walk", cue="walk_steady")
        _strength(b, strength)
        b.block(9, 120, "walk", interval=True)
        b.block(5, 110, "cool-down", cue="cooldown")
        b.event(20 * MINUTE, "line:route_middle", "route", slack=60.0)
        b.event(session.seconds - 4 * MINUTE, "line:route_end", "route", slack=45.0)
    elif session_id == "long_slow_saturday":  # steady 150-160, lyric-light, minimal talk
        b.block(5, 150, "settle", cue="long_slow_settle")
        b.block(80, 155, "steady")
        b.block(5, 150, "ease", cue="long_slow_ease")
    elif session_id == "recovery_sunday":  # easy walk at 105-115, then a 10-minute stretch over an ambient bed
        b.block(20, 110, "walk", cue="walk_easy")
        stretch = b.block(10, None, "stretch", interval=True, cue="stretch_intro")
        for i, cue in enumerate(STRETCHES):
            b.event(stretch.start + 25 + i * 62, cue, "change", slack=10.0)
    elif session_id == "easy_miles":  # fills an airing past its sessions
        b.block(length, 118, "walk", cue="easy_miles")
    if abs(b.t - session.seconds) > 0.5:
        raise AssertionError(f"{session_id}: blocks add up to {b.t / 60:g} min, not {length:g}")

    _add_common_events(session, rng)
    return session


def _add_common_events(session: Session, rng: random.Random) -> None:
    b = _Builder(session)
    blocks = session.blocks
    for previous, block in zip(blocks, blocks[1:]):
        if block.interval:
            b.event(block.start - BELL_LEAD, "bell", "bell", slack=0.0)
            if previous.bpm and block.bpm and block.bpm > previous.bpm + 3:
                b.event(block.start, "countdown", "countdown", align="end", slack=0.0)
        if block.cue:
            b.event(block.start, block.cue, "change", slack=8.0)
    if blocks and blocks[0].cue:
        b.event(4.0, blocks[0].cue, "change", slack=150.0)  # after the open, which comes first
    every = 15 * MINUTE if session.minimal_talk else 5 * MINUTE
    halfway = session.seconds / 2
    t = every
    while t < session.seconds - MINUTE:
        if abs(t - halfway) < 1.0:
            pass  # the halfway call covers it
        elif abs(session.seconds - t - 5 * MINUTE) < 1.0:
            b.event(t, "remaining_05", "time")
        else:
            b.event(t, f"time_{round(t / MINUTE):02d}", "time")
        t += every
    b.event(halfway, "halfway", "halfway", slack=30.0)
    if not session.minimal_talk:
        steady = [blk for blk in blocks if blk.seconds >= 4 * MINUTE and blk.bpm]
        for blk in steady:
            for k in range(int(blk.seconds // (4 * MINUTE))):
                at = blk.start + 90 + k * 4 * MINUTE + rng.uniform(0, 40)
                if at < blk.end - 30:
                    b.event(at, f"encourage_{rng.randint(1, ENCOURAGEMENTS):02d}", "encourage", slack=40.0)
    b.event(session.seconds - 45, "session_done", "close", slack=20.0)


ENCOURAGEMENTS = 24


def place(events: Sequence[Event], durations: Mapping[str, float], seconds: float) -> list[tuple[float, Event]]:
    """Start times for the cues that fit: higher priority first, the rest wait for a gap or drop."""
    placed: list[tuple[float, float, Event]] = []
    for event in sorted(events, key=lambda e: (-e.priority, e.at)):
        length = durations.get(event.cue)
        if length is None:
            continue
        start = event.at - length if event.align == "end" else event.at
        chosen = None
        shift = 0.0
        while shift <= event.slack + 1e-9:
            s = start + shift
            if s >= 0 and s + length <= seconds and all(
                    s + length + CUE_GAP <= ps or s >= pe + CUE_GAP for ps, pe, _ in placed):
                chosen = s
                break
            shift += 0.5
        if chosen is not None:
            placed.append((chosen, chosen + length, event))
    return sorted(((s, e) for s, _, e in placed), key=lambda pair: pair[0])


def fill(menu: Sequence[str], minutes: float, day: int) -> list[tuple[str, float]]:
    """The sessions for an airing of `minutes`. The menu takes turns leading, day by day, so every
    session gets its mornings; the rest of the airing is the best fit from the others, and Easy
    Miles covers what's left. Returns (session id, minutes) in air order."""
    menu = [m for m in dict.fromkeys(menu) if m in LENGTHS]
    plan: list[tuple[str, float]] = []
    left = float(minutes)
    if menu:
        lead = menu[day % len(menu)]
        if LENGTHS[lead] <= left:
            plan.append((lead, float(LENGTHS[lead])))
            left -= LENGTHS[lead]
        others = [m for m in menu if m != lead]
        random.Random(day).shuffle(others)
        best: list[str] = []
        best_total = 0.0

        def search(chosen: list[str], total: float) -> None:
            nonlocal best, best_total
            if total > best_total:
                best, best_total = list(chosen), total
            for option in others:
                if option not in chosen and total + LENGTHS[option] <= left:
                    chosen.append(option)
                    search(chosen, total + LENGTHS[option])
                    chosen.pop()

        search([], 0.0)
        plan += [(sid, float(LENGTHS[sid])) for sid in best]
        left -= best_total
    if left >= 2:
        plan.append(("easy_miles", left))
    return plan


def chunks(session: Session, longest: float = 300.0) -> list[tuple[float, float]]:
    """Where to cut the continuous mix into files: at block boundaries where it can, and inside
    long blocks, so no file runs much over five minutes. The files play back to back with no gap."""
    cuts = [0.0]
    for block in session.blocks:
        if block.start - cuts[-1] > 0.5 and block.end - cuts[-1] > longest:
            cuts.append(block.start)
        if block.seconds > longest:
            pieces = -(-block.seconds // longest)
            cuts += [block.start + k * block.seconds / pieces for k in range(1, int(pieces))]
    cuts.append(session.seconds)
    cuts = sorted(set(round(c, 3) for c in cuts))
    return [(a, b) for a, b in zip(cuts, cuts[1:]) if b - a > 0.5]


def describe(session: Session) -> str:
    """The session's shape in words, for Logan's open."""
    tempos = session.tempos()
    if session.id == "intervals":
        return "a five-minute warm-up, seven rounds of two minutes jogging and one minute walking, then a cool-down"
    if session.id == "pyramid":
        return "the tempo climbs every few minutes, holds at the top for five, then steps back down"
    if session.id == "walk_it_out":
        return "a steady walk with one five-minute power-walk push"
    if session.id == "long_slow_saturday":
        return "ninety steady minutes, mostly music"
    if session.id == "recovery_sunday":
        return "an easy walk, then a ten-minute stretch"
    if session.id == "route_of_the_day" and session.route:
        return f"a walk along {session.route.get('name')}"
    return f"a steady walk around {round(sum(tempos) / len(tempos)) if tempos else 118} beats a minute"


def music_seconds(blocks: Iterable[Block]) -> float:
    return sum(b.seconds for b in blocks)
