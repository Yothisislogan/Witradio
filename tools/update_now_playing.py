#!/usr/bin/env python3
"""Update the now-playing runtime file."""

import json
import sys
import time
from pathlib import Path

STATE_FILE = Path("runtime/now_playing.json")

show = sys.argv[1] if len(sys.argv) > 1 else "The Day Shift"
track = sys.argv[2] if len(sys.argv) > 2 else "Unknown Track"
artist = sys.argv[3] if len(sys.argv) > 3 else "AI Generated"
host = sys.argv[4] if len(sys.argv) > 4 else "Dash"

payload = {
    "station": "WIT Radio",
    "stream_url": "http://localhost:8000/witradio",
    "show": show,
    "host": host,
    "track": track,
    "artist": artist,
    "updated_at": int(time.time()),
}

STATE_FILE.parent.mkdir(exist_ok=True)
STATE_FILE.write_text(json.dumps(payload, indent=2))

print("Updated now playing:")
print(json.dumps(payload, indent=2))
