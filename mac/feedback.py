"""Station-local listener votes and bounded music rotation preferences."""

import hashlib
import json
import math
import random
import re
import sqlite3
import time
from pathlib import Path


def track_id(path: Path, station) -> str:
    """Stable across checkout locations; Musicgen may supply a durable track_id."""
    identity = path.resolve().relative_to(station.output_dir.resolve()).as_posix()
    try:
        metadata = json.loads(path.with_suffix(".json").read_text())
        supplied = metadata.get("track_id")
        if isinstance(supplied, str) and supplied.strip():
            identity = supplied.strip()
    except (OSError, ValueError, AttributeError):
        pass
    return hashlib.sha256(f"{station.id}:{identity}".encode()).hexdigest()


class FeedbackStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tracks (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS votes (
                    track_id TEXT NOT NULL, listener_id TEXT NOT NULL,
                    value INTEGER NOT NULL CHECK(value IN (-1, 1)),
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(track_id, listener_id)
                );
                CREATE INDEX IF NOT EXISTS votes_listener_time
                    ON votes(listener_id, updated_at);
            """)

    def connect(self):
        return sqlite3.connect(self.db_path, timeout=5)

    def register(self, identity: str, title: str, now: float | None = None):
        with self.connect() as db:
            db.execute("""INSERT INTO tracks VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, last_seen=excluded.last_seen
            """, (identity, title, time.time() if now is None else now))

    def vote(self, identity: str, listener: str, value: str, now: float | None = None):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("Invalid track")
        if not isinstance(listener, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", listener):
            raise ValueError("Invalid listener identifier")
        if value not in ("like", "dislike"):
            raise ValueError("Choose like or dislike")
        now = time.time() if now is None else now
        with self.connect() as db:
            # Serialize checks and the upsert, including across API processes.
            db.execute("BEGIN IMMEDIATE")
            track = db.execute("SELECT last_seen FROM tracks WHERE id=?", (identity,)).fetchone()
            if not track or not 0 <= now - track[0] <= 120:
                raise ValueError("This track is no longer accepting feedback. Refresh and try again.")
            previous = db.execute("SELECT value, updated_at FROM votes WHERE track_id=? AND listener_id=?",
                                  (identity, listener)).fetchone()
            number = 1 if value == "like" else -1
            if previous and previous[0] == number:
                return  # Retries are idempotent.
            if previous and now - previous[1] < 3:
                raise OverflowError("Please wait a moment before changing your vote")
            count = db.execute("SELECT COUNT(*) FROM votes WHERE listener_id=? AND updated_at>?",
                               (listener, now - 600)).fetchone()[0]
            if count >= 30:
                raise OverflowError("Please wait before sending more feedback")
            db.execute("""INSERT INTO votes VALUES (?, ?, ?, ?)
                ON CONFLICT(track_id, listener_id) DO UPDATE
                SET value=excluded.value, updated_at=excluded.updated_at
            """, (identity, listener, number, now))

    def weights(self) -> dict[str, float]:
        with self.connect() as db:
            rows = db.execute("SELECT track_id, COUNT(*), SUM(value) FROM votes GROUP BY track_id")
            # At least three listeners; smoothing and a 25% cap preserve variety.
            return {identity: 1 + .25 * total / (count + 5)
                    for identity, count, total in rows if count >= 3}


def weighted_rotation(files: list[Path], station) -> list[Path]:
    if not files:
        return files
    try:
        weights = FeedbackStore(station.home_dir / "feedback.db").weights()
    except (OSError, sqlite3.Error, AttributeError):
        weights = {}  # Feedback outages must never stop audio.
    # Weighted sampling without replacement. Every candidate remains eligible.
    return sorted(files, key=lambda p: -math.log(max(random.random(), 1e-12)) /
                  weights.get(track_id(p, station), 1.0))
