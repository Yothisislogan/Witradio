"""The Market with Dottie Sterling: Opening Bell, Closing Tape, Week Ahead.

Each format follows the bible's clock as separate segments. The live ones
(The Tape, this morning's releases) are written 20 minutes before air so
they carry the latest numbers; the rest about 2 hours ahead. Every segment
fails closed: a script that can't pass the fact check is dropped, and the
director plays the next segment or music. Every episode ends with the
disclaimer.

Clearance is per segment: a segment whose facts came only from cleared
providers (Treasury, the Fed Board, FRED's government series) can air, while
one that leans on, say, the index closes FRED republishes rehearses until
those are licensed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from producers import Airing, Producer, Result, SlotWriter, single
from script_writer import Segment

LIVE_LEAD_MINUTES = 20
DISCLAIMER = "The Market is information, not investment advice."
TAPE = "THE TAPE (latest closes available)"
YIELDS = "TREASURY YIELDS"
COMMODITIES = "COMMODITIES"
RATES = "RATES"
ECONOMY = "THE ECONOMY"
CALENDAR = "CALENDAR (scheduled data releases)"
FED = "FEDERAL RESERVE BOARD (press releases)"
MORNING = "THIS MORNING'S RELEASES (Bureau of Labor Statistics)"


@dataclass(frozen=True)
class Part:
    order: int
    name: str
    title: str
    seconds: float
    live: bool
    sections: tuple[str, ...]
    brief: str


SIGN_OFF = ("Sign off in your own voice: \"That's the market. It'll be here tomorrow. So will I.\" Mention the next "
            f"show in FACTS. The very last line must be exactly: \"{DISCLAIMER}\"")
FORMATS: dict[str, list[Part]] = {
    "closing_tape": [
        Part(0, "tape", "The Tape", 210, True, (TAPE, YIELDS, COMMODITIES),
             "Open with \"Afternoon. Let's read the tape.\" Read the closes and yields in FACTS, each once, with "
             "its direction, then say in one line why it matters to a regular person."),
        Part(1, "big_story", "The Big Story", 300, False, (FED, ECONOMY, YIELDS),
             "The day's driver, from what FACTS reports: what happened, who said it, and why you care. Credit "
             "the source by name."),
        Part(2, "the_number", "The Number", 240, False, (ECONOMY,),
             "\"Here's the number. Here's why you care.\" Pick one economic figure from FACTS and explain it in "
             "plain English, like you're explaining it to your nephew."),
        Part(3, "main_street", "Main Street", 240, False, (RATES, YIELDS, ECONOMY, COMMODITIES),
             "What FACTS means for households and small businesses: borrowing costs, prices at the pump and the "
             "store, what to watch on the next bill. No advice, just what it means."),
        Part(4, "on_the_record", "On the Record", 210, False, (FED,),
             "What the Federal Reserve Board put on the record this week, in plain English, credited to the "
             "Federal Reserve Board."),
        Part(5, "tomorrow", "Tomorrow", 120, False, (CALENDAR,), "The data releases coming up, from FACTS."),
        Part(6, "sign_off", "Sign-off", 20, False, (), SIGN_OFF),
    ],
    "opening_bell": [
        Part(0, "this_morning", "This Morning", 300, True, (MORNING, YIELDS, TAPE),
             "Open with \"Morning. Let's read the tape.\" The 8:30 releases in FACTS first, then yesterday's "
             "closes and today's yields, each with why you care."),
        Part(1, "today", "Today's Calendar", 180, False, (CALENDAR,), "Today's scheduled releases from FACTS."),
        Part(2, "story_to_watch", "One Story to Watch", 240, False, (FED, ECONOMY),
             "One story to watch today, from FACTS, and why it matters."),
        Part(3, "sign_off", "Sign-off", 20, False, (), SIGN_OFF),
    ],
    "week_ahead": [
        Part(0, "last_week", "Last Week", 240, False, (TAPE, YIELDS),
             "\"Let's read the tape.\" Where things closed, from FACTS."),
        Part(1, "the_week", "The Week Ahead", 420, False, (CALENDAR,),
             "The week's scheduled releases, day by day, from FACTS, and which ones matter most and why."),
        Part(2, "explainer", "The Explainer", 600, False, (ECONOMY, RATES, YIELDS),
             "One deeper explainer built on FACTS: what the figure measures, where it stands, why it moves, "
             "and what it means for your wallet."),
        Part(3, "sign_off", "Sign-off", 20, False, (), SIGN_OFF),
    ],
}


class MarketProducer(Producer):
    program = "market"
    lead_hours = 2.0
    fail_closed = True

    def facts(self, fmt: str, airing: Airing):
        build: Callable[..., Any] | None = self.ctx.services.get("market_facts")
        if build is None:
            import market_feed

            config = self.ctx.feeds_config.get("markets") or {}
            providers = market_feed.providers_from_env(config)
            return market_feed.build_facts(fmt, providers, config, today=airing.start.date())
        return build(fmt, airing.start.date())

    def next_show(self, airing: Airing) -> str:
        try:
            return self.ctx.schedule.resolve(airing.end).title
        except Exception:  # never let the schedule stop a sign-off
            return ""

    def produce(self, airing: Airing, now: datetime | None = None) -> Result:
        now = now or self.ctx.schedule.now()
        fmt = str(airing.params.get("format", "closing_tape"))
        parts = FORMATS.get(fmt)
        result = Result(show_id=airing.show_id, slot=airing.slot, status="done")
        if parts is None:
            result.status, result.problems = "skipped", [f"no Market format {fmt}"]
            return result
        live_due = self.ctx.schedule.to_wall(now) >= airing.start - timedelta(minutes=LIVE_LEAD_MINUTES)
        sheet = None
        rehearsed = aired = 0
        for part in parts:
            if part.live and not live_due:
                continue
            if any(SlotWriter(root, airing.show_id, airing.slot).has(part.name)
                   for root in (self.ctx.buffer_dir, self.ctx.rehearsal_dir)):
                continue
            if part.sections:
                sheet = sheet or self.facts(fmt, airing)
                facts = sheet.text(*part.sections)
                if not facts.strip():
                    result.problems.append(f"{part.name}: no facts from the feeds")
                    continue
                cleared = sheet.cleared_for(*part.sections)
                sources = {n for h in part.sections for n in sheet.section_sources.get(h, [])}
                credit = "; ".join(sorted({sheet.sources[n].credit for n in sources if sheet.sources[n].credit}))
            else:
                following = self.next_show(airing)
                facts = f"Show: {airing.title}, {airing.start:%A %B} {airing.start.day}." + (
                    f" Next on WIT-FM: {following}." if following else "")
                cleared, credit = True, ""
            segment = Segment(id=f"{fmt}/{part.name}", brief=part.brief, speakers=["dottie"], seconds=part.seconds,
                              facts=facts, allow_names=["Market", "Main Street", "Wall Street", "WIT-FM", "Fed"],
                              allow_numbers=["8:30", "4:00"])
            template = None
            if part.name == "sign_off":
                template = lambda: single("dottie", "That's the market. It'll be here tomorrow. So will I. "  # noqa: E731
                                          + DISCLAIMER, segment.id)
            out = self.ctx.work_dir / airing.show_id / airing.slot / f"{part.name}.flac"
            out.parent.mkdir(parents=True, exist_ok=True)
            written, path, problems = self.talk(segment, out, template=template)
            result.problems += problems
            if path is None:
                continue  # fails closed: nothing airs for this segment
            if part.name == "sign_off" and written is not None and DISCLAIMER not in written.script.text:
                self.ctx.renderer.render(template(), out)
                result.problems.append("sign_off: the disclaimer was missing; used the plain sign-off")
            root = self.root_for(cleared)
            SlotWriter(root, airing.show_id, airing.slot).add(part.order, part.name, path, {
                "title": f"The Market: {part.title}", "artist": "Dottie Sterling", "credit": credit,
                "kind": "talk", "show": airing.title})
            result.segments.append(part.name)
            if root == self.ctx.rehearsal_dir:
                rehearsed += 1
            else:
                aired += 1
        if rehearsed and not aired:
            result.status = "rehearsed"
        elif rehearsed or (result.problems and result.segments):
            result.status = "partial"
        elif not result.segments and result.problems:
            result.status = "failed"
        result.folder = self.ctx.buffer_dir / airing.show_id / airing.slot
        return result
