#!/usr/bin/env python3
"""Exact tempo for Miles with Logan: measure a track, stretch it onto the target, check it.

The development plan's Phase 4 gate: "Miles sessions hold within 2 BPM of
target." The music engine is asked for an exact BPM but doesn't always land
on it, so every track is measured here and time-stretched (pitch kept) onto
the target before it airs, then measured again.

The measurement needs to be finer than a beat tracker's tempo grid (librosa's
estimate moves in steps of several BPM at running tempos), so it works in two
passes over a spectral-flux onset envelope (numpy only):

1. autocorrelate the envelope and find the strongest beat period within 15%
   of the target, in whichever octave the track keeps its pulse;
2. find each beat's onset near where that period predicts it, and fit a line
   through the beat times. Over a couple of minutes of music the slope is
   good to a small fraction of a BPM.

A half-time track counts as on tempo: 80 BPM locks to a 160 BPM stride.

    python3 mac/tempo_lock.py measure track.flac --bpm 160
    python3 mac/tempo_lock.py lock track.flac locked.flac --bpm 160
"""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

RATE = 22050  # analysis sample rate
HOP = 256  # 11.6 ms frames
N_FFT = 1024
TOLERANCE = 2.0  # BPM, the Phase 4 gate
SEARCH = 0.15  # look for the beat within 15% of the target
MIN_ON_GRID = 0.75  # at least this share of beats must land where the tempo says,
MAX_SPREAD = 1.2  # and their onsets sit this close to the fitted line (frames, median)
BASS_HZ = 200.0


class TempoError(RuntimeError):
    pass


def _np():
    try:
        import numpy as np
    except ImportError as exc:  # numpy is a project dependency
        raise TempoError("numpy is needed to measure tempo") from exc
    return np


def decode(path: Path, *, seconds: float | None = None, offset: float = 0.0, rate: int = RATE):
    """Mono float32 samples at `rate`."""
    np = _np()
    command = ["ffmpeg", "-v", "error"]
    if offset:
        command += ["-ss", f"{offset:.3f}"]
    if seconds:
        command += ["-t", f"{seconds:.3f}"]
    command += ["-i", str(path), "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"]
    result = subprocess.run(command, capture_output=True, timeout=300)
    if result.returncode != 0:
        raise TempoError(f"can't decode {path}: {result.stderr.decode(errors='replace').strip()[-200:]}")
    return np.frombuffer(result.stdout, dtype=np.float32)


def onset_envelopes(samples, *, hop: int = HOP, n_fft: int = N_FFT):
    """Half-wave-rectified log spectral flux, one value per hop, local average removed: for the
    whole spectrum, and for the bass band (kick and bass, below ~200 Hz), where the beat lives."""
    np = _np()
    if len(samples) < n_fft * 4:
        empty = np.zeros(0, dtype=np.float32)
        return empty, empty
    frames = np.lib.stride_tricks.sliding_window_view(samples, n_fft)[::hop]
    window = np.hanning(n_fft).astype(np.float32)
    bass_bins = max(2, int(BASS_HZ * n_fft / RATE) + 1)
    previous = previous_bass = None
    flux = np.zeros(len(frames), dtype=np.float32)
    bass = np.zeros(len(frames), dtype=np.float32)
    for start in range(0, len(frames), 1024):
        block = frames[start:start + 1024] * window
        linear = np.abs(np.fft.rfft(block, axis=1)).astype(np.float32)
        magnitude = np.log1p(100.0 * linear)
        before = np.vstack([(previous if previous is not None else magnitude[0])[None, :], magnitude[:-1]])
        flux[start:start + len(block)] = np.maximum(magnitude - before, 0).sum(axis=1)
        # The bass band stays linear: compressed, faint hi-hat leakage would count as much as the kick.
        low = linear[:, 1:bass_bins].sum(axis=1)
        low_before = np.concatenate([[previous_bass if previous_bass is not None else low[0]], low[:-1]])
        bass[start:start + len(block)] = np.maximum(low - low_before, 0)
        previous, previous_bass = magnitude[-1], low[-1]
    width = max(1, int(0.4 * RATE / hop))  # remove a 0.4 s moving average
    kernel = np.ones(width, dtype=np.float32) / width
    return (np.maximum(flux - np.convolve(flux, kernel, mode="same"), 0),
            np.maximum(bass - np.convolve(bass, kernel, mode="same"), 0))


def onset_envelope(samples, *, hop: int = HOP, n_fft: int = N_FFT):
    return onset_envelopes(samples, hop=hop, n_fft=n_fft)[0]


def _bpm(lag_frames: float, hop: int = HOP) -> float:
    return 60.0 * RATE / (hop * lag_frames)


def _lag(bpm: float, hop: int = HOP) -> float:
    return 60.0 * RATE / (hop * bpm)


def octave_near(bpm: float, target: float) -> float:
    """bpm doubled or halved until it's as close to target as it gets (80 -> 160)."""
    if bpm <= 0:
        return bpm
    k = round(math.log2(target / bpm))
    return bpm * (2.0 ** k)


def _autocorrelation(envelope):
    np = _np()
    centered = envelope - envelope.mean()
    size = 1 << (2 * len(centered) - 1).bit_length()
    spectrum = np.fft.rfft(centered, size)
    ac = np.fft.irfft(spectrum * np.conj(spectrum), size)[:len(centered)]
    overlap = len(centered) - np.arange(len(centered))
    return ac / np.maximum(overlap, 1)  # unbiased: long lags aren't penalised


def _peak(values, k: int) -> float:
    """Sub-frame position of the peak at k (parabolic interpolation)."""
    a, b, c = values[k - 1], values[k], values[k + 1]
    denominator = a - 2 * b + c
    return k + (0.5 * (a - c) / denominator if denominator != 0 else 0.0)


def coarse_tempo(ac, near: float) -> tuple[float, float] | None:
    """The strongest beat period within SEARCH of `near`, or its half or double, from the
    envelope's autocorrelation. Returns (bpm, strength)."""
    np = _np()
    if ac[0] <= 0:
        return None
    best: tuple[float, float] | None = None
    for octave in (0.5, 1.0, 2.0):
        centre = near * octave
        if not 40 <= centre <= 260:
            continue
        low = max(2, int(math.floor(_lag(centre * (1 + SEARCH)))))
        high = min(len(ac) - 2, int(math.ceil(_lag(centre * (1 - SEARCH)))))
        if high - low < 3:
            continue
        k = low + int(np.argmax(ac[low:high + 1]))
        if k in (low, high):
            continue  # a peak on the edge of the window isn't a beat period
        strength = float(ac[k] / ac[0])
        bpm = octave_near(_bpm(_peak(ac, k)), near)
        if best is None or strength > best[1] * 1.15 or (strength > best[1] * 0.87 and octave == 1.0):
            best = (bpm, strength)
    return best


def precise_period(ac, period: float) -> float:
    """Sharpen a beat period by finding the autocorrelation peak 2, 4 ... 32 beats out."""
    np = _np()
    for beats in (2, 4, 8, 16, 32):
        centre = period * beats
        radius = max(2, int(period * 0.1))
        lo, hi = int(centre) - radius, int(centre) + radius
        if lo < 2 or hi >= len(ac) - 1:
            break
        k = lo + int(np.argmax(ac[lo:hi + 1]))
        if k in (lo, hi):
            break
        period = _peak(ac, k) / beats
    return period


def track_beats(envelope, period: float) -> tuple[list[int], list[float]]:
    """Follow the beat from the grid's best phase: each beat is looked for where the last one
    found predicts it, so a slightly wrong period can't drift off the music."""
    np = _np()
    n = len(envelope)
    phases = np.arange(0, period, 0.25)
    span = np.arange(0, min(n - period, period * 64), period)  # the first 64 beats set the phase
    frames = np.arange(n)
    scores = [float(np.interp(span + p, frames, envelope).sum()) for p in phases]
    t = float(phases[int(np.argmax(scores))])
    floor = 0.1 * float(np.percentile(envelope, 95))
    radius = max(2, int(period * 0.15))
    beats: list[int] = []
    times: list[float] = []
    k = 0
    while t < n - 2:
        lo, hi = max(1, int(round(t)) - radius), min(n - 2, int(round(t)) + radius)
        found = None
        if hi > lo:
            j = lo + int(np.argmax(envelope[lo:hi + 1]))
            if envelope[j] > floor and 0 < j < n - 1:
                found = _peak(envelope, j)
        if found is not None:
            beats.append(k)
            times.append(found)
            if len(times) >= 16 and k % 8 == 0:  # follow gentle drift
                period = float(np.polyfit(beats[-32:], times[-32:], 1)[0])
            t = found + period
        else:
            t += period
        k += 1
    return beats, times


def refine(envelope, bpm: float, ac=None) -> tuple[float, float, float, int, float]:
    """Fit a line through the tracked beat onsets. Returns (bpm, first beat in seconds, share of
    beats found, beats, median distance of a beat from the line in frames)."""
    np = _np()
    period = _lag(bpm)
    n = len(envelope)
    if n < period * 8:
        return bpm, 0.0, 0.0, 0, math.inf
    if ac is not None:
        period = precise_period(ac, period)
    beats, times = track_beats(envelope, period)
    if len(beats) < 8:
        return bpm, 0.0, 0.0, len(beats), math.inf
    k_arr, t_arr = np.array(beats, dtype=float), np.array(times, dtype=float)
    for _ in range(3):  # fit, drop beats far off the line, fit again
        slope, intercept = np.polyfit(k_arr, t_arr, 1)
        residual = np.abs(t_arr - (slope * k_arr + intercept))
        keep = residual <= max(1.5, 3 * float(np.median(residual)))
        if keep.all():
            break
        k_arr, t_arr = k_arr[keep], t_arr[keep]
    slope, intercept = np.polyfit(k_arr, t_arr, 1)
    found_share = len(k_arr) / max(1, beats[-1] + 1)
    spread = float(np.median(np.abs(t_arr - (slope * k_arr + intercept))))
    first = intercept % slope
    # A frame's flux lands at its window's centre.
    return _bpm(slope), float((first * HOP + N_FFT / 2) / RATE), float(found_share), len(k_arr), spread


@dataclass
class Tempo:
    bpm: float  # in the octave nearest the target asked about
    first_beat: float  # seconds into the file
    on_grid: float  # share of beats that landed on the fitted grid (0-1)
    beats: int


def measure(path: Path, near: float, *, seconds: float = 150.0, offset: float = 0.0) -> Tempo | None:
    """The tempo nearest `near` BPM, or None when the track has no steady pulse there."""
    samples = decode(path, seconds=seconds, offset=offset)
    envelope, bass = onset_envelopes(samples)
    if len(envelope) < 64 or float(envelope.max()) <= 0:
        return None
    ac = _autocorrelation(envelope)
    coarse = coarse_tempo(ac, near)
    if coarse is None:
        return None
    bpm, first, on_grid, beats, spread = refine(envelope, coarse[0], ac)
    if beats < 8 or on_grid < MIN_ON_GRID or spread > MAX_SPREAD:
        return None  # onsets don't keep to that tempo: nothing steady near `near`
    if not _bass_agrees(bass, bpm):
        return None  # a grid the hi-hats fit but the kick and bass don't (say, every third 16th)
    first = _first_sounding_beat(samples, first, 60.0 / bpm) + offset
    return Tempo(bpm=float(round(octave_near(bpm, near), 3)), first_beat=float(round(first, 4)),
                 on_grid=float(round(on_grid, 3)), beats=int(beats))


def _bass_agrees(bass, bpm: float) -> bool:
    """The kick and bass keep the same pulse, in some octave of it."""
    if len(bass) < 64 or float(bass.max()) <= 0:
        return False
    ac = _autocorrelation(bass)
    coarse = coarse_tempo(ac, bpm)
    if coarse is None:
        return False
    low, _, on_grid, beats, spread = refine(bass, coarse[0], ac)
    if beats < 8 or on_grid < 0.6 or spread > 2 * MAX_SPREAD:
        return False
    return abs(octave_near(low, bpm) / bpm - 1) <= 0.01


def _first_sounding_beat(samples, first: float, period: float) -> float:
    """The first beat of the grid after the music actually starts."""
    np = _np()
    loud = np.nonzero(np.abs(samples) > 0.01)[0]
    if not len(loud):
        return first
    start = loud[0] / RATE
    while first + 1e-6 < start - 0.05:
        first += period
    return first


@lru_cache(maxsize=1)
def _has_rubberband() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return " rubberband " in out


def stretch(source: Path, dest: Path, factor: float) -> Path:
    """Tempo times `factor`, pitch kept: rubberband when ffmpeg has it, else atempo."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if abs(factor - 1.0) < 1e-4:
        chain = "anull"
    elif _has_rubberband():
        chain = f"rubberband=tempo={factor:.6f}:transients=crisp:detector=percussive:pitchq=quality"
    else:
        chain = f"atempo={factor:.6f}"
    result = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-af", chain, "-ar", "44100",
                             "-ac", "2", str(dest)], capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise TempoError(f"stretch failed: {result.stderr.strip()[-200:]}")
    return dest


@dataclass
class Locked:
    path: Path
    target: float
    measured: float  # before stretching
    final: float  # after
    factor: float
    first_beat: float
    on_grid: float

    @property
    def error(self) -> float:
        return abs(self.final - self.target)

    def ok(self, tolerance: float = TOLERANCE) -> bool:
        return self.error <= tolerance


def lock(source: Path, dest: Path, target: float, *, tolerance: float = TOLERANCE, max_stretch: float = 0.12,
         seconds: float = 150.0) -> Locked:
    """Measure, stretch onto `target` if needed, measure again. Raises TempoError if it can't hold."""
    found = measure(source, target, seconds=seconds)
    if found is None:
        raise TempoError(f"{source.name}: no steady beat near {target:g} BPM")
    factor = target / found.bpm
    if abs(factor - 1) > max_stretch:
        raise TempoError(f"{source.name}: {found.bpm:g} BPM is too far from {target:g} to stretch")
    if abs(found.bpm - target) <= 0.25:  # already there: keep the original audio
        factor = 1.0
    stretch(source, dest, factor)
    final = measure(dest, target, seconds=seconds) if factor != 1.0 else found
    if final is None:
        raise TempoError(f"{source.name}: lost the beat after stretching")
    result = Locked(path=dest, target=target, measured=found.bpm, final=final.bpm, factor=round(factor, 6),
                    first_beat=final.first_beat, on_grid=final.on_grid)
    if not result.ok(tolerance):
        raise TempoError(f"{source.name}: {final.bpm:g} BPM after stretching, wanted {target:g} ± {tolerance:g}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure and lock a track's tempo")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_measure = sub.add_parser("measure")
    p_measure.add_argument("path", type=Path)
    p_measure.add_argument("--bpm", type=float, required=True, help="the tempo it should be near")
    p_lock = sub.add_parser("lock")
    p_lock.add_argument("path", type=Path)
    p_lock.add_argument("dest", type=Path)
    p_lock.add_argument("--bpm", type=float, required=True)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "measure":
            found = measure(args.path, args.bpm)
            if found is None:
                print(f"no steady beat near {args.bpm:g} BPM")
                return 1
            print(f"{found.bpm:.2f} BPM, first beat at {found.first_beat:.3f} s, {found.beats} beats, "
                  f"{found.on_grid:.0%} on the grid")
            return 0 if abs(found.bpm - args.bpm) <= TOLERANCE else 1
        locked = lock(args.path, args.dest, args.bpm)
        print(f"{locked.measured:.2f} -> {locked.final:.2f} BPM (stretch {locked.factor:.4f}) -> {locked.path}")
        return 0
    except TempoError as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
