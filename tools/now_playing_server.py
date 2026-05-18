#!/usr/bin/env python3
"""Simple WIT Radio now-playing API prototype."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import time
from pathlib import Path

STATE_FILE = Path("runtime/now_playing.json")

DEFAULT_STATE = {
    "station": "WIT Radio",
    "stream_url": "http://localhost:8000/witradio",
    "show": "Unknown",
    "host": "WIT Radio",
    "track": "Station starting",
    "artist": "WIT Radio",
    "updated_at": None,
}


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return DEFAULT_STATE
    return DEFAULT_STATE


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/now-playing", "/health"):
            self.send_response(404)
            self.end_headers()
            return

        if self.path == "/health":
            payload = {"ok": True, "service": "wit-now-playing", "time": int(time.time())}
        else:
            payload = load_state()

        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    STATE_FILE.parent.mkdir(exist_ok=True)
    server = HTTPServer(("0.0.0.0", 8787), Handler)
    print("WIT now-playing API running at http://localhost:8787/now-playing")
    server.serve_forever()
