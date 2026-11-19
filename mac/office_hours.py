#!/usr/bin/env python3
"""Office Hours with Professor Ex: the class material, its sign-offs and the review queue.

The show bible's guardrails: general education only; a licensed person
reviews the Licensing Lab question bank and any script that mentions WIT
products; state rules are checked against that state's Department of
Insurance and dated; no personal information in War Stories, ever. The
development plan's Phase 5 gate: "the reviewer signs off on the first 10
Office Hours scripts."

So nothing reaches air on the model's say-so:

- Class material (lecture notes, Define That terms, practice questions, FAQ
  answers, war stories, the recruiting line) airs only once it's signed by a
  licensed person. A signature is bound to the item's text: change the text
  and it needs signing again. Signatures made here go to
  WRIT_OFFICE_HOURS_DIR/signatures.yaml; an item can also carry reviewed_by
  and reviewed_on in its own file.
- Episode scripts go to a review queue. Until gate_episodes have been
  approved every episode waits; after that only flagged ones do (see
  config/office_hours.yaml). An episode still waiting near air time is
  replaced by an encore of the last approved one.

    python3 mac/office_hours.py content
    python3 mac/office_hours.py sign q-basics-001 deductibles --reviewer "Jane Doe, licensed P&C agent"
    python3 mac/office_hours.py review
    python3 mac/office_hours.py show office_hours_2026-12-07_1800
    python3 mac/office_hours.py approve office_hours_2026-12-07_1800 --reviewer "Jane Doe"
    python3 mac/office_hours.py reject office_hours_2026-12-07_1800 --reviewer "Jane Doe" --note "..."
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "office_hours.yaml"
PUBLIC_DIR = PROJECT_ROOT / "config" / "office_hours"
STATE_RULE = re.compile(r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|"
                        r"NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b \(checked \d{4}-\d{2}-\d{2}\)")
PII = [  # what must never be in a war story
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "an email address"),
    (re.compile(r"\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"), "a phone number"),
    (re.compile(r"\b\d{1,6}\s+(?:[A-Z][a-z]+\s){1,3}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Lane|Ln|Dr|Drive|Ct|Court|"
                r"Way|Pl|Place)\b"), "a street address"),
    (re.compile(r"\b(?:policy|claim)\s*(?:no\.?|number|#)\s*[:#]?\s*[A-Z0-9-]{5,}", re.I), "a policy or claim number"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "a Social Security number"),
]


def private_dir() -> Path:
    return Path(os.environ.get("WRIT_OFFICE_HOURS_DIR") or Path.home() / ".writ" / "office_hours").expanduser()


def load_config(path: Path | None = None) -> dict:
    import yaml

    path = path or Path(os.environ.get("WRIT_OFFICE_HOURS_CONFIG") or CONFIG_PATH)
    try:
        return yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        return {}


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def pii_in(texts: Iterable[str]) -> list[str]:
    found = []
    for text in texts:
        for pattern, label in PII:
            if pattern.search(text) and label not in found:
                found.append(label)
    return found


# ── class material ────────────────────────────────────────────────────────


@dataclass
class Item:
    kind: str  # lecture, term, question, faq, story, line
    id: str
    title: str
    path: Path
    reviewed_by: str | None = None
    reviewed_on: str | None = None
    data: dict = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return _digest(self.kind, self.id, self.data)

    def facts(self) -> list[str]:
        if self.kind == "lecture":
            return [f for _, lines in self.data.get("sections", []) for f in lines]
        return list(self.data.get("notes") or self.data.get("facts") or [])

    @property
    def state_rules(self) -> list[str]:
        return [f for f in self.facts() if STATE_RULE.search(f)]


def _front_matter(text: str) -> tuple[dict, str]:
    import yaml

    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    return yaml.safe_load(text[4:end]) or {}, text[end + 5:]


def _sections(body: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    for line in body.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif line.strip().startswith("- "):
            if not sections:
                sections.append(("", []))
            sections[-1][1].append(line.strip()[2:].strip())
    return [(h, lines) for h, lines in sections if lines]


class Library:
    """Everything Office Hours can teach, from the private folder first, then the public one."""

    def __init__(self, folders: Sequence[Path] | None = None, config: dict | None = None):
        self.folders = list(folders) if folders is not None else [private_dir(), PUBLIC_DIR]
        self.config = config if config is not None else load_config()
        self.items: dict[tuple[str, str], Item] = {}
        for folder in reversed(self.folders):  # private overrides public
            self._load(folder)
        lines = self.config.get("lines") or {}
        if lines.get("recruiting"):
            self.items[("line", "recruiting")] = Item("line", "recruiting", "Recruiting line", CONFIG_PATH,
                                                      data={"text": lines["recruiting"]})
        self.signatures = self._signatures()

    def _load(self, folder: Path) -> None:
        import yaml

        for path in sorted((folder / "lectures").glob("*/*.md")):
            meta, body = _front_matter(path.read_text())
            lecture_id = f"{path.parent.name}/{path.stem}"
            self.items[("lecture", lecture_id)] = Item(
                "lecture", lecture_id, str(meta.get("title") or path.stem), path, meta.get("reviewed_by"),
                _date(meta.get("reviewed_on")), data={"title": meta.get("title"), "homework": meta.get("homework"),
                                                     "define_that": meta.get("define_that"),
                                                     "sections": _sections(body)})
        for path in sorted((folder / "war_stories").glob("*.md")):
            meta, body = _front_matter(path.read_text())
            if not meta:
                continue
            facts = [line.strip()[2:] for line in body.splitlines() if line.strip().startswith("- ")]
            self.items[("story", path.stem)] = Item(
                "story", path.stem, str(meta.get("title") or path.stem), path, meta.get("reviewed_by"),
                _date(meta.get("reviewed_on")), data={"title": meta.get("title"), "source": meta.get("source"),
                                                     "citation": meta.get("citation"), "lesson": meta.get("lesson"),
                                                     "facts": facts})
        for name, kind in (("glossary.yaml", "term"), ("questions.yaml", "question"), ("faq.yaml", "faq")):
            path = folder / name
            if not path.exists():
                continue
            raw = yaml.safe_load(path.read_text()) or {}
            entries = raw.get("terms") if kind == "term" else raw.get("questions")
            if isinstance(entries, dict):
                entries = [{"id": key, **value} for key, value in entries.items()]
            for entry in entries or []:
                if not entry.get("id"):
                    continue
                data = {k: v for k, v in entry.items() if k not in ("id", "reviewed_by", "reviewed_on")}
                title = entry.get("term") or entry.get("question") or entry["id"]
                self.items[(kind, entry["id"])] = Item(kind, entry["id"], str(title), path, entry.get("reviewed_by"),
                                                       _date(entry.get("reviewed_on")), data=data)

    # ── signatures ──

    @property
    def signatures_path(self) -> Path:
        return self.folders[0] / "signatures.yaml"

    def _signatures(self) -> dict:
        import yaml

        try:
            return (yaml.safe_load(self.signatures_path.read_text()) or {}).get("signed") or {}
        except FileNotFoundError:
            return {}

    def signed(self, item: Item) -> bool:
        if item.reviewed_by and item.reviewed_on:
            return True
        entry = self.signatures.get(f"{item.kind}:{item.id}")
        return bool(entry and entry.get("digest") == item.digest)

    def signer(self, item: Item) -> str:
        if item.reviewed_by:
            return f"{item.reviewed_by} ({item.reviewed_on})"
        entry = self.signatures.get(f"{item.kind}:{item.id}") or {}
        return f"{entry.get('reviewer')} ({entry.get('date')})" if entry else ""

    def sign(self, item: Item, reviewer: str, today: date | None = None) -> None:
        import yaml

        self.signatures[f"{item.kind}:{item.id}"] = {"reviewer": reviewer, "date": (today or date.today()).isoformat(),
                                                     "digest": item.digest, "title": item.title}
        self.signatures_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.signatures_path.with_name(".signatures.yaml.tmp")
        tmp.write_text("# Office Hours sign-offs: python3 mac/office_hours.py sign. Each is bound to the text it\n"
                       "# signed; edit the item and it needs signing again.\n"
                       + yaml.safe_dump({"signed": self.signatures}, sort_keys=True, allow_unicode=True))
        tmp.replace(self.signatures_path)

    # ── lookups ──

    def find(self, key: str) -> list[Item]:
        """Items by id, or kind:id, or a lecture's topic name."""
        kind, _, ident = key.rpartition(":") if ":" in key else ("", "", key)
        return [i for (k, iid), i in self.items.items()
                if (not kind or k == kind) and (iid == ident or (k == "lecture" and iid.split("/")[-1] == ident))]

    def ready(self, kind: str) -> list[Item]:
        return [i for (k, _), i in sorted(self.items.items()) if k == kind and self.signed(i)]

    def waiting(self) -> list[Item]:
        return [i for _, i in sorted(self.items.items()) if not self.signed(i)]

    def lecture(self, semester: str, topic: str) -> Item | None:
        return self.items.get(("lecture", f"{semester}/{topic}"))


def _date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return value.isoformat() if isinstance(value, date) else str(value)


# ── the curriculum ────────────────────────────────────────────────────────


def topics(config: dict) -> list[tuple[str, str, str]]:
    """(semester id, semester title, topic) in teaching order."""
    return [(s["id"], s.get("title", s["id"]), t) for s in config.get("semesters") or [] for t in s.get("topics") or []]


def class_number(config: dict, day: date) -> int:
    """Weeknights since the course started (0 on the first night, negative before it)."""
    start = date.fromisoformat(str(config.get("start_date") or "2026-01-05"))
    if day < start:
        return -1
    weeks, extra = divmod((day - start).days, 7)
    return weeks * 5 + min(extra, 5) - (1 if day.weekday() >= 5 else 0)


def lecture_for(library: Library, day: date) -> Item | None:
    """Tonight's lecture: the next topic in order, skipping any whose notes aren't written and signed."""
    order = topics(library.config)
    if not order:
        return None
    n = max(0, class_number(library.config, day))
    for step in range(len(order)):
        semester, _, topic = order[(n + step) % len(order)]
        item = library.lecture(semester, topic)
        if item is not None and library.signed(item):
            return item
    return None


# ── the review queue ──────────────────────────────────────────────────────


class Queue:
    """Episode scripts waiting for, or past, a reviewer. One JSON file per airing."""

    def __init__(self, root: Path):
        self.root = root
        self.episodes = root / "episodes"
        self.readable = root / "review"
        self.log = root / "reviews.jsonl"

    def path(self, episode_id: str) -> Path:
        return self.episodes / f"{episode_id}.json"

    def load(self, episode_id: str) -> dict | None:
        try:
            return json.loads(self.path(episode_id).read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, episode: dict) -> None:
        self.episodes.mkdir(parents=True, exist_ok=True)
        path = self.path(episode["id"])
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(episode, indent=1, ensure_ascii=False))
        tmp.replace(path)
        self.readable.mkdir(parents=True, exist_ok=True)
        readable = self.readable / f"{episode['id']}.md"
        if episode.get("status") == "pending_review":
            readable.write_text(as_text(episode))
        else:
            readable.unlink(missing_ok=True)

    def all(self) -> list[dict]:
        out = []
        for path in sorted(self.episodes.glob("*.json")):
            try:
                out.append(json.loads(path.read_text()))
            except json.JSONDecodeError:
                continue
        return out

    def approved_count(self) -> int:
        return sum(1 for e in self.all() if e.get("reviewer") and e.get("status") in ("approved", "rendered"))

    def decide(self, episode_id: str, *, approve: bool, reviewer: str, note: str = "") -> dict:
        episode = self.load(episode_id)
        if episode is None:
            raise KeyError(episode_id)
        if episode.get("status") not in ("pending_review", "approved", "rejected"):
            raise ValueError(f"{episode_id} is {episode.get('status')}; it can't be reviewed now")
        episode.update(status="approved" if approve else "rejected", reviewer=reviewer, note=note,
                       reviewed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.save(episode)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.log.open("a") as log:
            log.write(json.dumps({"episode": episode_id, "decision": episode["status"], "reviewer": reviewer,
                                  "note": note, "at": episode["reviewed_at"]}) + "\n")
        return episode


def as_text(episode: dict) -> str:
    """An episode as a reviewer reads it."""
    out = [f"# {episode.get('title', 'Office Hours')}: {episode.get('topic_title', '')}",
           f"Airs {episode.get('airs')}. Episode {episode['id']}.", ""]
    if episode.get("flags"):
        out += ["Flagged for review: " + "; ".join(episode["flags"]), ""]
    if episode.get("sources"):
        out += ["Class material used (signed): " + "; ".join(episode["sources"]), ""]
    for segment in episode.get("segments", []):
        out.append(f"## {segment['name'].replace('_', ' ').title()} (about {round(segment.get('seconds', 0) / 60, 1)} min)")
        for part in segment.get("parts", []):
            if "pause" in part:
                out.append(f"[{part['pause']} second pause]")
            for line in (part.get("script") or {}).get("lines", []):
                out.append(line["text"])
        out.append("")
    return "\n".join(out)


def queue_root(station) -> Path:
    return station.runtime_dir / "producers" / "office_hours"


def main(argv: Sequence[str] | None = None) -> int:
    from station_config import load_station_config

    parser = argparse.ArgumentParser(description="Office Hours: class material sign-offs and episode review")
    parser.add_argument("--station", default=os.environ.get("WRIT_STATION_ID", "wit-next"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("content", help="class material waiting for a licensed person's signature")
    p_sign = sub.add_parser("sign", help="sign class material by id (a lecture topic, term, question, faq, story)")
    p_sign.add_argument("ids", nargs="+")
    p_sign.add_argument("--reviewer", required=True, help="the licensed person, e.g. 'Jane Doe, VA P&C license'")
    sub.add_parser("review", help="episodes waiting for sign-off")
    p_show = sub.add_parser("show")
    p_show.add_argument("episode")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("episode")
        p.add_argument("--reviewer", required=True)
        p.add_argument("--note", default="", help="what to change (a rejected episode is rewritten with it)")
    args = parser.parse_args(argv)

    if args.cmd in ("content", "sign"):
        library = Library()
        if args.cmd == "content":
            waiting = library.waiting()
            for item in waiting:
                print(f"{item.kind:9s} {item.id:40s} {item.title[:60]}")
            ready = len(library.items) - len(waiting)
            print(f"\n{len(waiting)} waiting for a signature, {ready} signed")
            return 0
        for key in args.ids:
            matches = library.find(key)
            if not matches:
                print(f"nothing called {key}")
                return 1
            for item in matches:
                library.sign(item, args.reviewer)
                print(f"signed {item.kind} {item.id}")
        return 0

    queue = Queue(queue_root(load_station_config(args.station)))
    if args.cmd == "review":
        waiting = [e for e in queue.all() if e.get("status") == "pending_review"]
        for episode in waiting:
            flags = f"  flagged: {'; '.join(episode['flags'])}" if episode.get("flags") else ""
            print(f"{episode['id']:40s} airs {episode.get('airs')}  {episode.get('topic_title', '')}{flags}")
            print(f"    read it: {queue.readable / (episode['id'] + '.md')}")
        print(f"{len(waiting)} waiting; {queue.approved_count()} approved so far "
              f"(the first {load_config().get('review', {}).get('gate_episodes', 10)} all need sign-off)")
        return 0
    if args.cmd == "show":
        episode = queue.load(args.episode)
        if episode is None:
            print(f"no episode {args.episode}")
            return 1
        print(as_text(episode))
        return 0
    try:
        episode = queue.decide(args.episode, approve=args.cmd == "approve", reviewer=args.reviewer, note=args.note)
    except (KeyError, ValueError) as exc:
        print(exc)
        return 1
    print(f"{episode['id']}: {episode['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
