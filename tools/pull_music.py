#!/usr/bin/env python3
"""Pull cleared lossless classical into the WIT-FM library and catalog.

A thin entry point for mac/music_library.py; see that file for the details.

    python3 tools/pull_music.py sources
    python3 tools/pull_music.py fetch all --dry-run
    python3 tools/pull_music.py fetch all
    python3 tools/pull_music.py local ~/Music/musopen --source musopen-site \\
        --license public_domain --credit "{performer} via Musopen"
    python3 tools/pull_music.py approve ishizaka-art-of-fugue
    python3 tools/pull_music.py report
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mac"))

from music_library import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
