"""Show producers: a show's next airing becomes finished audio in the buffer.

Producers run on the Halo, ahead of air: the bible renders most speech
overnight and keeps live-data segments to the last 15 minutes. What they
write is what mac/playout_director.py reads:

- show segments: buffer/<show_id>/<slot>/NN_<name>.flac, each with a sidecar
  NN_<name>.json (title, artist, credit, license, gain_db, kind), played in
  name order;
- the Count's links: buffer/talk/<plan_id>/NNN.flac, numbered by plan entry.

A segment is written under a dot name and renamed into place, so the sync
never ships half a file. Shows that fail closed write to runtime/rehearsal/
instead of the buffer when a fact came from a provider that isn't cleared
for air, or when WRIT_REHEARSE=1.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_tools as at  # noqa: E402
from schedule import StationSchedule, slot_key  # noqa: E402
from script_writer import ScriptError, ScriptWriter, Segment, Written  # noqa: E402
from voices import Line, Renderer, VoiceError, VoiceScript  # noqa: E402

TALK_LUFS = -16.0
CLASSICAL_TALK_LUFS = -18.0  # the Count's links sit with the classical works


@dataclass
class Airing:
    show_id: str
    start: datetime  # wall clock, schedule zone
    end: datetime
    program: str
    params: dict
    host: str
    title: str

    @property
    def slot(self) -> str:
        return slot_key(self.start)

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class Result:
    show_id: str
    slot: str
    status: str  # done | rehearsed | partial | skipped | failed
    folder: Path | None = None
    segments: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def upcoming_airings(schedule: StationSchedule, now: datetime, hours: float) -> list[Airing]:
    """Airings that start within `hours` of now, plus the one on air, keyed like the director's slots."""
    pairs = schedule.next_airings(now, count=200, horizon_hours=int(hours) + 30)
    airings = []
    for (show_id, start), following in zip(pairs, pairs[1:]):
        if start > schedule.to_wall(now) + timedelta(hours=hours):
            break
        show = schedule.shows[show_id]
        airings.append(Airing(show_id=show_id, start=start, end=following[1], program=show.program,
                              params=dict(show.params), host=show.host,
                              title=schedule.resolve(start).title or show.name))
    return airings


class SlotWriter:
    """Adds finished segments to a show's slot folder, one atomic rename each."""

    def __init__(self, root: Path, show_id: str, slot: str):
        self.folder = root / show_id / slot  # made with the first segment, never empty

    def has(self, name: str) -> bool:
        return any(p.stem.split("_", 1)[-1] == name for p in self.folder.glob("*.flac"))

    def add(self, order: int, name: str, audio: Path, meta: Mapping[str, Any]) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        final = self.folder / f"{order:02d}_{name}.flac"
        hidden = self.folder / f".{final.name}.partial.flac"
        if audio.suffix.lower() == ".flac":
            shutil.copyfile(audio, hidden)
        else:
            at.normalize(audio, hidden)
        sidecar = final.with_suffix(".json")
        sidecar_tmp = self.folder / f".{sidecar.name}.tmp"
        sidecar_tmp.write_text(json.dumps({"license": "generated", "gain_db": 0.0, **meta}, indent=1))
        sidecar_tmp.replace(sidecar)  # the sidecar first, so the audio never appears without it
        hidden.replace(final)
        return final


class Producer:
    program = ""
    lead_hours = 2.0  # produce this far ahead of air
    fail_closed = False

    def __init__(self, ctx: "Context"):
        self.ctx = ctx

    def produce(self, airing: Airing) -> Result:  # pragma: no cover - each producer implements it
        raise NotImplementedError

    def root_for(self, cleared: bool) -> Path:
        if os.environ.get("WRIT_REHEARSE") == "1" or (self.fail_closed and not cleared):
            return self.ctx.rehearsal_dir
        return self.ctx.buffer_dir

    def talk(self, segment: Segment, out: Path, *, template: Callable[[], VoiceScript] | None = None,
             lufs: float = TALK_LUFS) -> tuple[Written | None, Path | None, list[str]]:
        """Write and voice one segment. With a template, a failed script falls back to it."""
        problems: list[str] = []
        script: VoiceScript | None = None
        written: Written | None = None
        try:
            written = self.ctx.writer.write(segment)
            script = written.script
        except ScriptError as exc:
            problems.append(str(exc))
            if template is None:
                return None, None, problems
            script = template()
            problems.append(f"{segment.id}: used the plain template")
        try:
            self.ctx.renderer.render(script, out, target_lufs=lufs)
        except VoiceError as exc:
            problems.append(f"{segment.id}: voice failed: {exc}")
            return written, None, problems
        if script is not None:
            out.with_suffix(".script.json").write_text(json.dumps(script.to_json(), indent=1, ensure_ascii=False))
        return written, out, problems


@dataclass
class Context:
    """What every producer shares on the Halo."""

    schedule: StationSchedule
    renderer: Renderer
    writer: ScriptWriter
    buffer_dir: Path
    rehearsal_dir: Path
    work_dir: Path
    state_dir: Path
    feeds_config: dict = field(default_factory=dict)
    log: Callable[[str], None] = print
    services: dict = field(default_factory=dict)  # injectable clients (feeds, music) for tests

    @classmethod
    def for_station(cls, station, **overrides: Any) -> "Context":
        from feeds import load_feeds_config
        from llm_client import LLMClient
        from schedule import load_schedule

        renderer = overrides.pop("renderer", None) or Renderer.from_config()
        llm = overrides.pop("llm", None) or LLMClient.from_station(station)
        runtime = station.runtime_dir
        return cls(schedule=overrides.pop("schedule", None) or load_schedule(station.schedule_path),
                   renderer=renderer, writer=ScriptWriter.for_renderer(llm, renderer),
                   buffer_dir=station.output_dir / "buffer", rehearsal_dir=runtime / "rehearsal",
                   work_dir=runtime / "producers" / "work", state_dir=runtime / "producers",
                   feeds_config=load_feeds_config(), **overrides)


def words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def single(speaker: str, text: str, segment: str, **line: Any) -> VoiceScript:
    return VoiceScript(segment=segment, lines=[Line(speaker=speaker, text=text, **line)])
