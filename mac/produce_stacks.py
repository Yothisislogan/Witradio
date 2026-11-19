"""The Stacks with Steve: the talk, remixes, speech samples and Then & Now tracks.

The server plans each Stacks block from the approved records in the catalog
(mac/stacks_planner.py); the records themselves play from the library. For
every talk entry in a plan this renders buffer/talk/<plan_id>/NNN.flac:

- Steve's Pull One, shelf intros, Call Number, Then & Now intro and Back to
  the Stacks, written only from the plan's record facts (year, performer,
  label, the Library of Congress notes). A script that fails the fact check
  becomes a plain announcement built from those facts.
- The Remix: a line from Steve, then the record re-cut over a new beat
  (mac/remix.py).
- The Speech Sample: a short intro, then the recording over an ambient bed.
- Then & Now: a new track in the original's feel, from the local music engine.

Each segment that carries a borrowed recording gets NNN.meta.json with its
credit and the recording's library path, so now-playing credits it and the
director logs its airplay. The recordings a plan needs are pulled from the
server's library before rendering (buffer_sync pull-media).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json  # noqa: E402
import tempfile  # noqa: E402
from typing import Any  # noqa: E402

import audio_tools as at  # noqa: E402
import remix  # noqa: E402
from produce_classical import ClassicalLinks  # noqa: E402
from producers import Result, single  # noqa: E402
from script_writer import Segment  # noqa: E402
from stacks_planner import BED_STYLES, SPEECH_LONGEST  # noqa: E402

STEVE_NAMES = ["Stacks", "Library of Congress", "Steve", "WIT-FM", "Citizen DJ", "National Jukebox", "Pull One",
               "Call Number", "Then", "Now"]
BRIEFS = {
    "pull_one": ("Open the hour with \"Let's pull one from the stacks.\" Set the hour's theme from FACTS with real "
                 "delight and tease a record or two from it."),
    "shelf_intro": ("Introduce the record in FACTS in about 30 seconds: its year, its performer and its label, and "
                    "one detail that makes it worth hearing. Credit it the way FACTS gives the credit line."),
    "call_number": ("Call Number: the story behind this record in about a minute, built only on FACTS: when it was "
                    "recorded or published, the label and issue number, who made it. Get giddy about the details."),
    "then_now_intro": ("Introduce this original from FACTS (year, performer, label), then say that a brand-new track "
                       "in the same feel follows right after it."),
    "closing_more": ("Back to the Stacks: recap this hour's records with their credits from FACTS, then tease the "
                     "next hour's theme from FACTS."),
    "closing_last": ("Back to the Stacks: recap this hour's records with their credits from FACTS, then sign off: "
                     "\"Return your books on time, and keep digging.\""),
    "remix_tag": "One line, right before the beat drops: name the record from FACTS and say it's been re-cut.",
    "speech_intro": "Introduce this spoken recording from FACTS in about 15 seconds: who, what and when.",
}


def record_lines(entry: dict[str, Any]) -> list[str]:
    lines = [f"Record: {entry.get('title')}."]
    if entry.get("performer"):
        lines.append(f"Performer: {entry['performer']}.")
    if entry.get("composer"):
        lines.append(f"Written by: {entry['composer']}.")
    if entry.get("year"):
        lines.append(f"Published: {entry['year']}.")
    if entry.get("credit"):
        lines.append(f"Credit line: {entry['credit']}")
    lines += [f"Library of Congress notes: {fact}" for fact in entry.get("facts") or []]
    return lines


def closing_text(records: list[dict[str, Any]], last: bool) -> str:
    """Back to the Stacks from the plan alone: each record and performer, then each credit once."""
    played = "; ".join(f"{r.get('title')}" + (f", {r['performer']}" if r.get("performer") else "") for r in records)
    credits = list(dict.fromkeys(str(r["credit"]).rstrip(".") for r in records if r.get("credit")))
    text = "Back to the stacks." + (f" That hour: {played}." if played else "")
    text += "".join(f" Courtesy of {c}." for c in credits)
    return text + (" Return your books on time, and keep digging." if last else " Stay right there.")


class StacksProducer(ClassicalLinks):
    program = "stacks"
    plan_program = "stacks"
    lead_hours = 36.0

    def media(self, source: dict[str, Any]) -> Path | None:
        """The Halo's copy of a planned recording, by its library-relative path, or the path itself."""
        if self.ctx.library_dir is not None and source.get("library_rel"):
            local = self.ctx.library_dir / source["library_rel"]
            if local.exists():
                return local
        path = Path(source.get("path") or "")
        return path if source.get("path") and path.exists() else None

    def media_needed(self, now) -> list[str]:
        """Library-relative paths the due plans need that aren't on the Halo yet."""
        missing = []
        for plan in self.plans_due(now):
            for entry in plan["entries"]:
                source = entry.get("source") or {}
                if entry.get("type") == "talk" and source and self.media(source) is None and source.get("library_rel"):
                    missing.append(source["library_rel"])
        return sorted(set(missing))

    def _records(self, plan: dict[str, Any]) -> dict[int, dict[str, Any]]:
        return {e["work_id"]: e for e in plan["entries"] if e.get("type") == "work" and e.get("work_id")}

    def _hour(self, plan: dict[str, Any], index: int) -> tuple[int, int]:
        """The bounds of the hour an entry belongs to: from its Pull One to its closing."""
        entries = plan["entries"]
        start = max((i for i in range(index + 1) if entries[i].get("role") == "pull_one"), default=0)
        end = next((i for i in range(index, len(entries)) if entries[i].get("role") == "closing"), len(entries) - 1)
        return start, end

    def steve_segment(self, plan: dict[str, Any], index: int) -> tuple[Segment, Any]:
        entries = plan["entries"]
        entry = entries[index]
        role = entry["role"]
        records = self._records(plan)
        start, end = self._hour(plan, index)
        hour_records = [e for e in entries[start:end + 1] if e.get("type") == "work"]
        about = [records[i] for i in entry.get("about") or [] if i in records]
        theme = entries[start].get("theme", "")
        lines = [f"Show: {plan.get('title')}."]
        if theme:
            lines.append(f"This hour's theme: {theme}.")
        if role == "pull_one":
            for record in hour_records[:4]:
                lines += record_lines(record)
            brief = BRIEFS["pull_one"]
        elif role == "closing":
            for record in hour_records:
                lines.append(f"Played: {record.get('title')}. Credit line: {record.get('credit')}")
            following = next((e for e in entries[end + 1:] if e.get("role") == "pull_one"), None)
            if following is not None and following.get("theme"):
                lines.append(f"Next hour's theme: {following['theme']}.")
            last = entry.get("last_hour", following is None)
            brief = BRIEFS["closing_last" if last else "closing_more"]
        else:
            for record in about:
                lines += record_lines(record)
            brief = BRIEFS[role]
        segment = Segment(id=f"{plan['plan_id']}#{index}:{role}", brief=brief, speakers=["steve"],
                          seconds=max(8.0, float(entry.get("seconds") or 30)), facts="\n".join(lines),
                          allow_names=STEVE_NAMES)

        def template():
            first = about[0] if about else (hour_records[0] if hour_records else {})
            year, who, title = first.get("year"), first.get("performer") or "an unknown performer", first.get("title", "")
            text = {
                "pull_one": f"Let's pull one from the stacks. This hour: {theme or 'whatever the shelf gives us'}.",
                "shelf_intro": f"From {year}, here's {who} with {title}." if year else f"Here's {who} with {title}.",
                "call_number": f"Call number time. {title}, by {who}" + (f", published in {year}." if year else "."),
                "then_now_intro": f"From {year}, {title}. Right after it, something brand new in the same feel."
                if year else f"{title}. Right after it, something brand new in the same feel.",
                "closing": closing_text(hour_records, brief == BRIEFS["closing_last"]),
            }[role]
            return single("steve", text, segment.id)

        return segment, template

    def _tag(self, segment_id: str, record: dict[str, Any], brief: str, seconds: float, out: Path,
             fallback: str) -> tuple[Path | None, list[str]]:
        """Steve's short line ahead of a remix or speech sample."""
        segment = Segment(id=segment_id, brief=brief, speakers=["steve"], seconds=seconds,
                          facts="\n".join(record_lines(record)), allow_names=STEVE_NAMES)
        _, path, problems = self.talk(segment, out, template=lambda: single("steve", fallback, segment_id))
        return path, problems

    def render_entry(self, plan: dict[str, Any], index: int, out: Path) -> tuple[dict[str, Any], list[str]]:
        """Render one talk entry to `out`. Returns the sidecar meta and any problems."""
        entry = plan["entries"][index]
        role = entry["role"]
        problems: list[str] = []
        steve = {"title": "The Stacks with Steve", "artist": "Steve"}
        if role in ("pull_one", "shelf_intro", "call_number", "then_now_intro", "closing"):
            segment, template = self.steve_segment(plan, index)
            _, path, problems = self.talk(segment, out, template=template)
            return (steve if path else {}), problems
        source = entry.get("source") or {}
        audio = self.media(source)
        if audio is None:
            return {}, [f"{plan['plan_id']}#{index}: {source.get('title')} isn't on the Halo yet"]
        planned = float(entry.get("seconds") or 180)
        title = source.get("title", "")
        with tempfile.TemporaryDirectory(prefix="stacks-") as tmp:
            work = Path(tmp)
            try:
                if role == "remix":
                    tag, tag_problems = self._tag(f"{plan['plan_id']}#{index}:remix_tag", source,
                                                  BRIEFS["remix_tag"], 8, work / "tag.wav",
                                                  f"Here's {title}, re-cut.")
                    problems += tag_problems
                    info = remix.remix(audio, self.make_music, work / "remix.flac",
                                       style=str(entry.get("bed_style") or "boom-bap"), rotation=BED_STYLES,
                                       bed_seconds=max(30.0, planned - 12))
                    parts = ([tag] if tag else []) + [work / "remix.flac"]
                    at.normalize(at.concat(parts, work / "joined.wav", [0] + [400] * (len(parts) - 1)), out, -16.0)
                    self._keep_script(work / "tag.script.json", out)
                    meta = {"title": f"Remix: {source.get('title')}", "artist": "WIT-FM",
                            "credit": f"Remix of {source.get('credit')} New beat made for WIT-FM.",
                            "license": source.get("license", "public_domain"), "catalog_path": source.get("path", ""),
                            "remix": info}
                elif role == "speech_sample":
                    intro, intro_problems = self._tag(f"{plan['plan_id']}#{index}:speech_intro", source,
                                                      BRIEFS["speech_intro"], 15, work / "intro.wav",
                                                      f"From the stacks: {title}.")
                    problems += intro_problems
                    remix.speech_over_bed(audio, self.make_music, work / "speech.flac", longest=SPEECH_LONGEST)
                    parts = ([intro] if intro else []) + [work / "speech.flac"]
                    at.normalize(at.concat(parts, work / "joined.wav", [0] + [600] * (len(parts) - 1)), out, -16.0)
                    self._keep_script(work / "intro.script.json", out)
                    meta = {"title": f"Speech Sample: {source.get('title')}", "artist": source.get("performer") or "",
                            "credit": source.get("credit", ""), "license": source.get("license", "public_domain"),
                            "catalog_path": source.get("path", "")}
                elif role == "then_now":
                    found = remix.analyse(audio)
                    feel = ", ".join(t for t in source.get("tags") or [] if t not in ("stacks", "loc") and "-" not in t)
                    caption = (f"a brand-new instrumental in the feel of a {source.get('year') or 'century-old'} "
                               f"record ({feel or 'early recording'}), modern production"
                               + (f", {round(found.tempo)} BPM" if found.tempo else "")
                               + (f", {found.key}" if found.key else "") + ", instrumental only, no vocals")
                    if not self.make_music(caption, work / "track.flac", planned):
                        return {}, [f"{plan['plan_id']}#{index}: the music engine didn't return a track"]
                    at.normalize(work / "track.flac", out, -16.0)
                    meta = {"title": f"Then & Now: a new take on {source.get('title')}", "artist": "WIT-FM",
                            "credit": "Made for WIT-FM", "license": "generated", "caption": caption}
                else:
                    return {}, [f"{plan['plan_id']}#{index}: no renderer for {role}"]
            except at.AudioError as exc:
                return {}, [f"{plan['plan_id']}#{index}: {role} failed: {exc}"]
        return meta, problems

    @staticmethod
    def _keep_script(script: Path, out: Path) -> None:
        """Keep what Steve said with the segment, as the Count's links do."""
        if script.exists():
            script.replace(out.with_suffix(".script.json"))

    def produce_plan(self, plan: dict[str, Any]) -> Result:
        result = Result(show_id=plan.get("show_id", ""), slot=plan["plan_id"], status="done")
        for index, entry in enumerate(plan["entries"]):
            if entry.get("type") != "talk":
                continue
            final = self.link_path(plan["plan_id"], index)
            if final.exists():
                continue
            final.parent.mkdir(parents=True, exist_ok=True)
            hidden = final.with_name(f".{final.stem}.partial.flac")
            meta, problems = self.render_entry(plan, index, hidden)
            result.problems += problems
            if not meta or not hidden.exists():
                result.status = "partial"
                continue
            script = hidden.with_suffix(".script.json")
            if script.exists():
                script.replace(final.with_suffix(".json"))
            final.with_name(f"{final.stem}.meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
            hidden.replace(final)
            result.segments.append(final.name)
        result.folder = self.ctx.buffer_dir / "talk" / plan["plan_id"]
        return result
