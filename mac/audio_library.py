"""Approved emergency music and persistent quarantine, scoped to one station."""

import json
import functools
import subprocess
from pathlib import Path

AUDIO_SUFFIXES = {".flac", ".mp3", ".wav", ".ogg"}


@functools.lru_cache(maxsize=1024)
def _duration(path: str, modified: int, size: int) -> float:
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ], capture_output=True, text=True, timeout=5, check=True)
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def audio_duration(path: Path) -> float:
    try:
        stat = path.stat()
        return _duration(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return 0


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_local(path: Path, station) -> bool:
    try:
        path.resolve().relative_to(station.output_dir.resolve())
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def quarantine_path(station) -> Path:
    return station.output_dir / "runtime" / "quarantine.json"


def is_quarantined(path: Path, station) -> bool:
    return str(path.resolve()) in read_json(quarantine_path(station))


def quarantine(path: Path, station, reason: str):
    if not is_local(path, station):
        return
    target = quarantine_path(station)
    data = read_json(target)
    data[str(path.resolve())] = reason
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2))
    temp.replace(target)


def backup_tracks(station) -> list[Path]:
    """Only explicit approved backup files; never borrow another station's pool."""
    root = station.output_dir / "backup_music"
    if not root.exists():
        return []
    result = []
    for path in sorted(root.iterdir()):
        if (not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES
                or not is_local(path, station) or is_quarantined(path, station)):
            continue
        metadata = read_json(path.with_suffix(".json"))
        if metadata.get("approved") is True and metadata.get("station_id") == station.id:
            result.append(path)
    return result
