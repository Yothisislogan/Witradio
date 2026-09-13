import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mac.content_generator import talk_generator, persona
from mac.play_history import PlayHistory
from mac import feeder


class ShortBreakTests(unittest.TestCase):
    def test_overlong_script_is_rejected_before_tts(self):
        with patch.object(talk_generator, 'run_claude', return_value='word '*61):
            self.assertIsNone(talk_generator.run_generation('prompt', 'dj_break'))
        with patch.object(talk_generator, 'run_claude', return_value='word '*45):
            self.assertIsNotNone(talk_generator.run_generation('prompt', 'dj_break'))

    def test_wit_host_prompt_uses_real_config_and_short_break_instructions(self):
        prompt = talk_generator.build_generation_prompt('wit_dash', 'dj_break', 'A little afternoon energy', 'The Day Shift', 'Upbeat instrumental music', 'workday_vibes', show_id='day_shift')
        self.assertIn('You are Dash', prompt)
        self.assertIn('32-60 words', prompt)
        self.assertNotIn('You are Signal', prompt)
        self.assertNotIn('Extended segments', prompt)

    def test_budget_survives_restart_and_ignores_music(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history.db'
            history = PlayHistory(path)
            for i in range(4):
                history.record_play(f'talk{i}.wav', vibe='talk')
            history.record_play('music.mp3', vibe='bumper')
            self.assertEqual(PlayHistory(path).talk_budget_remaining(), 0)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE plays SET played_at=datetime('now', '-2 hours') WHERE vibe='talk'")
            self.assertEqual(PlayHistory(path).talk_budget_remaining(), 4)

    def test_preexisting_long_break_is_not_queued_for_wit_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            slot_dir = Path(tmp)/'day_shift'/'slot'
            slot_dir.mkdir(parents=True)
            short = slot_dir/'00_dj_break.wav'
            long = slot_dir/'01_deep_dive.wav'
            short.write_bytes(b'audio'); long.write_bytes(b'audio')
            with patch.object(feeder, 'TALK_DIR', Path(tmp)), patch.object(feeder, '_is_current_station_content', return_value=True), patch.object(feeder, 'audio_duration', side_effect=lambda p: 25 if p==short else 300), patch.object(feeder, 'HISTORY_ENABLED', False):
                self.assertEqual(feeder.get_talk_segments('day_shift', 'slot'), [short])
