"""ffmpeg helpers for assembling spoken segments and shows.

Everything ends up as 16-bit/44.1 kHz stereo, the playout's master format.
Speech is loudness-matched with ffmpeg's two-pass loudnorm in linear mode,
which applies one gain where it can; classical works never pass through
here (they keep their one fixed catalog gain).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

RATE = 44100


class AudioError(RuntimeError):
    pass


def _run(args: Sequence[str], timeout: float = 600) -> subprocess.CompletedProcess:
    result = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise AudioError(f"{args[0]} failed: {result.stderr.strip()[-400:]}")
    return result


def duration(path: Path) -> float:
    result = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], 60)
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise AudioError(f"no duration for {path}") from exc


def to_wav(src: Path, dest: Path) -> Path:
    """Any audio to 16-bit/44.1 kHz stereo WAV."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ar", str(RATE), "-ac", "2", "-sample_fmt", "s16", str(dest)])
    return dest


def silence(dest: Path, seconds: float) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"anullsrc=r={RATE}:cl=stereo",
          "-t", f"{max(0.0, seconds):.3f}", "-sample_fmt", "s16", str(dest)])
    return dest


def concat(parts: Sequence[Path], dest: Path, gaps_ms: Sequence[int] | None = None) -> Path:
    """Join WAVs, with gaps_ms[i] of silence before part i."""
    if not parts:
        raise AudioError("nothing to join")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        listing, entries = Path(tmp) / "list.txt", []
        for index, part in enumerate(parts):
            gap = (gaps_ms or [0] * len(parts))[index]
            if gap > 0:
                entries.append(silence(Path(tmp) / f"gap{index}.wav", gap / 1000))
            entries.append(to_wav(part, Path(tmp) / f"part{index}.wav"))
        listing.write_text("".join(f"file '{p}'\n" for p in entries))
        _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
              "-ar", str(RATE), "-ac", "2", "-sample_fmt", "s16", str(dest)])
    return dest


def measure(path: Path, target_lufs: float = -16.0, true_peak: float = -1.5) -> dict:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
         f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, timeout=600)
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr, re.S)
    if not match:
        raise AudioError(f"could not measure {path}: {result.stderr.strip()[-300:]}")
    return json.loads(match.group(0))


def normalize(src: Path, dest: Path, target_lufs: float = -16.0, true_peak: float = -1.5) -> Path:
    """Two-pass loudnorm to target_lufs, written as WAV or FLAC by dest's suffix."""
    stats = measure(src, target_lufs, true_peak)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if stats.get("input_i") in ("-inf", None) or float(stats["input_i"]) < -70:  # silent: just convert
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ar", str(RATE), "-ac", "2",
              "-sample_fmt", "s16", str(dest)])
        return dest
    chain = (f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11:measured_I={stats['input_i']}:"
             f"measured_TP={stats['input_tp']}:measured_LRA={stats['input_lra']}:"
             f"measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}:linear=true")
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-af", chain, "-ar", str(RATE), "-ac", "2",
          "-sample_fmt", "s16", str(dest)])
    return dest


def tone(dest: Path, seconds: float, frequency: int = 440) -> Path:
    """A test tone (tests and fake engines)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
          f"sine=frequency={frequency}:sample_rate={RATE}:duration={seconds}", "-ac", "2", str(dest)])
    return dest
