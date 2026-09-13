#!/usr/bin/env python3
"""Validate and copy operator-approved finished music into a station's backup pool."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mac"))
from audio_library import AUDIO_SUFFIXES, audio_duration, read_json
from station_config import load_station_config


def import_track(source, station):
    source = source.resolve()
    if source.suffix.lower() not in AUDIO_SUFFIXES or not source.is_file():
        raise ValueError("Expected a finished MP3, WAV, FLAC or OGG file")
    metadata = read_json(source.with_suffix(".json"))
    if metadata.get("approved") is False:
        raise ValueError("Track is marked rejected")
    if metadata.get("station_id") not in (None, station.id):
        raise ValueError("Track belongs to another station")
    duration = audio_duration(source)
    if not 30 <= duration <= 1800:
        raise ValueError("Backup tracks must be between 30 seconds and 30 minutes")
    # Decode the complete file, including its tail; never approve a partial upload.
    result = subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-xerror", "-i", str(source),
        "-vn", "-af", "volumedetect", "-f", "null", "-",
    ], capture_output=True, text=True, timeout=120)
    import re
    peaks = re.findall(r"max_volume:\s*(-?inf|-?[\d.]+) dB", result.stderr)
    if result.returncode or not peaks or float(peaks[-1]) <= -55:
        raise ValueError("Audio is silent or could not be fully decoded")
    with source.open("rb") as handle:
        identity = hashlib.file_digest(handle, "sha256").hexdigest()
    root = station.output_dir / "backup_music"
    root.mkdir(parents=True, exist_ok=True)
    target = root / (identity[:24] + source.suffix.lower())
    pending = target.with_suffix(target.suffix + ".part")
    shutil.copyfile(source, pending)
    metadata.update({
        "station_id": station.id, "approved": True, "duration_seconds": duration,
        "track_id": metadata.get("track_id") or identity,
        "display_name": metadata.get("display_name") or source.stem,
    })
    sidecar = target.with_suffix(".json")
    sidecar_pending = sidecar.with_suffix(".json.part")
    sidecar_pending.write_text(json.dumps(metadata, indent=2))
    sidecar_pending.replace(sidecar)
    pending.replace(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path, help="Finished music you have approved for backup broadcast")
    parser.add_argument("--station", help="Defaults to the configured station")
    args = parser.parse_args()
    station = load_station_config(args.station)
    failures = 0
    for path in args.files:
        try:
            imported = import_track(path, station)
            print(f"Ready: {path.name} -> {imported.name}")
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            failures += 1
            print(f"Not imported: {path.name}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
