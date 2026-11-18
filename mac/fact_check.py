#!/usr/bin/env python3
"""The fact-check gate (show bible, pipeline step 4).

Every number and proper name in a script must appear in the fetched data
before the script goes to a voice. The Market and Ying & Yang fail closed: a
script that still misses after a rewrite is dropped and music plays
instead.

Numbers: digits in the script (money, percents, scores, "1.2 million") must
match a number in the facts, allowing fewer decimal places ("6,712" for
6712.34) but no invented figures. Clock times ("8:30") must appear as
written. Names: runs of capitalized words, except a lone capitalized word
starting a sentence, must appear in the facts or in the allow list (host
names, show names, days and months, and words from the host briefs).

    python3 mac/fact_check.py --facts facts.txt --script script.txt
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence

MULTIPLIERS = {"thousand": 10 ** 3, "million": 10 ** 6, "billion": 10 ** 9, "trillion": 10 ** 12}
TIME_RE = re.compile(r"(?<![\d:])(\d{1,2}:\d{2})(?![\d:])")
NUMBER_RE = re.compile(
    r"(?<![\w.,])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?%?(?:\s+(thousand|million|billion|trillion)\b)?(?!\w)"
)
CONNECTORS = {"of", "the", "de", "da", "du", "del", "der", "van", "von", "la", "le", "y", "and", "&"}
ALWAYS_ALLOWED = {
    "i", "i'm", "i'll", "i've", "i'd", "ok", "okay", "am", "pm", "a.m", "p.m", "et", "eastern", "tv",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "today", "tonight", "tomorrow", "yesterday", "mr", "mrs", "ms", "dr",
    "wit-fm", "wit", "fm",
}
SENTENCE_BREAK = re.compile(r"[.!?:;]\s*$|^\s*$|[\"“‘(\[]\s*$")


@dataclass(frozen=True)
class Number:
    raw: str
    value: Decimal  # absolute value
    base: Decimal  # before the multiplier
    places: int
    multiplier: int


def numbers_in(text: str) -> list[Number]:
    found = []
    for match in NUMBER_RE.finditer(TIME_RE.sub(" ", text)):
        whole, fraction, word = match.group(1), match.group(2) or "", match.group(3)
        try:
            base = Decimal(whole.replace(",", "") + fraction)
        except InvalidOperation:
            continue
        multiplier = MULTIPLIERS.get((word or "").lower(), 1)
        found.append(Number(match.group(0), abs(base) * multiplier, abs(base), max(0, len(fraction) - 1), multiplier))
    return found


def times_in(text: str) -> list[str]:
    return TIME_RE.findall(text)


def _matches(said: Number, facts: Sequence[Number]) -> bool:
    step = Decimal(1).scaleb(-said.places)
    for fact in facts:
        if said.value == fact.value:
            return True
        scaled = fact.value / said.multiplier
        if said.multiplier > 1 or said.places < fact.places:
            if scaled.quantize(step, rounding=ROUND_HALF_UP) == said.base:
                return True
    return False


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w’'&.\-]+|[^\w\s]", text)


def names_in(text: str) -> list[str]:
    """Runs of capitalized words; a lone capitalized word opening a sentence is skipped."""
    names, current, opened_sentence = [], [], False
    previous = ""
    for token in _tokens(text):
        word = token.strip(".’'")
        capital = bool(word) and word[0].isupper() and any(c.isalpha() for c in word)
        if capital:
            if not current:
                opened_sentence = bool(SENTENCE_BREAK.search(previous))
            current.append(word)
        elif current and token.lower() in CONNECTORS and token.islower():
            current.append(token)
        else:
            _close(current, opened_sentence, names)
            current = []
        previous = token if not capital else ""
        if token.endswith(".") and capital and len(word) > 2:  # "Inc." / sentence end inside a run
            _close(current, opened_sentence, names)
            current, previous = [], "."
    _close(current, opened_sentence, names)
    return names


def _close(run: list[str], opened_sentence: bool, names: list[str]) -> None:
    while run and run[-1].lower() in CONNECTORS:
        run.pop()
    if not run:
        return
    if len(run) == 1 and opened_sentence:
        return
    if opened_sentence and len(run) > 1 and run[0].lower() in ALWAYS_ALLOWED | {"the", "a", "an"}:
        run = run[1:]
    names.append(" ".join(run))


def _plain(word: str) -> str:
    word = word.lower().replace("’", "'")
    for suffix in ("'s", "s'", "'"):
        if word.endswith(suffix) and len(word) > len(suffix) + 1:
            return word[: -len(suffix)] if suffix != "s'" else word[:-1]
    return word


def allowed_words(texts: Iterable[str]) -> set[str]:
    """Capitalized words from host briefs and rules, to allow in scripts."""
    words = set()
    for text in texts:
        for token in _tokens(text):
            if token[:1].isupper():
                words.add(_plain(token.strip(".")))
    return words


@dataclass
class FactCheck:
    missing_numbers: list[str] = field(default_factory=list)
    missing_names: list[str] = field(default_factory=list)
    numbers_checked: int = 0
    names_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.missing_numbers and not self.missing_names

    def feedback(self) -> str:
        parts = []
        if self.missing_numbers:
            parts.append("These numbers are not in FACTS: " + ", ".join(self.missing_numbers))
        if self.missing_names:
            parts.append("These names are not in FACTS: " + ", ".join(self.missing_names))
        return ". ".join(parts) + (". Use only what FACTS says." if parts else "")


def check(text: str, facts: str, *, allow_names: Iterable[str] = (), allow_numbers: Iterable[str] = ()) -> FactCheck:
    result = FactCheck()
    fact_numbers = numbers_in(facts)
    allowed_numbers = [n for raw in allow_numbers for n in numbers_in(str(raw))]
    for number in numbers_in(text):
        result.numbers_checked += 1
        if not _matches(number, fact_numbers) and not _matches(number, allowed_numbers):
            result.missing_numbers.append(number.raw.strip())
    fact_times = set(times_in(facts)) | {str(t) for t in allow_numbers if TIME_RE.fullmatch(str(t))}
    for moment in times_in(text):
        result.numbers_checked += 1
        if moment not in fact_times:
            result.missing_numbers.append(moment)

    facts_lower = facts.lower().replace("’", "'")
    fact_words = {_plain(w.strip(".")) for w in _tokens(facts) if any(c.isalpha() for c in w)}
    allowed = ALWAYS_ALLOWED | {_plain(a) for name in allow_names for a in str(name).split()}
    for name in names_in(text):
        result.names_checked += 1
        if name.lower().replace("’", "'") in facts_lower:
            continue
        words = [_plain(w) for w in re.split(r"[\s\-]+", name) if w and w.lower() not in CONNECTORS]
        if all(w in fact_words or w in allowed for w in words):
            continue
        result.missing_names.append(name)
    result.missing_numbers = list(dict.fromkeys(result.missing_numbers))
    result.missing_names = list(dict.fromkeys(result.missing_names))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a script against its facts")
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--script", type=Path, required=True)
    args = parser.parse_args(argv)
    result = check(args.script.read_text(), args.facts.read_text())
    print("ok" if result.ok else result.feedback())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
