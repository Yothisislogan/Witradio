#!/usr/bin/env python3
"""Simple WIT Radio schedule daemon."""

import subprocess
import time
from pathlib import Path

LAST_SHOW_FILE = Path("runtime/last_show.txt")


def current_show():
    result = subprocess.check_output(["python3", "tools/current_show.py"])
    return result.decode().strip()


while True:
    show = current_show()

    previous = ""
    if LAST_SHOW_FILE.exists():
        previous = LAST_SHOW_FILE.read_text().strip()

    if show != previous:
        print(f"Switching playlist to: {show}")
        subprocess.call(["bash", "tools/rotate_playlist.sh"])
        LAST_SHOW_FILE.parent.mkdir(exist_ok=True)
        LAST_SHOW_FILE.write_text(show)

    time.sleep(60)
