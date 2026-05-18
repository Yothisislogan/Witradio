#!/usr/bin/env python3
"""Build a simple playlist file from a show directory."""

from pathlib import Path
import random
import sys

show = sys.argv[1] if len(sys.argv) > 1 else "day_shift"
music_dir = Path("music") / show
playlist_file = Path("runtime") / f"{show}.m3u"

playlist_file.parent.mkdir(exist_ok=True)

tracks = []
for ext in ("*.mp3", "*.wav", "*.ogg"):
    tracks.extend(music_dir.glob(ext))

tracks = [str(t.resolve()) for t in tracks]
random.shuffle(tracks)

playlist_file.write_text("\n".join(tracks))

print(f"Built playlist for {show}: {len(tracks)} tracks")
print(f"Saved to {playlist_file}")
