"""Remixes and beds for The Stacks (show bible: the remix recipe).

1. Pull a phrase from a cleared recording: a Citizen DJ clip already is one;
   from a full record, the strongest stretch of a few seconds.
2. Detect its tempo and key (librosa).
3. Generate an instrumental bed at that tempo and key on the local music
   engine, prompted "instrumental only, no vocals".
4. Time-stretch the phrase to the bed's measured tempo, lay it on the bar
   lines, and duck the bed under it.
5. Master to station loudness. The credits go in the segment's sidecar.

Without librosa (it is a project dependency, so the Halo has it) the tempo
and key are unknown: the bed is made at the style's own tempo and the phrase
isn't stretched.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import audio_tools as at

STYLES = {  # bed style: (tempo range, sound)
    "boom-bap": ((84, 96), "dusty boom-bap drums, upright bass, vinyl crackle"),
    "lo-fi": ((70, 90), "lo-fi drums, warm keys, tape hiss"),
    "house": ((118, 126), "four-on-the-floor house groove, warm bass, open hats"),
    "electro-swing": ((115, 128), "swing drums, walking bass, brass stabs, stride piano"),
    "trip-hop": ((75, 95), "slow breakbeat, deep bass, moody strings"),
    "ambient": ((60, 80), "soft ambient pads, slow swells, no drums"),
}
NOTES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]  # Krumhansl-Kessler
MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


@dataclass
class Analysis:
    seconds: float
    tempo: float | None = None
    key: str | None = None


def _librosa():
    try:
        import librosa  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return None
    import librosa

    return librosa


def analyse(path: Path, max_seconds: float = 90.0) -> Analysis:
    seconds = at.duration(path)
    librosa = _librosa()
    if librosa is None:
        return Analysis(seconds)
    import numpy as np

    y, sr = librosa.load(str(path), sr=22050, mono=True, duration=max_seconds)
    if y.size == 0 or float(np.max(np.abs(y))) < 1e-4:
        return Analysis(seconds)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    tempo = float(np.atleast_1d(tempo)[0]) if np.size(tempo) else 0.0
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
    best, key = -2.0, None
    for shift in range(12):
        for profile, mode in ((MAJOR, "major"), (MINOR, "minor")):
            score = float(np.corrcoef(np.roll(profile, shift), chroma)[0, 1])
            if score > best:
                best, key = score, f"{NOTES[shift]} {mode}"
    return Analysis(seconds, round(tempo, 1) if tempo > 30 else None, key)


def _distance(tempo: float, style: str) -> float:
    low, high = STYLES[style][0]
    return 0.0 if low <= tempo <= high else min(abs(tempo - low), abs(tempo - high))


def fit_tempo(tempo: float | None, style: str) -> float:
    """The bed's tempo: the phrase's own, halved or doubled, nearest the style's range."""
    low, high = STYLES.get(style, STYLES["boom-bap"])[0]
    if not tempo:
        return round((low + high) / 2)
    return round(min((tempo * f for f in (1.0, 0.5, 2.0)), key=lambda t: _distance(t, style)))


def choose_style(tempo: float | None, planned: str, rotation: Sequence[str]) -> str:
    """The planned bed style if the phrase's tempo fits it; otherwise the rotation's closest fit."""
    if not tempo or planned not in STYLES:
        return planned if planned in STYLES else "boom-bap"

    def gap(style: str) -> float:
        return min(_distance(tempo * f, style) for f in (1.0, 0.5, 2.0))

    if gap(planned) <= 4:
        return planned
    return min((s for s in rotation if s in STYLES), key=gap, default=planned)


def bed_caption(style: str, tempo: float, key: str | None, purpose: str = "a remix bed") -> str:
    sound = STYLES.get(style, STYLES["boom-bap"])[1]
    parts = [f"{style} instrumental", f"{round(tempo)} BPM"] + ([key] if key else []) + [
        sound, f"{purpose}, room for a sample on top", "instrumental only, no vocals"]
    return ", ".join(parts)


def phrase(source: Path, out: Path, *, longest: float = 12.0) -> Path:
    """The source itself if it's short (a clip), else its strongest `longest` seconds."""
    seconds = at.duration(source)
    start = 0.0
    if seconds > longest * 1.5:
        librosa = _librosa()
        if librosa is not None:
            import numpy as np

            y, sr = librosa.load(str(source), sr=22050, mono=True, duration=min(seconds, 240))
            hop = 512
            strength = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
            window = max(1, int(longest * sr / hop))
            if strength.size > window:
                totals = np.convolve(strength, np.ones(window), mode="valid")
                start = float(np.argmax(totals)) * hop / sr
        else:
            start = seconds * 0.25
    length = min(longest, seconds - start)
    at._run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(source),
             "-af", "afade=t=in:d=0.02,areverse,afade=t=in:d=0.08,areverse", "-ar", str(at.RATE), "-ac", "2",
             str(out)])
    return out


def stretch(source: Path, out: Path, factor: float) -> Path:
    """Change tempo by `factor` without changing pitch (ffmpeg atempo, chained past 0.5-2.0)."""
    factor = max(0.5, min(2.0, factor))
    filters = f"atempo={factor:.4f}" if abs(factor - 1) > 0.005 else "anull"
    at._run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-af", filters, "-ar", str(at.RATE), "-ac", "2", str(out)])
    return out


def placements(bed_seconds: float, phrase_seconds: float, tempo: float, *, intro_bars: int = 4) -> list[float]:
    """Where the phrase starts: on the bar lines after an intro, until near the end. A short phrase
    loops back to back; one longer than four bars alternates with the beat on its own."""
    bar = 4 * 60.0 / tempo
    spacing = max(bar, bar * round(phrase_seconds / bar + 0.49))
    if phrase_seconds > 4 * bar:
        spacing *= 2
    times, t = [], intro_bars * bar
    while t + phrase_seconds <= bed_seconds - 2 * bar:
        times.append(round(t, 3))
        t += spacing
    return times


def layer(bed: Path, sample: Path, out: Path, times: Sequence[float], *, duck: bool = True,
          sample_gain_db: float = 0.0) -> Path:
    """The sample at each time over the bed, with the bed ducked under it."""
    if not times:
        raise at.AudioError("no room in the bed for the sample")
    copies = len(times)
    graph = [f"[1:a]volume={sample_gain_db}dB,asplit={copies}" + "".join(f"[s{i}]" for i in range(copies))]
    graph += [f"[s{i}]adelay={int(t * 1000)}|{int(t * 1000)}[d{i}]" for i, t in enumerate(times)]
    graph.append("".join(f"[d{i}]" for i in range(copies)) + f"amix=inputs={copies}:normalize=0:duration=longest[samp]")
    bed_seconds = at.duration(bed)
    graph.append(f"[samp]apad=whole_dur={bed_seconds:.3f},asplit=2[top][key]")  # the bed keeps its length
    if duck:
        graph.append("[0:a][key]sidechaincompress=threshold=0.04:ratio=8:attack=15:release=300[under]")
    else:
        graph.append("[key]anullsink;[0:a]anull[under]")
    graph.append("[under][top]amix=inputs=2:normalize=0:duration=first[mix]")
    at._run(["ffmpeg", "-y", "-v", "error", "-i", str(bed), "-i", str(sample), "-filter_complex", ";".join(graph),
             "-map", "[mix]", "-ar", str(at.RATE), "-ac", "2", str(out)])
    return out


def remix(source: Path, bed_maker, out: Path, *, style: str, bed_seconds: float = 150.0,
          rotation: Sequence[str] = ("boom-bap", "lo-fi", "house", "electro-swing", "trip-hop")) -> dict:
    """The whole recipe. bed_maker(caption, path, seconds) -> bool makes the bed. Returns what was done."""
    with tempfile.TemporaryDirectory(prefix="remix-") as tmp:
        work = Path(tmp)
        cut = phrase(source, work / "phrase.wav")
        found = analyse(cut)
        style = choose_style(found.tempo, style, rotation)
        tempo = fit_tempo(found.tempo, style)
        caption = bed_caption(style, tempo, found.key)
        bed = work / "bed.flac"
        if not bed_maker(caption, bed, bed_seconds):
            raise at.AudioError("the music engine didn't return a bed")
        measured = analyse(bed).tempo or tempo
        factor = (measured / found.tempo) if found.tempo else 1.0
        while factor > 1.6:
            factor /= 2
        while factor < 0.625:
            factor *= 2
        fitted = stretch(cut, work / "fitted.wav", factor)
        times = placements(at.duration(bed), at.duration(fitted), measured)
        mixed = layer(at.to_wav(bed, work / "bed.wav"), fitted, work / "mixed.wav", times)
        at.normalize(mixed, out, -16.0)
    return {"caption": caption, "style": style, "phrase_tempo": found.tempo, "bed_tempo": measured,
            "key": found.key, "stretch": round(factor, 3), "placements": len(times)}


def loop_to(source: Path, out: Path, seconds: float) -> Path:
    """The source as WAV, looped if it's shorter than `seconds` (an engine can return a short bed)."""
    if at.duration(source) >= seconds - 0.5:
        return at.to_wav(source, out)
    at._run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(source), "-t", f"{seconds:.3f}",
             "-ar", str(at.RATE), "-ac", "2", "-sample_fmt", "s16", str(out)])
    return out


def trim(source: Path, out: Path, seconds: float, fade: float = 3.0) -> Path:
    """The first `seconds` of a recording, faded out at the end."""
    at._run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-t", f"{seconds:.3f}", "-af",
             f"afade=t=out:st={max(0.0, seconds - fade):.3f}:d={fade}", "-ar", str(at.RATE), "-ac", "2", str(out)])
    return out


def speech_over_bed(speech: Path, bed_maker, out: Path, *, longest: float = 240.0) -> dict:
    """A speech or field recording over an ambient bed, the bed dipping under the voice.
    A recording longer than `longest` seconds fades out there."""
    with tempfile.TemporaryDirectory(prefix="speech-") as tmp:
        work = Path(tmp)
        voice = at.to_wav(speech, work / "speech.wav")
        if at.duration(voice) > longest:
            voice = trim(voice, work / "speech-trimmed.wav", longest)
        seconds = at.duration(voice)
        caption = bed_caption("ambient", 70, None, purpose="a calm bed under a spoken recording")
        bed = work / "bed.flac"
        if not bed_maker(caption, bed, seconds + 12):
            raise at.AudioError("the music engine didn't return a bed")
        padded = loop_to(bed, work / "bed.wav", seconds + 12)
        mixed = layer(padded, voice, work / "mixed.wav", [4.0], sample_gain_db=2.0)
        at._run(["ffmpeg", "-y", "-v", "error", "-i", str(mixed), "-af",
                 f"afade=t=in:d=3,afade=t=out:st={max(0.0, seconds + 8):.2f}:d=4", str(work / "faded.wav")])
        at.normalize(work / "faded.wav", out, -16.0)
    return {"caption": caption, "seconds": seconds}
