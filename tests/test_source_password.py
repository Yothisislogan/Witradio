"""The Icecast source password must come from the environment and nowhere else."""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mac import feeder
from mac.station_config import (
    PROJECT_ROOT,
    MissingSourcePasswordError,
    StreamConfig,
    load_station_config,
    station_ids,
)

# Assembled at runtime so this file does not itself contain the old literal.
OLD_COMMITTED_PASSWORD = "writ_source_" + "2024"


def _env_without(name: str) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != name}


class StationSourcePasswordTests(unittest.TestCase):
    def test_password_is_read_from_environment(self):
        with patch.dict(os.environ, {"WRIT_SOURCE_PASSWORD": "rotated-secret"}):
            station = load_station_config("writ-fm")

        self.assertEqual(station.stream.source_password, "rotated-secret")
        self.assertEqual(station.stream.source_password_env, "WRIT_SOURCE_PASSWORD")
        # It must never leak through the printable views of the config.
        self.assertNotIn("rotated-secret", repr(station))
        self.assertNotIn("rotated-secret", json.dumps(station.to_dict()))
        self.assertNotIn("rotated-secret", json.dumps(station.env()))

    def test_missing_password_has_no_fallback(self):
        with patch.dict(os.environ, _env_without("WRIT_SOURCE_PASSWORD"), clear=True):
            for station_id in station_ids():
                station = load_station_config(station_id)
                self.assertEqual(station.stream.source_password, "", station_id)
                with self.assertRaises(MissingSourcePasswordError):
                    station.stream.require_source_password()

    def test_password_literal_in_station_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "stations.yaml"
            config.write_text(
                "stations:\n"
                "  test-fm:\n"
                "    stream:\n"
                "      mount: /test\n"
                "      source_password: hunter2\n"
            )
            with self.assertRaises(ValueError) as ctx:
                load_station_config("test-fm", path=config)

        self.assertIn("WRIT_SOURCE_PASSWORD", str(ctx.exception))
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_no_tracked_file_contains_the_old_password(self):
        try:
            listing = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git is not available")

        needle = OLD_COMMITTED_PASSWORD.encode()
        offenders = []
        for raw in listing.split(b"\0"):
            if not raw:
                continue
            path = PROJECT_ROOT / raw.decode()
            try:
                if needle in path.read_bytes():
                    offenders.append(raw.decode())
            except OSError:
                continue
        self.assertEqual(offenders, [])


class FeederEzstreamConfigTests(unittest.TestCase):
    def _station(self, password: str) -> SimpleNamespace:
        return SimpleNamespace(
            stream=StreamConfig(
                icecast_host="localhost",
                icecast_port=8000,
                mount="/next",
                api_port=8002,
                source_password=password,
                stream_name="WIT-FM Next",
                stream_genre="Radio",
                stream_description="Staging",
            )
        )

    def test_rendered_config_uses_env_password_and_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "runtime" / "radio.xml"
            with (
                patch.object(feeder, "STATION", self._station("from-env")),
                patch.object(feeder, "RADIO_XML", target),
            ):
                feeder.write_ezstream_config()

            text = target.read_text()
            mode = stat.S_IMODE(target.stat().st_mode)

        self.assertIn("<password>from-env</password>", text)
        self.assertIn("<mountpoint>/next</mountpoint>", text)
        self.assertEqual(mode, 0o600)

    def test_rendering_refuses_without_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "radio.xml"
            with (
                patch.object(feeder, "STATION", self._station("")),
                patch.object(feeder, "RADIO_XML", target),
            ):
                with self.assertRaises(MissingSourcePasswordError):
                    feeder.write_ezstream_config()
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
