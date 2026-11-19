#!/usr/bin/env python3
"""Pull public-domain stories for Hushworth's Bedtime Stories into the story library.

A thin entry point for mac/story_library.py; see that file for the details.

    python3 tools/pull_stories.py sources
    python3 tools/pull_stories.py pull --all
    python3 tools/pull_stories.py pull dracula --file ~/Downloads/dracula.txt
    python3 tools/pull_stories.py list
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mac"))

from story_library import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
