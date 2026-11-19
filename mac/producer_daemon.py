#!/usr/bin/env python3
"""The producer daemon: runs on the Halo and makes every show before it airs.

Each cycle:
1. pull the server's plans and unlock status (if WRIT_SYNC_REMOTE is set);
2. render the planned shows' talk for plans within 36 hours: the Count's
   links, and The Stacks' talk, remixes, speech samples and Then & Now
   tracks (pulling the library recordings those re-cut first);
3. for each upcoming airing whose lead time has come, run its producer:
   The Market (2 hours ahead, The Tape 20 minutes), Ying & Yang (2 hours),
   Monster Mix (12 hours), Miles with Logan (20 hours, one session per
   cycle), Office Hours (drafted 30 hours ahead for review, voiced once
   approved), and Monster Mix for classical and Stacks slots that haven't
   unlocked yet. The shows with the shortest lead go first, so a long render
   never holds up The Tape;
4. push the finished audio to the server.

Shows without a producer yet (Bedtime Stories) are skipped; their slots fall
back to the safe library.

    python3 mac/producer_daemon.py once
    python3 mac/producer_daemon.py loop --interval 300
    python3 mac/producer_daemon.py show market_closing_tape --at "2026-11-18 16:30" --rehearse
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from buffer_sync import Sync, SyncError  # noqa: E402
from produce_classical import ClassicalLinks  # noqa: E402
from produce_market import MarketProducer  # noqa: E402
from produce_miles import MilesProducer  # noqa: E402
from produce_office_hours import OfficeHoursProducer  # noqa: E402
from produce_monster_mix import MonsterMixProducer  # noqa: E402
from produce_stacks import StacksProducer  # noqa: E402
from produce_ying_yang import YingYangProducer  # noqa: E402
from producers import Airing, Context, Result, upcoming_airings  # noqa: E402

PRODUCERS = {"market": MarketProducer, "ying_yang": YingYangProducer, "monster_mix": MonsterMixProducer,
             "miles": MilesProducer, "office_hours": OfficeHoursProducer}
PLANNED = {"classical": ClassicalLinks, "stacks": StacksProducer}  # the server plans these; the Halo renders talk


class Daemon:
    def __init__(self, ctx: Context, sync: Sync | None = None):
        self.ctx, self.sync = ctx, sync
        self.planned = {name: cls(ctx) for name, cls in PLANNED.items()}
        self.producers = {name: cls(ctx) for name, cls in PRODUCERS.items()}

    def unlocked(self) -> dict[str, bool]:
        path = self.ctx.buffer_dir / "plans" / "_status.json"
        try:
            return dict(json.loads(path.read_text()).get("unlocked") or {})
        except (OSError, json.JSONDecodeError):
            return {}  # unknown: treat planned shows as locked, so their slots still get Monster Mix

    def resolve(self, airing: Airing) -> tuple[Airing, str] | None:
        """The airing as a producer sees it, or None when nothing on the Halo makes it."""
        if airing.program in self.planned:
            if self.unlocked().get(airing.show_id):
                return None  # the plan's talk segments cover it
            fallback = str(airing.params.get("fallback_show", "monster_mix"))
            show = self.ctx.schedule.shows.get(fallback)
            if show is None:
                return None
            return replace(airing, show_id=fallback, program=show.program, params=dict(show.params),
                           host=show.host), show.program
        return (airing, airing.program) if airing.program in self.producers else None

    URGENT_HOURS = 2.0  # shows made this close to air run before everything else in a cycle

    def cycle(self, now: datetime | None = None) -> list[Result]:
        now = self.ctx.schedule.to_wall(now) if now else self.ctx.schedule.now()
        results: list[Result] = []
        if self.sync:
            try:
                self.sync.pull()
            except SyncError as exc:
                self.ctx.log(f"pull failed: {exc}")
        horizon = max(p.lead_hours for p in self.producers.values())
        due = []
        for airing in upcoming_airings(self.ctx.schedule, now, horizon):
            resolved = self.resolve(airing)
            if resolved is None or airing.end <= now:
                continue
            target, program = resolved
            producer = self.producers[program]
            if target.start - now <= timedelta(hours=producer.lead_hours):
                due.append((producer.lead_hours, target.start, target, producer))
        due.sort(key=lambda d: (d[0], d[1]))  # shortest lead first
        results += self._run([d for d in due if d[0] <= self.URGENT_HOURS], now)
        results += self._planned(self.ctx.schedule.localize(now))
        results += self._run([d for d in due if d[0] > self.URGENT_HOURS], now)
        if self.sync:
            try:
                self.sync.push()
            except SyncError as exc:
                self.ctx.log(f"push failed: {exc}")
        return results

    def _run(self, due, now: datetime) -> list[Result]:
        results = []
        for _, _, target, producer in due:
            try:
                result = producer.produce(target, now)
            except Exception as exc:
                result = Result(target.show_id, target.slot, "failed", problems=[repr(exc)])
            if result.segments or result.status not in ("done",):
                self.ctx.log(f"{target.show_id} {target.slot}: {result.status} "
                             f"{len(result.segments)} new" + (f"; {result.problems[0]}" if result.problems else ""))
            results.append(result)
        return results

    def _planned(self, aware: datetime) -> list[Result]:
        """The talk for the server's plans: the Count's links, and The Stacks (its recordings first)."""
        results = []
        stacks = self.planned["stacks"]
        missing = stacks.media_needed(aware) if self.ctx.library_dir is not None else []
        if missing and self.sync:
            try:
                self.sync.pull_media(missing, self.ctx.library_dir)
            except SyncError as exc:
                self.ctx.log(f"library pull failed: {exc}")
        for name, producer in self.planned.items():
            try:
                for result in producer.produce_due(aware):
                    if result.segments or result.status != "done":
                        self.ctx.log(f"{result.slot}: {result.status} {len(result.segments)} new"
                                     + (f"; {result.problems[0]}" if result.problems else ""))
                    results.append(result)
            except Exception as exc:  # one bad plan shouldn't stop the other shows
                self.ctx.log(f"{name} plans failed: {exc!r}")
        return results


def main(argv: Sequence[str] | None = None) -> int:
    from station_config import load_station_config

    parser = argparse.ArgumentParser(description="Make WIT-FM's shows ahead of air, on the Halo")
    parser.add_argument("--station", default=os.environ.get("WRIT_STATION_ID", "wit-next"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("once", help="one cycle")
    p_loop = sub.add_parser("loop", help="a cycle every --interval seconds")
    p_loop.add_argument("--interval", type=float, default=300.0)
    p_show = sub.add_parser("show", help="make one airing now, e.g. to rehearse a show")
    p_show.add_argument("show_id")
    p_show.add_argument("--at", required=True, help="the airing's start, wall clock, e.g. '2026-11-18 16:30'")
    p_show.add_argument("--rehearse", action="store_true", help="write to runtime/rehearsal/, never the buffer")
    args = parser.parse_args(argv)

    station = load_station_config(args.station)
    if getattr(args, "rehearse", False):
        os.environ["WRIT_REHEARSE"] = "1"
    ctx = Context.for_station(station, log=lambda m: print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True))
    daemon = Daemon(ctx, Sync.from_env(ctx.buffer_dir))

    if args.cmd == "show":
        at = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
        for airing in upcoming_airings(ctx.schedule, at, 1):
            if airing.show_id == args.show_id and airing.start == at:
                resolved = daemon.resolve(airing) if airing.program in daemon.planned else (airing, airing.program)
                if resolved is None and airing.program in daemon.planned:
                    print(f"{args.show_id} is unlocked: its talk renders from the server's plan (run 'once')")
                    return 1
                if resolved is None or resolved[1] not in daemon.producers:
                    print(f"{args.show_id} has no producer on the Halo yet")
                    return 1
                producer = daemon.producers[resolved[1]]
                result = producer.produce(resolved[0], at - timedelta(minutes=10))
                while result.status == "partial" and result.segments:  # Miles makes one session per call
                    more = producer.produce(resolved[0], at - timedelta(minutes=10))
                    result.segments += more.segments
                    result.problems += more.problems
                    result.status = more.status
                print(f"{result.status}: {', '.join(result.segments) or 'nothing new'} -> {result.folder}")
                for problem in result.problems:
                    print(f"  {problem}")
                return 0 if result.status != "failed" else 1
        print(f"{args.show_id} doesn't start at {args.at}")
        return 1

    state = ctx.state_dir
    state.mkdir(parents=True, exist_ok=True)
    with (state / ".daemon.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another producer daemon is running")
            return 1
        while True:
            daemon.cycle()
            if args.cmd == "once":
                return 0
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
