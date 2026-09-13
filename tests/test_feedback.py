import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mac.feedback import FeedbackStore, track_id, weighted_rotation


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = FeedbackStore(self.root / 'feedback.db')
        self.track = 'a' * 64
        self.listener = '00000000-0000-4000-8000-000000000001'
        self.store.register(self.track, 'Test music', now=1000)

    def test_votes_survive_restart_and_retries_do_not_multiply(self):
        self.store.vote(self.track, self.listener, 'like', now=1001)
        second = FeedbackStore(self.root / 'feedback.db')
        second.vote(self.track, self.listener, 'like', now=1002)
        second.vote(self.track, self.listener, 'dislike', now=1010)
        with second.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*), SUM(value) FROM votes').fetchone(), (1, -1))

    def test_concurrent_duplicate_votes_are_idempotent(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: self.store.vote(self.track, self.listener, 'like', now=1001), range(12)))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM votes').fetchone()[0], 1)

    def test_unknown_stale_and_malformed_votes_are_rejected(self):
        for identity, listener, vote, now in [
            ('b'*64, self.listener, 'like', 1001),
            (self.track, self.listener, 'like', 1121),
            (self.track, self.listener, 'skip', 1001),
            (self.track, 'not-a-listener', 'like', 1001),
            (None, self.listener, 'like', 1001),
        ]:
            with self.subTest(identity=identity, vote=vote), self.assertRaises(ValueError):
                self.store.vote(identity, listener, vote, now=now)

    def test_three_listeners_required_and_weight_stays_bounded(self):
        for i in range(2):
            self.store.vote(self.track, f'00000000-0000-4000-8000-{i:012d}', 'like', now=1001)
        self.assertNotIn(self.track, self.store.weights())
        self.store.vote(self.track, '00000000-0000-4000-8000-000000000002', 'like', now=1001)
        self.assertGreater(self.store.weights()[self.track], 1)
        self.assertLess(self.store.weights()[self.track], 1.25)

    def test_rapid_vote_changes_are_limited(self):
        self.store.vote(self.track, self.listener, 'like', now=1001)
        with self.assertRaises(OverflowError):
            self.store.vote(self.track, self.listener, 'dislike', now=1002)

    def test_ids_survive_checkout_moves_but_are_station_scoped(self):
        one = SimpleNamespace(id='wit', output_dir=self.root / 'one')
        two = SimpleNamespace(id='wit', output_dir=self.root / 'two')
        self.assertEqual(track_id(one.output_dir/'music/a.mp3', one), track_id(two.output_dir/'music/a.mp3', two))
        two.id = 'other'
        self.assertNotEqual(track_id(one.output_dir/'music/a.mp3', one), track_id(two.output_dir/'music/a.mp3', two))

    def test_musicgen_id_survives_moving_into_backup(self):
        station = SimpleNamespace(id='wit', output_dir=self.root)
        first = self.root / 'music' / 'a.mp3'
        backup = self.root / 'backup_music' / 'a.mp3'
        for path in (first, backup):
            path.parent.mkdir(parents=True)
            path.with_suffix('.json').write_text(json.dumps({'track_id': 'musicgen-123'}))
        self.assertEqual(track_id(first, station), track_id(backup, station))

    def test_feedback_changes_rotation_without_removing_tracks(self):
        station = SimpleNamespace(id='wit', output_dir=self.root, home_dir=self.root)
        files = [self.root/'a.mp3', self.root/'b.mp3', self.root/'c.mp3']
        identities = [track_id(p, station) for p in files]
        for identity in identities:
            self.store.register(identity, 'Music', now=1000)
        for i in range(3):
            listener = f'00000000-0000-4000-8000-{i:012d}'
            self.store.vote(identities[0], listener, 'dislike', now=1001)
            self.store.vote(identities[2], listener, 'like', now=1001)
        with patch('mac.feedback.random.random', return_value=.5):
            rotation = weighted_rotation(files, station)
        self.assertEqual(rotation, [files[2], files[1], files[0]])
