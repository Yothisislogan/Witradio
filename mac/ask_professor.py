#!/usr/bin/env python3
"""Ask the Professor: listener questions for Office Hours, answered by a reviewer before air.

Listeners send questions from web/ask-the-professor.html (POST /ask-the-professor
on the station API). Nothing a listener writes goes to air on its own: a
licensed reviewer picks the questions worth answering and writes the answer
notes, and Professor Ex answers only from those notes, then tells the
listener to call their agent or carrier about their own claim.

The form keeps a question, an optional first name and city, and nothing
else; questions holding an email, phone number, street address or policy
number are turned away. Answering rewrites buffer/plans/_ask_the_professor.json,
which the Halo pulls with the plans.

    python3 mac/ask_professor.py pending
    python3 mac/ask_professor.py answer 7 --reviewer "Jane Doe" --note "Flood is commonly excluded..." --note "..."
    python3 mac/ask_professor.py reject 8
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from miles_club import CITY_RE, NAME_RE, URLISH, ClubError  # noqa: E402
from office_hours import pii_in  # noqa: E402

KEEP_DAYS = 120  # answered questions stay available this long


class AskError(ClubError):
    pass


def clean_question(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("age_13_plus") is not True:
        raise AskError("Ask the Professor is for listeners 13 and older.")
    question = " ".join(str(data.get("question") or "").split())
    if not 10 <= len(question) <= 500:
        raise AskError("Ask a question between 10 and 500 characters.")
    if URLISH.search(question) or pii_in([question]):
        raise AskError("Please leave out links and personal details: no emails, phone numbers, addresses or "
                       "policy numbers.")
    name = " ".join(str(data.get("name") or "").split())
    city = " ".join(str(data.get("city") or "").split())
    if name and (len(name) > 30 or len(name.split()) > 2 or not NAME_RE.match(name)):
        raise AskError("Use a first name or nickname, or leave it blank.")
    if city and (len(city) > 40 or not CITY_RE.match(city)):
        raise AskError("Use a city name, or leave it blank.")
    return {"question": question, "name": name, "city": city}


class Mailbag:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY, asked_at TEXT NOT NULL, name TEXT NOT NULL, city TEXT NOT NULL,
            question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', notes TEXT NOT NULL DEFAULT '[]',
            reviewer TEXT, reviewed_at TEXT)""")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Mailbag":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def submit(self, data: dict[str, Any], now: datetime | None = None) -> int:
        entry = clean_question(data)
        cur = self.db.execute("INSERT INTO questions (asked_at, name, city, question) VALUES (?, ?, ?, ?)",
                              ((now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), entry["name"],
                               entry["city"], entry["question"]))
        self.db.commit()
        return int(cur.lastrowid)

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM questions WHERE status = 'pending' ORDER BY id").fetchall()

    def answer(self, question_id: int, notes: Sequence[str], reviewer: str, now: datetime | None = None) -> None:
        notes = [n.strip() for n in notes if n.strip()]
        if not notes:
            raise AskError("an answer needs at least one note")
        self.db.execute("UPDATE questions SET status = 'answered', notes = ?, reviewer = ?, reviewed_at = ? "
                        "WHERE id = ?", (json.dumps(notes), reviewer,
                                         (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), question_id))
        self.db.commit()

    def reject(self, question_id: int) -> None:
        self.db.execute("UPDATE questions SET status = 'rejected' WHERE id = ?", (question_id,))
        self.db.commit()

    def answered(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM questions WHERE status = 'answered' AND reviewed_at >= "
                               "datetime('now', ?) ORDER BY id", (f"-{KEEP_DAYS} days",)).fetchall()
        return [{"id": r["id"], "name": r["name"], "city": r["city"], "question": r["question"],
                 "notes": json.loads(r["notes"]), "reviewer": r["reviewer"], "reviewed_at": r["reviewed_at"]}
                for r in rows]

    def export(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.tmp")
        tmp.write_text(json.dumps({"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   "answered": self.answered()}, indent=1, ensure_ascii=False))
        tmp.replace(dest)
        return dest


def mailbag_path(station) -> Path:
    return Path(os.environ.get("WRIT_ASK_PROFESSOR_DB") or station.runtime_dir / "ask_the_professor.db")


def export_path(station) -> Path:
    return station.output_dir / "buffer" / "plans" / "_ask_the_professor.json"


def main(argv: Sequence[str] | None = None) -> int:
    from station_config import load_station_config

    parser = argparse.ArgumentParser(description="Answer listener questions for Office Hours")
    parser.add_argument("--station", default=os.environ.get("WRIT_STATION_ID", "wit-next"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending")
    p_answer = sub.add_parser("answer", help="approve a question with the notes the Professor answers from")
    p_answer.add_argument("id", type=int)
    p_answer.add_argument("--reviewer", required=True)
    p_answer.add_argument("--note", action="append", required=True, help="one fact per --note")
    p_reject = sub.add_parser("reject")
    p_reject.add_argument("id", type=int)
    args = parser.parse_args(argv)
    station = load_station_config(args.station)
    with Mailbag(mailbag_path(station)) as mailbag:
        if args.cmd == "pending":
            rows = mailbag.pending()
            for row in rows:
                who = " in ".join(x for x in (row["name"], row["city"]) if x) or "anonymous"
                print(f"{row['id']:5d}  {row['asked_at'][:16]}  {who}\n       {row['question']}")
            print(f"{len(rows)} waiting")
            return 0
        if args.cmd == "answer":
            mailbag.answer(args.id, args.note, args.reviewer)
        else:
            mailbag.reject(args.id)
        mailbag.export(export_path(station))
        print(f"{args.cmd}ed {args.id}; {len(mailbag.answered())} answered questions ready for the Halo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
