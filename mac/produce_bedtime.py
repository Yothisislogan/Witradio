"""Hushworth's Bedtime Stories: public-domain stories read with every voice, twice a night.

Early Seating (7:30 PM, 30 min, families): a welcome, a 20-25 minute story,
and a goodnight over soft music that runs to the end of the slot. No promos,
no ads, no invitations to send anything in, soft score, no jump scares.
Late Seating (10:00 PM, 1 hr, grown-ups): a creak and a cold open, the story,
the Count ringing the booth during Dracula to object to Stoker's "slander",
a 2-minute afterword with one fact about the author, then a quiet bed to
11:00. Tension allowed, but no sudden loud peaks.

The readings come from the story library (mac/story_library.py), in
config/story_sources.yaml's order: serials continue night after night,
short fables are bundled, and a story too long for the night is either
abridged or split, with "to be continued". The model never rewrites the
author: to abridge, it may only choose which whole paragraphs to keep (the
reading is then announced as "adapted"); to perform, it only says who speaks
each piece and how, so Hushworth can do the wolf, the grandmother and the
creaking door. What Hushworth says around the story is written from facts:
the work, its credit "from the public domain", and the author facts in
config/bedtime_authors.yaml.

Under it all, a soft instrumental bed at 60-75 BPM from the local music
engine (one continuous bed across the reading's files), rain, wind or fire
where the story wants it, the whole show at a lower loudness than daytime,
with every peak held under the limit.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime  # noqa: E402
from typing import Any  # noqa: E402

import audio_tools as at  # noqa: E402
import story_library as sl  # noqa: E402
from producers import Airing, Producer, Result, SlotWriter  # noqa: E402
from script_writer import ScriptError, Segment  # noqa: E402
from voices import Line, VoiceScript  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "bedtime.yaml"
AUTHORS_PATH = PROJECT_ROOT / "config" / "bedtime_authors.yaml"
HOST = "hushworth"
RATE = at.RATE
QUOTE = re.compile(r"(“[^”]*”|\"[^\"]*\"|‘(?:[^’]|’(?=\w))*’(?!\w))")
INVITES = re.compile(r"\b(website|submit|send (us|in)|write (in|to us)|e-?mail|text us|call us|log your|"
                     r"subscribe|follow us|sponsor)\b", re.I)
NAMES = ["Sir Reginald", "Sir Reginald Hushworth", "Hushworth", "WIT-FM", "Bedtime Stories", "Lights", "Blanket",
         "Once", "Goodnight", "Count", "Lock"]
LINES = {"open": "Lights low. Blanket up. Once upon a time...", "leave": "And that, dear listener, is where we leave them.",
         "door": "Lock the door. No, the other door.", "close": "Goodnight. Sleep well. Sir Reginald insists."}
BRIEFS = {
    "welcome": ("Welcome the families to the Early Seating, cozy and warm, about a minute and a half. Name tonight's "
                "story and its author from FACTS and credit it as FACTS gives the credit line. If FACTS says the "
                "reading is adapted, say so. If FACTS gives a chapter, name it. You may share one fact about the "
                "author from FACTS. End with: \"{open}\""),
    "goodnight": ("Say goodnight to the Early Seating, gentle and slow, about 40 seconds. If FACTS says the story "
                  "continues tomorrow, say \"{leave}\" and that it continues tomorrow night. End with: \"{close}\""),
    "cold_open": ("Open the Late Seating: one line of delicious dread, then name tonight's story and its author from "
                  "FACTS and credit it as FACTS gives the credit line; say if FACTS says it's adapted, and name the "
                  "chapter if FACTS gives one. About 25 seconds. Never shout."),
    "afterword": ("The Late Seating's afterword, about 90 seconds, quiet and unhurried: one fact about the author from "
                  "FACTS, a thought about tonight's story, and if FACTS says the story continues, \"{leave}\" and "
                  "that it continues tomorrow night. End with: \"{close}\""),
    "the_count": ("You are the Count, ringing the booth during tonight's reading of Dracula to object, indignant but "
                  "impeccably polite, to Bram Stoker's \"slander\". About 35 seconds. Use only FACTS; don't add plot "
                  "details. Sir Reginald may get one word in."),
}


def _yaml(path: Path) -> dict:
    import yaml

    try:
        return yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        return {}


def _np():
    import numpy as np

    return np


def pieces(paragraph: str, longest: int = 420) -> list[tuple[str, bool]]:
    """A paragraph as spoken pieces: narration and quoted speech, in order, the text untouched."""
    out: list[tuple[str, bool]] = []
    for part in QUOTE.split(paragraph):
        text = part.strip()
        if not text:
            continue
        quoted = bool(QUOTE.fullmatch(text))
        while len(text) > longest:  # long narration: break at a sentence end
            cut = max(text.rfind(". ", 0, longest), text.rfind("! ", 0, longest), text.rfind("? ", 0, longest),
                      text.rfind("; ", 0, longest))
            if cut < longest // 3:
                cut = text.rfind(" ", 0, longest)
            out.append((text[:cut + 1].strip(), quoted))
            text = text[cut + 1:].strip()
        if text:
            out.append((text, quoted))
    return out


def chunk_paragraphs(paragraphs: list[str], words: int) -> list[list[str]]:
    chunks: list[list[str]] = [[]]
    count = 0
    for para in paragraphs:
        n = len(para.split())
        if chunks[-1] and count + n > words * 1.15:
            chunks.append([])
            count = 0
        chunks[-1].append(para)
        count += n
    return [c for c in chunks if c]


class BedtimeProducer(Producer):
    program = "bedtime"
    lead_hours = 24.0
    segments_per_call = 3

    def __init__(self, ctx):
        super().__init__(ctx)
        self.config = _yaml(Path(os.environ.get("WRIT_BEDTIME_CONFIG") or CONFIG_PATH))
        self.authors = _yaml(AUTHORS_PATH).get("authors") or {}
        sources = sl.load_sources()
        self.blocked = list(sources.get("not_in_the_books") or [])
        root = Path(os.environ["WRIT_STORIES_DIR"]).expanduser() if os.environ.get("WRIT_STORIES_DIR") else (
            ctx.library_dir / "stories" if ctx.library_dir is not None else ctx.state_dir / "stories")
        self.library = sl.StoryLibrary(root, sources)
        self.root = ctx.state_dir / "bedtime"

    # ── which reading tonight ──

    def _progress(self) -> dict:
        try:
            return json.loads((self.root / "progress.json").read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_progress(self, progress: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "progress.json").write_text(json.dumps(progress, indent=1))

    def targets(self, seating: str) -> tuple[int, int]:
        low, high = (self.config.get("seatings") or {}).get(seating, {}).get("story_minutes", [20, 25])
        wpm = float(self.config.get("words_per_minute", 125))
        return int(low * wpm), int(high * wpm)

    def next_reading(self, seating: str, problems: list[str]) -> dict | None:
        works = self.library.pulled(seating)
        if not works:
            return None
        progress = self._progress()
        pos = progress.get(seating) or {}
        work_id = pos.get("work") if pos.get("work") in works else works[0]
        unit_i, para_i = int(pos.get("unit", 0)), int(pos.get("para", 0))
        units = self.library.units(work_id)
        if unit_i >= len(units):
            work_id, unit_i, para_i = works[(works.index(work_id) + 1) % len(works)], 0, 0
            units = self.library.units(work_id)
        meta = self.library.meta(work_id)
        low, high = self.targets(seating)
        unit = units[unit_i]
        remaining = unit["paragraphs"][para_i:]
        words = sum(len(p.split()) for p in remaining)
        titles, paragraphs, adapted, continues = [unit["title"]], list(remaining), False, False
        next_unit, next_para = unit_i + 1, 0
        if meta.get("bundle") and para_i == 0:  # short fables: several a night
            while next_unit < len(units) and words + units[next_unit]["words"] <= high:
                paragraphs += [f"{units[next_unit]['title']}."] + units[next_unit]["paragraphs"]
                titles.append(units[next_unit]["title"])
                words += units[next_unit]["words"]
                next_unit += 1
        elif words <= high:
            if meta.get("kind") == "serial" and words < low and next_unit < len(units) \
                    and words + units[next_unit]["words"] <= high:
                paragraphs += [f"{units[next_unit]['title']}."] + units[next_unit]["paragraphs"]
                titles.append(units[next_unit]["title"])
                next_unit += 1
        elif meta.get("kind") != "serial" and para_i == 0 and words <= high * float(self.config.get("abridge_up_to", 1.6)):
            keep = self.abridge(paragraphs, high, problems)
            if keep is not None:
                paragraphs, adapted = [paragraphs[i] for i in keep], True
            else:
                paragraphs, next_unit, next_para, continues = self.split(paragraphs, high, unit_i, para_i)
        else:
            paragraphs, next_unit, next_para, continues = self.split(paragraphs, high, unit_i, para_i)
        if next_unit >= len(units) and not continues:
            following = works[(works.index(work_id) + 1) % len(works)]
            progress[seating] = {"work": following, "unit": 0, "para": 0}
        else:
            progress[seating] = {"work": work_id, "unit": next_unit, "para": next_para}
        self._save_progress(progress)
        return {"work": work_id, "titles": titles, "paragraphs": paragraphs, "adapted": adapted,
                "continues": continues or (meta.get("kind") == "serial" and next_unit < len(units)),
                "part": para_i > 0, "meta": meta}

    @staticmethod
    def split(paragraphs: list[str], high: int, unit_i: int, para_i: int) -> tuple[list[str], int, int, bool]:
        """Read up to the night's length, stopping at a paragraph; the rest is tomorrow's."""
        taken, count = [], 0
        for para in paragraphs:
            n = len(para.split())
            if taken and count + n > high:
                break
            taken.append(para)
            count += n
        return taken, unit_i, para_i + len(taken), True

    def abridge(self, paragraphs: list[str], target: int, problems: list[str]) -> list[int] | None:
        """Ask the model which whole paragraphs to keep. It can't change a word: it only picks."""
        total = sum(len(p.split()) for p in paragraphs)
        numbered = "\n".join(f"[{i}] {p}" for i, p in enumerate(paragraphs))
        messages = [
            {"role": "system", "content": "You abridge public-domain stories for reading aloud at bedtime. You never "
                                          "rewrite a word: you only choose which whole paragraphs to keep."},
            {"role": "user", "content": f"Choose paragraphs to keep so the reading is about {target} words (it is "
                                        f"{total} now). Keep the first and last paragraphs and the thread of the "
                                        "story. Reply as JSON: {\"keep\": [paragraph numbers, in order]}.\n\n" + numbered}]
        try:
            keep = json.loads(self.ctx.writer.llm.chat(messages, json_mode=True, temperature=0.2)).get("keep")
            keep = sorted({int(i) for i in keep})
        except Exception as exc:  # a bad reply means split instead
            problems.append(f"abridging failed ({exc}); splitting the story instead")
            return None
        kept = sum(len(paragraphs[i].split()) for i in keep if 0 <= i < len(paragraphs))
        if not keep or keep[0] != 0 or keep[-1] != len(paragraphs) - 1 or keep[-1] >= len(paragraphs) \
                or not 0.7 * target <= kept <= 1.1 * target:
            problems.append(f"the abridgement kept {kept} words of {total}, wanted about {target}; splitting instead")
            return None
        return keep

    # ── planning an airing ──

    def plan_path(self, airing: Airing) -> Path:
        return self.root / "airings" / f"{airing.show_id}_{airing.slot}.json"

    def plan(self, airing: Airing, problems: list[str]) -> dict | None:
        path = self.plan_path(airing)
        if path.exists():
            return json.loads(path.read_text())
        seating = str(airing.params.get("seating", "early"))
        reading = self.next_reading(seating, problems)
        if reading is None:
            return None
        chunks = chunk_paragraphs(reading["paragraphs"], int(self.config.get("chunk_words", 650)))
        names = [f"story_{i + 1:02d}" for i in range(len(chunks))]
        if seating == "early":
            order = ["welcome"] + names + ["goodnight", "lullaby"]
        else:
            order = ["cold_open"] + names + (["the_count"] if reading["meta"].get("count_calls_in") else []) + \
                    ["afterword", "quiet_bed"]
        plan = {"id": f"{airing.show_id}_{airing.slot}", "seating": seating, "seconds": airing.seconds,
                "reading": {k: v for k, v in reading.items() if k != "paragraphs"}, "chunks": chunks,
                "order": order, "elapsed": {}, "made": datetime.now().isoformat(timespec="seconds")}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, indent=1, ensure_ascii=False))
        return plan

    # ── the words around the story ──

    def facts(self, plan: dict) -> list[str]:
        reading, meta = plan["reading"], plan["reading"]["meta"]
        facts = [f"Tonight's story: {meta['title']}, by {meta['author']}.", f"Credit line: {meta['credit']}"]
        if meta.get("kind") != "single":
            facts.append(f"Tonight's part: {', '.join(reading['titles'])}.")
        if reading.get("part"):
            facts.append("Tonight continues where last night's reading stopped.")
        if reading.get("adapted"):
            facts.append("This reading is adapted: abridged from the original.")
        if reading.get("continues"):
            facts.append("The story continues tomorrow night.")
        return facts

    def author_facts(self, plan: dict, count: int = 1) -> list[str]:
        meta = plan["reading"]["meta"]
        facts = list(self.authors.get(meta.get("author"), []))
        if not facts:
            return []
        start = sum(1 for p in (self.root / "airings").glob("*.json")) % len(facts)  # a different one each night
        return [facts[(start + k) % len(facts)] for k in range(min(count, len(facts)))]

    def words_around(self, name: str, plan: dict, problems: list[str]) -> VoiceScript:
        seating, meta = plan["seating"], plan["reading"]["meta"]
        speaker = "count" if name == "the_count" else HOST
        facts = self.facts(plan)
        seconds = {"welcome": 90, "goodnight": 40, "cold_open": 25, "afterword": 90, "the_count": 35}[name]
        if name in ("welcome", "afterword"):
            facts += [f"About the author: {f}" for f in self.author_facts(plan, 2 if name == "welcome" else 1)]
        if name == "the_count":
            facts = [f"Tonight's chapter of Dracula: {', '.join(plan['reading']['titles'])}.",
                     "Bram Stoker's novel Dracula, published in 1897, makes Count Dracula its villain.",
                     "The Count, WIT-FM's night host, considers the book slander."]
        segment = Segment(id=f"bedtime/{plan['id']}/{name}", brief=BRIEFS[name].format(**LINES), speakers=[speaker],
                          seconds=seconds, facts="\n".join(facts), allow_names=NAMES + ["Dracula", "Stoker", "Bram"])
        try:
            voice = self.ctx.writer.write(segment).script
            spoken = [l.text for l in voice.lines]
            if sl.film_only(spoken, self.blocked):
                raise ScriptError(f"names from the film versions: {', '.join(sl.film_only(spoken, self.blocked))}")
            if seating == "early" and INVITES.search(" ".join(spoken)):
                raise ScriptError("invited listeners to send something in, at the Early Seating")
        except ScriptError as exc:
            problems.append(f"{name}: {exc}; used the plain version")
            voice = VoiceScript(segment=segment.id, lines=[Line(speaker=speaker, text=t) for t in
                                                           self.template(name, plan)])
        texts = " ".join(l.text for l in voice.lines).lower()
        if name in ("welcome", "cold_open"):
            if meta["author"].lower() not in texts or "public domain" not in texts:
                voice.lines.append(Line(speaker=HOST, text=f"Tonight: {meta['credit']}"))
            if plan["reading"].get("adapted") and "adapted" not in texts:
                voice.lines.append(Line(speaker=HOST, text="Tonight's reading is adapted from the original."))
        if name == "welcome" and not texts.rstrip(" .").endswith("once upon a time"):
            voice.lines.append(Line(speaker=HOST, text=LINES["open"], pace="slow"))
        if name == "cold_open":
            voice.lines.insert(0, Line(speaker=HOST, text=LINES["door"], emotion="a low, conspiratorial whisper"))
        if name in ("goodnight", "afterword") and LINES["close"].lower().rstrip(".") not in texts:
            voice.lines.append(Line(speaker=HOST, text=LINES["close"], pace="slow"))
        return voice

    def template(self, name: str, plan: dict) -> list[str]:
        meta, reading = plan["reading"]["meta"], plan["reading"]
        part = f" {', '.join(reading['titles'])}." if meta.get("kind") != "single" else ""
        more = "And that, dear listener, is where we leave them. More tomorrow night." if reading.get("continues") else ""
        return {
            "welcome": [f"Good evening, my dears. Tonight, {meta['credit']}{part}"],
            "goodnight": [x for x in (more, LINES["close"]) if x],
            "cold_open": [f"Good evening. Tonight, {meta['credit']}{part}"],
            "afterword": [x for x in ([f"A word about the author. {f}" for f in self.author_facts(plan)] +
                                      [more, LINES["close"]]) if x],
            "the_count": ["Sir Reginald, forgive the intrusion. Mr. Stoker has been most unkind. I shall say no more."],
        }[name]

    def perform(self, paragraphs: list[str], plan: dict, problems: list[str]) -> VoiceScript:
        """The author's text, every word, with who says each piece and how."""
        spoken = []
        for k, para in enumerate(paragraphs):
            for j, (text, quoted) in enumerate(pieces(para)):
                spoken.append({"text": text, "quoted": quoted, "new_paragraph": j == 0 and k > 0})
        listing = "\n".join(f"[{i}] {'(speech) ' if p['quoted'] else ''}{p['text']}" for i, p in enumerate(spoken))
        mood = "gentle and cozy, for children" if plan["seating"] == "early" else "quietly menacing, but never loud"
        messages = [
            {"role": "system", "content": "You direct a solo actor reading a public-domain story aloud. You never "
                                          "change the text; you only say who speaks each piece and how."},
            {"role": "user", "content": f"Label each numbered piece. Give each character a short voice description "
                                        f"(the actor does them all). The reading is {mood}. Reply as JSON: "
                                        "{\"cast\": {\"character\": \"voice\"}, \"lines\": [{\"i\": 0, \"who\": "
                                        "\"narrator or a character\", \"how\": \"a few words of delivery\"}]}\n\n"
                                        + listing}]
        cast: dict[str, str] = {}
        labels: dict[int, dict] = {}
        try:
            reply = json.loads(self.ctx.writer.llm.chat(messages, json_mode=True, temperature=0.3))
            cast = {str(k): str(v) for k, v in (reply.get("cast") or {}).items()}
            labels = {int(x["i"]): x for x in reply.get("lines") or [] if isinstance(x, dict) and "i" in x}
        except Exception as exc:  # unlabeled, it's still Hushworth reading the story
            problems.append(f"performance notes failed ({exc}); read as narration")
        lines = []
        for i, piece in enumerate(spoken):
            label = labels.get(i, {})
            who = str(label.get("who") or "narrator")
            how = str(label.get("how") or ("softly" if plan["seating"] == "early" else "low and measured"))[:80]
            if who.lower() != "narrator" and piece["quoted"]:
                emotion = f"as {who}: {cast.get(who, '')}, {how}".replace(": , ", ": ")
            else:
                emotion = f"narrating, {how}"
            lines.append(Line(speaker=HOST, text=piece["text"], emotion=emotion[:120],
                              pause_before_ms=650 if piece["new_paragraph"] else 0))
        return VoiceScript(segment=f"bedtime/{plan['id']}", lines=lines)

    # ── sound ──

    def bed(self, seating: str, problems: list[str]) -> Path | None:
        """Tonight's soft score: a new bed from the music engine if it answers, else a recent one."""
        settings = (self.config.get("seatings") or {}).get(seating, {})
        folder = self.root / "beds" / seating
        folder.mkdir(parents=True, exist_ok=True)
        caption = (f"{settings.get('bed_style', 'gentle lullaby')}, {settings.get('bed_bpm', 64)} BPM, very soft and "
                   "steady, no sudden changes, a bed for a bedtime story, instrumental only, no vocals")
        raw = folder / f".new-{datetime.now():%Y%m%d%H%M%S}.flac"
        if self.make_music(caption, raw, 180):
            dest = folder / raw.name[5:]
            at.normalize(raw, dest, float(self.config.get("loudness", -20.0)))
            raw.unlink(missing_ok=True)
            return dest
        raw.unlink(missing_ok=True)
        beds = sorted(folder.glob("*.flac"))
        if not beds:
            problems.append("no music bed: the music engine didn't answer and none is cached")
            return None
        return beds[-1]

    def produce(self, airing: Airing, now: datetime | None = None) -> Result:
        writer = SlotWriter(self.root_for(True), airing.show_id, airing.slot)
        result = Result(airing.show_id, airing.slot, "done", folder=writer.folder)
        plan = self.plan(airing, result.problems)
        if plan is None:
            result.status = "skipped"
            result.problems.append("no stories in the library: python3 tools/pull_stories.py pull --all")
            return result
        if not plan.get("bed"):
            bed = self.bed(plan["seating"], result.problems)
            plan["bed"] = str(bed) if bed else ""
            self._save(airing, plan)
        made = 0
        for order, name in enumerate(plan["order"]):
            if writer.has(name):
                continue
            if made >= self.segments_per_call:
                result.status = "partial"
                break
            with tempfile.TemporaryDirectory(prefix="bedtime-") as tmp:
                audio, meta = self.render(name, plan, Path(tmp), result.problems)
                if audio is None:
                    plan["elapsed"][name] = 0.0
                    self._save(airing, plan)
                    continue
                plan["elapsed"][name] = at.duration(audio)
                self._save(airing, plan)
                writer.add(order, name, audio, meta)
            result.segments.append(name)
            made += 1
        return result

    def _save(self, airing: Airing, plan: dict) -> None:
        self.plan_path(airing).write_text(json.dumps(plan, indent=1, ensure_ascii=False))

    def render(self, name: str, plan: dict, work: Path, problems: list[str]) -> tuple[Path | None, dict]:
        lufs = float(self.config.get("loudness", -20.0))
        meta = plan["reading"]["meta"]
        show = {"title": "Hushworth's Bedtime Stories", "artist": "Sir Reginald Hushworth", "credit": "",
                "license": "generated", "kind": "talk"}
        offset = sum(plan["elapsed"].get(n, 0.0) for n in plan["order"][:plan["order"].index(name)])
        bed = Path(plan["bed"]) if plan.get("bed") else None
        if name in ("lullaby", "quiet_bed"):
            seconds = plan["seconds"] - offset - 3.0
            if seconds < 10 or bed is None:
                return None, {}
            out = self.mix(None, bed, offset, work / f"{name}.flac", seconds=seconds, bed_gain_db=-8.0,
                           fade_out=min(20.0, seconds / 2))
            return out, {**show, "credit": "Music made for WIT-FM", "kind": "music"}
        voice_path = work / "voice.wav"
        if name.startswith("story_"):
            index = int(name.split("_")[1]) - 1
            script = self.perform(plan["chunks"][index], plan, problems)
            title = f"{meta['title']}: {', '.join(plan['reading']['titles'])}"
            info = {**show, "title": title + (" (adapted)" if plan["reading"].get("adapted") else ""),
                    "credit": meta["credit"], "license": "public_domain", "kind": "reading", "work": meta["id"]}
        else:
            script = self.words_around(name, plan, problems)
            info = {**show, "artist": "The Count" if name == "the_count" else show["artist"]}
        self.ctx.renderer.render(script, voice_path, target_lufs=lufs)
        if name == "cold_open":
            voice_path = at.concat([creak(work / "creak.wav"), voice_path], work / "opened.wav", [0, 500])
        ambience = meta.get("ambience") if name.startswith("story_") else None
        out = self.mix(voice_path, bed, offset, work / f"{name}.flac", ambience=ambience,
                       fade_in=4.0 if name in ("welcome", "cold_open") else 0.0)
        return out, info

    def mix(self, voice: Path | None, bed: Path | None, offset: float, out: Path, *, seconds: float | None = None,
            bed_gain_db: float | None = None, ambience: str | None = None, fade_in: float = 0.0,
            fade_out: float = 0.0) -> Path:
        """Voice over the bed, which carries on from `offset`, so consecutive files flow as one."""
        np = _np()
        speech = _decode(voice) if voice is not None else None
        n = len(speech) if speech is not None else int(round((seconds or 0) * RATE))
        mix = np.zeros((n, 2), dtype=np.float32)
        if speech is not None:
            mix += speech
        gain = 10 ** ((bed_gain_db if bed_gain_db is not None else float(self.config.get("bed_db", -20.0))) / 20)
        if bed is not None and bed.exists():
            mix += gain * _loop(_decode(bed), int(round(offset * RATE)), n)
        if ambience:
            sound = ambient(self.root / "ambience" / f"{ambience}.wav", ambience)
            mix += 10 ** (-26 / 20) * _loop(_decode(sound), int(round(offset * RATE)), n)
        if fade_in:
            k = min(n, int(fade_in * RATE))
            mix[:k] *= np.linspace(0, 1, k, dtype=np.float32)[:, None]
        if fade_out:
            k = min(n, int(fade_out * RATE))
            mix[n - k:] *= np.linspace(1, 0, k, dtype=np.float32)[:, None]
        limit = 10 ** (float(self.config.get("limit_db", -3.0)) / 20)  # the limiter: no sudden peaks at bedtime
        knee = limit * 0.9
        over = np.abs(mix) > knee
        if over.any():
            mix[over] = np.sign(mix[over]) * (knee + (limit - knee) * np.tanh((np.abs(mix[over]) - knee) / (limit - knee)))
        return _write(out, mix)


def _decode(path: Path):
    np = _np()
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(RATE), "-"],
                            capture_output=True, timeout=600)
    if result.returncode != 0:
        raise at.AudioError(f"can't decode {path}")
    return np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 2).copy()


def _loop(samples, start: int, n: int):
    """n samples of a loop from `start`, the loop point crossfaded so it never clicks."""
    np = _np()
    if len(samples) == 0:
        return np.zeros((n, 2), dtype=np.float32)
    fade = min(len(samples) // 4, 2 * RATE)
    loop = samples[:len(samples) - fade].copy() if fade else samples.copy()
    if fade:
        ramp = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
        loop[:fade] = samples[:fade] * ramp + samples[len(samples) - fade:] * (1 - ramp)
    idx = (start + np.arange(n)) % len(loop)
    return loop[idx]


def _write(out: Path, samples) -> Path:
    np = _np()
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    result = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "-",
                             "-c:a", "flac", str(out)], input=pcm, capture_output=True, timeout=600)
    if result.returncode != 0:
        raise at.AudioError("flac encode failed")
    return out


def _write_wav(dest: Path, mono) -> Path:
    import wave

    np = _np()
    dest.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
    with wave.open(str(dest), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.repeat(pcm[:, None], 2, axis=1).tobytes())
    return dest


def ambient(dest: Path, kind: str, seconds: float = 30.0) -> Path:
    """Rain, wind or fire, made here (once) from filtered noise."""
    if dest.exists():
        return dest
    np = _np()
    rng = np.random.default_rng({"rain": 1, "wind": 2, "fire": 3}.get(kind, 4))
    n = int(seconds * RATE)
    white = rng.standard_normal(n).astype(np.float32)
    if kind == "wind":
        brown = np.cumsum(white)
        brown -= np.convolve(brown, np.ones(4410) / 4410, mode="same")
        swell = 0.6 + 0.4 * np.sin(2 * np.pi * np.arange(n) / RATE / 7.0) * np.sin(2 * np.pi * np.arange(n) / RATE / 3.1)
        sound = brown / (np.abs(brown).max() + 1e-9) * swell
    elif kind == "fire":
        brown = np.cumsum(white)
        brown -= np.convolve(brown, np.ones(2205) / 2205, mode="same")
        sound = 0.5 * brown / (np.abs(brown).max() + 1e-9)
        for t in rng.uniform(0, seconds - 0.05, int(seconds * 6)):  # crackles
            i = int(t * RATE)
            k = int(0.004 * RATE)
            sound[i:i + k] += rng.uniform(0.2, 0.7) * np.exp(-np.arange(k) / (0.0008 * RATE)) * rng.choice([-1, 1])
    else:  # rain
        hiss = white - np.convolve(white, np.ones(8) / 8, mode="same")
        sound = 0.35 * hiss / (np.abs(hiss).max() + 1e-9)
        for t in rng.uniform(0, seconds - 0.02, int(seconds * 40)):  # drops
            i = int(t * RATE)
            k = int(0.006 * RATE)
            sound[i:i + k] += rng.uniform(0.05, 0.3) * np.exp(-np.arange(k) / (0.001 * RATE))
    sound = (sound / (np.abs(sound).max() + 1e-9) * 0.5).astype(np.float32)
    return _write_wav(dest, sound)


def creak(dest: Path) -> Path:
    """A slow door creak for the Late Seating's cold open."""
    if dest.exists():
        return dest
    np = _np()
    t = np.arange(int(1.4 * RATE)) / RATE
    pitch = 180 + 120 * np.sin(2 * np.pi * 0.6 * t) + 40 * np.sin(2 * np.pi * 7 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / RATE
    grain = (np.sin(2 * np.pi * 31 * t) > 0.6).astype(np.float32)  # stick-slip
    wave_ = (np.sin(phase) + 0.5 * np.sin(2 * phase) + 0.25 * np.sin(3 * phase)) * (0.4 + 0.6 * grain)
    envelope = np.minimum(1, t / 0.1) * np.minimum(1, (1.4 - t) / 0.3)
    return _write_wav(dest, (0.3 * wave_ * envelope / np.abs(wave_).max()).astype(np.float32))
