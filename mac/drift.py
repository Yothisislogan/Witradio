"""The Drift (show bible, Monster Mix): variety but flow.

Each next track copies the last track's attributes and changes exactly one:
tempo by up to 8 BPM, key to a neighbor on the circle of fifths, one
ingredient, or mood by one notch. Every fourth track is a landmark that may
jump to a related genre. Tempo stays inside the daypart's energy band. The
drift log keeps every track's attributes so Wit can narrate the trip, and so
the next show picks up where the last one left off.

No prompt ever names a real artist or song: the palettes below are styles
and instruments only.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

GENRES: dict[str, tuple[tuple[int, int], tuple[str, ...]]] = {
    "lo-fi hip-hop": ((70, 95), ("dusty drum loop", "warm Rhodes", "vinyl crackle", "upright bass", "muted guitar")),
    "jazz cafe": ((70, 125), ("upright bass", "brushed drums", "piano trio", "muted trumpet", "vibraphone")),
    "neo-soul": ((78, 100), ("Rhodes piano", "fretless bass", "brushed drums", "soft horns", "wah guitar")),
    "bossa nova": ((90, 135), ("nylon-string guitar", "shaker", "soft flute", "upright bass", "rim clicks")),
    "boogie funk": ((100, 118), ("slap bass", "talk box", "clavinet", "four-on-the-floor kick", "brass stabs")),
    "nu-disco": ((110, 124), ("filtered strings", "octave bass", "hand claps", "funky guitar", "analog synth")),
    "French house": ((118, 126), ("filtered sample chops", "sidechained pads", "punchy kick", "phaser guitar",
                                  "vocoder hum")),
    "synthwave": ((90, 118), ("arpeggiated synth", "gated reverb drums", "analog bass", "lush pads", "lead synth")),
    "chillwave": ((80, 100), ("hazy synths", "washed-out drums", "chorus guitar", "tape-saturated bass")),
    "electro-swing": ((115, 128), ("swing horns", "walking bass", "stride piano", "crisp claps", "clarinet")),
    "trip-hop": ((75, 95), ("dusty breakbeat", "moody strings", "deep bass", "vinyl scratches", "theremin")),
    "surf rock": ((120, 160), ("reverb guitar", "driving drums", "twangy bass", "organ")),
    "reggae": ((70, 90), ("skank guitar", "one-drop drums", "melodica", "deep bass", "organ bubble")),
    "afrobeat": ((100, 120), ("highlife guitar", "horn section", "shekere", "talking drum", "bass groove")),
    "deep house": ((118, 124), ("warm chords", "rolling bass", "shuffled hi-hats", "soft pads")),
    "downtempo": ((80, 105), ("soft piano", "field recordings", "muted kick", "airy pads", "cello")),
}
NEIGHBORS: dict[str, tuple[str, ...]] = {
    "lo-fi hip-hop": ("jazz cafe", "neo-soul", "trip-hop", "chillwave"),
    "jazz cafe": ("lo-fi hip-hop", "bossa nova", "neo-soul", "electro-swing"),
    "neo-soul": ("lo-fi hip-hop", "boogie funk", "jazz cafe", "downtempo"),
    "bossa nova": ("jazz cafe", "surf rock", "afrobeat", "downtempo"),
    "boogie funk": ("neo-soul", "nu-disco", "afrobeat", "electro-swing"),
    "nu-disco": ("boogie funk", "French house", "deep house", "synthwave"),
    "French house": ("nu-disco", "deep house", "synthwave"),
    "synthwave": ("French house", "chillwave", "nu-disco"),
    "chillwave": ("synthwave", "lo-fi hip-hop", "downtempo"),
    "electro-swing": ("jazz cafe", "boogie funk", "surf rock"),
    "trip-hop": ("lo-fi hip-hop", "downtempo", "reggae"),
    "surf rock": ("bossa nova", "electro-swing", "afrobeat"),
    "reggae": ("trip-hop", "afrobeat", "downtempo"),
    "afrobeat": ("boogie funk", "reggae", "bossa nova", "surf rock"),
    "deep house": ("nu-disco", "French house", "downtempo"),
    "downtempo": ("chillwave", "trip-hop", "neo-soul", "deep house"),
}
MAJORS = ["C", "G", "D", "A", "E", "B", "F#", "Db", "Ab", "Eb", "Bb", "F"]
MINORS = ["A", "E", "B", "F#", "C#", "G#", "D#", "Bb", "F", "C", "G", "D"]
MOODS = ["melancholy", "wistful", "mellow", "warm", "hopeful", "bright", "playful", "joyful", "euphoric"]


@dataclass
class Track:
    genre: str
    bpm: int
    key: str  # e.g. "D minor"
    ingredients: list[str]
    mood: str
    landmark: bool = False
    change: str = "start"

    def caption(self) -> str:
        return (f"{self.genre}, instrumental, {self.bpm} BPM, {self.key}, {', '.join(self.ingredients)}, "
                f"{self.mood} mood, radio-ready mix")

    def describe(self) -> str:
        return f"{self.genre}, {self.bpm} BPM, {self.key}, {', '.join(self.ingredients)}, {self.mood}"


def key_neighbors(key: str) -> list[str]:
    tonic, mode = key.split(" ", 1)
    ring = MAJORS if mode == "major" else MINORS
    i = ring.index(tonic)
    relative = (f"{MINORS[i]} minor" if mode == "major" else f"{MAJORS[i]} major")
    return [f"{ring[(i - 1) % 12]} {mode}", f"{ring[(i + 1) % 12]} {mode}", relative]


@dataclass
class Drift:
    energy: tuple[int, int] = (95, 115)
    landmark_every: int = 4
    rng: random.Random = field(default_factory=random.Random)

    def _clamp(self, bpm: int, genre: str) -> int:
        low, high = GENRES[genre][0]
        low, high = max(low, self.energy[0]), min(high, self.energy[1])
        if low > high:  # the genre sits outside the band: stay at the band's nearest edge
            return min(max(bpm, self.energy[0]), self.energy[1])
        return min(max(bpm, low), high)

    def first(self, genre: str | None = None) -> Track:
        fitting = [g for g, ((lo, hi), _) in GENRES.items() if lo <= self.energy[1] and hi >= self.energy[0]]
        genre = genre or self.rng.choice(fitting or list(GENRES))
        palette = list(GENRES[genre][1])
        mode = self.rng.choice(["major", "minor"])
        key = f"{self.rng.choice(MAJORS if mode == 'major' else MINORS)} {mode}"
        bpm = self._clamp(self.rng.randint(*self.energy), genre)
        return Track(genre, bpm, key, self.rng.sample(palette, 3), self.rng.choice(MOODS[2:7]))

    def next(self, previous: Track, index: int) -> Track:
        track = Track(previous.genre, previous.bpm, previous.key, list(previous.ingredients), previous.mood)
        if self.landmark_every and index % self.landmark_every == 0:
            options = [g for g in NEIGHBORS.get(previous.genre, ()) if g in GENRES]
            if options:
                genre = self.rng.choice(options)
                track.genre, track.landmark = genre, True
                track.ingredients = self.rng.sample(list(GENRES[genre][1]), 3)
                track.bpm = self._clamp(previous.bpm + self.rng.randint(-8, 8), genre)
                track.change = f"landmark: {previous.genre} to {genre}"
                return track
        for choice in self.rng.sample(["tempo", "key", "ingredient", "mood"], 4):
            if choice == "tempo":
                step = self.rng.choice([-8, -6, -4, -3, 3, 4, 6, 8])
                bpm = self._clamp(previous.bpm + step, previous.genre)
                if bpm != previous.bpm:
                    track.bpm, track.change = bpm, f"tempo {previous.bpm} to {bpm} BPM"
                    return track
            elif choice == "key":
                track.key = self.rng.choice(key_neighbors(previous.key))
                track.change = f"key {previous.key} to {track.key}"
                return track
            elif choice == "ingredient":
                spare = [i for i in GENRES[previous.genre][1] if i not in previous.ingredients]
                if spare:
                    slot = self.rng.randrange(len(track.ingredients))
                    old, track.ingredients[slot] = track.ingredients[slot], self.rng.choice(spare)
                    track.change = f"{old} became {track.ingredients[slot]}"
                    return track
            else:
                i = MOODS.index(previous.mood) if previous.mood in MOODS else 4
                j = min(len(MOODS) - 1, max(0, i + self.rng.choice([-1, 1])))
                if j != i:
                    track.mood, track.change = MOODS[j], f"mood {previous.mood} to {MOODS[j]}"
                    return track
        return track

    def run(self, count: int, start: Track | None = None, offset: int = 0) -> list[Track]:
        tracks: list[Track] = []
        previous = start
        for n in range(count):
            track = self.first() if previous is None else self.next(previous, offset + n + 1)
            tracks.append(track)
            previous = track
        return tracks


class DriftLog:
    """Every track's attributes, newest last, so the drift carries across shows."""

    def __init__(self, path: Path):
        self.path = path

    def last(self) -> tuple[Track | None, int]:
        if not self.path.exists():
            return None, 0
        lines = [l for l in self.path.read_text().splitlines() if l.strip()]
        if not lines:
            return None, 0
        data = json.loads(lines[-1])
        data.pop("slot", None)
        return Track(**data), len(lines)

    def append(self, tracks: Sequence[Track], slot: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            for track in tracks:
                handle.write(json.dumps({**asdict(track), "slot": slot}) + "\n")
