#!/usr/bin/env python3
"""The story library for Hushworth's Bedtime Stories: public-domain texts, ready to read.

The show bible's production rules:

- Texts: editions published before 1931, public domain in the US as of 2026.
  Project Gutenberg is the easy source; strip every Project Gutenberg
  reference (their trademark) and credit the author and "from the public
  domain."
- Translations: old public-domain translations only, never modern ones.
- Pooh, Oz and Alice: the books are free; the Disney and film versions are
  not. No Disney names, songs or character designs anywhere.

For each work in config/story_sources.yaml this downloads the plain text
(or reads a local file), checks it is the book named there, gates it on the
edition's and the translation's publication years, strips the Gutenberg
header, licence, footer and every line naming Gutenberg, unwraps the text
into paragraphs and splits it into stories or chapters, which it keeps in
the story library (WRIT_STORIES_DIR, default <library>/stories/).

    python3 tools/pull_stories.py sources
    python3 tools/pull_stories.py pull alice oz
    python3 tools/pull_stories.py pull dracula --file ~/Downloads/dracula.txt
    python3 tools/pull_stories.py list
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = PROJECT_ROOT / "config" / "story_sources.yaml"
GUTENBERG = "https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt"
USER_AGENT = "story-library/1.0"
MIN_UNIT_WORDS = 40  # shorter "units" are contents entries and title pages (min_words per work overrides)
START = re.compile(r"^\*{3}\s*START OF (THE|THIS) PROJECT GUTENBERG", re.I | re.M)
END = re.compile(r"^\*{3}\s*END OF (THE|THIS) PROJECT GUTENBERG", re.I | re.M)
NOISE = re.compile(r"(gutenberg|^produced by|transcriber'?s? note|^\[illustration|^\[ *pg|^e-?text prepared|"
                   r"distributed proofread|^updated editions|www\.\w+\.(org|net|com))", re.I)


class StoryError(RuntimeError):
    pass


def public_domain_cutoff(today: date | None = None) -> int:
    """Latest US publication year in the public domain: 1930 during 2026."""
    return (today or date.today()).year - 96


def load_sources(path: Path | None = None) -> dict:
    import yaml

    return yaml.safe_load((path or SOURCES_PATH).read_text()) or {}


def stories_dir(station=None) -> Path:
    env = os.environ.get("WRIT_STORIES_DIR")
    if env:
        return Path(env).expanduser()
    if station is not None:
        return station.library_dir / "stories"
    return Path.home() / ".writ" / "stories"


def fold(text: str) -> str:
    """Lowercase letters and digits only, for matching titles and headings."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def gate(work: dict, today: date | None = None) -> list[str]:
    """Reasons a work may not be read on air. Empty means it's cleared."""
    cutoff = public_domain_cutoff(today)
    reasons = []
    published = work.get("published")
    if not published:
        reasons.append("no publication year on file: no year, no reading")
    elif int(published) > cutoff:
        reasons.append(f"this edition was published in {published}; only {cutoff} and earlier are public domain")
    if work.get("translator"):
        translated = work.get("translated")
        if not translated:
            reasons.append("a translation with no publication year on file")
        elif int(translated) > cutoff:
            reasons.append(f"the translation dates from {translated}: use an older public-domain translation")
    return reasons


def fetch(gutenberg_id: int, *, base: str | None = None, timeout: float = 60.0) -> str:
    url = (base or os.environ.get("WRIT_GUTENBERG_URL") or GUTENBERG).format(id=int(gutenberg_id))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise StoryError(f"couldn't download {url}: {getattr(exc, 'reason', exc)}") from exc
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise StoryError(f"{url} isn't text")


def header_fields(text: str) -> dict[str, str]:
    fields = {}
    head = START.split(text, maxsplit=1)[0] if START.search(text) else text[:3000]
    for key in ("Title", "Author", "Translator", "Release Date", "Language"):
        match = re.search(rf"^{key}:\s*(.+(?:\n {{2,}}.+)*)", head, re.M)
        if match:
            fields[key.lower()] = " ".join(match.group(1).split())
    return fields


def strip_gutenberg(text: str) -> str:
    """The book alone: no header, licence or footer, and no line that names Project Gutenberg."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    start = START.search(text)
    if start:
        text = text[text.index("\n", start.end()) + 1:] if "\n" in text[start.end():] else ""
    end = END.search(text)
    if end:
        text = text[:end.start()]
    kept = [line for line in text.split("\n") if not NOISE.search(line)]
    body = "\n".join(kept).strip()
    if "gutenberg" in body.lower():  # belt and braces: nothing of theirs goes to air
        body = "\n".join(line for line in body.split("\n") if "gutenberg" not in line.lower())
    return body


def paragraphs(text: str) -> list[str]:
    """Unwrap hard-wrapped lines into paragraphs. A short line on its own stays its own paragraph,
    so headings survive."""
    out = []
    for block in re.split(r"\n\s*\n", text):
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        joined = " ".join(lines)
        joined = re.sub(r"\s+", " ", joined.replace("_", "")).strip()
        if joined:
            out.append(joined)
    return out


@dataclass
class Unit:
    title: str
    paragraphs: list[str]

    @property
    def words(self) -> int:
        return sum(len(p.split()) for p in self.paragraphs)

    def to_json(self) -> dict:
        return {"title": self.title, "paragraphs": self.paragraphs, "words": self.words}


def split_units(paras: list[str], work: dict) -> tuple[list[Unit], list[str]]:
    """Stories or chapters, by the work's heading pattern. Returns (units, problems)."""
    problems: list[str] = []
    title = work.get("title", "")
    if work.get("kind") == "single" or not work.get("split"):
        body = [p for p in paras if fold(p) not in (fold(title), "the end")]
        return [Unit(title=title, paragraphs=body)], problems
    heading = re.compile(work["split"])
    units: list[Unit] = []
    for para in paras:
        if heading.match(para) and len(para) < 120:
            units.append(Unit(title=para.strip(), paragraphs=[]))
        elif units:
            units[-1].paragraphs.append(para)
    units = [u for u in units if u.words >= int(work.get("min_words", MIN_UNIT_WORDS))]  # drops contents pages
    # A chapter heading on its own line often has its name on the next: "CHAPTER I." then "Down the Rabbit-Hole".
    for unit in units:
        if unit.paragraphs and len(unit.paragraphs[0]) < 80 and not unit.paragraphs[0].endswith((".", "!", "?", '"')):
            unit.title = f"{unit.title.rstrip('.')}: {unit.paragraphs.pop(0)}"
    if work.get("select"):
        chosen = []
        for wanted in work["select"]:
            key = fold(wanted)
            match = next((u for u in units if fold(u.title).startswith(key) or key in fold(u.title)), None)
            if match is None:
                problems.append(f"{work.get('title')}: no story called {wanted!r} found")
            elif match not in chosen:
                chosen.append(match)
        units = chosen
    if not units:
        problems.append(f"{title}: no chapters or stories found with split {work['split']!r}")
    return units, problems


def matches_book(fields: dict[str, str], work: dict) -> list[str]:
    """Is the downloaded text the book the config names?"""
    problems = []
    want_title = [w for w in fold(work.get("title", "")).split() if len(w) > 3]
    have_title = fold(fields.get("title", ""))
    if want_title and not all(w in have_title for w in want_title):
        problems.append(f"the file is {fields.get('title')!r}, not {work.get('title')!r}")
    author = fold(work.get("author", "")).split()
    have_author = fold(fields.get("author", ""))
    if author and fields.get("author") and author[-1] not in have_author:
        problems.append(f"the file's author is {fields.get('author')!r}, not {work.get('author')!r}")
    return problems


@dataclass
class Pulled:
    work_id: str
    units: list[Unit] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    ok: bool = False


class StoryLibrary:
    def __init__(self, root: Path, sources: dict | None = None):
        self.root = Path(root)
        self.sources = sources if sources is not None else load_sources()

    @property
    def works(self) -> dict[str, dict]:
        return dict(self.sources.get("works") or {})

    def folder(self, work_id: str) -> Path:
        return self.root / work_id

    def pull(self, work_id: str, *, text: str | None = None, today: date | None = None) -> Pulled:
        """Download (or take `text`), check, clean, split and store one work."""
        work = self.works.get(work_id)
        result = Pulled(work_id)
        if work is None:
            result.problems.append(f"no work called {work_id} in config/story_sources.yaml")
            return result
        result.problems += gate(work, today)
        if result.problems:
            return result
        if text is None:
            if not work.get("gutenberg"):
                result.problems.append("no Gutenberg number: pull it with --file")
                return result
            try:
                text = fetch(work["gutenberg"])
            except StoryError as exc:
                result.problems.append(str(exc))
                return result
        fields = header_fields(text)
        if fields:
            result.problems += matches_book(fields, work)
            if fields.get("translator") and not work.get("translator"):
                result.problems.append(f"the file is a translation by {fields['translator']}: add translator and "
                                       "translated (its year) to the config, so the gate can check it")
            if result.problems:
                return result
        units, problems = split_units(paragraphs(strip_gutenberg(text)), work)
        result.problems += problems
        if not units:
            return result
        folder = self.folder(work_id)
        folder.mkdir(parents=True, exist_ok=True)
        credit = credit_line(work)
        (folder / "units.json").write_text(json.dumps([u.to_json() for u in units], indent=1, ensure_ascii=False))
        (folder / "meta.json").write_text(json.dumps({
            "id": work_id, "title": work.get("title"), "author": work.get("author"),
            "translator": work.get("translator"), "published": work.get("published"),
            "translated": work.get("translated"), "seating": work.get("seating"), "kind": work.get("kind"),
            "bundle": bool(work.get("bundle")), "ambience": work.get("ambience"),
            "count_calls_in": bool(work.get("count_calls_in")), "credit": credit,
            "source": f"gutenberg:{work.get('gutenberg')}" if work.get("gutenberg") else "local file",
            "pulled": date.today().isoformat()}, indent=1, ensure_ascii=False))
        result.units, result.ok = units, True
        return result

    def pulled(self, seating: str | None = None) -> list[str]:
        """Work ids in the library, in the config's order."""
        out = []
        for work_id, work in self.works.items():
            if (self.folder(work_id) / "units.json").exists() and (seating is None or work.get("seating") == seating):
                if not gate(work):
                    out.append(work_id)
        return out

    def units(self, work_id: str) -> list[dict]:
        return json.loads((self.folder(work_id) / "units.json").read_text())

    def meta(self, work_id: str) -> dict:
        return json.loads((self.folder(work_id) / "meta.json").read_text())


def credit_line(work: dict) -> str:
    """How the reading is credited on air and in now-playing."""
    parts = [f"{work.get('title')}, by {work.get('author')}"]
    if work.get("translator"):
        parts.append(f"translated by {work['translator']}")
    return ", ".join(parts) + ", from the public domain."


def film_only(texts: Iterable[str], blocked: Sequence[str]) -> list[str]:
    """Names and songs from the Disney and film versions, found in a script."""
    found = []
    joined = " ".join(texts)
    for name in blocked:
        if re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", joined, re.I) and name not in found:
            found.append(name)
    return found


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Public-domain stories for Hushworth's Bedtime Stories")
    parser.add_argument("--station", default=os.environ.get("WRIT_STATION_ID", "wit-next"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sources", help="the repertoire and whether each work clears the gate")
    p_pull = sub.add_parser("pull", help="download, check and split works into the library")
    p_pull.add_argument("works", nargs="*")
    p_pull.add_argument("--all", action="store_true")
    p_pull.add_argument("--file", type=Path, help="read this local text instead of downloading (one work)")
    sub.add_parser("list", help="what's in the library")
    args = parser.parse_args(argv)

    station = None
    try:
        from station_config import load_station_config

        station = load_station_config(args.station)
    except Exception:  # the library also works without a station
        pass
    library = StoryLibrary(stories_dir(station))
    if args.cmd == "sources":
        for work_id, work in library.works.items():
            reasons = gate(work)
            print(f"{work_id:18s} {work.get('seating', ''):6s} {work.get('kind', ''):10s} "
                  f"{work.get('published', '?')!s:5s} {'ok' if not reasons else '; '.join(reasons)}  {work.get('title')}")
        return 0
    if args.cmd == "list":
        for work_id in library.pulled():
            units = library.units(work_id)
            print(f"{work_id:18s} {len(units):3d} parts, {sum(u['words'] for u in units):7d} words")
        return 0
    targets = list(library.works) if args.all else args.works
    if not targets:
        print("name the works to pull, or --all")
        return 2
    if args.file and len(targets) != 1:
        print("--file takes exactly one work")
        return 2
    failed = 0
    for i, work_id in enumerate(targets):
        if i and not args.file:
            time.sleep(2)  # one download at a time, politely
        text = args.file.read_text(errors="replace") if args.file else None
        result = library.pull(work_id, text=text)
        status = f"{len(result.units)} parts" if result.ok else "skipped"
        print(f"{work_id}: {status}")
        for problem in result.problems:
            print(f"  {problem}")
        failed += not result.ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
