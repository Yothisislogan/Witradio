import os
import unittest
from unittest.mock import patch

from mac.station_config import load_station_config, station_ids


class StationConfigTests(unittest.TestCase):
    def test_configured_stations_have_isolated_runtime_and_api_ports(self):
        klod = load_station_config("klod-fm")
        cdex = load_station_config("cdex-fm")

        self.assertIn("klod-fm", station_ids())
        self.assertIn("cdex-fm", station_ids())
        self.assertEqual(klod.stream.mount, "/klod-fm")
        self.assertEqual(cdex.stream.mount, "/cdex-fm")
        self.assertNotEqual(klod.stream.api_port, cdex.stream.api_port)
        self.assertNotEqual(klod.output_dir, cdex.output_dir)
        self.assertNotEqual(klod.current_track_file, cdex.current_track_file)

    def test_codex_and_claude_content_paths_are_isolated(self):
        klod = load_station_config("klod-fm")
        cdex = load_station_config("cdex-fm")
        path_attrs = (
            "output_dir",
            "home_dir",
            "talk_dir",
            "bumper_dir",
            "archive_dir",
            "scripts_dir",
            "show_log_dir",
            "intent_dir",
            "topic_bank_file",
            "messages_file",
            "ledger_path",
            "active_threads_path",
            "history_db_path",
            "playlist_file",
            "current_track_file",
            "now_playing_file",
        )

        for attr in path_attrs:
            self.assertNotEqual(getattr(klod, attr), getattr(cdex, attr), attr)

    def test_agent_assignment_matches_station_owner(self):
        # KLOD-FM moved to the Gemini wrapper with the rest of WIT Radio (May 2026).
        self.assertEqual(load_station_config("klod-fm").agent.kind, "gemini")
        self.assertEqual(load_station_config("cdex-fm").agent.kind, "codex")

    def test_wit_next_is_isolated_on_its_own_mount(self):
        nxt = load_station_config("wit-next")
        others = [load_station_config(sid) for sid in station_ids() if sid != "wit-next"]

        self.assertEqual(nxt.stream.mount, "/next")
        self.assertEqual(nxt.schedule_path.name, "wit_next_schedule.yaml")
        self.assertEqual(nxt.agent.kind, "openai")
        for other in others:
            self.assertNotEqual(nxt.stream.mount, other.stream.mount, other.id)
            self.assertNotEqual(nxt.stream.api_port, other.stream.api_port, other.id)
            for attr in ("output_dir", "home_dir", "current_track_file", "history_db_path"):
                self.assertNotEqual(getattr(nxt, attr), getattr(other, attr), (other.id, attr))
        # The live station keeps its mount until the Phase 3 cutover.
        self.assertEqual(load_station_config("writ-fm").stream.mount, "/stream")

    def test_llm_settings_resolve_from_config_and_environment(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("WRIT_LLM_", "SCRIPT_MODEL"))}
        with patch.dict(os.environ, clean, clear=True):
            default = load_station_config("wit-next").llm
            self.assertEqual(default.base_url, "http://halo.local:8080/v1")
            self.assertEqual(default.model, "")  # ${SCRIPT_MODEL} is unset

        with patch.dict(os.environ, {**clean, "SCRIPT_MODEL": "qwen3-35b-a3b"}, clear=True):
            self.assertEqual(load_station_config("wit-next").llm.model, "qwen3-35b-a3b")

        overrides = {
            **clean,
            "SCRIPT_MODEL": "ignored",
            "WRIT_LLM_BASE_URL": "http://127.0.0.1:8080/v1/",
            "WRIT_LLM_MODEL": "gpt-oss-120b",
        }
        with patch.dict(os.environ, overrides, clear=True):
            llm = load_station_config("wit-next").llm
        self.assertEqual(llm.base_url, "http://127.0.0.1:8080/v1")
        self.assertEqual(llm.model, "gpt-oss-120b")

    def test_klod_inherits_writ_schedule_and_cdex_has_own_schedule(self):
        writ = load_station_config("writ-fm")
        klod = load_station_config("klod-fm")
        cdex = load_station_config("cdex-fm")

        self.assertEqual(klod.schedule_path, writ.schedule_path)
        self.assertNotEqual(cdex.schedule_path, writ.schedule_path)
        self.assertEqual(cdex.schedule_path.name, "cdex_schedule.yaml")


if __name__ == "__main__":
    unittest.main()
