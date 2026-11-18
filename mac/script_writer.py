#!/usr/bin/env python3
"""Scripts from the Halo's LLM (show bible, pipeline steps 3 and 4).

The system prompt is the station's house rules plus each speaker's brief
(config/prompts/<host>.md, or WRIT_PROMPTS_DIR). The user prompt is the
segment's job, a word budget, any context, and the FACTS block, which is the
only source of facts. The model replies with a JSON voice script.

Each reply is reviewed: valid JSON, known speakers, a sensible length, the
required extra fields, and the fact check (every number and name must be in
FACTS). Problems go back to the model in the same conversation for another
try; after the last try the writer gives up with ScriptError, and the
producer decides what airs instead (nothing, for the shows that fail
closed).
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fact_check  # noqa: E402
from llm_client import LLMClient, LLMError  # noqa: E402
from voices import VoiceError, VoiceScript  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WPS = 2.5


def prompts_dir() -> Path:
    return Path(os.environ.get("WRIT_PROMPTS_DIR") or PROJECT_ROOT / "config" / "prompts")


class ScriptError(RuntimeError):
    def __init__(self, message: str, problems: Sequence[str] = ()):
        super().__init__(message)
        self.problems = list(problems)


@dataclass
class Segment:
    id: str
    brief: str
    speakers: Sequence[str]
    seconds: float
    facts: str = ""
    context: str = ""
    fields: Mapping[str, str] = field(default_factory=dict)  # extra JSON keys the reply must have
    allow_names: Sequence[str] = ()
    allow_numbers: Sequence[str] = ()
    fact_check: bool = True


@dataclass
class Written:
    script: VoiceScript
    check: fact_check.FactCheck
    attempts: int


def parse_reply(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


class ScriptWriter:
    def __init__(self, llm: LLMClient, *, words_per_second: Mapping[str, float] | None = None,
                 nonverbals: Mapping[str, Sequence[str]] | None = None, attempts: int = 3,
                 temperature: float | None = None, prompts: Path | None = None):
        self.llm = llm
        self.wps = dict(words_per_second or {})
        self.nonverbals = {k: list(v) for k, v in (nonverbals or {}).items()}
        self.attempts = max(1, attempts)
        self.temperature = temperature
        self.prompts = Path(prompts) if prompts else prompts_dir()

    @classmethod
    def for_renderer(cls, llm: LLMClient, renderer, **kwargs) -> "ScriptWriter":
        cards = renderer.cards
        return cls(llm, words_per_second={h: c.words_per_second for h, c in cards.items()},
                   nonverbals={h: c.nonverbals for h, c in cards.items()}, **kwargs)

    def _read(self, name: str) -> str:
        path = self.prompts / f"{name}.md"
        return path.read_text().strip() if path.exists() else ""

    def target_words(self, segment: Segment) -> int:
        rates = [self.wps.get(s, DEFAULT_WPS) for s in segment.speakers] or [DEFAULT_WPS]
        return max(8, round(segment.seconds * sum(rates) / len(rates)))

    def allow_names(self, segment: Segment) -> set[str]:
        texts = [self._read("house_rules")] + [self._read(s) for s in segment.speakers]
        return fact_check.allowed_words(texts) | {s for s in segment.speakers} | set(segment.allow_names)

    def system_prompt(self, segment: Segment) -> str:
        briefs = [self._read(s) for s in segment.speakers]
        cues = []
        for speaker in segment.speakers:
            allowed = self.nonverbals.get(speaker) or []
            cues.append(f"{speaker}: {', '.join(allowed) if allowed else 'none'}")
        extra = "".join(f', "{key}": <{description}>' for key, description in segment.fields.items())
        shape = ('{"title": "<a short title>", "lines": [{"speaker": "<who>", "text": "<what they say>", '
                 '"emotion": "<one or two words>", "intensity": <0.0 to 1.0>, "pace": "<slow, medium or fast>"}]'
                 + extra + "}")
        return "\n\n".join(filter(None, [
            self._read("house_rules"), *briefs,
            "Reply with one JSON object and nothing else, shaped like this:\n" + shape,
            f"Speakers: {', '.join(segment.speakers)}. A line may also carry \"pause_before_ms\" (0 to 2000) "
            "and \"nonverbal\", only from these: " + "; ".join(cues) + ".",
            "Write every number in digits, exactly as FACTS gives it (you may round away decimals). "
            "Don't work out new numbers.",
        ]))

    def user_prompt(self, segment: Segment) -> str:
        words = self.target_words(segment)
        parts = [f"SEGMENT: {segment.id}", segment.brief.strip(),
                 f"Length: about {words} words in total (about {round(segment.seconds)} seconds on air)."]
        if segment.context.strip():
            parts.append("CONTEXT (not a source of facts):\n" + segment.context.strip())
        parts.append("FACTS (the only source of facts):\n" + (segment.facts.strip() or "(none)"))
        return "\n\n".join(parts)

    def review(self, reply: str, segment: Segment) -> tuple[list[str], VoiceScript | None, fact_check.FactCheck]:
        empty = fact_check.FactCheck()
        try:
            script = VoiceScript.from_json(parse_reply(reply), segment=segment.id)
        except (ValueError, VoiceError) as exc:
            return [f"The reply isn't a valid voice script: {exc}"], None, empty
        problems = []
        strangers = sorted(set(script.speakers) - set(segment.speakers))
        if strangers:
            problems.append(f"Unknown speakers {', '.join(strangers)}; use only {', '.join(segment.speakers)}")
        target = self.target_words(segment)
        if script.words > target * 1.35:
            problems.append(f"Too long: {script.words} words; cut it to about {target}")
        elif script.words < target * 0.3:  # a truncated or lazy reply
            problems.append(f"Too short: {script.words} words; write about {target}")
        missing = [key for key in segment.fields if key not in script.extra]
        if missing:
            problems.append(f"Missing keys: {', '.join(missing)}")
        check = empty
        if segment.fact_check:
            check = fact_check.check(script.text, segment.facts, allow_names=self.allow_names(segment),
                                     allow_numbers=segment.allow_numbers)
            if not check.ok:
                problems.append(check.feedback())
        return problems, script, check

    def write(self, segment: Segment) -> Written:
        messages = [{"role": "system", "content": self.system_prompt(segment)},
                    {"role": "user", "content": self.user_prompt(segment)}]
        problems: list[str] = []
        for attempt in range(1, self.attempts + 1):
            try:
                reply = self.llm.chat(messages, json_mode=True, temperature=self.temperature)
            except LLMError as exc:
                raise ScriptError(f"{segment.id}: the LLM failed: {exc}", [str(exc)]) from exc
            problems, script, check = self.review(reply, segment)
            if not problems and script is not None:
                return Written(script, check, attempt)
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": "That reply has problems:\n- " + "\n- ".join(problems)
                          + "\nReply with the whole corrected JSON object only."}]
        raise ScriptError(f"{segment.id}: no usable script after {self.attempts} tries: {'; '.join(problems)}",
                          problems)
