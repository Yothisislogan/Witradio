import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mac import feeder, stream_monitor
from mac.audio_library import backup_tracks, quarantine
from tools.import_backup import import_track


class AudioRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.station = SimpleNamespace(id='wit', output_dir=self.root/'output', home_dir=self.root/'home')
        self.backup = self.station.output_dir / 'backup_music'
        self.backup.mkdir(parents=True)

    def add_track(self, name, **metadata):
        path = self.backup / f'{name}.wav'
        path.write_bytes(b'audio')
        path.with_suffix('.json').write_text(json.dumps(metadata))
        return path

    def test_only_approved_local_non_quarantined_backups_are_used(self):
        good = self.add_track('good', approved=True, station_id='wit')
        self.add_track('unapproved', station_id='wit')
        self.add_track('foreign', approved=True, station_id='other')
        bad = self.add_track('silent', approved=True, station_id='wit')
        quarantine(bad, self.station, 'silence')
        outside = self.root / 'outside.wav'
        outside.write_bytes(b'foreign')
        link = self.backup / 'linked.wav'
        link.symlink_to(outside)
        link.with_suffix('.json').write_text('{"approved":true,"station_id":"wit"}')
        self.assertEqual(backup_tracks(self.station), [good])

    def test_empty_show_uses_approved_backup_without_silence(self):
        path = self.add_track('good', approved=True, station_id='wit', display_name='Emergency music')
        with patch.object(feeder, 'STATION', self.station), patch.object(feeder, 'get_bumpers', return_value=[]), patch.object(feeder, 'get_talk_segments', return_value=[]):
            entries = feeder.build_playlist('night_shift', 'slot')
        self.assertEqual(entries, [{'path': str(path), 'type': 'backup', 'name': 'Emergency music'}])

    def test_no_media_keeps_explicit_last_resort_silence(self):
        with patch.object(feeder, 'STATION', self.station), patch.object(feeder, 'get_bumpers', return_value=[]), patch.object(feeder, 'get_talk_segments', return_value=[]):
            entries = feeder.build_playlist('night_shift', 'slot')
        self.assertEqual(entries[0]['type'], 'silence')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
    def test_real_audio_probe_and_backup_import_reject_silence(self):
        tone = self.root/'approved.wav'
        silence = self.root/'silence.wav'
        for path, source in [(tone, 'sine=frequency=440:sample_rate=8000'), (silence, 'anullsrc=r=8000:cl=mono')]:
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', source, '-t', '31', str(path)], check=True)
        self.assertEqual(stream_monitor.sample_audio(str(tone)), 'audible')
        self.assertEqual(stream_monitor.sample_audio(str(silence)), 'silent')
        imported = import_track(tone, self.station)
        self.assertIn(imported, backup_tracks(self.station))
        with self.assertRaises(ValueError):
            import_track(silence, self.station)

    def test_failed_probe_is_not_reported_as_audible(self):
        with patch.object(stream_monitor.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ffmpeg', 18)):
            self.assertEqual(stream_monitor.sample_audio('http://station/stream'), 'unavailable')

    def test_quarantine_requires_two_silent_samples_of_the_same_track(self):
        marker = self.root/'current.txt'
        marker.write_text('one.wav')
        monitor = stream_monitor.StreamMonitor('test', marker, interval=0)
        calls = []
        def sample(url):
            calls.append(url)
            if len(calls) == 1:
                marker.write_text('two.wav')  # A change during the first sample.
            if len(calls) == 2:
                self.assertFalse(monitor.snapshot().get('quarantine'))
            if len(calls) == 3:
                self.assertFalse(monitor.snapshot().get('quarantine'))
                monitor.stop()
            return 'silent'
        with patch.object(stream_monitor, 'sample_audio', side_effect=sample):
            monitor.run()
        self.assertTrue(monitor.snapshot()['quarantine'])
        self.assertEqual(monitor.snapshot()['track_path'], 'two.wav')
