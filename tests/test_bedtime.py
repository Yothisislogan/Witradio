"""Hushworth's Bedtime Stories: the story library and its gate, readings, the performance and the seatings."""

import http.server
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))  # flat, the way the modules import each other
sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_tools as at  # noqa: E402
import story_library as sl  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from produce_bedtime import BedtimeProducer, _loop, chunk_paragraphs, pieces  # noqa: E402
from producers import Airing, Context  # noqa: E402
from schedule import load_schedule  # noqa: E402
from script_writer import ScriptWriter  # noqa: E402
from test_producers import FakeLLMHandler, voices_config  # noqa: E402
from voices import Renderer  # noqa: E402

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

HAVE_TOOLS = shutil.which("ffmpeg") is not None and np is not None
SCHEDULE = PROJECT_ROOT / "config" / "wit_next_schedule.yaml"
FILLER = ("The evening was quiet and the lamps were low, and everything in the little house was still except "
          "the clock, which went on ticking as it always had and always would.")


def gutenberg(title: str, author: str, body: str, translator: str = "") -> str:
    """A text laid out the way Project Gutenberg serves it."""
    extra = f"Translator: {translator}\n" if translator else ""
    return (f"﻿The Project Gutenberg eBook of {title}\n\nThis ebook is for the use of anyone anywhere in the "
            f"United States and most other parts of the world at no cost.\n\nTitle: {title}\n\nAuthor: {author}\n"
            f"{extra}\nRelease Date: June 1, 2008 [eBook #11]\n\nLanguage: English\n\n"
            f"*** START OF THE PROJECT GUTENBERG EBOOK {title.upper()} ***\n\nProduced by Some Volunteers\n\n"
            f"{body}\n\n*** END OF THE PROJECT GUTENBERG EBOOK {title.upper()} ***\n\nUpdated editions will replace "
            "the previous one.\n\nSection 1. General Terms of Use and Redistributing Project Gutenberg-tm electronic "
            "works\n\nPlease read this before you distribute or use this work.\n")


def wrap(text: str, width: int = 70) -> str:
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return "\n".join(lines + [line])


ALICE = gutenberg("Alice's Adventures in Wonderland", "Lewis Carroll", "\n\n".join([
    "Contents", "CHAPTER I. Down the Rabbit-Hole", "CHAPTER II. The Pool of Tears",
    "CHAPTER I.", "Down the Rabbit-Hole",
    wrap("Alice was beginning to get very tired of sitting by her sister on the bank. " + FILLER),
    wrap("‘Oh dear! Oh dear! I shall be late!’ said the Rabbit, and Alice didn’t think it so very much out of the "
         "way. " + FILLER),
    wrap(FILLER + " " + FILLER),
    "CHAPTER II.", "The Pool of Tears",
    wrap("‘Curiouser and curiouser!’ cried Alice. " + FILLER + " " + FILLER),
    wrap(FILLER + " " + FILLER),
]))
FABLES = gutenberg("The Aesop for Children", "Aesop", "\n\n".join(
    ["The Aesop for Children", "Contents"] + [part for n, name in enumerate(
        ["The Wolf and the Kid", "The Tortoise and the Ducks", "The Young Crab and His Mother",
         "The Frogs and the Ox"]) for part in (name, wrap(f"{FILLER} The {name.split()[-1].lower()} learned a "
                                                          f"lesson that day. " + FILLER), "Be kind and patient.")]))
PETER = gutenberg("The Tale of Peter Rabbit", "Beatrix Potter", "\n\n".join(
    ["THE TALE OF PETER RABBIT"] + [wrap(f"Paragraph {n}. " + FILLER) for n in range(8)] + ["THE END"]))
DRACULA = gutenberg("Dracula", "Bram Stoker", "\n\n".join([
    "CHAPTER I", "JONATHAN HARKER'S JOURNAL",
    wrap("3 May. Bistritz. Left Munich at 8:35 P. M., on 1st May. " + FILLER),
    wrap("“Welcome to my house!” said the Count. “Enter freely and of your own will!” " + FILLER),
]))


class BedtimeLLM(http.server.ThreadingHTTPServer):
    """Abridges by picking paragraphs, labels the speakers, and writes Hushworth's words from FACTS."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), BedtimeLLMHandler)
        self.invent = False
        self.calls = 0
        self.abridge = "good"
        self.invite = False
        self.prompts = []

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class BedtimeLLMHandler(FakeLLMHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls += 1
        messages = body["messages"]
        user = messages[-1]["content"]
        self.server.prompts.append(user)
        if user.startswith("Choose paragraphs"):
            numbers = [int(n) for n in re.findall(r"^\[(\d+)\]", user, re.M)]
            target = int(re.search(r"about (\d+) words", user).group(1))
            if self.server.abridge == "rewrite":
                reply = {"keep": [1, 2]}  # drops the first paragraph: refused
            else:
                keep, words = [], 0
                lengths = {int(n): len(t.split()) for n, t in re.findall(r"^\[(\d+)\] (.+)$", user, re.M)}
                for n in numbers:
                    if n in (numbers[0], numbers[-1]) or words + lengths[n] + lengths[numbers[-1]] <= target:
                        keep.append(n)
                        words += lengths[n]
                reply = {"keep": keep}
        elif user.startswith("Label each numbered piece"):
            lines = [{"i": int(n), "who": "the Speaker" if speech else "narrator", "how": "warmly"}
                     for n, speech in re.findall(r"^\[(\d+)\] (\(speech\) )?", user, re.M)]
            reply = {"cast": {"the Speaker": "bright and quick"}, "lines": lines}
        else:
            reply = self.script(messages[0]["content"], user)
            if self.server.invite:
                reply["lines"].insert(0, {"speaker": reply["lines"][0]["speaker"],
                                          "text": "Do visit our website and send us your drawings."})
        data = json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class StoryLibraryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.library = sl.StoryLibrary(Path(tmp.name) / "stories")

    def test_the_gate(self):
        today = date(2026, 11, 19)
        self.assertEqual(sl.public_domain_cutoff(today), 1930)
        self.assertEqual(sl.gate({"published": 1902}, today), [])
        self.assertIn("1931", sl.gate({"published": 1931}, today)[0])
        self.assertIn("no year, no reading", sl.gate({}, today)[0])
        self.assertIn("no publication year", sl.gate({"published": 1884, "translator": "Someone"}, today)[0])
        self.assertIn("older public-domain translation",
                      sl.gate({"published": 1884, "translator": "Someone", "translated": 1998}, today)[0])
        self.assertEqual(sl.gate({"published": 1931}, date(2027, 1, 1)), [])  # 1931 joins on New Year's Day 2027

    def test_every_gutenberg_reference_is_stripped(self):
        body = sl.strip_gutenberg(ALICE)
        self.assertNotIn("gutenberg", body.lower())
        self.assertNotIn("Produced by", body)
        self.assertNotIn("Section 1", body)
        self.assertTrue(body.startswith("Contents"))
        fields = sl.header_fields(ALICE)
        self.assertEqual((fields["title"], fields["author"]), ("Alice's Adventures in Wonderland", "Lewis Carroll"))

    def test_a_serial_splits_into_chapters(self):
        result = self.library.pull("alice", text=ALICE)
        self.assertTrue(result.ok, result.problems)
        units = self.library.units("alice")
        self.assertEqual([u["title"] for u in units], ["CHAPTER I: Down the Rabbit-Hole", "CHAPTER II: The Pool of Tears"])
        self.assertTrue(units[0]["paragraphs"][0].startswith("Alice was beginning"))
        self.assertNotIn("\n", units[0]["paragraphs"][0])  # unwrapped
        meta = self.library.meta("alice")
        self.assertEqual(meta["credit"], "Alice's Adventures in Wonderland, by Lewis Carroll, from the public domain.")
        self.assertEqual(self.library.pulled("early"), ["alice"])

    def test_the_wrong_book_or_a_modern_translation_is_refused(self):
        wrong = self.library.pull("oz", text=ALICE)
        self.assertFalse(wrong.ok)
        self.assertIn("not \"The Wonderful Wizard of Oz\"", wrong.problems[0].replace("'", '"'))
        translated = gutenberg("Alice's Adventures in Wonderland", "Lewis Carroll", "x", translator="A. Translator")
        result = self.library.pull("alice", text=translated)
        self.assertFalse(result.ok)
        self.assertIn("translation", result.problems[0])
        works = {"newer": {"title": "A Newer Book", "author": "Someone", "published": 1935, "seating": "late"}}
        self.assertIn("1935", sl.StoryLibrary(self.library.root, {"works": works}).pull("newer").problems[0])

    def test_downloads_come_from_gutenberg_or_a_mirror(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = (ALICE if self.path.endswith("/11/pg11.txt") else "").encode()
                self.send_response(200 if data else 404)
                self.end_headers()
                self.wfile.write(data)
                self.server.agents.append(self.headers.get("User-Agent"))

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.agents = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/cache/epub/{{id}}/pg{{id}}.txt"
        with patch.dict(os.environ, {"WRIT_GUTENBERG_URL": url}):
            self.assertTrue(self.library.pull("alice").ok)
            self.assertFalse(self.library.pull("oz").ok)
        self.assertEqual(server.agents[0], "story-library/1.0")  # nothing about the station or its owner

    def test_collections_pick_stories_by_title(self):
        text = gutenberg("Grimms' Fairy Tales", "Jacob Grimm and Wilhelm Grimm", "\n\n".join(
            ["THE GOLDEN GOOSE", wrap(FILLER * 2), "THE ELVES AND THE SHOEMAKER", wrap(FILLER * 2),
             "HANSEL AND GRETEL", wrap(FILLER * 2)]), translator="Edgar Taylor and Marian Edwardes")
        result = self.library.pull("grimm", text=text)
        self.assertTrue(result.ok)
        self.assertEqual([u.title for u in result.units], ["THE ELVES AND THE SHOEMAKER", "THE GOLDEN GOOSE"])
        self.assertIn("Grimms' Fairy Tales: no story called 'The Travelling Musicians' found", result.problems)

    def test_names_from_the_films_are_caught(self):
        blocked = sl.load_sources()["not_in_the_books"]
        self.assertEqual(sl.film_only(["She clicked her ruby slippers."], blocked), ["ruby slippers"])
        self.assertEqual(sl.film_only(["The silver shoes carried her home."], blocked), [])


class PiecesTests(unittest.TestCase):
    def test_speech_and_narration_split_without_touching_a_word(self):
        para = "‘Oh dear! I shall be late!’ said the Rabbit, and Alice didn’t think it odd. “Enter freely,” he said."
        parts = pieces(para)
        self.assertEqual([q for _, q in parts], [True, False, True, False])
        self.assertEqual(parts[0][0], "‘Oh dear! I shall be late!’")
        self.assertIn("didn’t", parts[1][0])
        self.assertEqual(" ".join(t for t, _ in parts), para)
        long = pieces("One two three. " * 60)
        self.assertTrue(all(len(t) <= 420 for t, _ in long))
        self.assertEqual(" ".join(t for t, _ in long), ("One two three. " * 60).strip())
        self.assertEqual(chunk_paragraphs(["a " * 100, "b " * 100, "c " * 100], 150), [["a " * 100], ["b " * 100], ["c " * 100]])

    @unittest.skipUnless(np is not None, "numpy is required")
    def test_the_bed_carries_on_across_files(self):
        bed = np.random.default_rng(0).standard_normal((44100 * 3, 2)).astype(np.float32)
        whole = _loop(bed, 1000, 200000)
        self.assertTrue(np.array_equal(whole, np.vstack([_loop(bed, 1000, 70000), _loop(bed, 71000, 130000)])))


@unittest.skipUnless(HAVE_TOOLS, "ffmpeg and numpy are required")
class BedtimeProducerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        config = self.root / "bedtime.yaml"
        config.write_text(json.dumps({
            "seatings": {"early": {"story_minutes": [1, 2], "bed_bpm": 62, "bed_style": "a soft music box lullaby"},
                         "late": {"story_minutes": [1, 2], "bed_bpm": 66, "bed_style": "a quiet nocturne"}},
            "words_per_minute": 125, "abridge_up_to": 1.6, "chunk_words": 120, "loudness": -20.0, "limit_db": -3.0,
            "bed_db": -20.0}))
        env = patch.dict(os.environ, {"WRIT_BEDTIME_CONFIG": str(config), "WRIT_STORIES_DIR": str(self.root / "stories")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WRIT_REHEARSE", None)
        library = sl.StoryLibrary(self.root / "stories")
        for work, text in (("aesop", FABLES), ("peter_rabbit", PETER), ("alice", ALICE), ("dracula", DRACULA)):
            self.assertTrue(library.pull(work, text=text).ok, work)
        self.llm = BedtimeLLM()
        threading.Thread(target=self.llm.serve_forever, daemon=True).start()
        self.addCleanup(self.llm.server_close)
        self.addCleanup(self.llm.shutdown)
        renderer = Renderer.from_config(voices_config(self.root), self.root / "none.yaml")
        self.music = []
        self.ctx = Context(
            schedule=load_schedule(SCHEDULE), renderer=renderer,
            writer=ScriptWriter.for_renderer(LLMClient(base_url=self.llm.base, retries=0), renderer, attempts=2),
            buffer_dir=self.root / "buffer", rehearsal_dir=self.root / "rehearsal", work_dir=self.root / "work",
            state_dir=self.root / "state", log=lambda m: None, services={"music": self.fake_music})
        self.producer = BedtimeProducer(self.ctx)

    def fake_music(self, caption, out, seconds):
        self.music.append(caption)
        at.tone(Path(out), 12, 262)
        return True

    def airing(self, show_id: str, start: datetime, minutes: float = 4) -> Airing:
        show = self.ctx.schedule.shows[show_id]
        return Airing(show_id=show_id, start=start, end=start + timedelta(minutes=minutes), program="bedtime",
                      params=dict(show.params), host="hushworth", title=show.name)

    def make(self, airing: Airing):
        result = self.producer.produce(airing)
        while result.status == "partial":
            more = self.producer.produce(airing)
            result.segments += more.segments
            result.problems += more.problems
            result.status = more.status
        plan = json.loads(self.producer.plan_path(airing).read_text())
        return result, plan

    def test_four_nights_of_the_early_seating(self):
        night = [self.airing("bedtime_early", datetime(2026, 11, 19 + k, 19, 30)) for k in range(4)]

        # Night one: fables, bundled.
        result, plan = self.make(night[0])
        first_night = plan["reading"]["titles"]
        self.assertEqual(result.status, "done")
        self.assertEqual(plan["reading"]["work"], "aesop")
        self.assertGreater(len(plan["reading"]["titles"]), 1)
        self.assertEqual(plan["order"][0], "welcome")
        self.assertEqual(plan["order"][-2:], ["goodnight", "lullaby"])
        folder = self.ctx.buffer_dir / "bedtime_early" / night[0].slot
        files = sorted(p.name for p in folder.glob("*.flac"))
        self.assertEqual(files[0], "00_welcome.flac")
        self.assertTrue(files[-1].endswith("_lullaby.flac"))
        total = sum(at.duration(p) for p in folder.glob("*.flac"))
        self.assertAlmostEqual(total, night[0].seconds - 3.0, delta=1.0)  # the soft music runs to the end of the slot
        story = json.loads((folder / "01_story_01.json").read_text())
        self.assertEqual((story["license"], story["kind"]), ("public_domain", "reading"))
        self.assertEqual(story["credit"], "The Aesop for Children, by Aesop, from the public domain.")
        self.assertTrue(all("BPM" in c and "no vocals" in c for c in self.music))
        samples = __import__("produce_bedtime")._decode(folder / "01_story_01.flac")
        self.assertLessEqual(float(np.abs(samples).max()), 10 ** (-3 / 20) + 1e-3)  # nothing over the limit
        self.assertAlmostEqual(float(at.measure(folder / "01_story_01.flac", -20)["input_i"]), -20.0, delta=2.0)

        # Night two: the rest of the fables.
        _, plan = self.make(night[1])
        self.assertEqual(plan["reading"]["work"], "aesop")
        self.assertEqual(first_night + plan["reading"]["titles"],
                         ["The Wolf and the Kid", "The Tortoise and the Ducks", "The Young Crab and His Mother",
                          "The Frogs and the Ox"])

        # Night three: Peter Rabbit runs long, so it's abridged by whole paragraphs and announced as adapted.
        _, plan = self.make(night[2])
        self.assertEqual(plan["reading"]["work"], "peter_rabbit")
        self.assertTrue(plan["reading"]["adapted"])
        original = self.producer.library.units("peter_rabbit")[0]["paragraphs"]
        read = [p for chunk in plan["chunks"] for p in chunk]
        self.assertTrue(set(read) <= set(original))  # every word the author's
        self.assertEqual((read[0], read[-1]), (original[0], original[-1]))
        self.assertLess(len(read), len(original))
        welcome = self.llm.prompts
        self.assertTrue(any("This reading is adapted" in p for p in welcome))

        # Night four: Alice, chapter one; to be continued.
        _, plan = self.make(night[3])
        self.assertEqual((plan["reading"]["work"], plan["reading"]["titles"]), ("alice", ["CHAPTER I: Down the Rabbit-Hole"]))
        self.assertTrue(plan["reading"]["continues"])
        progress = json.loads((self.ctx.state_dir / "bedtime" / "progress.json").read_text())
        self.assertEqual(progress["early"], {"work": "alice", "unit": 1, "para": 0})

    def test_the_performance_keeps_every_word_and_gives_the_characters_voices(self):
        plan = {"id": "test", "seating": "early"}
        paragraphs = self.producer.library.units("alice")[0]["paragraphs"]
        script = self.producer.perform(paragraphs, plan, [])
        self.assertEqual(" ".join(l.text for l in script.lines), " ".join(paragraphs))
        rabbit = next(l for l in script.lines if l.text.startswith("‘Oh dear!"))
        self.assertEqual(rabbit.emotion, "as the Speaker: bright and quick, warmly")
        self.assertTrue(next(l for l in script.lines if l.text.startswith("said the Rabbit")).emotion.startswith("narrating"))
        self.assertTrue(any(l.pause_before_ms for l in script.lines))  # a breath between paragraphs

    def test_a_bad_abridgement_splits_the_story_instead(self):
        self.llm.abridge = "rewrite"
        library = self.producer.library
        (library.root / "aesop").rename(library.root / "aesop.off")  # Peter Rabbit first
        problems = []
        reading = self.producer.next_reading("early", problems)
        self.assertEqual(reading["work"], "peter_rabbit")
        self.assertFalse(reading["adapted"])
        self.assertTrue(reading["continues"])
        self.assertTrue(any("splitting instead" in p for p in problems))
        again = self.producer.next_reading("early", problems)
        self.assertTrue(again["part"])  # tomorrow picks up where tonight stopped

    def test_the_early_seating_never_invites_entries(self):
        self.llm.invite = True
        airing = self.airing("bedtime_early", datetime(2026, 11, 19, 19, 30))
        problems = []
        plan = self.producer.plan(airing, problems)
        voice = self.producer.words_around("welcome", plan, problems)
        spoken = " ".join(l.text for l in voice.lines)
        self.assertNotIn("website", spoken)
        self.assertTrue(any("Early Seating" in p for p in problems))
        self.assertTrue(spoken.endswith("Lights low. Blanket up. Once upon a time..."))
        self.assertIn("from the public domain", spoken)

    def test_the_late_seating_with_dracula(self):
        library = self.producer.library
        airing = self.airing("bedtime_late", datetime(2026, 11, 19, 22, 0), minutes=5)
        result, plan = self.make(airing)
        self.assertEqual(plan["reading"]["work"], "dracula")
        self.assertEqual(plan["order"][0], "cold_open")
        self.assertIn("the_count", plan["order"])
        self.assertEqual(plan["order"][-2:], ["afterword", "quiet_bed"])
        self.assertLess(plan["order"].index("the_count"), plan["order"].index("afterword"))
        folder = self.ctx.buffer_dir / "bedtime_late" / airing.slot
        count = json.loads(next(folder.glob("*_the_count.json")).read_text())
        self.assertEqual(count["artist"], "The Count")
        cold = self.producer.words_around("cold_open", plan, [])
        self.assertEqual(cold.lines[0].text, "Lock the door. No, the other door.")
        after = self.producer.words_around("afterword", plan, [])
        self.assertTrue(after.lines[-1].text.endswith("Sir Reginald insists."))
        self.assertTrue(any("Bram Stoker" in p and ("Lyceum" in p or "1897" in p) for p in self.llm.prompts))
        self.assertGreater(len(library.units("dracula")), 0)

    def test_no_stories_no_show(self):
        shutil.rmtree(self.root / "stories")
        result = self.producer.produce(self.airing("bedtime_early", datetime(2026, 11, 19, 19, 30)))
        self.assertEqual(result.status, "skipped")
        self.assertIn("pull_stories.py", result.problems[0])


if __name__ == "__main__":
    unittest.main()
