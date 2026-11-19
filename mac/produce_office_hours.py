"""Office Hours with Professor Ex: drafted from signed class material, reviewed, then voiced.

The bible's hour clock:

  :00  Roll Call          the bell, tonight's topic
  :02  The Lecture        15 minutes on one topic, one part per section of its notes
  :17  Define That        one term in 60 seconds
  :19  Licensing Lab      five practice questions: read as written, a pause, the answer, the walkthrough
       (recruiting)       "Studying for your license? We Insure Things is hiring..." once a show
  :32  War Stories        a real claim, anonymized, and its lesson
  :45  Ask the Professor  listener questions (answered by a reviewer) or the FAQ
  :55  Homework           one thing to check on your own policy; "Class dismissed."

Saturday's Exam Cram runs 25 practice questions back to back.

Each airing is drafted about 30 hours ahead, from class material a licensed
person has signed (mac/office_hours.py). The draft goes to the review queue;
once approved (or when the review policy lets it through), it's voiced a few
segments per daemon cycle into the show's slot folder. An episode still
waiting 90 minutes before air is replaced by an encore of the last one that
aired. A rejected episode is rewritten with the reviewer's note.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402
from typing import Any  # noqa: E402

import audio_tools as at  # noqa: E402
import office_hours as oh  # noqa: E402
from producers import TALK_LUFS, Airing, Producer, Result, SlotWriter  # noqa: E402
from script_writer import ScriptError, Segment  # noqa: E402
from voices import Line, VoiceScript  # noqa: E402

HOST = "professor_ex"
NAMES = ["Professor Ex", "Professor", "Harold Exley", "Exley", "Office Hours", "WIT-FM", "Licensing Lab",
         "War Stories", "Ask", "Define That", "Roll Call", "Homework", "Exam Cram", "Pop"]
WIT_MENTION = re.compile(r"\bWe Insure Things\b|\bWIT\b(?!-FM)|\bWIT Insurance\b")
BRIEFS = {
    "roll_call": ("Roll Call: open the class. Greet the class, name tonight's topic from FACTS, and tease it with "
                  "one question the lecture will answer. About a minute."),
    "lecture": ("Teach this part of tonight's lecture from FACTS: one idea at a time, a plain everyday example, "
                "and why it matters to the listener's own policy. Use only what FACTS says; no new numbers or "
                "names. End this part with: \"{disclaimer}\""),
    "define_that": "Define That: define the term in FACTS in about 60 seconds, with one everyday example.",
    "walkthrough": ("Walk through this practice question from FACTS like a teacher: why the right answer is right, "
                    "why the wrong choices are wrong, and one way to remember it."),
    "war_story": ("War Stories: tell this claim story from FACTS in order, then its lesson. It's anonymized: never "
                  "add a name, place or detail FACTS doesn't give. If FACTS cites a source, say where it's from. "
                  "End with: \"{disclaimer}\""),
    "ask": ("Ask the Professor: read the question in FACTS (with the listener's first name and city if FACTS "
            "gives them), then answer it from the answer notes in FACTS only. General education, no promises "
            "about anyone's claim. End with: \"{specific}\" and \"{disclaimer}\""),
    "homework": ("Homework: give the class tonight's homework from FACTS, briefly say why it matters, then "
                 "dismiss the class with: \"{dismissal}\""),
}
NUMBER_WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
                "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
                "twenty-one", "twenty-two", "twenty-three", "twenty-four", "twenty-five"]


def _np():
    import numpy as np

    return np


def line(text: str, **kw: Any) -> dict:
    return Line(speaker=HOST, text=text, **kw).to_json()


def script(segment: str, *texts: str, **kw: Any) -> dict:
    return VoiceScript(segment=segment, lines=[Line(speaker=HOST, text=t, **kw) for t in texts if t]).to_json()


def has_line(voice: dict, text: str) -> bool:
    plain = re.sub(r"[^a-z ]", "", text.lower())
    return plain in re.sub(r"[^a-z ]", "", " ".join(l["text"] for l in voice.get("lines", [])).lower())


class OfficeHoursProducer(Producer):
    program = "office_hours"
    lead_hours = 30.0  # time for a reviewer to read it
    segments_per_call = 3

    def __init__(self, ctx):
        super().__init__(ctx)
        self.config = oh.load_config()
        self.root = ctx.state_dir / "office_hours"
        self.queue = oh.Queue(self.root)
        self.archive = self.root / "archive"

    @property
    def lines(self) -> dict:
        return self.config.get("lines") or {}

    def library(self) -> oh.Library:
        return oh.Library(config=self.config)

    @staticmethod
    def episode_id(airing: Airing) -> str:
        return f"{airing.show_id}_{airing.slot}"

    # ── usage, for rotation ──

    def _used(self) -> dict:
        try:
            return json.loads((self.root / "used.json").read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _mark_used(self, kind: str, ids: list[str], day: date) -> None:
        used = self._used()
        for item_id in ids:
            used.setdefault(kind, {})[item_id] = day.isoformat()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "used.json").write_text(json.dumps(used, indent=1))

    def _least_used(self, kind: str, items: list, count: int) -> list:
        used = self._used().get(kind, {})
        return sorted(items, key=lambda i: (used.get(i.id if hasattr(i, "id") else i["id"], ""),
                                            i.id if hasattr(i, "id") else i["id"]))[:count]

    # ── writing ──

    def write(self, name: str, brief: str, facts: list[str], seconds: float, note: str,
              fallback: list[str]) -> tuple[dict, list[str]]:
        """A segment in the Professor's voice, written from facts. A script that can't pass the fact
        check falls back to reading the signed notes as written."""
        if note:
            brief += f" The reviewer asked for this change last time: {note}"
        segment = Segment(id=f"office_hours/{name}", brief=brief, speakers=[HOST], seconds=seconds,
                          facts="\n".join(facts), allow_names=NAMES)
        try:
            written = self.ctx.writer.write(segment)
            return written.script.to_json(), []
        except ScriptError as exc:
            return script(f"office_hours/{name}", *fallback), [f"{name}: {exc}; read the notes as written"]

    def _question_text(self, number: int, question: dict) -> str:
        choices = question.get("choices") or {}
        keys = sorted(choices)
        read = ", ".join(f"{k}: {choices[k]}" for k in keys[:-1]) + (f", or {keys[-1]}: {choices[keys[-1]]}"
                                                                      if len(keys) > 1 else "")
        return f"Question {NUMBER_WORDS[number - 1]}. {question['question']} Is it {read}?"

    def lab_parts(self, questions: list[oh.Item], walkthrough_seconds: float, note: str, problems: list[str], *,
                  first: int = 1, intro: bool = True) -> list[dict]:
        parts = [{"script": script("office_hours/lab_intro", "Licensing Lab. Pencils up.",
                                   self.lines.get("practice", ""))}] if intro else []
        for number, item in enumerate(questions, start=first):
            q = item.data
            facts = [f"Question: {q['question']}"] + [f"Choice {k}: {v}" for k, v in sorted((q.get('choices') or {}).items())]
            facts += [f"Correct answer: {q['answer']}", f"Explanation: {q.get('explanation', '')}"]
            if q.get("why_not"):
                facts.append(f"Why the others are wrong: {q['why_not']}")
            parts.append({"script": script(f"office_hours/q{number}", self._question_text(number, q), pace="slow")})
            parts.append({"pause": float(self.config.get("answer_pause", 8))})
            walk, issues = self.write(f"walkthrough_{item.id}", BRIEFS["walkthrough"], facts, walkthrough_seconds,
                                      note, [q.get("explanation", ""), q.get("why_not", "")])
            problems += issues
            walk["lines"].insert(0, line(f"The answer is {q['answer']}."))
            parts.append({"script": walk})
        return parts

    # ── drafting ──

    def draft(self, airing: Airing, note: str = "") -> dict | None:
        library = self.library()
        if airing.params.get("format") == "exam_cram":
            episode = self.draft_cram(airing, library, note)
        else:
            episode = self.draft_class(airing, library, note)
        if episode is None:
            return None
        flags = episode.setdefault("flags", [])
        for segment in episode["segments"]:
            if segment["name"] == "recruiting":
                continue
            text = " ".join(l["text"] for p in segment["parts"] for l in (p.get("script") or {}).get("lines", []))
            if WIT_MENTION.search(text) and "names WIT outside the recruiting line" not in flags:
                flags.append("names WIT outside the recruiting line")
        review = self.config.get("review") or {}
        gate = int(review.get("gate_episodes", 10))
        needs = self.queue.approved_count() < gate or review.get("after_gate", "flagged") == "always" or bool(flags)
        episode.update(id=self.episode_id(airing), show_id=airing.show_id, slot=airing.slot,
                       airs=airing.start.strftime("%a %Y-%m-%d %H:%M"), title=airing.title,
                       status="pending_review" if needs else "approved", reviewer=None, note="",
                       made=datetime.now().isoformat(timespec="seconds"), rendered=[])
        self.queue.save(episode)
        return episode

    def draft_class(self, airing: Airing, library: oh.Library, note: str) -> dict | None:
        lecture = oh.lecture_for(library, airing.start.date())
        if lecture is None:
            return None
        day = airing.start.date()
        clock = self.config.get("clock") or {}
        problems: list[str] = []
        flags: list[str] = []
        sources = [f"lecture {lecture.id}, signed by {library.signer(lecture)}"]
        title = lecture.data.get("title") or lecture.title
        disclaimer = self.lines.get("disclaimer", "")
        segments = []

        def add(name: str, seconds: float, parts: list[dict]) -> None:
            segments.append({"name": name, "seconds": seconds, "parts": parts})

        first = [f for _, lines in lecture.data["sections"][:1] for f in lines[:3]]
        roll, issues = self.write("roll_call", BRIEFS["roll_call"], [f"Tonight's topic: {title}."] + first, 60, note,
                                  [f"Good evening, class. Tonight: {title}."])
        problems += issues
        add("roll_call", 60, [{"logo": True}, {"script": roll}])

        sections = lecture.data["sections"]
        total = float(clock.get("lecture", 15)) * 60
        weights = [max(1, len(lines)) for _, lines in sections]
        for k, ((heading, lines), weight) in enumerate(zip(sections, weights), start=1):
            seconds = total * weight / sum(weights)
            facts = [f"Tonight's lecture: {title}.", f"This part: {heading}."] + lines
            part, issues = self.write(f"lecture_{k}", BRIEFS["lecture"].format(disclaimer=disclaimer), facts, seconds,
                                      note, [heading + "."] + lines)
            problems += issues
            if disclaimer and not has_line(part, disclaimer):
                part["lines"].append(line(disclaimer))
            add(f"lecture_{k}", seconds, [{"script": part}])
        if lecture.state_rules:
            flags.append("state rules: " + "; ".join(lecture.state_rules))

        terms = [t for t in library.ready("term")]
        wanted = lecture.data.get("define_that")
        term = next((t for t in terms if t.id == wanted), None) or (self._least_used("terms", terms, 1) or [None])[0]
        if term is not None:
            define, issues = self.write("define_that", BRIEFS["define_that"], [f"Term: {term.data.get('term')}."]
                                        + term.facts(), 60, note, [f"Define that: {term.data.get('term')}."] + term.facts())
            problems += issues
            define["lines"].insert(0, line("Define that.", pace="slow"))
            add("define_that", 60, [{"script": define}])
            sources.append(f"term {term.id}, signed by {library.signer(term)}")
            self._mark_used("terms", [term.id], day)

        count = int(self.config.get("lab_questions", 5))
        questions = self._least_used("questions", library.ready("question"), count)
        if questions:
            seconds = float(clock.get("licensing_lab", 13)) * 60
            walk = max(45.0, (seconds - 20) / len(questions) - 30)
            add("licensing_lab", seconds, self.lab_parts(questions, walk, note, problems))
            sources += [f"question {q.id}, signed by {library.signer(q)}" for q in questions]
            self._mark_used("questions", [q.id for q in questions], day)
            if any(q.data.get("state", "general") != "general" for q in questions):
                flags.append("state-specific practice questions")

        recruiting = library.items.get(("line", "recruiting"))
        if recruiting is not None and library.signed(recruiting):
            add("recruiting", 15, [{"script": script("office_hours/recruiting", recruiting.data["text"])}])

        stories = []
        for story in library.ready("story"):
            found = oh.pii_in(story.facts() + [story.data.get("lesson") or "", story.data.get("title") or ""])
            if found:
                problems.append(f"war story {story.id} skipped: its notes look like they hold {', '.join(found)}")
            else:
                stories.append(story)
        story = (self._least_used("stories", stories, 1) or [None])[0]
        if story is not None:
            facts = [f"Story: {story.data.get('title')}."] + story.facts() + [f"The lesson: {story.data.get('lesson')}"]
            if story.data.get("citation"):
                facts.append(f"Source: {story.data['citation']}")
            seconds = min(float(clock.get("war_stories", 13)) * 60, 60 + 45 * len(story.facts()))
            told, issues = self.write("war_story", BRIEFS["war_story"].format(disclaimer=disclaimer), facts, seconds,
                                      note, facts)
            problems += issues
            if disclaimer and not has_line(told, disclaimer):
                told["lines"].append(line(disclaimer))
            told["lines"].insert(0, line("War stories.", pace="slow"))
            add("war_stories", seconds, [{"script": told}])
            sources.append(f"story {story.id} ({story.data.get('source')}), signed by {library.signer(story)}")
            self._mark_used("stories", [story.id], day)
            if story.data.get("source") == "wit_claim":
                flags.append("a story from WIT's own claims")

        asked = self.listener_questions()
        faq = self._least_used("faq", library.ready("faq"), 2)
        picks = asked[:2] or [{"id": f.id, "question": f.data.get("question"), "notes": f.facts()} for f in faq]
        if picks:
            seconds = float(clock.get("ask_the_professor", 10)) * 60 / max(1, len(picks))
            parts = []
            for pick in picks:
                facts = [f"Question: {pick['question']}"] + [f"Answer note: {n}" for n in pick.get("notes") or []]
                who = " in ".join(x for x in (pick.get("name"), pick.get("city")) if x)
                if who:
                    facts.insert(0, f"From a listener: {who}.")
                answer, issues = self.write(f"ask_{pick['id']}", BRIEFS["ask"].format(
                    specific=self.lines.get("specific_claims", ""), disclaimer=disclaimer), facts,
                    min(seconds, 60 + 30 * len(pick.get("notes") or [])), note,
                    [pick["question"]] + list(pick.get("notes") or []))
                problems += issues
                for closing in (self.lines.get("specific_claims"), disclaimer):
                    if closing and not has_line(answer, closing):
                        answer["lines"].append(line(closing))
                parts.append({"script": answer})
            parts[0]["script"]["lines"].insert(0, line("Ask the Professor."))
            add("ask_the_professor", seconds * len(picks), parts)
            if asked:
                flags.append("listener questions")
                self._mark_used("listener", [p["id"] for p in picks], day)
            else:
                self._mark_used("faq", [p["id"] for p in picks], day)
                sources += [f"faq {p['id']}" for p in picks]

        dismissal = self.lines.get("dismissal", "")
        homework, issues = self.write("homework", BRIEFS["homework"].format(dismissal=dismissal),
                                      [f"Tonight's homework: {lecture.data.get('homework')}"], 75, note,
                                      [f"Homework: {lecture.data.get('homework')}"])
        problems += issues
        if dismissal and not has_line(homework, dismissal):
            homework["lines"].append(line(dismissal))
        add("homework", 75, [{"script": homework}])
        return {"kind": "class", "topic": lecture.id, "topic_title": title, "segments": segments, "flags": flags,
                "sources": sources, "problems": problems}

    def draft_cram(self, airing: Airing, library: oh.Library, note: str) -> dict | None:
        wanted = int(airing.params.get("questions", 25))
        questions = self._least_used("questions", library.ready("question"), wanted)
        if len(questions) < 5:
            return None
        problems: list[str] = []
        opening = script("office_hours/cram_open", "Good afternoon, class. Exam Cram.",
                         f"{NUMBER_WORDS[len(questions) - 1].capitalize()} practice questions, back to back. Pencils up.",
                         self.lines.get("practice", ""))
        segments = [{"name": "roll_call", "seconds": 30, "parts": [{"logo": True}, {"script": opening}]}]
        half = (len(questions) + 1) // 2
        for block, chunk, first in (("questions_1", questions[:half], 1), ("questions_2", questions[half:], half + 1)):
            parts = self.lab_parts(chunk, 50.0, note, problems, first=first, intro=False)  # the opening said it
            segments.append({"name": block, "seconds": 110.0 * len(chunk), "parts": parts})
            recruiting = library.items.get(("line", "recruiting"))
            if block == "questions_1" and recruiting is not None and library.signed(recruiting):
                segments.append({"name": "recruiting", "seconds": 15,
                                 "parts": [{"script": script("office_hours/recruiting", recruiting.data["text"])}]})
        segments.append({"name": "dismissal", "seconds": 15, "parts": [{"script": script(
            "office_hours/cram_close", "That's the cram. Pencils down.", self.lines.get("dismissal", ""))}]})
        self._mark_used("questions", [q.id for q in questions], airing.start.date())
        flags = ["state-specific practice questions"] if any(q.data.get("state", "general") != "general"
                                                             for q in questions) else []
        return {"kind": "exam_cram", "topic": "exam-cram", "topic_title": f"Exam Cram ({len(questions)} questions)",
                "segments": segments, "flags": flags, "problems": problems,
                "sources": [f"question {q.id}, signed by {library.signer(q)}" for q in questions]}

    def listener_questions(self) -> list[dict]:
        """Listener questions a reviewer has answered on the playout server, not yet aired."""
        try:
            data = json.loads((self.ctx.buffer_dir / "plans" / "_ask_the_professor.json").read_text())
        except (OSError, json.JSONDecodeError):
            return []
        used = self._used().get("listener", {})
        return [q for q in data.get("answered") or [] if q.get("id") is not None and str(q["id"]) not in used
                and q.get("notes")]

    # ── voicing ──

    def produce(self, airing: Airing, now: datetime | None = None) -> Result:
        now = now or self.ctx.schedule.now()
        writer = SlotWriter(self.root_for(True), airing.show_id, airing.slot)
        result = Result(airing.show_id, airing.slot, "done", folder=writer.folder)
        episode = self.queue.load(self.episode_id(airing))
        if episode is None or episode.get("status") == "rejected":
            note = (episode or {}).get("note", "")
            episode = self.draft(airing, note)
            if episode is not None:
                result.status = "drafted"
                result.problems += episode.get("problems", [])
                if episode["status"] == "pending_review":
                    result.problems.insert(0, f"waiting for review: python3 mac/office_hours.py show {episode['id']}")
        minutes = (airing.start - now).total_seconds() / 60
        encore_after = float((self.config.get("review") or {}).get("encore_after", 90))
        if episode is None or episode.get("status") == "pending_review":
            if minutes <= encore_after and not any(writer.folder.glob("*.flac")):
                if episode is not None:  # too late now: an approval can't mix into the encore's slot
                    episode["status"] = "missed"
                    self.queue.save(episode)
                return self.encore(airing, writer, result, missing=episode is None)
            if episode is None:
                result.status = "skipped"
                result.problems.append("no signed lecture notes (or questions) to make this episode from")
            return result
        if episode.get("status") == "approved":
            self.render(episode, writer, result)
        return result

    def render(self, episode: dict, writer: SlotWriter, result: Result) -> None:
        made = 0
        for order, segment in enumerate(episode["segments"]):
            if writer.has(segment["name"]):
                continue
            if made >= self.segments_per_call:
                result.status = "partial"
                return
            with tempfile.TemporaryDirectory(prefix="office-hours-") as tmp:
                audio = self.voice(segment, Path(tmp))
                writer.add(order, segment["name"], audio, {
                    "title": f"Office Hours with Professor Ex: {episode.get('topic_title', '')}",
                    "artist": "Professor Ex", "credit": "", "license": "generated", "kind": "talk",
                    "episode": episode["id"]})
            result.segments.append(segment["name"])
            made += 1
        episode["status"] = "rendered"
        self.queue.save(episode)
        keep = self.archive / episode["id"]
        keep.mkdir(parents=True, exist_ok=True)
        for path in writer.folder.iterdir():
            if not path.name.startswith("."):
                shutil.copy2(path, keep / path.name)
        (keep / "episode.json").write_text(json.dumps({k: episode[k] for k in ("id", "kind", "topic_title", "airs")}))

    def voice(self, segment: dict, work: Path) -> Path:
        pieces: list[Path] = []
        for i, part in enumerate(segment["parts"]):
            if part.get("logo"):
                pieces.append(sonic_logo(work / "logo.wav"))
            elif "pause" in part:
                pieces.append(ticks(work / f"pause{i}.wav", float(part["pause"])))
            elif part.get("script"):
                out = work / f"part{i:03d}.wav"
                self.ctx.renderer.render(VoiceScript.from_json(part["script"]), out, target_lufs=TALK_LUFS)
                pieces.append(out)
        joined = at.concat(pieces, work / "joined.wav", [0] + [350] * (len(pieces) - 1))
        return at.normalize(joined, work / "segment.flac", TALK_LUFS)

    def encore(self, airing: Airing, writer: SlotWriter, result: Result, *, missing: bool) -> Result:
        """The last approved episode of the same kind, again: nothing unreviewed goes to air."""
        kind = "exam_cram" if airing.params.get("format") == "exam_cram" else "class"
        horizon = (airing.start.date() - timedelta(days=14)).isoformat()
        options = []
        for folder in sorted(self.archive.glob("*")) if self.archive.is_dir() else []:
            try:
                info = json.loads((folder / "episode.json").read_text())
            except (OSError, json.JSONDecodeError):
                continue
            encores = info.get("encores") or []
            if info.get("kind") == kind and folder.name != self.episode_id(airing) and \
                    not any(d >= horizon for d in encores):
                options.append((info.get("airs", ""), folder, info))
        if not options:
            result.status = "skipped"
            result.problems.append("nothing approved to air and no encore available: the safe library covers it")
            return result
        _, folder, info = max(options, key=lambda o: o[0])
        for audio in sorted(folder.glob("*.flac")):
            meta = json.loads(audio.with_suffix(".json").read_text()) if audio.with_suffix(".json").exists() else {}
            meta["title"] = f"Office Hours (encore): {info.get('topic_title', '')}"
            order, name = audio.stem.split("_", 1)
            writer.add(int(order), name, audio, meta)
            result.segments.append(name)
        info.setdefault("encores", []).append(airing.start.date().isoformat())
        (folder / "episode.json").write_text(json.dumps(info))
        result.status = "encore"
        result.problems.append(f"encore of {folder.name}: "
                               + ("nothing could be drafted" if missing else "tonight's episode wasn't approved in time"))
        return result


def sonic_logo(dest: Path) -> Path:
    """A chalkboard tap and a school bell (show bible), made here unless WRIT_OFFICE_HOURS_LOGO is set."""
    import os

    custom = os.environ.get("WRIT_OFFICE_HOURS_LOGO")
    if custom and Path(custom).exists():
        return at.to_wav(Path(custom), dest)
    np = _np()
    rate = at.RATE
    rng = np.random.default_rng(7)
    out = np.zeros(int(2.4 * rate), dtype=np.float32)
    for start in (0.0, 0.18):  # two chalk taps
        n = int(0.05 * rate)
        tap = rng.standard_normal(n) * np.exp(-np.arange(n) / (0.008 * rate))
        tap = np.diff(np.concatenate([[0.0], tap]))  # brighten
        i = int(start * rate)
        out[i:i + n] += (0.5 * tap / np.abs(tap).max()).astype(np.float32)
    t = np.arange(int(1.8 * rate)) / rate
    strike = 0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 18 * t))  # an electric bell's hammer
    bell = sum(g * np.sin(2 * np.pi * f * t) for f, g in ((1180, 1.0), (2360, 0.4), (3540, 0.2)))
    bell = bell * (0.35 + 0.65 * strike) * np.minimum(1, (1.8 - t) / 0.3) * np.minimum(1, t / 0.01)
    i = int(0.5 * rate)
    out[i:i + len(t)] += (0.4 * bell / np.abs(bell).max()).astype(np.float32)
    return _write_wav(dest, out, rate)


def ticks(dest: Path, seconds: float) -> Path:
    """A quiet clock ticking under the pause for the answer, so it's never dead air."""
    np = _np()
    rate = at.RATE
    out = np.zeros(int(seconds * rate), dtype=np.float32)
    n = int(0.012 * rate)
    tick = (np.sin(2 * np.pi * 2400 * np.arange(n) / rate) * np.exp(-np.arange(n) / (0.002 * rate))).astype(np.float32)
    for k in range(int(seconds)):
        i = int((k + 0.25) * rate)
        out[i:i + n] += 0.12 * tick[:len(out[i:i + n])]
    return _write_wav(dest, out, rate)


def _write_wav(dest: Path, mono, rate: int) -> Path:
    import wave

    np = _np()
    dest.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
    with wave.open(str(dest), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.repeat(pcm[:, None], 2, axis=1).tobytes())
    return dest
