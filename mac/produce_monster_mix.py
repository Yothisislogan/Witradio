"""Monster Mix with Wit: drifting AI music, generated on the Halo.

Every track comes from the local music engine (music-gen.server running
ACE-Step, MUSIC_GEN_URL) on a prompt from the Drift. Wit breaks in at :00,
:15, :30 (the Risk Report, then a fresh track "out of the lab") and :45, in
30 seconds or less, narrating the drift from the log. The same producer
fills classical slots that haven't unlocked yet, since the director plays
Monster Mix there.

Generating hours of music takes time, so each slot gets at most
WRIT_MONSTER_MIX_TRACKS_PER_HOUR new tracks (default 8); when a slot runs
out, the director moves on to the safe library.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import os
from datetime import datetime
from typing import Any, Callable

import audio_tools as at
from drift import Drift, DriftLog, Track
from producers import Airing, Producer, Result, SlotWriter, single
from script_writer import Segment

TRACK_SECONDS = 180.0
BREAK_MINUTES = (0, 15, 30, 45)


def energy_for(airing: Airing) -> tuple[int, int]:
    bands = airing.params.get("energy_bpm") or {}
    hour, weekday = airing.start.hour, airing.start.weekday() < 5
    if hour >= 22 or hour < 5:
        return tuple(bands.get("late_night", (78, 100)))  # Monster Mix covering the overnight
    if weekday and hour < 12:
        return tuple(bands.get("weekday_morning", (95, 115)))
    return tuple(bands.get("evening_and_weekend", (100, 125)))


class MonsterMixProducer(Producer):
    program = "monster_mix"
    lead_hours = 12.0  # music takes longest: work well ahead

    def generate(self, caption: str, out, seconds: float) -> bool:
        return self.make_music(caption, out, seconds)

    def produce(self, airing: Airing, now: datetime | None = None) -> Result:
        result = Result(show_id=airing.show_id, slot=airing.slot, status="done")
        slot = SlotWriter(self.root_for(True), airing.show_id, airing.slot)
        if any(slot.folder.glob("*.flac")):
            return result  # already made
        hours = max(0.25, airing.seconds / 3600)
        per_hour = int(os.environ.get("WRIT_MONSTER_MIX_TRACKS_PER_HOUR", "8"))
        count = max(1, min(int(airing.seconds // TRACK_SECONDS), round(per_hour * hours)))
        log = DriftLog(self.ctx.state_dir / "drift.jsonl")
        previous, seen = log.last()
        drift = Drift(energy=energy_for(airing), landmark_every=int(airing.params.get("landmark_every", 4)))
        planned = drift.run(count, start=previous, offset=seen)
        work = self.ctx.work_dir / airing.show_id / airing.slot
        work.mkdir(parents=True, exist_ok=True)

        made: list[tuple[Track, Any]] = []
        for n, track in enumerate(planned):
            raw = work / f"track{n:02d}.flac"
            if not self.generate(track.caption(), raw, TRACK_SECONDS):
                result.problems.append(f"track {n}: the music engine didn't return audio")
                if not made and n >= 1:
                    break  # the engine is down: don't wait out every track
                continue
            level = work / f"track{n:02d}.level.flac"
            at.normalize(raw, level, -16.0)
            made.append((track, level))
        if not made:
            result.status = "failed"
            return result
        log.append([t for t, _ in made], airing.slot)

        # Lay out the hour clock: a Wit break before the tracks that start near :00, :15, :30, :45.
        order, clock, since_break = 0, airing.start.minute * 60.0, []
        breaks_done: set[tuple[float, int]] = set()
        for index, (track, path) in enumerate(made):
            minute = int((clock // 60) % 60)
            due = next((m for m in BREAK_MINUTES if m <= minute < m + 15 and (clock // 3600, m) not in breaks_done), None)
            if due is not None and (index == 0 or since_break):
                breaks_done.add((clock // 3600, due))
                kind = "open" if index == 0 else "risk" if due == 30 else "drift"
                piece, problems = self.wit_break(airing, kind, since_break, track, work / f"break{order:02d}.flac")
                result.problems += problems
                name = "wit_open" if kind == "open" else f"wit_{due:02d}"
                if piece is not None:
                    slot.add(order, name, piece, {"title": "Monster Mix: Wit", "artist": "Wit", "credit": "",
                                                  "kind": "talk", "show": airing.title})
                    order += 1
                    result.segments.append(name)
                since_break = []
            title = f"{track.genre.title()} drift, {track.bpm} BPM"
            slot.add(order, f"track_{index:02d}", path, {
                "title": title, "artist": "WIT-FM", "credit": "Generated for WIT-FM", "kind": "music",
                "show": airing.title, "drift": track.describe(), "caption": track.caption()})
            order += 1
            result.segments.append(f"track_{index:02d}")
            since_break.append(track)
            clock += TRACK_SECONDS
        result.folder = slot.folder
        if result.problems:
            result.status = "partial"
        return result

    def wit_break(self, airing: Airing, kind: str, played: list[Track], coming: Track, out):
        """kind: open (the show's first break), drift (narrate the trip) or risk (the :30 Risk Report)."""
        facts = [f"Show: {airing.title}, {airing.start:%A}."]
        if played:
            facts.append(f"The drift so far: started at {played[0].describe()}; now at {played[-1].describe()}.")
            facts += [f"Change: {t.change}." for t in played if t.change != "start"]
        facts.append(f"Coming up: {coming.describe()}.")
        brief = {
            "open": "Open the show: \"It's your monster, Wit, and we are drifting.\" Tease the first track from FACTS.",
            "drift": ("A Wit break, 25 seconds at most: say where the drift started and where it is now, from FACTS, "
                      "then tease what's coming."),
            "risk": ("Open with \"Today I learned something about risk.\" Tell a 20-second misadventure of your own "
                     "(no real people, brands or numbers), then say a brand-new track is fresh out of the lab."),
        }[kind]
        segment = Segment(id=f"monster_mix/wit_{kind}", brief=brief, speakers=["wit"], seconds=25,
                          facts="\n".join(facts), allow_names=["Monster Mix", "Wit", "WIT"], fact_check=kind != "risk")
        template = lambda: single("wit", "It's your monster, Wit, and we are drifting.", segment.id)  # noqa: E731
        _, path, problems = self.talk(segment, out, template=template)
        return path, problems
