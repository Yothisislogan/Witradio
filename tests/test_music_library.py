"""Pulling music into the library: downloads, conversion, grouping, intake."""

import hashlib
import http.server
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from datetime import date
from pathlib import Path

from mac import catalog as cat
from mac import music_library as ml
from mac.catalog import Catalog

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
TODAY = date(2026, 11, 18)


def make_flac(path: Path, seconds: float = 3.0, rate: int = 48000, fmt: str = "s32",
              tags: dict | None = None) -> Path:
    """Pink noise (full band, so it passes the spectrogram check) with tags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", f"anoisesrc=color=pink:amplitude=0.25:sample_rate={rate}:duration={seconds}",
            "-ac", "2", "-sample_fmt", fmt, "-c:a", "flac"]
    for key, value in (tags or {}).items():
        args += ["-metadata", f"{key}={value}"]
    subprocess.run(args + [str(path)], check=True)
    return path


class FakeArchive(http.server.ThreadingHTTPServer):
    """Serves /metadata/<item> and /download/<item>/<name>, with Range support."""

    def __init__(self, root: Path):
        super().__init__(("127.0.0.1", 0), FakeArchiveHandler)
        self.root = root
        self.items: dict[str, dict] = {}
        self.downloads: list[tuple[str, str | None]] = []

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def add_item(self, item: str, files: dict[str, tuple[Path, dict]]) -> None:
        entries = []
        for name, (path, meta) in files.items():
            data = path.read_bytes()
            entries.append({"name": name, "source": "original", "format": "Flac",
                            "size": str(len(data)), "md5": hashlib.md5(data).hexdigest(), **meta})
            target = self.root / item / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        entries.append({"name": f"{item}_meta.xml", "source": "original", "format": "Metadata"})
        self.items[item] = {"metadata": {"identifier": item}, "files": entries}


class FakeArchiveHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.unquote(self.path)
        if path.startswith("/metadata/"):
            body = json.dumps(self.server.items.get(path.split("/")[2], {})).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/download/"):
            _, _, item, name = path.split("/", 3)
            file = self.server.root / item / name
            data = file.read_bytes()
            rng = self.headers.get("Range")
            self.server.downloads.append((name, rng))
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            self.send_response(206 if rng else 200)
            self.send_header("Content-Length", str(len(data) - start))
            self.end_headers()
            self.wfile.write(data[start:])
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


def source(**overrides) -> ml.Source:
    base = dict(name="test-album", license="cc0", credit="{performer} (courtesy credit)",
                license_evidence="CC0 test", archive_item="test-album", grouping="album",
                composer="Johann Sebastian Bach", performer="Test Pianist", year=2020,
                tags=("keyboard",), work={"title": "Goldberg Variations, BWV 988",
                                          "form": "variations", "composition_year": 1741})
    base.update(overrides)
    return ml.Source(**base)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.server = FakeArchive(root / "served")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        src = root / "src"
        self.masters = {
            f"{n:02d} Variation {n}.flac": make_flac(src / f"v{n}.flac", tags={"title": f"Variation {n}", "track": n})
            for n in (1, 2, 3)
        }
        self.server.add_item("test-album", {
            name: (path, {"title": name[3:-5], "track": name[:2], "length": "3.0"})
            for name, path in self.masters.items()
        })
        self.library = root / "library"
        self.catalog = Catalog(":memory:")
        self.addCleanup(self.catalog.close)
        self.log: list[str] = []

    def fetch(self, src=None, **kwargs):
        return ml.fetch_source(self.catalog, src or source(), self.library, base=self.server.base,
                               log=self.log.append, **kwargs)

    def test_fetch_downloads_converts_and_catalogs(self):
        results = self.fetch()
        self.assertEqual(len(results), 1)
        work = self.catalog.work(results[0].work_id)
        self.assertEqual(work.title, "Goldberg Variations, BWV 988")
        self.assertEqual((work.composer, work.performer, work.form), ("Johann Sebastian Bach", "Test Pianist", "variations"))
        self.assertEqual((work.license, work.credit_line, work.composition_year),
                         ("cc0", "Test Pianist (courtesy credit)", 1741))
        self.assertEqual(work.status, "active")
        self.assertIsNotNone(work.gain_db)
        self.assertEqual([f.part_title for f in work.files], ["Variation 1", "Variation 2", "Variation 3"])
        for f in work.files:
            info = cat.probe(Path(f.path))
            self.assertEqual((info.codec, info.sample_rate, info.bit_depth, info.channels), ("flac", 44100, 16, 2))
            self.assertTrue(Path(f.master_path).is_file())
            self.assertIn("/masters/test-album/", f.master_path)
            self.assertIn("/air/test-album/", f.path)
        self.assertEqual(self.catalog.airability(work, TODAY), [])
        self.assertTrue((self.library / "reports" / "test-album.yaml").is_file())

    def test_rerun_downloads_and_adds_nothing(self):
        self.fetch()
        downloads = len(self.server.downloads)
        results = self.fetch()
        self.assertEqual(len(self.server.downloads), downloads)
        self.assertEqual(results[0].skipped, "already in the catalog")
        self.assertEqual(len(self.catalog.works(status=None)), 1)

    def test_dry_run_downloads_nothing(self):
        self.assertEqual(self.fetch(dry_run=True), [])
        self.assertEqual(self.server.downloads, [])
        self.assertTrue(any("1 works" in line for line in self.log))

    def test_partial_download_resumes(self):
        name = "01 Variation 1.flac"
        data = self.masters[name].read_bytes()
        part = self.library / "masters" / "test-album" / f"{name}.part"
        part.parent.mkdir(parents=True)
        part.write_bytes(data[: len(data) // 2])
        self.fetch()
        ranges = [r for n, r in self.server.downloads if n == name]
        self.assertEqual(ranges, [f"bytes={len(data) // 2}-"])
        self.assertEqual((self.library / "masters" / "test-album" / name).read_bytes(), data)

    def test_corrupt_partial_is_detected_and_downloaded_again(self):
        name = "02 Variation 2.flac"
        data = self.masters[name].read_bytes()
        part = self.library / "masters" / "test-album" / f"{name}.part"
        part.parent.mkdir(parents=True)
        part.write_bytes(b"\0" * len(data))  # right size, wrong bytes
        self.fetch()
        self.assertEqual((self.library / "masters" / "test-album" / name).read_bytes(), data)

    def test_verify_source_arrives_quarantined_until_approved(self):
        results = self.fetch(source(verify="Third-party upload; confirm the license."))
        work = self.catalog.work(results[0].work_id)
        self.assertEqual(work.status, "quarantined")
        self.assertIn("[verify-license]", work.notes)
        self.assertEqual(ml.approve(self.catalog, "test-album", ["verify-publication"]), 0)
        self.assertEqual(ml.approve(self.catalog, "test-album", ["verify-license"]), 1)
        self.assertEqual(self.catalog.work(work.id).status, "active")

    def test_sixteen_bit_44k_master_is_copied_bit_for_bit(self):
        cd = make_flac(Path(self.tmp.name) / "cd.flac", rate=44100, fmt="s16")
        self.server.add_item("cd-rip", {"01 Track.flac": (cd, {"title": "Track", "track": "1"})})
        results = self.fetch(source(name="cd-rip", archive_item="cd-rip"))
        air = Path(self.catalog.work(results[0].work_id).files[0].path)
        self.assertEqual(air.read_bytes(), cd.read_bytes())

    def test_fake_lossless_master_is_rejected(self):
        noise = make_flac(Path(self.tmp.name) / "n.flac", rate=44100, fmt="s16")
        lossy = Path(self.tmp.name) / "n.mp3"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(noise), "-b:a", "128k", str(lossy)], check=True)
        fake = Path(self.tmp.name) / "fake.flac"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(lossy), "-sample_fmt", "s16", str(fake)], check=True)
        self.server.add_item("fake", {"01 Fake.flac": (fake, {"title": "Fake", "track": "1"})})
        results = self.fetch(source(name="fake", archive_item="fake"))
        self.assertIsNone(results[0].work_id)
        self.assertTrue(results[0].rejected)
        self.assertEqual(self.catalog.works(status=None), [])

    def test_local_folder_import_reads_tags(self):
        folder = Path(self.tmp.name) / "musopen-downloads"
        make_flac(folder / "Nocturne in E flat major, Op. 9 no. 2.flac", rate=44100, fmt="s16",
                  tags={"title": "Nocturne Op. 9 no. 2", "artist": "Test Pianist"})
        make_flac(folder / "Waltz in A minor, B. 150.flac", rate=44100, fmt="s16",
                  tags={"title": "Waltz in A minor, B. 150", "artist": "Test Pianist"})
        src = ml.Source(name="musopen-site", license="public_domain", credit="{performer} via Musopen",
                        grouping="movements", title_from="filename", composer="Frédéric Chopin")
        works = ml.group_tracks(ml.tracks_from_folder(folder), src)
        results = ml.ingest_all(self.catalog, src, works, self.library, jobs=1, spectral=True, log=self.log.append)
        by_title = {self.catalog.work(r.work_id).title: self.catalog.work(r.work_id) for r in results}
        nocturne = by_title["Nocturne in E flat major, Op. 9 no. 2"]
        self.assertEqual((nocturne.status, nocturne.credit_line, nocturne.form, nocturne.composition_year),
                         ("active", "Test Pianist via Musopen", "nocturne", 1849))
        waltz = by_title["Waltz in A minor, B. 150"]
        self.assertEqual(waltz.status, "quarantined")  # published 1955: needs a check
        self.assertIn("[verify-publication]", waltz.notes)

    def test_safe_library_command_from_the_runbook(self):
        # docs/live-setup.md 6.5: the generated safe library goes in through the
        # catalog and lands in library/air/safe/, where the director and
        # Liquidsoap look for it (StationConfig.safe_dir).
        root = Path(self.tmp.name)
        folder = root / "safe-tracks"
        make_flac(folder / "Night Drive.flac", seconds=4, rate=44100, fmt="s16",
                  tags={"title": "Night Drive", "artist": "WIT-FM"})
        db = root / "catalog.db"
        code = ml.main(["--db", str(db), "--library", str(self.library), "local", str(folder),
                        "--source", "safe", "--kind", "generated", "--license", "generated",
                        "--credit", "Generated for WIT-FM", "--jobs", "1"])
        self.assertEqual(code, 0)
        with Catalog(db) as catalog:
            (work,) = catalog.works(status=None)
            self.assertEqual((work.kind, work.status, work.license, work.credit_line),
                             ("generated", "active", "generated", "Generated for WIT-FM"))
            self.assertTrue(catalog.is_airable(work))
            self.assertIsNotNone(work.gain_db)
            air = Path(work.files[0].path)
        self.assertTrue(air.is_relative_to(self.library / "air" / "safe"))
        from mac.station_config import load_station_config

        station = load_station_config("wit-next")
        self.assertEqual(station.safe_dir, station.library_dir / "air" / "safe")
        self.assertEqual(air.suffix, ".flac")
        self.assertEqual(list(self.library.rglob("*.part")), [])


class GroupingTests(unittest.TestCase):
    def test_folder_grouping_reads_musopen_title_tags(self):
        src = ml.Source(name="mk", license="public_domain", credit="{performer} via Musopen", grouping="folder")
        tracks = [
            ml.Track(name=f"Brahms_SymphonyNo.1/{n}.flac", number=n, artist="Czech National Symphony Orchestra",
                     title=f"Johannes Brahms - Symphony No. 1 in C Minor, Op. 68 - {n:02d} - {mvt}", length=600)
            for n, mvt in ((1, "Un poco sostenuto - Allegro"), (2, "Andante sostenuto"))
        ] + [
            ml.Track(name="Borodin_StringQuartetNo.1/1.flac", number=44, length=500,
                     artist="Czech National Symphony Orchestra",
                     title="Alexander Borodin - String Quartet No. 1 in A Major - 01 - Moderato - Allegro"),
            ml.Track(name="Smetana_Vltava/1.flac", number=140, artist="Czech National Symphony Orchestra",
                     title="Bedřich Smetana - Má vlast - Vltava", length=700),
            ml.Track(name="Bach_Goldberg/1.flac", number=1, artist="Shelley Katz", length=290,
                     title="Johann Sebastian Bach - 01 - Goldberg Variations, BWV. 988 - Aria"),
        ]
        works = {w.title: w for w in ml.group_tracks(tracks, src)}
        brahms = works["Symphony No. 1 in C Minor, Op. 68"]
        self.assertEqual((brahms.composer, brahms.form, brahms.composition_year), ("Johannes Brahms", "symphony", 1897))
        self.assertEqual(brahms.parts, ["Un poco sostenuto - Allegro", "Andante sostenuto"])
        self.assertEqual(brahms.credit, "Czech National Symphony Orchestra via Musopen")
        self.assertIn("Má vlast: Vltava", works)
        self.assertIn("Goldberg Variations, BWV 988", works)
        quartet = works["String Quartet No. 1 in A Major"]
        self.assertTrue(any("check the credit" in note for note in quartet.review))

    def test_catalog_number_grouping_pairs_preludes_and_fugues(self):
        src = ml.Source(name="wtc", license="cc0", credit="x", grouping="catalog_number",
                        composer="Johann Sebastian Bach",
                        work={"title_template": "Prelude and Fugue No. {number} in {key}, BWV {catalog}",
                              "composition_year": 1722})
        titles = ["Prelude No. 1 in C major, BWV 846", "Fugue No. 1 in C major, BWV 846",
                  "Prelude No. 3 in C-sharp major, BWV 848", "Fugue No. 3 in C-sharp major, BWV 848"]
        tracks = [ml.Track(name=f"{i:02d}.flac", number=i, title=t) for i, t in enumerate(titles, 1)]
        works = ml.group_tracks(tracks, src)
        self.assertEqual([w.title for w in works], [
            "Prelude and Fugue No. 1 in C major, BWV 846",
            "Prelude and Fugue No. 3 in C-sharp major, BWV 848",
        ])
        self.assertEqual([len(w.tracks) for w in works], [2, 2])

    def test_movements_grouping_orders_roman_numerals(self):
        src = ml.Source(name="c", license="cc0", credit="x", grouping="movements", title_from="filename",
                        composer="Frédéric Chopin")
        names = ["Cello Sonata in G Minor, Op. 65 - III. Largo.flac", "Cello Sonata in G Minor, Op. 65 - I. Allegro.flac",
                 "Cello Sonata in G Minor, Op. 65 - II. Scherzo.flac", "Etude Op. 10, no. 12 in C minor - 'Revolutionary'.flac"]
        works = {w.title: w for w in ml.group_tracks([ml.Track(name=n) for n in names], src)}
        sonata = works["Cello Sonata in G Minor, Op. 65"]
        self.assertEqual(sonata.parts, ["I. Allegro", "II. Scherzo", "III. Largo"])
        self.assertIn("cello", sonata.tags)
        self.assertIn("Etude Op. 10, no. 12 in C minor - 'Revolutionary'", works)

    def test_composer_lookup_and_public_domain_years(self):
        self.assertEqual(ml.lookup_composer("Antonín Dvořák"), ("Antonín Dvořák", 1904))
        self.assertEqual(ml.lookup_composer("Peter Tchaikovsky")[1], 1893)
        self.assertIsNone(ml.lookup_composer("Schumann"))  # Robert or Clara?
        self.assertEqual(ml.lookup_composer("Clara Schumann")[1], 1896)
        src = ml.Source(name="s", license="public_domain", credit="x", composer="Josef Suk")
        suk = ml.describe(src, "Meditation", [ml.Track(name="m.flac")], [None], today=TODAY)
        self.assertIsNone(suk.composition_year)  # died 1935: someone checks the publication year
        self.assertTrue(any("1935" in note for note in suk.review))

    def test_posthumous_detection(self):
        self.assertTrue(ml.maybe_posthumous("Waltz in A minor, B. 150"))
        self.assertTrue(ml.maybe_posthumous("Miscellaneous no.12 - B. 151 posth. Albumblatt"))
        self.assertTrue(ml.maybe_posthumous("Bagatelle, WoO 59"))
        self.assertFalse(ml.maybe_posthumous("Ballade No. 2 in F Major, Op. 38"))
        self.assertFalse(ml.maybe_posthumous("Prelude No. 2 in C minor, BWV 847"))
        self.assertFalse(ml.maybe_posthumous("Sonata in B flat minor"))

    def test_sources_file_is_valid(self):
        sources = ml.load_sources()
        self.assertIn("open-goldberg", sources)
        self.assertTrue(sources["ishizaka-art-of-fugue"].verify)
        for s in sources.values():
            self.assertIn(s.license, ("cc0", "public_domain"))
            self.assertTrue(s.archive_item)
            self.assertTrue(s.license_evidence)


if __name__ == "__main__":
    unittest.main()
