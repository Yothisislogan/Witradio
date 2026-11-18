import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))
import api_server  # noqa: E402

WIT_NEXT_SCHEDULE = PROJECT_ROOT / "config" / "wit_next_schedule.yaml"


class ApiServerTests(unittest.TestCase):
    def test_schedule_upcoming_uses_exact_next_airings(self):
        station = SimpleNamespace(schedule_path=WIT_NEXT_SCHEDULE)
        with patch.object(api_server, "STATION", station):
            # Wednesday Nov 18, 2026, 4:45 PM Eastern
            info = api_server.get_schedule_info(datetime(2026, 11, 18, 16, 45))

        self.assertEqual(info["timezone"], "America/New_York")
        self.assertEqual(info["current"]["show_id"], "market_closing_tape")
        self.assertEqual(info["current"]["program"], "market")
        self.assertEqual(info["upcoming"][0]["show_id"], "ying_yang")
        self.assertEqual(info["upcoming"][0]["starts_around"], "17:00")
        self.assertEqual(info["upcoming"][0]["starts_at"], "2026-11-18T17:00:00")
        self.assertEqual(info["upcoming"][0]["starts_at_utc"], "2026-11-18T22:00:00+00:00")
        self.assertEqual(
            [u["show_id"] for u in info["upcoming"]],
            ["ying_yang", "office_hours", "monster_mix", "bedtime_early"],
        )

    def test_station_prefixed_now_playing_route_is_resolved(self):
        self.assertEqual(
            api_server.parse_station_route("/cdex-fm/now-playing"),
            ("cdex-fm", "/now-playing"),
        )
        self.assertEqual(
            api_server.parse_station_route("/stations/klod-fm/diary"),
            ("klod-fm", "/diary"),
        )
        self.assertEqual(
            api_server.parse_station_route("/klod-fm/message"),
            ("klod-fm", "/message"),
        )

    def test_unknown_non_station_route_is_ignored(self):
        self.assertIsNone(api_server.parse_station_route("/assets/app.js"))
        self.assertIsNone(api_server.parse_station_route("/now-playing"))

    def test_standalone_api_health_does_not_require_local_streamer(self):
        with (
            patch.object(api_server, "_api_mode", "standalone"),
            patch.object(api_server, "_encoder_getter", lambda: None),
            patch.object(api_server, "check_url", return_value=True),
            patch.object(api_server, "check_process", return_value=True),
        ):
            info = api_server.get_health_status()

        self.assertEqual(info["status"], "healthy")
        self.assertEqual(info["mode"], "standalone")
        self.assertEqual(info["components"]["streamer"]["status"], "not_attached")

    def test_stream_api_health_requires_local_streamer(self):
        with (
            patch.object(api_server, "_api_mode", "stream"),
            patch.object(api_server, "_encoder_getter", lambda: None),
            patch.object(api_server, "check_url", return_value=True),
            patch.object(api_server, "check_process", return_value=True),
        ):
            info = api_server.get_health_status()

        self.assertEqual(info["status"], "degraded")
        self.assertEqual(info["components"]["streamer"]["status"], "down")


if __name__ == "__main__":
    unittest.main()
