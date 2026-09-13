"""Bounded audio sampling in a background thread, independent of generation."""

import re
import subprocess
import threading
import time


def sample_audio(url: str) -> str:
    try:
        result = subprocess.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-rw_timeout", "5000000",
            "-i", url, "-t", "8", "-vn", "-af", "volumedetect", "-f", "null", "-",
        ], capture_output=True, text=True, timeout=18)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    values = re.findall(r"max_volume:\s*(-?inf|-?[\d.]+) dB", result.stderr)
    if result.returncode or not values:
        return "unavailable"
    return "silent" if float(values[-1]) <= -55 else "audible"


class StreamMonitor:
    def __init__(self, url, current_track_file, interval=30):
        self.url = url
        self.current_track_file = current_track_file
        self.interval = interval
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.state = {"status": "checking", "checked_at": None, "failures": 0, "track_path": ""}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _track(self):
        try:
            return self.current_track_file.read_text().strip()
        except OSError:
            return ""

    def run(self):
        while not self.stop_event.is_set():
            before = self._track()
            status = sample_audio(self.url)
            # A sample spanning a track change cannot justify quarantining either track.
            same_track = before if before == self._track() else ""
            with self.lock:
                previous = self.state
                failures = 0 if status == "audible" else previous["failures"] + 1
                silent_same_track = (status == "silent" and previous["status"] == "silent"
                                     and same_track and same_track == previous["track_path"])
                self.state = {"status": status, "checked_at": time.time(), "failures": failures,
                              "track_path": same_track, "quarantine": bool(silent_same_track)}
            self.stop_event.wait(self.interval)

    def start(self):
        threading.Thread(target=self.run, name="stream-monitor", daemon=True).start()

    def stop(self):
        self.stop_event.set()
