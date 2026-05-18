#!/usr/bin/env python3
"""Simple event log helper for WIT Radio."""

import json
import time
from pathlib import Path

LOG_FILE = Path("runtime/events.log")


def write_event(event_name, details):
    payload = {
        "time": int(time.time()),
        "event": event_name,
        "details": details,
    }

    LOG_FILE.parent.mkdir(exist_ok=True)

    with LOG_FILE.open("a") as f:
        f.write(json.dumps(payload) + "\n")


if __name__ == "__main__":
    write_event("startup", {"service": "WIT Radio"})
    print("Event written.")
