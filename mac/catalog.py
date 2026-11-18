#!/usr/bin/env python3
"""WIT-FM catalog: what the station may air, why it may, and what it aired.

One SQLite database holds:

- works        one entry per work (a symphony, a 78, a generated track), with
               its source, license, credit line, years and one fixed gain
- files        the audio files of each work, in order (movements, sides)
- airplay      every file that went to air, with the license and credit it
               carried at the time
- scheduled    works the planner has put on a future program (rotation counts
               these too, so a work isn't planned twice before it airs)
- permissions  the permissions log: who, what, when, and the written reply

House rules from the show bible (Licensing and credit rules):

1. No publication year or license on file, no airplay.
2. Every borrowed item is credited on air and in now-playing metadata.
3. A permissions log lives next to the library.

`airability()` encodes rule 1 for each kind of work. Classical works must also
be lossless end to end: the planner refuses any work with a lossy file.

Usage:
    python mac/catalog.py add --kind classical --title "Goldberg Variations, BWV 988" \\
        --composer "Johann Sebastian Bach" --composition-year 1741 \\
        --performer "Kimiko Ishizaka" --form variations \\
        --source open_goldberg --license cc0 \\
        --credit "Kimiko Ishizaka (courtesy credit)" goldberg/*.flac
    python mac/catalog.py import manifest.yaml
    python mac/catalog.py stats
    python mac/catalog.py list --unairable
    python mac/catalog.py audit
    python mac/catalog.py dispute-note 12
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]

KINDS = ("classical", "stacks", "generated", "spoken", "bed", "station")
LICENSES = ("public_domain", "cc0", "generated", "permission")
STATUSES = ("active", "quarantined", "rejected")

# Kinds whose underlying composition or text must itself be public domain.
NEEDS_COMPOSITION_YEAR = {"classical", "stacks", "spoken"}

LOSSLESS_CODECS = {
    "flac", "alac", "wavpack", "ape", "tta", "mlp", "truehd",
    "pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be", "pcm_s32le", "pcm_s32be",
    "pcm_f32le", "pcm_f32be", "pcm_f64le", "pcm_f64be", "pcm_u8",
}

# Loudness for classical: one fixed gain per work, never a limiter, so the gain
# is capped by the work's true peak.
CLASSICAL_TARGET_LUFS = float(os.environ.get("WRIT_CLASSICAL_TARGET_LUFS", "-18.0"))
TRUE_PEAK_CEILING_DBTP = float(os.environ.get("WRIT_TRUE_PEAK_CEILING_DBTP", "-1.0"))

# Spectrogram check: a lossy encoder leaves a cliff in the spectrum. Below
# REJECT_HZ it is almost certainly a transcode; up to SUSPECT_HZ it needs a
# human ear (CD anti-alias filters also cut steeply, near 20-22 kHz).
CUTOFF_REJECT_HZ = 19000.0
CUTOFF_SUSPECT_HZ = 20500.0
CUTOFF_MIN_DROP_DB = 25.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    composer TEXT,
    performer TEXT,
    form TEXT,
    year INTEGER,
    composition_year INTEGER,
    source TEXT NOT NULL,
    source_url TEXT,
    license TEXT NOT NULL,
    license_evidence TEXT,
    credit_line TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    gain_db REAL,
    loudness_lufs REAL,
    true_peak_dbtp REAL,
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    part_title TEXT,
    path TEXT NOT NULL UNIQUE,
    sha256 TEXT,
    codec TEXT,
    sample_rate INTEGER,
    bit_depth INTEGER,
    channels INTEGER,
    duration_sec REAL NOT NULL,
    lossless INTEGER NOT NULL DEFAULT 0,
    hf_cutoff_hz REAL,
    added_at TEXT NOT NULL,
    UNIQUE (work_id, seq)
);

CREATE TABLE IF NOT EXISTS airplay (
    id INTEGER PRIMARY KEY,
    aired_at TEXT NOT NULL,
    path TEXT NOT NULL,
    file_id INTEGER REFERENCES files(id),
    work_id INTEGER REFERENCES works(id),
    station_id TEXT,
    show_id TEXT,
    role TEXT,
    license TEXT,
    credit_line TEXT
);

CREATE TABLE IF NOT EXISTS scheduled (
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id),
    starts_at TEXT NOT NULL,
    show_id TEXT,
    role TEXT,
    plan_id TEXT
);

CREATE TABLE IF NOT EXISTS permissions (
    id INTEGER PRIMARY KEY,
    party TEXT NOT NULL,
    item TEXT NOT NULL,
    requested_at TEXT,
    replied_at TEXT,
    reply TEXT,
    status TEXT NOT NULL DEFAULT 'requested',
    work_id INTEGER REFERENCES works(id)
);

CREATE INDEX IF NOT EXISTS airplay_work_time ON airplay (work_id, aired_at);
CREATE INDEX IF NOT EXISTS airplay_time ON airplay (aired_at);
CREATE INDEX IF NOT EXISTS scheduled_work_time ON scheduled (work_id, starts_at);
CREATE INDEX IF NOT EXISTS files_work ON files (work_id, seq);
"""


class CatalogError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    if when.tzinfo is None:
        raise CatalogError("Catalog timestamps must be timezone-aware")
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def recording_pd_cutoff(today: date) -> int:
    """Latest publication year of a US sound recording now in the public domain.

    1925 and earlier as of January 1, 2026; 1926 joins on January 1, 2027.
    """
    return today.year - 101


def composition_pd_cutoff(today: date) -> int:
    """Latest publication year of a US composition or text now in the public domain.

    1930 and earlier as of January 1, 2026; 1931 joins on January 1, 2027.
    """
    return today.year - 96


# ── records ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FileRecord:
    id: int
    work_id: int
    seq: int
    path: str
    duration_sec: float
    lossless: bool
    part_title: str | None = None
    sha256: str | None = None
    codec: str | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    channels: int | None = None
    hf_cutoff_hz: float | None = None


@dataclass(frozen=True)
class Work:
    id: int
    kind: str
    title: str
    source: str
    license: str
    credit_line: str
    composer: str | None = None
    performer: str | None = None
    form: str | None = None
    year: int | None = None
    composition_year: int | None = None
    source_url: str | None = None
    license_evidence: str | None = None
    tags: tuple[str, ...] = ()
    gain_db: float | None = None
    loudness_lufs: float | None = None
    true_peak_dbtp: float | None = None
    status: str = "active"
    notes: str | None = None
    added_at: str = ""
    files: tuple[FileRecord, ...] = field(default=(), compare=False)

    @property
    def duration_sec(self) -> float:
        return sum(f.duration_sec for f in self.files)

    @property
    def lossless(self) -> bool:
        return bool(self.files) and all(f.lossless for f in self.files)

    def display(self) -> str:
        parts = [p for p in (self.composer, self.title) if p]
        text = ": ".join(parts) if len(parts) == 2 else (parts[0] if parts else f"work {self.id}")
        return f"{text} ({self.performer})" if self.performer else text


@dataclass(frozen=True)
class Play:
    """One past or planned airing, for rotation rules."""

    work_id: int
    at: datetime
    composer: str | None
    role: str | None


# ── probing ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Probe:
    codec: str
    sample_rate: int | None
    bit_depth: int | None
    channels: int | None
    duration_sec: float

    @property
    def lossless(self) -> bool:
        return self.codec in LOSSLESS_CODECS


def probe(path: Path) -> Probe:
    """Read codec, rate, depth, channels and duration with ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,bits_per_raw_sample,bits_per_sample,sample_fmt:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise CatalogError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise CatalogError(f"No audio stream in {path}")
    stream = streams[0]

    def _int(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number or None

    bit_depth = _int(stream.get("bits_per_raw_sample")) or _int(stream.get("bits_per_sample"))
    if bit_depth is None:
        match = re.search(r"(\d+)", str(stream.get("sample_fmt", "")))
        bit_depth = int(match.group(1)) if match and stream.get("codec_name", "").startswith("pcm") else None
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        raise CatalogError(f"Unknown duration for {path}")
    return Probe(
        codec=str(stream.get("codec_name", "")),
        sample_rate=_int(stream.get("sample_rate")),
        bit_depth=bit_depth,
        channels=_int(stream.get("channels")),
        duration_sec=duration,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


_LUFS_RE = re.compile(r"I:\s+(-?[\d.]+|-inf)\s+LUFS")
_PEAK_RE = re.compile(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS")


def measure_loudness(paths: Sequence[Path]) -> tuple[float, float]:
    """Integrated loudness (LUFS) and true peak (dBTP) of files played in order.

    Movements are measured together so a work gets one gain, and the quiet
    slow movement stays quieter than the finale.
    """
    if not paths:
        raise CatalogError("No files to measure")
    args = ["ffmpeg", "-hide_banner", "-nostats", "-v", "info"]
    for path in paths:
        args += ["-i", str(path)]
    inputs = "".join(f"[{i}:a:0]" for i in range(len(paths)))
    graph = f"{inputs}concat=n={len(paths)}:v=0:a=1,ebur128=peak=true[out]"
    args += ["-filter_complex", graph, "-map", "[out]", "-f", "null", "-"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        raise CatalogError(f"Loudness scan failed: {result.stderr.strip()[-500:]}")
    summary = result.stderr[result.stderr.rfind("Summary:"):]
    lufs_match = _LUFS_RE.search(summary)
    peak_match = _PEAK_RE.search(summary)
    if not lufs_match or not peak_match:
        raise CatalogError("Could not read the loudness summary from ffmpeg")

    def _num(text: str) -> float:
        return -math.inf if text == "-inf" else float(text)

    return _num(lufs_match.group(1)), _num(peak_match.group(1))


def fixed_gain_db(
    loudness_lufs: float,
    true_peak_dbtp: float,
    target_lufs: float = CLASSICAL_TARGET_LUFS,
    ceiling_dbtp: float = TRUE_PEAK_CEILING_DBTP,
) -> float:
    """One gain for the whole work: toward the target, never past the ceiling.

    There is no limiter on classical, so a quiet work with loud peaks stays
    quieter than the target rather than clipping.
    """
    if not math.isfinite(loudness_lufs) or loudness_lufs <= -70.0:
        return 0.0  # silence or near-silence: leave it alone
    gain = target_lufs - loudness_lufs
    if math.isfinite(true_peak_dbtp):
        gain = min(gain, ceiling_dbtp - true_peak_dbtp)
    return round(gain, 1)


@dataclass(frozen=True)
class SpectralCheck:
    cutoff_hz: float | None  # where a hard cliff starts, if there is one
    drop_db: float
    verdict: str  # "pass", "suspect" or "reject"


def spectral_check(path: Path, seconds: float = 60.0) -> SpectralCheck:
    """Look for the hard high-frequency cutoff a lossy encoder leaves behind.

    A FLAC made from an MP3 is still lossy. MP3 and AAC encoders low-pass at
    16-20 kHz with a cliff; a real recording rolls off gradually or runs to
    the anti-alias filter near Nyquist.
    """
    import numpy as np

    info = probe(path)
    rate = info.sample_rate or 44100
    start = max(0.0, (info.duration_sec - seconds) / 2)
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}",
            "-i", str(path), "-ac", "1", "-f", "f32le", "-",
        ],
        capture_output=True, timeout=300,
    )
    if result.returncode != 0:
        raise CatalogError(f"Could not decode {path} for the spectrogram check")
    samples = np.frombuffer(result.stdout, dtype=np.float32)
    frame = 8192
    if samples.size < frame * 4:
        return SpectralCheck(None, 0.0, "suspect")
    hops = range(0, samples.size - frame, frame // 2)
    window = np.hanning(frame).astype(np.float32)
    power = np.zeros(frame // 2 + 1)
    for start_idx in hops:
        chunk = samples[start_idx:start_idx + frame] * window
        power += np.abs(np.fft.rfft(chunk)) ** 2
    power /= max(1, len(hops))
    freqs = np.fft.rfftfreq(frame, 1.0 / rate)
    level = 10 * np.log10(power + 1e-20)

    nyquist = rate / 2
    best_drop, best_fc = 0.0, None
    for fc in np.arange(11000.0, min(nyquist - 600.0, 21600.0), 100.0):
        below = level[(freqs >= fc - 1000) & (freqs < fc - 200)]
        above = level[(freqs > fc + 200) & (freqs <= min(fc + 1000, nyquist - 100))]
        rest = level[(freqs > fc + 200) & (freqs <= nyquist - 100)]
        if below.size == 0 or above.size == 0 or rest.size == 0:
            continue
        drop = float(np.mean(below) - np.mean(above))
        # A cliff, not a slope: nothing above the cutoff comes back up.
        if drop > best_drop and float(np.max(rest)) < float(np.mean(below)) - CUTOFF_MIN_DROP_DB / 2:
            best_drop, best_fc = drop, float(fc)

    if best_fc is None or best_drop < CUTOFF_MIN_DROP_DB:
        return SpectralCheck(None, round(best_drop, 1), "pass")
    if best_fc < CUTOFF_REJECT_HZ:
        verdict = "reject"
    elif best_fc < CUTOFF_SUSPECT_HZ:
        verdict = "suspect"
    else:
        verdict = "pass"
    return SpectralCheck(best_fc, round(best_drop, 1), verdict)


# ── the catalog ──────────────────────────────────────────────────────────────


def default_db_path() -> Path:
    env = os.environ.get("WRIT_CATALOG_DB")
    if env:
        return Path(env).expanduser()
    sys.path.insert(0, str(PROJECT_ROOT / "mac"))
    from station_config import load_station_config

    return load_station_config().catalog_db_path


class Catalog:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.db:
            yield self.db

    # ── writing ──

    def add_work(
        self,
        *,
        kind: str,
        title: str,
        source: str,
        license: str,
        credit_line: str,
        files: Sequence[dict[str, Any]],
        composer: str | None = None,
        performer: str | None = None,
        form: str | None = None,
        year: int | None = None,
        composition_year: int | None = None,
        source_url: str | None = None,
        license_evidence: str | None = None,
        tags: Iterable[str] = (),
        gain_db: float | None = None,
        loudness_lufs: float | None = None,
        true_peak_dbtp: float | None = None,
        status: str = "active",
        notes: str | None = None,
    ) -> int:
        """Insert a work and its files (dicts with path, duration_sec, lossless...)."""
        if kind not in KINDS:
            raise CatalogError(f"Unknown kind {kind!r}; use one of {', '.join(KINDS)}")
        if license not in LICENSES:
            raise CatalogError(f"Unknown license {license!r}; use one of {', '.join(LICENSES)}")
        if status not in STATUSES:
            raise CatalogError(f"Unknown status {status!r}")
        if not credit_line.strip():
            raise CatalogError("Every work needs a credit line")
        if not files:
            raise CatalogError("A work needs at least one file")
        now = _iso(utc_now())
        with self.transaction() as db:
            cur = db.execute(
                """INSERT INTO works (kind, title, composer, performer, form, year,
                       composition_year, source, source_url, license, license_evidence,
                       credit_line, tags, gain_db, loudness_lufs, true_peak_dbtp,
                       status, notes, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    kind, title, composer, performer, form, year, composition_year,
                    source, source_url, license, license_evidence, credit_line.strip(),
                    json.dumps(sorted({t.strip().lower() for t in tags if t.strip()})),
                    gain_db, loudness_lufs, true_peak_dbtp, status, notes, now,
                ),
            )
            work_id = int(cur.lastrowid)
            for seq, info in enumerate(files, start=1):
                db.execute(
                    """INSERT INTO files (work_id, seq, part_title, path, sha256, codec,
                           sample_rate, bit_depth, channels, duration_sec, lossless,
                           hf_cutoff_hz, added_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        work_id, seq, info.get("part_title"), str(info["path"]),
                        info.get("sha256"), info.get("codec"), info.get("sample_rate"),
                        info.get("bit_depth"), info.get("channels"),
                        float(info["duration_sec"]), int(bool(info.get("lossless"))),
                        info.get("hf_cutoff_hz"), now,
                    ),
                )
        return work_id

    def set_status(self, work_id: int, status: str, note: str | None = None) -> None:
        if status not in STATUSES:
            raise CatalogError(f"Unknown status {status!r}")
        with self.transaction() as db:
            db.execute(
                "UPDATE works SET status = ?, notes = COALESCE(?, notes) WHERE id = ?",
                (status, note, work_id),
            )

    def record_airplay(
        self,
        path: str | Path,
        *,
        aired_at: datetime | None = None,
        station_id: str | None = None,
        show_id: str | None = None,
        role: str | None = None,
    ) -> int:
        """Log a file that went to air, snapshotting its license and credit."""
        path = str(path)
        row = self.db.execute(
            """SELECT f.id AS file_id, w.id AS work_id, w.license, w.credit_line
               FROM files f JOIN works w ON w.id = f.work_id WHERE f.path = ?""",
            (path,),
        ).fetchone()
        with self.transaction() as db:
            cur = db.execute(
                """INSERT INTO airplay (aired_at, path, file_id, work_id, station_id,
                       show_id, role, license, credit_line)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _iso(aired_at or utc_now()), path,
                    row["file_id"] if row else None, row["work_id"] if row else None,
                    station_id, show_id, role,
                    row["license"] if row else None, row["credit_line"] if row else None,
                ),
            )
        return int(cur.lastrowid)

    def schedule_work(
        self, work_id: int, starts_at: datetime, *, show_id: str | None = None,
        role: str | None = None, plan_id: str | None = None,
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO scheduled (work_id, starts_at, show_id, role, plan_id) VALUES (?, ?, ?, ?, ?)",
                (work_id, _iso(starts_at), show_id, role, plan_id),
            )

    def clear_plan(self, plan_id: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM scheduled WHERE plan_id = ?", (plan_id,))

    def log_permission(
        self, *, party: str, item: str, status: str = "requested",
        requested_at: str | None = None, replied_at: str | None = None,
        reply: str | None = None, work_id: int | None = None,
    ) -> int:
        if status not in ("requested", "granted", "denied"):
            raise CatalogError("Permission status must be requested, granted or denied")
        with self.transaction() as db:
            cur = db.execute(
                """INSERT INTO permissions (party, item, requested_at, replied_at, reply, status, work_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (party, item, requested_at, replied_at, reply, status, work_id),
            )
        return int(cur.lastrowid)

    # ── reading ──

    def _files_for(self, work_ids: Sequence[int]) -> dict[int, list[FileRecord]]:
        grouped: dict[int, list[FileRecord]] = {wid: [] for wid in work_ids}
        if not work_ids:
            return grouped
        marks = ",".join("?" * len(work_ids))
        for row in self.db.execute(
            f"SELECT * FROM files WHERE work_id IN ({marks}) ORDER BY work_id, seq", tuple(work_ids)
        ):
            grouped[row["work_id"]].append(FileRecord(
                id=row["id"], work_id=row["work_id"], seq=row["seq"], path=row["path"],
                duration_sec=row["duration_sec"], lossless=bool(row["lossless"]),
                part_title=row["part_title"], sha256=row["sha256"], codec=row["codec"],
                sample_rate=row["sample_rate"], bit_depth=row["bit_depth"],
                channels=row["channels"], hf_cutoff_hz=row["hf_cutoff_hz"],
            ))
        return grouped

    def works(self, *, kind: str | None = None, status: str | None = "active") -> list[Work]:
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.execute(f"SELECT * FROM works {where} ORDER BY id", params).fetchall()
        files = self._files_for([r["id"] for r in rows])
        return [self._work_from_row(r, files[r["id"]]) for r in rows]

    def work(self, work_id: int) -> Work:
        row = self.db.execute("SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()
        if row is None:
            raise CatalogError(f"No work with id {work_id}")
        return self._work_from_row(row, self._files_for([work_id])[work_id])

    @staticmethod
    def _work_from_row(row: sqlite3.Row, files: list[FileRecord]) -> Work:
        return Work(
            id=row["id"], kind=row["kind"], title=row["title"], source=row["source"],
            license=row["license"], credit_line=row["credit_line"], composer=row["composer"],
            performer=row["performer"], form=row["form"], year=row["year"],
            composition_year=row["composition_year"], source_url=row["source_url"],
            license_evidence=row["license_evidence"], tags=tuple(json.loads(row["tags"] or "[]")),
            gain_db=row["gain_db"], loudness_lufs=row["loudness_lufs"],
            true_peak_dbtp=row["true_peak_dbtp"], status=row["status"], notes=row["notes"],
            added_at=row["added_at"], files=tuple(files),
        )

    def has_granted_permission(self, work_id: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM permissions WHERE work_id = ? AND status = 'granted' AND reply IS NOT NULL",
            (work_id,),
        ).fetchone()
        return row is not None

    def airability(self, work: Work, today: date | None = None) -> list[str]:
        """Reasons this work may not air. An empty list means it is cleared."""
        today = today or utc_now().date()
        reasons: list[str] = []
        if work.status != "active":
            reasons.append(f"status is {work.status}")
        if work.license not in LICENSES:
            reasons.append(f"unknown license {work.license!r}")
        if work.license == "permission" and not self.has_granted_permission(work.id):
            reasons.append("license is by permission but no written grant is logged")
        if not work.credit_line.strip():
            reasons.append("no credit line")
        if not work.source.strip():
            reasons.append("no source")
        if not work.files:
            reasons.append("no files")

        if work.kind in NEEDS_COMPOSITION_YEAR:
            if work.composition_year is None:
                reasons.append("no composition/text publication year on file")
            elif work.composition_year > composition_pd_cutoff(today):
                reasons.append(
                    f"composition published {work.composition_year} is not yet public domain"
                )
        if work.kind == "stacks":
            if work.year is None:
                reasons.append("no recording publication year on file")
            elif work.license == "public_domain" and work.year > recording_pd_cutoff(today):
                reasons.append(f"recording published {work.year} is not yet public domain")
        if work.kind == "classical" and not work.lossless:
            reasons.append("not lossless end to end")
        return reasons

    def is_airable(self, work: Work, today: date | None = None) -> bool:
        return not self.airability(work, today)

    def plays_since(self, since: datetime) -> list[Play]:
        """Aired and planned works since `since`, for rotation rules."""
        rows = self.db.execute(
            """SELECT a.work_id AS work_id, a.aired_at AS at, w.composer AS composer, a.role AS role
               FROM airplay a JOIN works w ON w.id = a.work_id
               WHERE a.aired_at >= ? AND a.work_id IS NOT NULL
               UNION ALL
               SELECT s.work_id, s.starts_at, w.composer, s.role
               FROM scheduled s JOIN works w ON w.id = s.work_id
               WHERE s.starts_at >= ?""",
            (_iso(since), _iso(since)),
        ).fetchall()
        plays: dict[tuple[int, str], Play] = {}
        for row in rows:
            at = datetime.fromisoformat(row["at"])
            # An aired work and its own plan entry describe the same airing.
            plays[(row["work_id"], at.strftime("%Y-%m-%dT%H:%M"))] = Play(
                work_id=row["work_id"], at=at, composer=row["composer"], role=row["role"]
            )
        return sorted(plays.values(), key=lambda p: p.at)

    def cleared_hours(self, kind: str = "classical", today: date | None = None) -> float:
        """Hours of airable material of one kind (for classical: lossless only)."""
        return sum(w.duration_sec for w in self.works(kind=kind) if self.is_airable(w, today)) / 3600.0

    def audit(self, since: datetime | None = None, today: date | None = None) -> list[dict[str, Any]]:
        """Airplay rows that should not have gone to air.

        Flags files with no catalog entry, and catalogued works that were not
        cleared at the time (no license, no year, permission missing...).
        """
        params: list[Any] = []
        where = ""
        if since is not None:
            where = "WHERE aired_at >= ?"
            params.append(_iso(since))
        rows = self.db.execute(f"SELECT * FROM airplay {where} ORDER BY aired_at", params).fetchall()
        problems = []
        cache: dict[int, list[str]] = {}
        for row in rows:
            if row["work_id"] is None:
                problems.append({**dict(row), "problem": "no license record (file not in catalog)"})
                continue
            if row["work_id"] not in cache:
                cache[row["work_id"]] = self.airability(self.work(row["work_id"]), today)
            if cache[row["work_id"]]:
                problems.append({**dict(row), "problem": "; ".join(cache[row["work_id"]])})
        return problems

    def dispute_note(self, work_id: int) -> str:
        """A ready-made reply for an automated copyright claim on YouTube or Twitch."""
        work = self.work(work_id)
        basis = {
            "public_domain": "is in the public domain in the United States",
            "cc0": "has been dedicated to the public domain under Creative Commons CC0",
            "generated": "was generated for WIT-FM and contains no third-party recording",
            "permission": "is broadcast with the rights holder's written permission",
        }.get(work.license, f"is licensed as {work.license}")
        lines = [
            f"This claim concerns \"{work.display()}\".",
            f"The recording we broadcast {basis}.",
        ]
        if work.composer and work.composition_year:
            lines.append(
                f"The composition by {work.composer} was published in {work.composition_year} "
                "and is in the public domain in the United States."
            )
        if work.year and work.kind == "stacks":
            lines.append(f"The recording was published in {work.year}.")
        source = f"Source: {work.source}"
        if work.source_url:
            source += f" ({work.source_url})"
        lines.append(source + ".")
        if work.license_evidence:
            lines.append(f"License evidence: {work.license_evidence}.")
        lines.append(f"On-air credit: {work.credit_line}.")
        lines.append(
            "Matching an automated reference does not establish ownership of this recording. "
            "Please release the claim."
        )
        return "\n".join(lines)


# ── intake ───────────────────────────────────────────────────────────────────


@dataclass
class IntakeReport:
    work_id: int | None
    rejected: list[str]
    warnings: list[str]


def intake(
    catalog: Catalog,
    paths: Sequence[Path],
    *,
    spectral: bool = True,
    measure: bool | None = None,
    hash_files: bool = True,
    **work: Any,
) -> IntakeReport:
    """Probe, check and add one work. Classical must be lossless and pass the spectrogram check."""
    kind = work.get("kind")
    rejected: list[str] = []
    warnings: list[str] = []
    suspects: list[str] = []
    files: list[dict[str, Any]] = []
    for path in paths:
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            rejected.append(f"{path}: file not found")
            continue
        info = probe(path)
        entry: dict[str, Any] = {
            "path": str(path),
            "codec": info.codec,
            "sample_rate": info.sample_rate,
            "bit_depth": info.bit_depth,
            "channels": info.channels,
            "duration_sec": info.duration_sec,
            "lossless": info.lossless,
        }
        if kind == "classical" and not info.lossless:
            rejected.append(f"{path.name}: {info.codec} is not lossless (classical accepts FLAC, WAV or CD rips)")
        if info.lossless and spectral:
            check = spectral_check(path)
            entry["hf_cutoff_hz"] = check.cutoff_hz
            if check.verdict == "reject":
                entry["lossless"] = False
                rejected.append(
                    f"{path.name}: hard cutoff at {check.cutoff_hz:.0f} Hz "
                    f"({check.drop_db:.0f} dB cliff), likely made from a lossy file"
                )
            elif check.verdict == "suspect":
                suspects.append(f"{path.name}: steep cutoff at {check.cutoff_hz:.0f} Hz")
        if hash_files:
            entry["sha256"] = sha256_file(path)
        files.append(entry)

    if rejected:
        return IntakeReport(None, rejected, warnings)

    if suspects:
        # Could be a high-bitrate transcode or a steep anti-alias filter. Keep
        # it off air until someone listens: `catalog.py status ID active`.
        work["status"] = "quarantined"
        note = "spectrogram suspect, listen before airing: " + "; ".join(suspects)
        work["notes"] = f"{work['notes']}\n{note}" if work.get("notes") else note
        warnings.append("quarantined: " + "; ".join(suspects))

    measure = (kind == "classical") if measure is None else measure
    if measure:
        lufs, peak = measure_loudness([Path(f["path"]) for f in files])
        work.setdefault("loudness_lufs", lufs if math.isfinite(lufs) else None)
        work.setdefault("true_peak_dbtp", peak if math.isfinite(peak) else None)
        work.setdefault("gain_db", fixed_gain_db(lufs, peak))

    work_id = catalog.add_work(files=files, **work)
    reasons = catalog.airability(catalog.work(work_id))
    if reasons:
        warnings.append("added but not airable: " + "; ".join(reasons))
    return IntakeReport(work_id, rejected, warnings)


WORK_FIELDS = (
    "kind", "title", "composer", "performer", "form", "year", "composition_year",
    "source", "source_url", "license", "license_evidence", "credit_line", "tags", "notes",
)


def import_manifest(catalog: Catalog, manifest_path: Path, **options: Any) -> list[IntakeReport]:
    """Add every work in a YAML manifest. File paths are relative to the manifest.

        works:
          - kind: classical
            title: "Piano Sonata No. 14 in C-sharp minor, Op. 27 No. 2"
            composer: "Ludwig van Beethoven"
            composition_year: 1802
            performer: "Paul Pitman"
            form: sonata
            source: musopen
            license: public_domain
            credit_line: "Paul Pitman via Musopen"
            tags: [keyboard]
            files: [beethoven_14_1.flac, beethoven_14_2.flac, beethoven_14_3.flac]
    """
    import yaml

    data = yaml.safe_load(Path(manifest_path).read_text()) or {}
    base = Path(manifest_path).resolve().parent
    reports = []
    for entry in data.get("works") or []:
        work = {k: entry[k] for k in WORK_FIELDS if k in entry}
        files = [base / str(f) for f in entry.get("files") or []]
        reports.append(intake(catalog, files, **work, **options))
    return reports


# ── CLI ──────────────────────────────────────────────────────────────────────


def _print_reports(reports: Iterable[IntakeReport]) -> int:
    failed = 0
    for report in reports:
        if report.work_id is None:
            failed += 1
            print("REJECTED")
        else:
            print(f"added work {report.work_id}")
        for line in report.rejected:
            print(f"  ✗ {line}")
        for line in report.warnings:
            print(f"  ! {line}")
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WIT-FM catalog")
    parser.add_argument("--db", type=Path, help="catalog database (default: station catalog.db)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="add one work from its files")
    p_add.add_argument("files", nargs="+", type=Path)
    p_add.add_argument("--kind", required=True, choices=KINDS)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--source", required=True)
    p_add.add_argument("--license", required=True, choices=LICENSES)
    p_add.add_argument("--credit", required=True, dest="credit_line")
    p_add.add_argument("--composer")
    p_add.add_argument("--performer")
    p_add.add_argument("--form")
    p_add.add_argument("--year", type=int, help="recording publication year")
    p_add.add_argument("--composition-year", type=int, help="composition or text publication year")
    p_add.add_argument("--source-url")
    p_add.add_argument("--license-evidence")
    p_add.add_argument("--tag", action="append", default=[], dest="tags")
    p_add.add_argument("--no-spectral", action="store_true")

    p_import = sub.add_parser("import", help="add every work in a YAML manifest")
    p_import.add_argument("manifest", type=Path)
    p_import.add_argument("--no-spectral", action="store_true")

    p_list = sub.add_parser("list", help="list works")
    p_list.add_argument("--kind", choices=KINDS)
    p_list.add_argument("--unairable", action="store_true", help="only works that may not air")

    sub.add_parser("stats", help="hours by kind and which classical blocks are unlocked")

    p_audit = sub.add_parser("audit", help="airplay that had no license record")
    p_audit.add_argument("--since", help="ISO date or datetime (UTC)")

    p_note = sub.add_parser("dispute-note", help="text for a YouTube/Twitch claim dispute")
    p_note.add_argument("work_id", type=int)

    p_status = sub.add_parser("status", help="quarantine, reject or reactivate a work")
    p_status.add_argument("work_id", type=int)
    p_status.add_argument("status", choices=STATUSES)
    p_status.add_argument("--note")

    p_perm = sub.add_parser("permission", help="log a permission request or reply")
    p_perm.add_argument("--party", required=True)
    p_perm.add_argument("--item", required=True)
    p_perm.add_argument("--status", default="requested", choices=("requested", "granted", "denied"))
    p_perm.add_argument("--requested-at")
    p_perm.add_argument("--replied-at")
    p_perm.add_argument("--reply", help="the written reply, verbatim, or a path to it")
    p_perm.add_argument("--work-id", type=int)

    args = parser.parse_args(argv)
    with Catalog(args.db or default_db_path()) as catalog:
        if args.cmd == "add":
            work = {k: getattr(args, k) for k in WORK_FIELDS if getattr(args, k, None) is not None}
            return _print_reports([intake(catalog, args.files, spectral=not args.no_spectral, **work)])

        if args.cmd == "import":
            return _print_reports(import_manifest(catalog, args.manifest, spectral=not args.no_spectral))

        if args.cmd == "list":
            for work in catalog.works(kind=args.kind, status=None):
                reasons = catalog.airability(work)
                if args.unairable and not reasons:
                    continue
                mark = "✓" if not reasons else "✗"
                minutes = work.duration_sec / 60
                lossless = "lossless" if work.lossless else "lossy"
                print(f"{mark} {work.id:5d} {work.kind:9s} {minutes:6.1f} min {lossless:8s} {work.display()}")
                for reason in reasons:
                    print(f"        {reason}")
            return 0

        if args.cmd == "stats":
            for kind in KINDS:
                works = catalog.works(kind=kind)
                if not works:
                    continue
                total = sum(w.duration_sec for w in works) / 3600
                cleared = catalog.cleared_hours(kind)
                print(f"{kind:10s} {len(works):5d} works  {total:7.1f} h  cleared {cleared:7.1f} h")
            classical = catalog.cleared_hours("classical")
            print(f"\nCleared lossless classical: {classical:.1f} h")
            for hours, blocks in ((100, "Nocturne, Evening Concert"), (200, "Day Shift"), (300, "Matinee, Concert Hall")):
                state = "unlocked" if classical >= hours else f"locked ({hours - classical:.1f} h to go)"
                print(f"  {hours:3d} h  {blocks:28s} {state}")
            return 0

        if args.cmd == "audit":
            since = None
            if args.since:
                since = datetime.fromisoformat(args.since)
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
            problems = catalog.audit(since)
            for row in problems:
                print(f"{row['aired_at']}  {row['show_id'] or '-':24s} {row['path']}\n    {row['problem']}")
            print(f"{len(problems)} problem airing(s)")
            return 1 if problems else 0

        if args.cmd == "dispute-note":
            print(catalog.dispute_note(args.work_id))
            return 0

        if args.cmd == "status":
            catalog.set_status(args.work_id, args.status, args.note)
            return 0

        if args.cmd == "permission":
            reply = args.reply
            if reply and Path(reply).is_file():
                reply = Path(reply).read_text()
            perm_id = catalog.log_permission(
                party=args.party, item=args.item, status=args.status,
                requested_at=args.requested_at, replied_at=args.replied_at,
                reply=reply, work_id=args.work_id,
            )
            print(f"logged permission {perm_id}")
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
