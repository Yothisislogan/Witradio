#!/usr/bin/env python3
"""The Miles Club: listeners log miles on the website; Logan reads the week's names on air.

The show bible: "Miles Club: listeners log miles on the website. Logan reads
weekly shout-outs and a running station total ('WIT-FM has now walked to Las
Vegas')." The development plan adds: a privacy policy and a 13+ age line, and
never inviting submissions during Bedtime's Early Seating.

Runs on the playout server, behind the station API (POST /miles-club, from
web/miles-club.html). It keeps only what the shout-out needs: a first name or
nickname, a city, the miles and when. No email, no IP address. Every entry
waits for a person to approve it before a name can reach air; approving
rewrites buffer/plans/_miles_club.json, which the Halo pulls with the plans.

    python3 mac/miles_club.py pending
    python3 mac/miles_club.py approve 12 13
    python3 mac/miles_club.py reject 14
    python3 mac/miles_club.py summary
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAME_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ '.\-])*$")  # letters, with spaces, apostrophes, dots, hyphens
CITY_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ '.,\-])*$")
MAX_MILES = 100.0  # per entry
URLISH = re.compile(r"(https?:|www\.|\.com\b|@)", re.I)


class ClubError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clean_entry(data: dict[str, Any]) -> dict[str, Any]:
    """Validate a submission. Raises ClubError with a message the form can show."""
    if data.get("age_13_plus") is not True:
        raise ClubError("The Miles Club is for listeners 13 and older.")
    name = " ".join(str(data.get("name") or "").split())
    city = " ".join(str(data.get("city") or "").split())
    if not name or len(name) > 30 or len(name.split()) > 2 or not NAME_RE.match(name) or URLISH.search(name):
        raise ClubError("Use a first name or nickname: letters only, up to 30 characters.")
    if city and (len(city) > 40 or not CITY_RE.match(city) or URLISH.search(city)):
        raise ClubError("Use a city name: letters only, up to 40 characters.")
    try:
        miles = round(float(data.get("miles")), 1)
    except (TypeError, ValueError):
        raise ClubError("Miles should be a number.") from None
    if not 0.1 <= miles <= MAX_MILES:
        raise ClubError(f"Log between 0.1 and {MAX_MILES:g} miles at a time.")
    return {"name": name, "city": city, "miles": miles}


class MilesClub:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY, logged_at TEXT NOT NULL, name TEXT NOT NULL, city TEXT NOT NULL,
            miles REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', reviewed_at TEXT)""")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "MilesClub":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def submit(self, data: dict[str, Any], now: datetime | None = None) -> int:
        entry = clean_entry(data)
        cur = self.db.execute("INSERT INTO entries (logged_at, name, city, miles) VALUES (?, ?, ?, ?)",
                              ((now or utc_now()).isoformat(timespec="seconds"), entry["name"], entry["city"],
                               entry["miles"]))
        self.db.commit()
        return int(cur.lastrowid)

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM entries WHERE status = 'pending' ORDER BY id").fetchall()

    def review(self, ids: Sequence[int], approve: bool, now: datetime | None = None) -> int:
        status = "approved" if approve else "rejected"
        cur = self.db.executemany("UPDATE entries SET status = ?, reviewed_at = ? WHERE id = ?",
                                  [(status, (now or utc_now()).isoformat(timespec="seconds"), i) for i in ids])
        self.db.commit()
        return cur.rowcount

    def summary(self, today: date | None = None, milestones: Sequence[dict] = ()) -> dict[str, Any]:
        """This week's approved names (Monday to Sunday) with their miles, and the all-time total."""
        today = today or utc_now().date()
        monday = today - timedelta(days=today.weekday())
        rows = self.db.execute(
            """SELECT MIN(name) AS name, MIN(city) AS city, ROUND(SUM(miles), 1) AS miles FROM entries
               WHERE status = 'approved' AND substr(logged_at, 1, 10) >= ? AND substr(logged_at, 1, 10) <= ?
               GROUP BY lower(name), lower(city) ORDER BY SUM(miles) DESC, MIN(id)""",
            (monday.isoformat(), (monday + timedelta(days=6)).isoformat())).fetchall()
        total = self.db.execute("SELECT COALESCE(SUM(miles), 0) FROM entries WHERE status = 'approved'").fetchone()[0]
        reached = [m for m in milestones if m.get("miles") and float(m["miles"]) <= total]
        return {"updated": utc_now().isoformat(timespec="seconds"), "week_of": monday.isoformat(),
                "this_week": [dict(r) for r in rows], "total_miles": round(float(total), 1),
                "milestone": max(reached, key=lambda m: float(m["miles"]))["name"] if reached else None}

    def export(self, dest: Path, today: date | None = None, milestones: Sequence[dict] = ()) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.tmp")
        tmp.write_text(json.dumps(self.summary(today, milestones), indent=1, ensure_ascii=False))
        tmp.replace(dest)
        return dest


def club_path(station) -> Path:
    return Path(os.environ.get("WRIT_MILES_CLUB_DB") or station.runtime_dir / "miles_club.db")


def summary_path(station) -> Path:
    return station.output_dir / "buffer" / "plans" / "_miles_club.json"


def milestones() -> list[dict]:
    import yaml

    try:
        config = yaml.safe_load((PROJECT_ROOT / "config" / "miles.yaml").read_text()) or {}
    except FileNotFoundError:
        return []
    return [m for m in (config.get("miles_club") or {}).get("milestones") or [] if m.get("miles")]


def main(argv: Sequence[str] | None = None) -> int:
    from station_config import load_station_config

    parser = argparse.ArgumentParser(description="Review the Miles Club")
    parser.add_argument("--station", default=os.environ.get("WRIT_STATION_ID", "wit-next"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending", help="entries waiting for review")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("ids", type=int, nargs="+")
    sub.add_parser("summary", help="what Logan will read, and rewrite it for the Halo")
    args = parser.parse_args(argv)
    station = load_station_config(args.station)
    with MilesClub(club_path(station)) as club:
        if args.cmd == "pending":
            rows = club.pending()
            for row in rows:
                print(f"{row['id']:5d}  {row['logged_at'][:16]}  {row['name']:20s} {row['city']:20s} {row['miles']:g} mi")
            print(f"{len(rows)} waiting")
            return 0
        if args.cmd in ("approve", "reject"):
            changed = club.review(args.ids, approve=args.cmd == "approve")
            print(f"{args.cmd}d {changed}")
        club.export(summary_path(station), milestones=milestones())
        print(json.dumps(club.summary(milestones=milestones()), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
