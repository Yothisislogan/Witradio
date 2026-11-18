"""Library of Congress intake for The Stacks, against a fake loc.gov."""

import http.server
import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

from mac import loc_library as ll
from mac.catalog import Catalog

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
TODAY = date(2026, 11, 18)  # recordings published in or before 1925 are public domain
SCREEN = ll.load_config()[1]


def tone(path: Path, seconds: float = 1.0, rate: int = 44100, codec: list[str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:sample_rate={rate}:duration={seconds}", "-ac", "2",
                    *(codec or []), str(path)], check=True)
    return path


def jukebox(item_id, title, recorded, *, label="Victor", catalog="17156", performer="Dunlap, Marguerite",
            composer="Clarke, Robert Coningsby", genres=("popular music",), file_id=None, wav=None, size=None):
    """A National Jukebox item, as a search result and as an item record."""
    people = [f"{performer} -- Vocalist -- Contralto", f"{composer} -- Composer -- Composer"]
    nested = {"contributors": people, "recording_date": recorded, "file_id": [file_id or f"dlc_victor_{catalog}_01_b1_01"],
              "summary": "Female vocal solo, with orchestra", "genre": [g.title() for g in genres]}
    result = {"id": f"http://www.loc.gov/item/{item_id}/", "url": f"https://www.loc.gov/item/{item_id}/",
              "title": title, "date": recorded, "subject_genre": list(genres),
              "description": [f"{label} {catalog}. Matrix/Take: B-1/1." if label else "Matrix/Take: B-1/1."],
              "item": nested}
    record = {**result, "contributor_names": people, "rights_advisory": "Courtesy of Sony Music Entertainment"}
    if label:
        record.update(recording_label=label, recording_catalog_number=catalog)
    files = [[
        {"mimetype": "audio/mpeg", "download": f"/streaming-services/{item_id}.mp3", "canDownload": True,
         "derivatives": [{"derivativeUrl": f"BASE/storage-services/service/{item_id}.mp3"}]},
        {"mimetype": "audio/wav", "url": wav or f"BASE/storage-services/master/{item_id}.wav", "size": size},
    ]]
    doc = {"item": record, "resources": [{"url": f"https://www.loc.gov/resource/{item_id}.0", "files": files}]}
    return result, doc


class FakeLoc(http.server.ThreadingHTTPServer):
    def __init__(self, root: Path):
        super().__init__(("127.0.0.1", 0), FakeLocHandler)
        self.root = root  # served files: <root>/<path after the host>
        self.collections: dict[str, list[dict]] = {}
        self.items: dict[str, dict] = {}
        self.cdj: dict[str, dict] = {}
        self.requests: list[str] = []
        self.refuse: dict[str, str] = {}  # path prefix -> "429" or "html"

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def add_item(self, collection: str, result: dict, doc: dict) -> None:
        item_id = result["url"].rstrip("/").rsplit("/", 1)[-1]
        self.collections.setdefault(collection, []).append(result)
        text = json.dumps(doc).replace("BASE/", self.base + "/")
        self.items[item_id] = json.loads(text)


class FakeLocHandler(http.server.BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, kind: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server: FakeLoc = self.server
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        server.requests.append(url.path)
        for prefix, how in server.refuse.items():
            if url.path.startswith(prefix):
                if how == "429":
                    return self._send(429, b"slow down", "text/plain")
                return self._send(200, b"<html>are you a robot?</html>", "text/html")
        if url.path.startswith("/collections/"):
            results = server.collections.get(url.path.split("/")[2], [])
            size, page = int(query["c"][0]), int(query.get("sp", ["1"])[0])
            chunk = results[(page - 1) * size: page * size]
            more = page * size < len(results)
            body = {"results": chunk, "pagination": {"current": page, "of": len(results),
                                                       "next": f"{server.base}{url.path}?sp={page + 1}" if more else None}}
            return self._send(200, json.dumps(body).encode(), "application/json")
        if url.path.startswith("/item/"):
            doc = server.items.get(url.path.split("/")[2])
            return self._send(200 if doc else 404, json.dumps(doc or {}).encode(), "application/json")
        if url.path.startswith("/cdj-data/"):
            return self._send(200, json.dumps(server.cdj[Path(url.path).stem]).encode(), "text/plain")
        if "/streaming-services/" in url.path:
            return self._send(403, b"streams are not for downloading", "text/plain")
        path = server.root / urllib.parse.unquote(url.path).lstrip("/")
        if not path.is_file():
            return self._send(404, b"", "text/plain")
        data = path.read_bytes()
        start = 0
        if self.headers.get("Range", "").startswith("bytes="):
            start = int(self.headers["Range"][6:].split("-")[0])
            return self._send(206, data[start:], "audio/wav",
                              {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
        return self._send(200, data, "audio/wav")

    def log_message(self, *args):
        pass


class ParsingTests(unittest.TestCase):
    def test_jukebox_record_credit_and_year(self):
        result, doc = jukebox("jukebox-131863", "A bowl of roses", "1912-05-27")
        rec = ll.parse_recording(doc["item"])
        decision = ll.judge(rec, SCREEN, TODAY)
        self.assertEqual((decision.action, decision.year, decision.quarantine), ("take", 1912, ["verify-content"]))
        self.assertEqual(rec.credit(decision.year, "Library of Congress, National Jukebox"),
                         "Marguerite Dunlap (Victor 17156, 1912). Library of Congress, National Jukebox")
        self.assertEqual(rec.composers, ["Robert Coningsby Clarke"])

    def test_issue_number_from_the_transfer_disc_when_the_fields_are_empty(self):
        result, _ = jukebox("jukebox-272693", "Happy students polka", "1923-07-10", label="",
                            file_id="jvm_victor_73978_01_b28269_01")
        self.assertEqual(ll.parse_recording(result).issue, "Victor 73978")
        result, _ = jukebox("jukebox-1", "La chiapaneca", "1926-04-30", label="", file_id="ucsb_col_2380x_01_95075_01")
        self.assertEqual(ll.parse_recording(result).issue, "Columbia 2380-X")

    def test_edison_imprint_gives_the_publication_year(self):
        record = {"url": "https://www.loc.gov/item/00694028/", "title": "Hungarian rag", "date": "1914",
                  "created_published": ["Orange, N.J. : Edison, 1914."], "call_number": ["Edison Diamond Disc 50123-L"],
                  "shelf_id": "https://repo.loc.gov/access/public/navcc/edrs-50123l/.resource.json",
                  "contributor_names": ["Lenzberg, Julius", "New York Military Band. prf"]}
        rec = ll.parse_recording(record)
        self.assertEqual((rec.published_year, rec.issue, rec.performers, rec.composers),
                         (1914, "Edison Diamond Disc 50123-L", ["New York Military Band"], ["Julius Lenzberg"]))
        self.assertEqual(ll.judge(rec, SCREEN, TODAY).year, 1914)

    def test_names(self):
        cases = {
            "Janpolski, Albert G.": "Albert G. Janpolski",
            "Suppé, Franz von, 1819-1895": "Franz von Suppé",
            "Orchestrina Napoletana [i.e., Cibelli's Neapolitan Orchestra]": "Cibelli's Neapolitan Orchestra",
            "New York Military Band. prf": "New York Military Band",
            "Fields, Arthur, performer.": "Arthur Fields",
        }
        for raw, expected in cases.items():
            self.assertEqual(ll.parse_person(raw)[0], expected)
        performers, conductors, _, _ = ll.sort_people(
            ["Bye, Erik Ole -- Vocalist -- Baritone", "Bourdon, Rosario -- Conductor -- Conductor"], set())
        self.assertEqual((performers, conductors), (["Erik Ole Bye"], ["Rosario Bourdon"]))


class GateTests(unittest.TestCase):
    def judge(self, recorded="1912-05-27", **kwargs):
        include = kwargs.pop("include", False)
        result, _ = jukebox("jukebox-1", kwargs.pop("title", "A bowl of roses"), recorded, **kwargs)
        return ll.judge(ll.parse_recording(result), SCREEN, TODAY, include=include)

    def test_publication_year_is_the_gate(self):
        self.assertEqual(self.judge("1924-11-30").year, 1924)
        cutoff_year = self.judge("1925-03-02")
        self.assertIsNone(cutoff_year.year)  # issued 1925 or 1926? someone checks
        self.assertIn("verify-publication", cutoff_year.quarantine)
        self.assertEqual(self.judge("1926-01-04").action, "skip")
        self.assertEqual(self.judge("").action, "skip")  # no year, no airplay

    def test_no_issue_number_means_no_year_yet(self):
        decision = self.judge(label="", file_id="nothing-to-parse")
        self.assertIsNone(decision.year)
        self.assertIn("verify-publication", decision.quarantine)

    def test_content_screen(self):
        self.assertEqual(self.judge(genres=("ethnic characterizations",)).action, "exclude")
        self.assertEqual(self.judge(title="Minstrel memories").action, "exclude")
        flagged = self.judge(genres=("humorous songs",))
        self.assertEqual((flagged.action, flagged.flags), ("take", ["genre: humorous songs"]))
        self.assertEqual(self.judge(title="The raccoon rag").flags, [])  # whole words only
        self.assertEqual(self.judge(title="Japan, my home").flags, [])
        forced = self.judge(genres=("ethnic characterizations",), include=True)
        self.assertEqual((forced.action, forced.quarantine[0]), ("take", "verify-content"))

    def test_sources_file(self):
        sources, screen = ll.load_config()
        self.assertEqual(len(sources["cdj-jukebox"].collections), 9)
        self.assertTrue(sources["loc-jukebox"].opt_in)
        self.assertFalse(sources["loc-edison"].opt_in)
        self.assertIn("ethnic characterizations", screen.exclude_genres)


class PacingTests(unittest.TestCase):
    def test_api_calls_are_spaced_and_cache_hits_are_free(self):
        now = [100.0]
        waits = []

        def sleep(seconds):
            waits.append(round(seconds, 2))
            now[0] += seconds

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = FakeLoc(Path(tmp.name))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        _, doc = jukebox("jukebox-1", "One", "1912")
        server.items["jukebox-1"] = doc
        client = ll.LocClient(Path(tmp.name) / "cache", api_base=server.base, sleep=sleep, clock=lambda: now[0])
        client.get_json(f"{server.base}/item/jukebox-1/?fo=json")
        now[0] += 1.0
        client.get_json(f"{server.base}/item/jukebox-1/?fo=json")
        client.item("jukebox-1")
        client.item("jukebox-1")  # cached: no request, no wait
        self.assertEqual(waits, [2.5, 3.5])
        self.assertEqual(client.api_calls, 3)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class FetchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.server = FakeLoc(self.root / "served")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.library = self.root / "library"
        self.catalog = Catalog(":memory:")
        self.addCleanup(self.catalog.close)
        self.client = ll.LocClient(self.library / "loc-cache", api_base=self.server.base,
                                   cdj_data_base=f"{self.server.base}/cdj-data",
                                   cdj_asset_base=f"{self.server.base}/cdj-assets", api_interval=0, media_interval=0)
        self.source = ll.LocSource(name="loc-jukebox", type="loc", collection="national-jukebox",
                                   credit="Library of Congress, National Jukebox")
        self.log: list[str] = []
        for item_id, title, recorded, genres in (
            ("jukebox-1", "A bowl of roses", "1912-05-27", ("popular music",)),
            ("jukebox-2", "A caricature record", "1911-01-01", ("ethnic characterizations",)),
            ("jukebox-3", "La chiapaneca", "1926-04-30", ("popular music",)),
            ("jukebox-4", "Sweet Georgia", "1925-03-02", ("popular music",)),
        ):
            wav = tone(self.root / "served" / "storage-services" / "master" / f"{item_id}.wav")
            result, doc = jukebox(item_id, title, recorded, genres=genres, size=wav.stat().st_size)
            self.server.add_item("national-jukebox", result, doc)

    def fetch(self, **kwargs):
        return ll.fetch_loc(self.client, self.catalog, self.source, self.library, SCREEN, today=TODAY,
                            log=self.log.append, **kwargs)

    def test_fetch_screens_downloads_masters_and_quarantines(self):
        outcomes = self.fetch()
        self.assertEqual({o.key: o.action for o in outcomes},
                         {"jukebox-1": "added", "jukebox-2": "excluded", "jukebox-3": "skipped", "jukebox-4": "added"})
        works = {w.title: w for w in self.catalog.works(status=None)}
        bowl, georgia = works["A bowl of roses"], works["Sweet Georgia"]
        self.assertEqual((bowl.kind, bowl.status, bowl.year, bowl.composition_year), ("stacks", "quarantined", 1912, 1912))
        self.assertEqual(bowl.credit_line, "Marguerite Dunlap (Victor 17156, 1912). Library of Congress, National Jukebox")
        self.assertIn("public domain since January 1, 2022", bowl.license_evidence)
        self.assertEqual(bowl.source_url, "https://www.loc.gov/item/jukebox-1/")
        self.assertIn("[verify-content]", bowl.notes)
        self.assertTrue(bowl.files[0].path.endswith(".flac"))
        self.assertIsNone(georgia.year)
        self.assertIn("[verify-publication]", georgia.notes)
        # Screened at the listing: no record or audio requests for the excluded and skipped items,
        # and never a streaming URL.
        self.assertNotIn("/item/jukebox-2/", self.server.requests)
        self.assertNotIn("/item/jukebox-3/", self.server.requests)
        self.assertFalse(any("streaming" in p or p.endswith("jukebox-2.wav") for p in self.server.requests))
        self.assertIn("/storage-services/master/jukebox-1.wav", self.server.requests)

    def test_rerun_skips_what_the_ledger_has_seen(self):
        self.fetch()
        before = len(self.server.requests)
        self.assertEqual(self.fetch(), [])
        self.assertEqual(self.server.requests[before:], ["/collections/national-jukebox/"])

    def test_dry_run_changes_nothing(self):
        outcomes = self.fetch(dry_run=True)
        self.assertEqual([o.action for o in outcomes], ["planned", "excluded", "skipped", "planned"])
        self.assertEqual(self.catalog.works(status=None), [])
        self.assertFalse(any(p.startswith("/item/") for p in self.server.requests))
        self.assertFalse((self.library / "loc-cache" / "ledger").exists())

    def test_limit_counts_items_brought_in(self):
        self.assertEqual([o.key for o in self.fetch(limit=1) if o.action == "added"], ["jukebox-1"])

    def test_rate_limit_stops_the_run_and_keeps_the_item_open(self):
        self.server.refuse["/item/"] = "429"
        with self.assertRaises(ll.RateLimited):
            self.fetch()
        ledger = ll.Ledger(self.library / "loc-cache" / "ledger" / "loc-jukebox.json")
        self.assertFalse(ledger.done("jukebox-1"))
        self.server.refuse = {"/collections/": "html"}  # a CAPTCHA page instead of JSON
        with self.assertRaises(ll.RateLimited):
            self.fetch()

    def test_partial_download_resumes(self):
        master = self.root / "served" / "storage-services" / "master" / "jukebox-1.wav"
        dest = self.root / "copy.wav"
        dest.with_name("copy.wav.part").write_bytes(master.read_bytes()[:1000])
        self.client.fetch_file(f"{self.server.base}/storage-services/master/jukebox-1.wav", dest,
                               size=master.stat().st_size)
        self.assertEqual(dest.read_bytes(), master.read_bytes())

    def test_citizen_dj_clips_become_remix_material(self):
        uid = "loc-jukebox-popular"
        clips = [{"filename": "A-bowl-of-roses_jukebox-1_001_00-01-20", "timeStamp": "01:20"},
                 {"filename": "A-bowl-of-roses_jukebox-1_002_00-02-00", "timeStamp": "02:00"}]
        for clip in clips:
            tone(self.root / "served" / "cdj-assets" / "audio" / "samplepacks" / uid / f"{clip['filename']}.wav", 0.5, 48000)
        self.server.cdj[uid] = {"items": [{"id": "jukebox-1", "title": "A bowl of roses", "clips": clips},
                                          {"id": "jukebox-2", "title": "A caricature record", "clips": []}]}
        source = ll.LocSource(name="cdj-jukebox", type="citizen_dj", collections=(uid,),
                              credit="Citizen DJ Project, Library of Congress, National Jukebox")
        outcomes = ll.fetch_citizen_dj(self.client, self.catalog, source, self.library, SCREEN, today=TODAY,
                                       log=self.log.append)
        self.assertEqual([o.action for o in outcomes], ["added", "excluded"])
        works = self.catalog.works(status=None)
        self.assertEqual([w.title for w in works], ["A bowl of roses (clip 1, at 01:20)", "A bowl of roses (clip 2, at 02:00)"])
        self.assertTrue(all("remix-clip" in w.tags for w in works))
        self.assertEqual(works[0].credit_line,
                         "Marguerite Dunlap (Victor 17156, 1912). Citizen DJ Project, Library of Congress, National Jukebox")
        with self.assertRaises(ll.LocError):
            ll.fetch_citizen_dj(self.client, self.catalog, source, self.library, SCREEN,
                                collections=["loc-jukebox-unknown"], today=TODAY, log=self.log.append)

    def test_review_approve_and_reject(self):
        self.fetch()
        works = {w.title: w.id for w in self.catalog.works(status=None)}
        bowl, georgia = works["A bowl of roses"], works["Sweet Georgia"]
        self.assertEqual({w.id for w in ll.review_queue(self.catalog, ["loc-jukebox"])}, {bowl, georgia})

        message, = ll.approve(self.catalog, [georgia], today=TODAY)
        self.assertIn("also needs a check of: publication", message)
        message, = ll.approve(self.catalog, [georgia], ["publication"], today=TODAY)
        self.assertIn("set the publication year first", message)
        self.catalog.update_work(georgia, year=1925)  # DAHR says it was issued in 1925
        message, = ll.approve(self.catalog, [georgia], ["publication"], by="Logan", today=TODAY)
        self.assertIn("approved", message)
        self.assertNotIn("not airable", message)
        self.assertEqual(self.catalog.work(georgia).status, "active")
        self.assertIn("approved after listening (content, publication) on 2026-11-18 by Logan",
                      self.catalog.work(georgia).notes)

        ll.reject(self.catalog, [bowl], "not for this show", today=TODAY)
        rejected = self.catalog.work(bowl)
        self.assertEqual(rejected.status, "rejected")
        self.assertIn("[verify-content]", rejected.notes)  # the earlier notes are kept
        self.assertIn("rejected in the content screen on 2026-11-18: not for this show", rejected.notes)


class CommandLineTests(unittest.TestCase):
    def test_opt_in_source_refuses_without_the_flag(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = io.StringIO()
        with redirect_stdout(out):
            code = ll.main(["--db", str(Path(tmp.name) / "c.db"), "--library", tmp.name, "fetch", "loc-jukebox"])
        self.assertEqual(code, 2)
        self.assertIn("opt-in", out.getvalue())


if __name__ == "__main__":
    unittest.main()
