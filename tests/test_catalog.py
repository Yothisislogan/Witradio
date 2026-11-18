"""Catalog: license records, intake checks, per-work gain, airplay audit."""

import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mac import catalog as cat
from mac.catalog import Catalog, CatalogError

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
TODAY = date(2026, 11, 18)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def sine_flac(path: Path, seconds: float, freq: float = 440.0, gain_db: float = -20.0) -> Path:
    ffmpeg(
        "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=44100:duration={seconds}",
        "-af", f"volume={gain_db}dB", "-ac", "2", "-sample_fmt", "s16", str(path),
    )
    return path


def noise_flac(path: Path, seconds: float) -> Path:
    ffmpeg(
        "-f", "lavfi", "-i", f"anoisesrc=color=white:amplitude=0.3:sample_rate=44100:duration={seconds}",
        "-ac", "2", "-sample_fmt", "s16", str(path),
    )
    return path


def mp3(path: Path, source: Path, bitrate: str = "128k") -> Path:
    ffmpeg("-i", str(source), "-c:a", "libmp3lame", "-b:a", bitrate, str(path))
    return path


def classical(**overrides):
    work = dict(
        kind="classical", title="Goldberg Variations, BWV 988", composer="Johann Sebastian Bach",
        composition_year=1741, performer="Kimiko Ishizaka", form="variations",
        source="open_goldberg", license="cc0", credit_line="Kimiko Ishizaka (courtesy credit)",
    )
    work.update(overrides)
    return work


def fake_files(*minutes: float, lossless: bool = True, prefix: str = "/library/x"):
    return [
        {"path": f"{prefix}_{i}.flac", "duration_sec": m * 60, "lossless": lossless}
        for i, m in enumerate(minutes)
    ]


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class IntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.quiet = sine_flac(root / "mvt1_quiet.flac", 4, 440, -30)
        cls.loud = sine_flac(root / "mvt2_loud.flac", 4, 660, -6)
        cls.noise = noise_flac(root / "full_band.flac", 4)
        cls.lossy = mp3(root / "lossy.mp3", cls.noise)
        cls.fake = root / "fake_lossless.flac"
        ffmpeg("-i", str(cls.lossy), "-sample_fmt", "s16", str(cls.fake))
        cls.fake320 = root / "fake_320k.flac"
        ffmpeg("-i", str(mp3(root / "lossy320.mp3", cls.noise, "320k")), "-sample_fmt", "s16", str(cls.fake320))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.catalog = Catalog(":memory:")
        self.addCleanup(self.catalog.close)

    def test_probe_reads_format(self):
        info = cat.probe(self.quiet)
        self.assertEqual((info.codec, info.sample_rate, info.bit_depth, info.channels),
                         ("flac", 44100, 16, 2))
        self.assertAlmostEqual(info.duration_sec, 4.0, places=1)
        self.assertTrue(info.lossless)
        self.assertFalse(cat.probe(self.lossy).lossless)

    def test_spectrogram_check_catches_a_flac_made_from_an_mp3(self):
        real = cat.spectral_check(self.noise)
        fake = cat.spectral_check(self.fake)
        self.assertEqual(real.verdict, "pass")
        self.assertEqual(fake.verdict, "reject")
        self.assertLess(fake.cutoff_hz, cat.CUTOFF_REJECT_HZ)
        self.assertGreater(fake.drop_db, cat.CUTOFF_MIN_DROP_DB)

    def test_classical_intake_rejects_lossy_and_fake_lossless(self):
        for path in (self.lossy, self.fake):
            with self.subTest(path=path.name):
                report = cat.intake(self.catalog, [path], hash_files=False, **classical())
                self.assertIsNone(report.work_id)
                self.assertTrue(report.rejected)
        self.assertEqual(self.catalog.works(), [])

    def test_suspect_cutoff_is_quarantined_not_aired(self):
        report = cat.intake(self.catalog, [self.fake320], hash_files=False, **classical())
        self.assertIsNotNone(report.work_id)
        work = self.catalog.work(report.work_id)
        self.assertEqual(work.status, "quarantined")
        self.assertIn("spectrogram suspect", work.notes)
        self.assertIn("status is quarantined", self.catalog.airability(work, TODAY))

    def test_one_gain_per_work_measured_across_movements(self):
        report = cat.intake(self.catalog, [self.quiet, self.loud], **classical())
        self.assertIsNotNone(report.work_id, report.rejected)
        work = self.catalog.work(report.work_id)

        self.assertEqual([Path(f.path).name for f in work.files], ["mvt1_quiet.flac", "mvt2_loud.flac"])
        self.assertTrue(work.lossless)
        self.assertTrue(all(f.sha256 for f in work.files))
        whole_lufs, whole_peak = cat.measure_loudness([self.quiet, self.loud])
        self.assertAlmostEqual(work.loudness_lufs, whole_lufs, places=1)
        self.assertEqual(work.gain_db, cat.fixed_gain_db(whole_lufs, whole_peak))
        # The loud movement sets the true peak, so the gain cannot push it past the ceiling.
        self.assertLessEqual(work.true_peak_dbtp + work.gain_db, cat.TRUE_PEAK_CEILING_DBTP + 0.05)
        self.assertEqual(self.catalog.airability(work, TODAY), [])

    def test_import_manifest_resolves_paths_next_to_the_manifest(self):
        manifest = Path(self.tmp.name) / "manifest.yaml"
        manifest.write_text(
            "works:\n"
            "  - kind: classical\n"
            "    title: Test Piece\n"
            "    composer: Test Composer\n"
            "    composition_year: 1800\n"
            "    source: musopen\n"
            "    license: public_domain\n"
            "    credit_line: Test Ensemble via Musopen\n"
            "    tags: [Keyboard, quiet]\n"
            "    files: [mvt1_quiet.flac]\n"
        )
        reports = cat.import_manifest(self.catalog, manifest, hash_files=False)
        self.assertEqual(len(reports), 1)
        work = self.catalog.work(reports[0].work_id)
        self.assertEqual(work.tags, ("keyboard", "quiet"))
        self.assertEqual(Path(work.files[0].path), self.quiet.resolve())


class GainTests(unittest.TestCase):
    def test_gain_moves_toward_target(self):
        self.assertEqual(cat.fixed_gain_db(-24.0, -10.0, target_lufs=-18.0, ceiling_dbtp=-1.0), 6.0)
        self.assertEqual(cat.fixed_gain_db(-12.0, -0.5, target_lufs=-18.0, ceiling_dbtp=-1.0), -6.0)

    def test_gain_never_pushes_peaks_past_the_ceiling(self):
        # Quiet overall but with a loud climax: no limiter, so it stays under target.
        self.assertEqual(cat.fixed_gain_db(-26.0, -2.0, target_lufs=-18.0, ceiling_dbtp=-1.0), 1.0)

    def test_silence_is_left_alone(self):
        self.assertEqual(cat.fixed_gain_db(float("-inf"), float("-inf")), 0.0)
        self.assertEqual(cat.fixed_gain_db(-70.0, -60.0), 0.0)


class AirabilityTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(":memory:")
        self.addCleanup(self.catalog.close)

    def add(self, files=None, **overrides):
        self._n = getattr(self, "_n", 0) + 1
        files = files or fake_files(10, prefix=f"/library/work{self._n}")
        return self.catalog.work(self.catalog.add_work(files=files, **classical(**overrides)))

    def reasons(self, work, today=TODAY):
        return self.catalog.airability(work, today)

    def test_cleared_classical_work_is_airable(self):
        self.assertEqual(self.reasons(self.add()), [])

    def test_composition_year_is_required_and_must_be_public_domain(self):
        self.assertIn("no composition/text publication year on file", self.reasons(self.add(composition_year=None)))
        rhapsody = self.add(title="Rhapsody on a Theme of Paganini", composition_year=1934)
        self.assertTrue(self.reasons(rhapsody))
        self.assertEqual(self.reasons(rhapsody, today=date(2031, 1, 1)), [])

    def test_1931_works_join_on_public_domain_day(self):
        work = self.add(composition_year=1931)
        self.assertTrue(self.reasons(work, today=date(2026, 12, 31)))
        self.assertEqual(self.reasons(work, today=date(2027, 1, 1)), [])

    def test_stacks_recordings_need_a_year_and_must_be_old_enough(self):
        stacks = dict(kind="stacks", source="citizen_dj", license="public_domain",
                      credit_line="Citizen DJ Project, Library of Congress, National Jukebox",
                      composition_year=1910, title="Get Out and Get Under", performer="Billy Murray")
        no_year = self.add(**stacks, year=None)
        self.assertIn("no recording publication year on file", self.reasons(no_year))
        from_1926 = self.add(**stacks, year=1926)
        self.assertTrue(self.reasons(from_1926))
        self.assertEqual(self.reasons(from_1926, today=date(2027, 1, 1)), [])
        self.assertEqual(self.reasons(self.add(**stacks, year=1913)), [])

    def test_permission_needs_a_written_grant_in_the_log(self):
        work = self.add(license="permission")
        self.assertTrue(self.reasons(work))
        self.catalog.log_permission(party="Example Podcast", item="Episode 12 clip", status="requested",
                                    work_id=work.id)
        self.assertTrue(self.reasons(work))
        self.catalog.log_permission(party="Example Podcast", item="Episode 12 clip", status="granted",
                                    replied_at="2026-11-20", reply="Yes, you may air it with credit.",
                                    work_id=work.id)
        self.assertEqual(self.reasons(work), [])

    def test_classical_must_be_lossless(self):
        work = self.add(files=fake_files(10, lossless=False))
        self.assertIn("not lossless end to end", self.reasons(work))

    def test_quarantine_takes_a_work_off_air(self):
        work = self.add()
        self.catalog.set_status(work.id, "quarantined", "claim on YouTube")
        self.assertIn("status is quarantined", self.reasons(self.catalog.work(work.id)))

    def test_add_work_validates_inputs(self):
        with self.assertRaises(CatalogError):
            self.catalog.add_work(files=fake_files(1), **classical(license="all_rights_reserved"))
        with self.assertRaises(CatalogError):
            self.catalog.add_work(files=fake_files(1), **classical(credit_line="  "))
        with self.assertRaises(CatalogError):
            self.catalog.add_work(files=[], **classical())

    def test_cleared_hours_count_only_airable_lossless_classical(self):
        self.add(files=fake_files(60, 30, prefix="/a"))
        self.add(files=fake_files(45, prefix="/b"), composition_year=None)
        self.add(files=fake_files(30, lossless=False, prefix="/c"))
        self.assertAlmostEqual(self.catalog.cleared_hours("classical", TODAY), 1.5)


class AirplayTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(":memory:")
        self.addCleanup(self.catalog.close)
        self.good = self.catalog.add_work(files=fake_files(10, prefix="/good"), **classical())
        self.bad = self.catalog.add_work(
            files=fake_files(10, prefix="/bad"), **classical(composition_year=None, title="Undated")
        )
        self.t0 = datetime(2026, 11, 18, 4, 0, tzinfo=timezone.utc)

    def test_audit_flags_unlicensed_and_unknown_files(self):
        self.catalog.record_airplay("/good_0.flac", aired_at=self.t0, show_id="nocturne")
        self.catalog.record_airplay("/bad_0.flac", aired_at=self.t0 + timedelta(minutes=10), show_id="nocturne")
        self.catalog.record_airplay("/mystery.flac", aired_at=self.t0 + timedelta(minutes=20), show_id="nocturne")

        problems = self.catalog.audit(today=TODAY)
        self.assertEqual([p["path"] for p in problems], ["/bad_0.flac", "/mystery.flac"])
        self.assertIn("composition", problems[0]["problem"])
        self.assertIn("no license record", problems[1]["problem"])

    def test_airplay_snapshots_license_and_credit(self):
        row_id = self.catalog.record_airplay("/good_0.flac", aired_at=self.t0)
        row = self.catalog.db.execute("SELECT * FROM airplay WHERE id = ?", (row_id,)).fetchone()
        self.assertEqual(row["license"], "cc0")
        self.assertEqual(row["credit_line"], "Kimiko Ishizaka (courtesy credit)")

    def test_naive_timestamps_are_refused(self):
        with self.assertRaises(CatalogError):
            self.catalog.record_airplay("/good_0.flac", aired_at=datetime(2026, 11, 18, 4, 0))

    def test_plays_merge_airplay_and_schedule_without_double_counting(self):
        self.catalog.schedule_work(self.good, self.t0, show_id="nocturne", plan_id="p1")
        self.catalog.record_airplay("/good_0.flac", aired_at=self.t0)  # the same airing
        self.catalog.schedule_work(self.good, self.t0 + timedelta(days=3), plan_id="p2")
        plays = self.catalog.plays_since(self.t0 - timedelta(days=1))
        self.assertEqual(len(plays), 2)
        self.assertEqual(plays[0].composer, "Johann Sebastian Bach")
        self.catalog.clear_plan("p2")
        self.assertEqual(len(self.catalog.plays_since(self.t0 - timedelta(days=1))), 1)

    def test_dispute_note_states_the_basis(self):
        note = self.catalog.dispute_note(self.good)
        self.assertIn("Creative Commons CC0", note)
        self.assertIn("published in 1741", note)
        self.assertIn("open_goldberg", note)
        self.assertIn("Kimiko Ishizaka (courtesy credit)", note)

    def test_cli_stats_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "catalog.db"
            with Catalog(db) as c:
                c.add_work(files=fake_files(120, prefix="/x"), **classical())
            self.assertEqual(cat.main(["--db", str(db), "stats"]), 0)


class PublicDomainCutoffTests(unittest.TestCase):
    def test_cutoffs_match_the_bible(self):
        self.assertEqual(cat.recording_pd_cutoff(date(2026, 11, 17)), 1925)
        self.assertEqual(cat.recording_pd_cutoff(date(2027, 1, 1)), 1926)
        self.assertEqual(cat.composition_pd_cutoff(date(2026, 11, 17)), 1930)
        self.assertEqual(cat.composition_pd_cutoff(date(2027, 1, 1)), 1931)


if __name__ == "__main__":
    unittest.main()
