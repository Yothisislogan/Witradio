"""The Count's links for the classical blocks.

The server plans each classical block (mac/playout_director.py plan-ahead)
and the sync brings the plans to the Halo. For every talk entry in a plan,
this writes the Count's link from that plan's own facts (the works, their
composers, performers and credits, plus a composer fact file where one
exists), voices it, and leaves it at buffer/talk/<plan_id>/NNN.flac for the
director. A link the model can't get through the fact check falls back to a
plain announcement built only from the plan, so nothing invented airs.

Composer fact files (the bible: "Every anecdote is checked against a
per-composer fact file (dates, premieres, places)") are plain text files in
WRIT_COMPOSER_FACTS (default config/composers/), named like
johann-sebastian-bach.txt, one fact per line.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from producers import CLASSICAL_TALK_LUFS, Producer, Result, single
from script_writer import Segment

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPENING_ROLES = {"recollection", "program", "open", "intro"}
CLOSING_ROLES = {"closing", "sign_off", "handoff"}
BRIEFS = {
    "recollection": ("Open the night: greet the night creatures, then tell a short recollection about the main "
                     "composer in FACTS, as if you were there, using only the dates, places and works FACTS gives. "
                     "End by introducing the first work."),
    "program": ("You have just woken at dusk. Read tonight's concert program in order: each work, its composer "
                "and its performers, briefly. Then introduce the first work."),
    "open": "Open the block warmly and introduce the first work: title, composer, performers.",
    "intro": ("A brief, calm introduction to the next work, recorded last night before you retired. Title, "
              "composer, performers. Nothing more."),
    "link": ("Back-sell the work that just ended (title, composer, performers) and credit the recording the way "
             "FACTS gives its credit line. Then introduce the next work."),
    "back_announce": ("Name the work or works just played, their composers and performers, and credit the "
                      "recording the way FACTS gives its credit line. Calm and brief."),
    "closing": "Close the block: back-sell the last work and its credit, then say goodbye.",
    "sign_off": ("Dawn is coming. Back-sell the last work and its credit, then give your dawn sign-off: "
                 "\"The sky is turning a color I find disagreeable. Until tomorrow night.\""),
    "handoff": "Back-sell the last work and its credit, then hand over to the next show named in FACTS.",
}


def slugify(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")


def work_facts(entry: dict[str, Any]) -> str:
    parts = [f"Work: {entry.get('title')}", f"composer: {entry.get('composer') or 'unknown'}"]
    if entry.get("performer"):
        parts.append(f"performed by {entry['performer']}")
    if entry.get("composition_year"):
        parts.append(f"composed by {entry['composition_year']}")
    if entry.get("form"):
        parts.append(f"form: {entry['form']}")
    parts.append(f"about {round(float(entry.get('seconds') or 0) / 60)} minutes")
    line = "; ".join(parts) + "."
    if entry.get("parts"):
        line += f" Movements: {', '.join(entry['parts'])}."
    if entry.get("credit"):
        line += f" Recording credit line: {entry['credit']}."
    return line


class ClassicalLinks(Producer):
    program = "classical"
    lead_hours = 36.0

    def facts_dir(self) -> Path:
        return Path(os.environ.get("WRIT_COMPOSER_FACTS") or PROJECT_ROOT / "config" / "composers")

    def composer_facts(self, composer: str) -> list[str]:
        path = self.facts_dir() / f"{slugify(composer)}.txt"
        if not composer or not path.exists():
            return []
        return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]

    def plans_due(self, now: datetime) -> list[dict[str, Any]]:
        if now.tzinfo is None:  # the schedule's clock is naive wall time
            now = self.ctx.schedule.localize(now)
        folder = self.ctx.buffer_dir / "plans"
        due = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            if path.name.startswith("_"):
                continue
            try:
                plan = json.loads(path.read_text())
                starts = datetime.fromisoformat(plan["starts_at"])
                ends = datetime.fromisoformat(plan["ends_at"])
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
            if ends > now and starts <= now + timedelta(hours=self.lead_hours):
                due.append(plan)
        return sorted(due, key=lambda p: p["starts_at"])

    def link_path(self, plan_id: str, index: int) -> Path:
        return self.ctx.buffer_dir / "talk" / plan_id / f"{index:03d}.flac"

    def segment_for(self, plan: dict[str, Any], index: int) -> tuple[Segment, Any]:
        entries = plan["entries"]
        entry = entries[index]
        role = entry["role"]
        works = [(i, e) for i, e in enumerate(entries) if e["type"] == "work"]
        before = [e for i, e in works if i < index]
        after = [e for i, e in works if i > index]
        about = [e for _, e in works if e.get("work_id") in set(entry.get("about") or [])]
        if role in ("program",):
            chosen = [e for _, e in works]
        elif role in OPENING_ROLES:
            chosen = about + [e for e in after[:1] if e not in about]
        elif role in CLOSING_ROLES:
            chosen = before[-1:]
        elif role == "back_announce":
            last_talk = max((i for i, e in enumerate(entries[:index]) if e["type"] == "talk"), default=-1)
            chosen = [e for i, e in works if last_talk < i < index]
        else:  # link
            chosen = before[-1:] + after[:1]
        start = datetime.fromisoformat(plan["starts_at"])
        lines = [f"Block: {plan.get('title')}, {start:%A %B} {start.day}, starting {start:%-I:%M %p} Eastern."]
        if plan.get("theme"):
            lines.append(f"Tonight's theme: {plan['theme']}.")
        for e in chosen:
            label = "Just played" if e in before else "Next" if e in after else "Tonight"
            lines.append(f"{label}. {work_facts(e)}")
        composers = []
        for e in chosen:
            if e.get("composer") and e["composer"] not in composers:
                composers.append(e["composer"])
        for composer in composers:
            lines += [f"About {composer}: {fact}" for fact in self.composer_facts(composer)]
        if role == "handoff":
            try:
                following_show = self.ctx.schedule.resolve(datetime.fromisoformat(plan["ends_at"])).title
            except Exception:  # an odd schedule shouldn't stop the link
                following_show = ""
            if following_show:
                lines.append(f"Next on WIT-FM: {following_show}.")
        brief = BRIEFS.get(role, BRIEFS["link"])
        if role in ("intro", "back_announce") or plan.get("block") == "day_shift":
            brief += " These daytime links were recorded last night: no 'tonight' or 'good evening'."
        segment = Segment(id=f"{plan['plan_id']}#{index}:{role}", brief=brief, speakers=["count"],
                          seconds=max(8.0, float(entry.get("seconds") or 20)), facts="\n".join(lines),
                          allow_numbers=["1689", "1824", "1913", "337"])

        def template():
            text = []
            if role in OPENING_ROLES and plan.get("block") != "day_shift":
                text.append("Good evening, night creatures.")
            last = before[-1] if before else None
            if last is not None and role not in OPENING_ROLES:
                text.append(f"That was {last['title']}, by {last.get('composer') or 'an unknown composer'}"
                            + (f", performed by {last['performer']}" if last.get("performer") else "") + ".")
            following = after[0] if after else None
            if following is not None and role not in CLOSING_ROLES:
                text.append(f"Next, {following['title']}, by {following.get('composer') or 'an unknown composer'}.")
            if role == "sign_off":
                text.append("Until tomorrow night.")
            return single("count", " ".join(text) or "Good evening.", segment.id, pace="slow")

        return segment, template

    def produce_plan(self, plan: dict[str, Any]) -> Result:
        result = Result(show_id=plan.get("show_id", ""), slot=plan["plan_id"], status="done")
        for index, entry in enumerate(plan["entries"]):
            if entry.get("type") != "talk":
                continue
            final = self.link_path(plan["plan_id"], index)
            if final.exists():
                continue
            final.parent.mkdir(parents=True, exist_ok=True)
            segment, template = self.segment_for(plan, index)
            hidden = final.with_name(f".{final.stem}.partial.flac")
            _, out, problems = self.talk(segment, hidden, template=template, lufs=CLASSICAL_TALK_LUFS)
            result.problems += problems
            if out is None:
                result.status = "partial"
                continue
            script = hidden.with_suffix(".script.json")
            if script.exists():
                script.replace(final.with_suffix(".json"))
            hidden.replace(final)
            result.segments.append(final.name)
        result.folder = self.ctx.buffer_dir / "talk" / plan["plan_id"]
        return result

    def produce_due(self, now: datetime | None = None) -> list[Result]:
        now = now or datetime.now(timezone.utc)
        return [self.produce_plan(plan) for plan in self.plans_due(now)]
