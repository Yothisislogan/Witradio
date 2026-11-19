"""Show producers end to end, with a fake LLM, a fake voice engine and a fake music engine."""

import http.server
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Import the modules the way they import each other (flat, from mac/), so an
# exception raised in one is the class another one catches.
sys.path.insert(0, str(PROJECT_ROOT / "mac"))
import audio_tools as at  # noqa: E402
import buffer_sync  # noqa: E402
import produce_monster_mix  # noqa: E402
from feeds import FactSheet, Source  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from produce_classical import ClassicalLinks  # noqa: E402
from produce_market import DISCLAIMER, MarketProducer  # noqa: E402
from produce_ying_yang import YingYangProducer  # noqa: E402
from producer_daemon import Daemon  # noqa: E402
from producers import Context, upcoming_airings  # noqa: E402
from schedule import load_schedule  # noqa: E402
from script_writer import ScriptWriter  # noqa: E402
from voices import Renderer  # noqa: E402
HAVE_TOOLS = shutil.which("ffmpeg") is not None and shutil.which("rsync") is not None


class FakeLLM(http.server.ThreadingHTTPServer):
    """Writes scripts by quoting the FACTS it was given, so they pass the fact check."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeLLMHandler)
        self.invent = False
        self.calls = 0

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class FakeLLMHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls += 1
        messages = body["messages"]
        user = messages[1]["content"] if len(messages) > 1 else messages[0]["content"]
        if user.startswith("Pick the three"):
            reply = json.dumps({"topics": [{"title": f"t{n}", "facts": [n]} for n in (1, 2, 3)]})
        else:
            reply = json.dumps(self.script(messages[0]["content"], user))
        data = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def script(self, system: str, user: str) -> dict:
        speakers = re.search(r"Speakers: ([a-z_, ]+)\.", system).group(1).split(", ")
        target = int(re.search(r"about (\d+) words", user).group(1))
        facts = user.split("FACTS (the only source of facts):\n", 1)[1]
        quoted = " ".join(line.lstrip("- ").strip() for line in facts.splitlines() if line.strip())
        segment = re.search(r"SEGMENT: (\S+)", user).group(1)
        if segment.endswith("/sign_off"):
            text = f"That's the market. It'll be here tomorrow. So will I. {DISCLAIMER}"
            return {"title": "Sign-off", "lines": [{"speaker": speakers[0], "text": text}]}
        words = quoted.split() or ["Hello."]
        body = " ".join((words * (target // len(words) + 1))[:target])
        if self.server.invent:
            body = "The figure was 987,654 points. " + body
        half = len(body.split()) // 2
        lines = [{"speaker": speakers[0], "text": " ".join(body.split()[:half]) or body},
                 {"speaker": speakers[-1], "text": " ".join(body.split()[half:]) or body}]
        reply = {"title": segment, "lines": lines}
        if '"prediction":' in system:
            game = re.search(r"- (.+?) at (.+?), \d", facts)
            reply["prediction"] = {"host": "ying", "claim": f"The {game.group(2)} win.", "team": game.group(2),
                                   "opponent": game.group(1)}
        return reply

    def log_message(self, *args):
        pass


def voices_config(root: Path) -> Path:
    tone = root / "tone.py"
    tone.write_text("import subprocess, sys\nsubprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', "
                    "'sine=frequency=330:duration=0.4', '-ac', '2', sys.argv[1]], check=True)\n")
    cards = "\n".join(f"  {host}: {{engine: tone, voice: {host}, words_per_second: 2.5}}"
                      for host in ("count", "dottie", "ying", "yang", "wit", "steve"))
    path = root / "voices.yaml"
    path.write_text(f"engines:\n  tone: {{transport: command, style: plain, command: \"{sys.executable} {tone} {{out}}\"}}\n"
                    f"voices:\n{cards}\n")
    return path


def sheet(name: str, sections: dict, sources: dict, data: dict | None = None) -> FactSheet:
    fs = FactSheet(name, "2026-11-18T20:00:00+00:00", data=data or {})
    for heading, (source, lines) in sections.items():
        for line in lines:
            fs.add(heading, line, sources[source])
    return fs


MARKET_SOURCES = {"fred": Source("fred", True, "Federal Reserve Bank of St. Louis"),
                  "third": Source("fred_third_party", False, "S&P Dow Jones Indices via FRED"),
                  "treasury": Source("treasury", True, "U.S. Treasury"),
                  "fed": Source("fed_press", True, "Federal Reserve Board")}


def market_sheet(fmt, day):
    return sheet(f"markets/{fmt}", {
        "THE TAPE (latest closes available)": ("third", ["S&P 500 closed at 6,712.34, down 1.23%."]),
        "TREASURY YIELDS": ("treasury", ["Treasury yields: 2-year 4.80%, 10-year 5.07%."]),
        "COMMODITIES": ("fred", ["WTI crude oil: $72.15 a barrel."]),
        "RATES": ("fred", ["Unemployment rate: 4.30% for October 2026."]),
        "THE ECONOMY": ("fred", ["Consumer price index: up 3.1% from a year earlier, for October 2026."]),
        "CALENDAR (scheduled data releases)": ("fred", ["Thursday November 19, 2026: Housing Starts."]),
        "FEDERAL RESERVE BOARD (press releases)": ("fed", ["Wednesday November 18, 2026: Minutes of the FOMC."]),
    }, MARKET_SOURCES)


def sports_sheet(day, final=False):
    espn = Source("espn", False, "ESPN")
    game = {"state": "post" if final else "pre", "start": "2026-11-19T00:00Z",
            "home": {"name": "Cleveland Cavaliers", "score": 101 if final else None},
            "away": {"name": "Memphis Grizzlies", "score": 99 if final else None}}
    return sheet("sports", {
        "RESULTS, NBA": ("espn", ["Tuesday November 17: New York Knicks 111, Toronto Raptors 102 (final)."]),
        "HEADLINES, NBA (ESPN reporting; restate in your own words)": ("espn", [
            "Knicks extend streak over Raptors. New York has won 15 straight.",
            "Lakers rally past Hornets in overtime."]),
        "COMING UP TODAY, NBA": ("espn", ["Memphis Grizzlies at Cleveland Cavaliers, 7:00 PM Eastern, Rocket Arena."]),
    }, {"espn": espn}, {"games": [game]})


@unittest.skipUnless(HAVE_TOOLS, "ffmpeg and rsync are required")
class ProducerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.llm = FakeLLM()
        threading.Thread(target=self.llm.serve_forever, daemon=True).start()
        self.addCleanup(self.llm.server_close)
        self.addCleanup(self.llm.shutdown)
        renderer = Renderer.from_config(voices_config(self.root), self.root / "none.yaml")
        self.music_calls = []
        self.ctx = Context(
            schedule=load_schedule(PROJECT_ROOT / "config" / "wit_next_schedule.yaml"), renderer=renderer,
            writer=ScriptWriter.for_renderer(LLMClient(base_url=self.llm.base, retries=0), renderer, attempts=2),
            buffer_dir=self.root / "buffer", rehearsal_dir=self.root / "rehearsal", work_dir=self.root / "work",
            state_dir=self.root / "state", log=lambda m: None,
            services={"market_facts": market_sheet, "sports_facts": sports_sheet, "music": self.fake_music})
        env = patch.dict(os.environ, {"WRIT_MONSTER_MIX_TRACKS_PER_HOUR": "2"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WRIT_REHEARSE", None)

    def fake_music(self, caption, out, seconds):
        self.music_calls.append(caption)
        at.tone(Path(out), 0.6, 220)
        return True

    def airing(self, show_id, start):
        show = self.ctx.schedule.shows[show_id]
        start = datetime.fromisoformat(start)
        for airing in upcoming_airings(self.ctx.schedule, start, 1):
            if airing.show_id == show_id and airing.start == start:
                return airing
        raise AssertionError(f"{show_id} doesn't start at {start}")

    def test_the_counts_links_from_a_plan(self):
        work = {"type": "work", "role": "first_set", "seconds": 600, "work_id": 1, "title": "Nocturne in E flat major, Op. 9 no. 2",
                "composer": "Frédéric Chopin", "performer": "Test Pianist", "credit": "Test Pianist via Musopen"}
        plan = {"plan_id": "nocturne@2026-11-19T0400Z", "show_id": "nocturne", "block": "nocturne", "title": "Nocturne",
                "theme": "Keyboard Night", "starts_at": "2026-11-18T23:00:00-05:00", "ends_at": "2026-11-19T05:00:00-05:00",
                "entries": [{"type": "talk", "role": "recollection", "seconds": 30, "about": [1]},
                            {"type": "gap", "role": "gap", "seconds": 3}, work,
                            {"type": "talk", "role": "sign_off", "seconds": 20}]}
        links = ClassicalLinks(self.ctx)
        result = links.produce_plan(plan)
        talk = self.ctx.buffer_dir / "talk" / plan["plan_id"]
        self.assertEqual(sorted(p.name for p in talk.glob("*.flac")), ["000.flac", "003.flac"])
        self.assertEqual(result.problems, [])
        script = json.loads((talk / "000.json").read_text())
        self.assertIn("Frédéric Chopin", " ".join(l["text"] for l in script["lines"]))
        self.assertEqual(float(at.measure(talk / "000.flac", -18)["input_i"]) // 1, -18.0)

        # A model that invents falls back to a plain announcement built from the plan.
        self.llm.invent = True
        (talk / "003.flac").unlink()
        result = links.produce_plan(plan)
        self.assertTrue(any("plain template" in p for p in result.problems))
        script = json.loads((talk / "003.json").read_text())
        self.assertEqual(script["lines"][0]["text"],
                         "That was Nocturne in E flat major, Op. 9 no. 2, by Frédéric Chopin, performed by Test Pianist. "
                         "Until tomorrow night.")

    def test_market_clears_segment_by_segment(self):
        airing = self.airing("market_closing_tape", "2026-11-18 16:30")
        producer = MarketProducer(self.ctx)
        early = producer.produce(airing, datetime(2026, 11, 18, 14, 40))
        self.assertNotIn("tape", early.segments)  # The Tape waits for the close
        slot = self.ctx.buffer_dir / "market_closing_tape" / "2026-11-18_1630"
        self.assertEqual(sorted(p.stem for p in slot.glob("*.flac")),
                         ["01_big_story", "02_the_number", "03_main_street", "04_on_the_record", "05_tomorrow", "06_sign_off"])
        meta = json.loads((slot / "01_big_story.json").read_text())
        self.assertEqual((meta["artist"], meta["license"], meta["kind"]), ("Dottie Sterling", "generated", "talk"))
        self.assertIn("Federal Reserve Board", meta["credit"])
        signoff = json.loads((self.ctx.work_dir / "market_closing_tape" / "2026-11-18_1630" / "sign_off.script.json").read_text())
        self.assertTrue(signoff["lines"][-1]["text"].endswith(DISCLAIMER))

        late = producer.produce(airing, datetime(2026, 11, 18, 16, 12))
        self.assertEqual(late.segments, ["tape"])
        rehearsal = self.ctx.rehearsal_dir / "market_closing_tape" / "2026-11-18_1630"
        self.assertEqual([p.name for p in rehearsal.glob("*.flac")], ["00_tape.flac"])  # S&P via FRED: not cleared
        self.assertEqual(late.status, "rehearsed")

    def test_market_fails_closed(self):
        self.llm.invent = True
        result = MarketProducer(self.ctx).produce(self.airing("market_opening_bell", "2026-11-18 09:00"),
                                                  datetime(2026, 11, 18, 8, 45))
        self.assertEqual(result.segments, ["sign_off"])  # the plain sign-off still carries the disclaimer
        self.assertTrue(any("987,654" in p for p in result.problems))

    def test_ying_yang_rehearses_logs_and_grades_a_prediction(self):
        producer = YingYangProducer(self.ctx)
        result = producer.produce(self.airing("ying_yang", "2026-11-18 17:00"))
        self.assertEqual(result.status, "rehearsed")  # ESPN isn't cleared for air
        folder = self.ctx.rehearsal_dir / "ying_yang" / "2026-11-18_1700"
        self.assertIn("07_handshake.flac", [p.name for p in folder.glob("*.flac")])
        self.assertFalse((self.ctx.buffer_dir / "ying_yang").exists())
        self.assertIn("receipts: nothing in the feed for it", result.problems)
        with sqlite3.connect(self.ctx.state_dir / "predictions.db") as db:
            rows = db.execute("SELECT host, team, opponent, game_date, status FROM predictions").fetchall()
        self.assertEqual(rows, [("ying", "Cleveland Cavaliers", "Memphis Grizzlies", "2026-11-19", "open")])

        store = producer.predictions()
        self.assertEqual(store.grade(sports_sheet(None, final=True).data["games"]), 1)
        (graded,) = store.to_air()
        self.assertEqual((graded["status"], graded["result"]), ("right", "Cleveland Cavaliers 101, Memphis Grizzlies 99"))

    def test_monster_mix_drifts_with_wit_breaks(self):
        airing = self.airing("monster_mix", "2026-11-21 08:30")  # Saturday morning
        result = produce_monster_mix.MonsterMixProducer(self.ctx).produce(airing)
        folder = self.ctx.buffer_dir / "monster_mix" / airing.slot
        names = sorted(p.stem for p in folder.glob("*.flac"))
        self.assertTrue(names[0].endswith("wit_open"))
        tracks = [n for n in names if "track" in n]
        self.assertEqual(len(tracks), round(2 * airing.seconds / 3600))
        meta = json.loads((folder / f"{tracks[0]}.json").read_text())
        self.assertEqual((meta["license"], meta["kind"], meta["artist"]), ("generated", "music", "WIT-FM"))
        self.assertIn("BPM", meta["caption"])
        self.assertTrue(all("instrumental" in c for c in self.music_calls))
        log = (self.ctx.state_dir / "drift.jsonl").read_text().splitlines()
        self.assertEqual(len(log), len(tracks))
        self.assertEqual(result.problems, [])

    def test_monster_mix_without_a_music_engine_fails_cleanly(self):
        self.ctx.services["music"] = lambda caption, out, seconds: False
        result = produce_monster_mix.MonsterMixProducer(self.ctx).produce(self.airing("monster_mix", "2026-11-21 08:30"))
        self.assertEqual(result.status, "failed")
        self.assertFalse(list((self.ctx.buffer_dir / "monster_mix").rglob("*.flac")))

    def test_daemon_fills_locked_classical_slots_with_monster_mix(self):
        daemon = Daemon(self.ctx)
        (self.ctx.buffer_dir / "plans").mkdir(parents=True)
        (self.ctx.buffer_dir / "plans" / "_status.json").write_text(json.dumps({"unlocked": {"evening_concert": False}}))
        with patch.object(produce_monster_mix.MonsterMixProducer, "lead_hours", 3.0):
            results = daemon.cycle(datetime(2026, 11, 18, 18, 30))
        made = {(r.show_id, r.slot) for r in results if r.segments}
        self.assertIn(("monster_mix", "2026-11-18_2000"), made)  # Evening Concert's slot, locked
        self.assertTrue((self.ctx.buffer_dir / "monster_mix" / "2026-11-18_2000").is_dir())

    def test_sync_pulls_plans_pushes_audio_and_prunes(self):
        halo, server = self.root / "halo", self.root / "server"
        (server / "plans").mkdir(parents=True)
        (server / "plans" / "_status.json").write_text("{}")
        (server / "plans" / "nocturne@2026-11-10T0400Z.json").write_text(json.dumps({"ends_at": "2026-11-10T05:00:00-05:00"}))
        slot = halo / "monster_mix" / "2026-11-18_2000"
        slot.mkdir(parents=True)
        (slot / "00_wit_00.flac").write_bytes(b"audio")
        (slot / ".01_track.flac.partial.flac").write_bytes(b"half")
        sync = buffer_sync.Sync(halo, str(server))
        sync.pull()
        self.assertTrue((halo / "plans" / "_status.json").exists())
        sync.push()
        self.assertTrue((server / "monster_mix" / "2026-11-18_2000" / "00_wit_00.flac").exists())
        self.assertFalse((server / "monster_mix" / "2026-11-18_2000" / ".01_track.flac.partial.flac").exists())
        removed = buffer_sync.prune(server, 3, now=datetime(2026, 11, 22, 12, 0))
        self.assertIn(server / "monster_mix" / "2026-11-18_2000", removed)
        self.assertFalse((server / "plans" / "nocturne@2026-11-10T0400Z.json").exists())


if __name__ == "__main__":
    unittest.main()
