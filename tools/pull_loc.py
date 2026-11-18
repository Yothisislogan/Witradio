#!/usr/bin/env python3
"""Pull Library of Congress recordings for The Stacks into the library and catalog.

A thin entry point for mac/loc_library.py; see that file for the details.

    python3 tools/pull_loc.py sources
    python3 tools/pull_loc.py fetch loc-edison --dry-run
    python3 tools/pull_loc.py fetch loc-edison --limit 25
    python3 tools/pull_loc.py fetch cdj-jukebox --collection loc-jukebox-jazz --limit 10
    python3 tools/pull_loc.py review
    python3 tools/pull_loc.py approve 41 42 43
    python3 tools/pull_loc.py reject 44 --why "dialect comedy"
    python3 tools/pull_loc.py excluded
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mac"))

from loc_library import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
