#!/usr/bin/env python3
"""Keep the Halo's buffer and the playout server's in step, over Tailscale.

Runs on the Halo. The server is the authority for plans, the Halo for audio:

    pull    the server's buffer/plans/ (classical and Stacks plans, _status.json)
    push    the Halo's buffer to the server: show slots and the plans' talk
            segments, never plans/
    prune   delete slots and links that aired more than --days ago
            (run it on both machines)

The producer daemon also pulls the library recordings a Stacks plan re-cuts
(remixes, speech samples, Then & Now) into the Halo's library, by their
library-relative paths, before it renders them.

Files land with rsync --delay-updates, and half-written files are dot-names
the director skips, so the playout only ever sees whole files.

WRIT_SYNC_REMOTE is the server's buffer folder as rsync sees it over SSH,
e.g. witfm@hetzner-playout:/home/witfm/Witradio/output/stations/wit-next/buffer
(a Tailscale MagicDNS name works as the host). WRIT_SYNC_SSH adds ssh
options, e.g. "-i ~/.ssh/witfm_sync". WRIT_SYNC_LIBRARY_REMOTE is the server's
library folder; by default it's WRIT_SYNC_REMOTE with buffer swapped for
library, which matches the station layout.

    python3 mac/buffer_sync.py pull
    python3 mac/buffer_sync.py push
    python3 mac/buffer_sync.py prune --days 3
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schedule import parse_slot_key  # noqa: E402


class SyncError(RuntimeError):
    pass


def library_remote_for(remote: str) -> str:
    """The server's library folder, next to its buffer folder."""
    remote = remote.rstrip("/")
    head, _, tail = remote.rpartition("/")
    return f"{head}/library" if tail == "buffer" and head else ""


class Sync:
    def __init__(self, local: Path, remote: str, *, ssh: str = "", rsync: str = "rsync", timeout: int = 900,
                 library_remote: str | None = None):
        self.local, self.remote = Path(local), remote.rstrip("/")
        self.ssh, self.rsync, self.timeout = ssh, rsync, timeout
        self.library_remote = (library_remote if library_remote is not None else library_remote_for(remote)).rstrip("/")

    @classmethod
    def from_env(cls, local: Path) -> "Sync | None":
        remote = os.environ.get("WRIT_SYNC_REMOTE", "").strip()
        if not remote:
            return None
        return cls(local, remote, ssh=os.environ.get("WRIT_SYNC_SSH", ""),
                   library_remote=os.environ.get("WRIT_SYNC_LIBRARY_REMOTE", "").strip() or None)

    def _rsync(self, *args: str, ok: Sequence[int] = (0, 24)) -> str:
        command = [self.rsync, "-a", "--delay-updates", "--timeout=60"]
        if self.ssh:
            command += ["-e", f"ssh {self.ssh}"]
        try:
            result = subprocess.run(command + list(args), capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SyncError(f"rsync: {exc}") from exc
        if result.returncode not in ok:  # 24: files vanished mid-run, harmless here
            raise SyncError(f"rsync exited {result.returncode}: {result.stderr.strip()[-300:]}")
        return result.stdout

    def pull(self) -> None:
        (self.local / "plans").mkdir(parents=True, exist_ok=True)
        self._rsync("--exclude=.*", f"{self.remote}/plans/", f"{self.local}/plans/")

    def push(self) -> None:
        self.local.mkdir(parents=True, exist_ok=True)
        self._rsync("--exclude=/plans/", "--exclude=.*", "--exclude=*.partial*", f"{self.local}/", f"{self.remote}/")

    def pull_media(self, rel_paths: Sequence[str], library: Path) -> list[str]:
        """Copy these library-relative recordings from the server's library into `library`."""
        wanted = sorted({p for p in rel_paths if p and not p.startswith("/") and ".." not in Path(p).parts})
        if not wanted:
            return []
        if not self.library_remote:
            raise SyncError("no library remote: set WRIT_SYNC_LIBRARY_REMOTE")
        library.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as listing:
            listing.write("".join(f"{p}\n" for p in wanted))
        try:
            # 23: some files weren't there; the producer reports what's still missing
            self._rsync(f"--files-from={listing.name}", f"{self.library_remote}/", f"{library}/", ok=(0, 23, 24))
        finally:
            os.unlink(listing.name)
        return wanted


def prune(buffer: Path, days: float, now: datetime | None = None) -> list[Path]:
    """Remove show slots, link folders and plans older than `days` (wall clock, by name)."""
    now = now or datetime.now()
    horizon = now - timedelta(days=days)
    removed: list[Path] = []
    if not buffer.is_dir():
        return removed
    for show in buffer.iterdir():
        if not show.is_dir() or show.name in ("plans", "talk"):
            continue
        for slot in show.iterdir():
            try:
                aired = parse_slot_key(slot.name)
            except ValueError:
                continue
            if aired < horizon:
                shutil.rmtree(slot, ignore_errors=True)
                removed.append(slot)
    for plan in sorted((buffer / "plans").glob("*.json")) if (buffer / "plans").is_dir() else []:
        if plan.name.startswith("_"):
            continue
        try:
            ends = datetime.fromisoformat(json.loads(plan.read_text())["ends_at"]).replace(tzinfo=None)
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
        if ends < horizon:
            plan.unlink(missing_ok=True)
            shutil.rmtree(buffer / "talk" / plan.stem, ignore_errors=True)
            removed.append(plan)
    return removed


def main(argv: Sequence[str] | None = None) -> int:
    from station_config import load_station_config

    parser = argparse.ArgumentParser(description="Sync the Halo's buffer with the playout server")
    parser.add_argument("--station", default=os.environ.get("WRIT_STATION_ID", "wit-next"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull")
    sub.add_parser("push")
    p_prune = sub.add_parser("prune")
    p_prune.add_argument("--days", type=float, default=3.0)
    args = parser.parse_args(argv)

    buffer = load_station_config(args.station).output_dir / "buffer"
    if args.cmd == "prune":
        removed = prune(buffer, args.days)
        print(f"removed {len(removed)} old slots, plans and links")
        return 0
    sync = Sync.from_env(buffer)
    if sync is None:
        print("WRIT_SYNC_REMOTE is not set (the server's buffer, e.g. witfm@hetzner-playout:/path/to/buffer)")
        return 2
    try:
        sync.pull() if args.cmd == "pull" else sync.push()
    except SyncError as exc:
        print(exc)
        return 1
    print(f"{args.cmd}: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
