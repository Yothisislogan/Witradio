"""Office Hours: signed class material, the curriculum, the review queue, encores and Ask the Professor."""

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))  # flat, the way the modules import each other
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ask_professor  # noqa: E402
import audio_tools as at  # noqa: E402
import office_hours as oh  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from produce_office_hours import OfficeHoursProducer  # noqa: E402
from producers import Context, upcoming_airings  # noqa: E402
from schedule import load_schedule  # noqa: E402
from script_writer import ScriptWriter  # noqa: E402
from test_producers import FakeLLM, voices_config  # noqa: E402
from voices import Renderer  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
SCHEDULE = PROJECT_ROOT / "config" / "wit_next_schedule.yaml"
MONDAY = datetime(2026, 12, 7, 18, 0)  # the course's first night
DISCLAIMER = "Your coverage depends on your policy language and your state."


def sign_everything(library: oh.Library) -> None:
    for item in list(library.items.values()):
        library.sign(item, "Test Reviewer, licensed P&C", date(2026, 11, 30))


class LibraryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.private = Path(tmp.name) / "private"

    def library(self) -> oh.Library:
        return oh.Library([self.private, oh.PUBLIC_DIR])

    def test_the_starter_material_waits_for_a_licensed_signature(self):
        library = self.library()
        kinds = {}
        for (kind, _), _ in library.items.items():
            kinds[kind] = kinds.get(kind, 0) + 1
        self.assertEqual(kinds, {"lecture": 5, "term": 14, "question": 10, "faq": 3, "line": 1})
        self.assertEqual(len(library.waiting()), len(library.items))  # nothing ships signed
        self.assertIsNone(oh.lecture_for(library, MONDAY.date()))
        lecture = library.lecture("insurance-101", "deductibles")
        self.assertEqual([h for h, _ in lecture.data["sections"]][:2], ["What a deductible is", "Why deductibles exist"])
        self.assertIn("Example: a $1,000 deductible on a $6,000 covered loss. The insurer pays $5,000.", lecture.facts())

    def test_a_signature_is_bound_to_the_text_it_signed(self):
        shutil.copytree(oh.PUBLIC_DIR / "lectures", self.private / "lectures")
        library = self.library()
        lecture = library.lecture("insurance-101", "risk-pooling")
        library.sign(lecture, "Jane Doe")
        self.assertTrue(self.library().signed(self.library().lecture("insurance-101", "risk-pooling")))
        path = self.private / "lectures" / "insurance-101" / "risk-pooling.md"
        path.write_text(path.read_text().replace("many people each pay", "lots of people each pay"))
        self.assertFalse(self.library().signed(self.library().lecture("insurance-101", "risk-pooling")))
        self.assertEqual(self.library().signer(self.library().lecture("insurance-101", "premiums")), "")

    def test_the_curriculum_runs_weeknights_in_order_and_skips_unsigned_topics(self):
        config = self.library().config
        self.assertEqual(oh.class_number(config, date(2026, 12, 7)), 0)
        self.assertEqual(oh.class_number(config, date(2026, 12, 11)), 4)  # Friday
        self.assertEqual(oh.class_number(config, date(2026, 12, 14)), 5)  # next Monday
        self.assertEqual(oh.class_number(config, date(2026, 12, 1)), -1)
        library = self.library()
        for topic in ("risk-pooling", "deductibles"):
            library.sign(library.lecture("insurance-101", topic), "Jane Doe")
        library = self.library()
        self.assertEqual(oh.lecture_for(library, date(2026, 12, 7)).id, "insurance-101/risk-pooling")
        self.assertEqual(oh.lecture_for(library, date(2026, 12, 8)).id, "insurance-101/deductibles")  # premiums unsigned
        self.assertEqual(oh.lecture_for(library, date(2026, 12, 9)).id, "insurance-101/deductibles")

    def test_personal_details_are_caught(self):
        self.assertEqual(oh.pii_in(["Call 757-555-0123 or write jo@example.com"]), ["an email address", "a phone number"])
        self.assertEqual(oh.pii_in(["The house at 1234 Elm Street flooded."]), ["a street address"])
        self.assertEqual(oh.pii_in(["Claim no. AB-123456 was denied."]), ["a policy or claim number"])
        self.assertEqual(oh.pii_in(["A pipe burst in a two-story house in January."]), [])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class OfficeHoursProducerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.private = self.root / "private"
        env = patch.dict(os.environ, {"WRIT_OFFICE_HOURS_DIR": str(self.private)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WRIT_REHEARSE", None)
        sign_everything(oh.Library())
        self.llm = FakeLLM()
        threading.Thread(target=self.llm.serve_forever, daemon=True).start()
        self.addCleanup(self.llm.server_close)
        self.addCleanup(self.llm.shutdown)
        renderer = Renderer.from_config(voices_config(self.root), self.root / "none.yaml")
        self.ctx = Context(
            schedule=load_schedule(SCHEDULE), renderer=renderer,
            writer=ScriptWriter.for_renderer(LLMClient(base_url=self.llm.base, retries=0), renderer, attempts=2),
            buffer_dir=self.root / "buffer", rehearsal_dir=self.root / "rehearsal", work_dir=self.root / "work",
            state_dir=self.root / "state", log=lambda m: None)
        self.producer = OfficeHoursProducer(self.ctx)

    def airing(self, show_id: str, start: datetime):
        return next(a for a in upcoming_airings(self.ctx.schedule, start, 1) if a.show_id == show_id and a.start == start)

    def text(self, segment: dict) -> str:
        return " ".join(l["text"] for p in segment["parts"] for l in (p.get("script") or {}).get("lines", []))

    def test_an_episode_is_drafted_for_review_on_the_bibles_clock(self):
        airing = self.airing("office_hours", MONDAY)
        result = self.producer.produce(airing, datetime(2026, 12, 6, 14, 0))
        self.assertEqual(result.status, "drafted")
        self.assertIn("waiting for review", result.problems[0])
        episode = self.producer.queue.load("office_hours_2026-12-07_1800")
        self.assertEqual(episode["status"], "pending_review")  # the first ten all need sign-off
        self.assertEqual(episode["topic"], "insurance-101/risk-pooling")
        names = [s["name"] for s in episode["segments"]]
        self.assertEqual(names, ["roll_call", "lecture_1", "lecture_2", "lecture_3", "lecture_4", "define_that",
                                 "licensing_lab", "recruiting", "ask_the_professor", "homework"])
        for segment in episode["segments"]:
            if segment["name"].startswith("lecture_"):
                self.assertTrue(segment["parts"][0]["script"]["lines"][-1]["text"] == DISCLAIMER)
        self.assertTrue(self.text(episode["segments"][-1]).endswith("Class dismissed. Go read your declarations page."))
        lab = episode["segments"][6]
        self.assertIn("not official exam questions", self.text(lab))
        pauses = [p["pause"] for p in lab["parts"] if "pause" in p]
        self.assertEqual(pauses, [8.0] * 5)
        first_question = lab["parts"][1]["script"]["lines"][0]["text"]
        self.assertTrue(first_question.startswith("Question one. The law of large numbers lets an insurer do which"))
        self.assertIn("or D: Eliminate the chance of a loss?", first_question)
        self.assertEqual(lab["parts"][3]["script"]["lines"][0]["text"], "The answer is B.")
        ask = self.text(episode["segments"][8])
        self.assertIn("For your own claim, call your agent or carrier.", ask)
        self.assertEqual(self.text(episode["segments"][7]),
                         "Studying for your license? We Insure Things is hiring, and we help new agents get licensed.")
        self.assertNotIn("names WIT outside the recruiting line", episode["flags"])
        self.assertTrue(any(s.startswith("lecture insurance-101/risk-pooling, signed by Test Reviewer") for s in episode["sources"]))
        readable = (self.producer.queue.readable / "office_hours_2026-12-07_1800.md").read_text()
        self.assertIn("## Licensing Lab", readable)
        self.assertIn("[8.0 second pause]", readable)
        self.assertFalse(list(self.ctx.buffer_dir.rglob("*.flac")))  # nothing voiced before sign-off

        # Approved: voiced three segments a cycle, then archived.
        self.producer.queue.decide(episode["id"], approve=True, reviewer="Jane Doe")
        first = self.producer.produce(airing, datetime(2026, 12, 6, 14, 5))
        self.assertEqual((first.status, first.segments), ("partial", ["roll_call", "lecture_1", "lecture_2"]))
        while first.status == "partial":
            first = self.producer.produce(airing, datetime(2026, 12, 6, 14, 10))
        folder = self.ctx.buffer_dir / "office_hours" / "2026-12-07_1800"
        files = sorted(p.name for p in folder.glob("*.flac"))
        self.assertEqual(files[0], "00_roll_call.flac")
        self.assertEqual(len(files), 10)
        meta = json.loads((folder / "06_licensing_lab.json").read_text())
        self.assertEqual((meta["artist"], meta["license"], meta["kind"]), ("Professor Ex", "generated", "talk"))
        self.assertEqual(meta["title"], "Office Hours with Professor Ex: Risk pooling: why insurance works at all")
        self.assertGreater(at.duration(folder / "06_licensing_lab.flac"), 5 * 8)  # five pauses for the answer
        self.assertEqual(self.producer.queue.load(episode["id"])["status"], "rendered")
        self.assertTrue((self.producer.archive / episode["id"] / "00_roll_call.flac").exists())

        # Tuesday's episode is still waiting 60 minutes before air: Monday's airs as an encore.
        tuesday = self.airing("office_hours", datetime(2026, 12, 8, 18, 0))
        self.producer.produce(tuesday, datetime(2026, 12, 7, 20, 0))
        encore = self.producer.produce(tuesday, datetime(2026, 12, 8, 17, 0))
        self.assertEqual(encore.status, "encore")
        tuesday_folder = self.ctx.buffer_dir / "office_hours" / "2026-12-08_1800"
        self.assertEqual(len(list(tuesday_folder.glob("*.flac"))), 10)
        self.assertTrue(json.loads((tuesday_folder / "00_roll_call.json").read_text())["title"].startswith(
            "Office Hours (encore):"))
        self.assertEqual(self.producer.queue.load("office_hours_2026-12-08_1800")["status"], "missed")
        with self.assertRaises(ValueError):
            self.producer.queue.decide("office_hours_2026-12-08_1800", approve=True, reviewer="Jane Doe")

    def test_a_rejected_episode_is_rewritten_with_the_note(self):
        airing = self.airing("office_hours", MONDAY)
        self.producer.produce(airing, datetime(2026, 12, 6, 14, 0))
        self.producer.queue.decide("office_hours_2026-12-07_1800", approve=False, reviewer="Jane Doe",
                                   note="Say more about adverse selection.")
        briefs = []
        real = self.ctx.writer.write

        def spy(segment):
            briefs.append(segment.brief)
            return real(segment)

        with patch.object(self.ctx.writer, "write", spy):
            result = self.producer.produce(airing, datetime(2026, 12, 6, 15, 0))
        self.assertEqual(result.status, "drafted")
        self.assertTrue(briefs and all("Say more about adverse selection." in b for b in briefs))
        self.assertEqual(self.producer.queue.load("office_hours_2026-12-07_1800")["status"], "pending_review")

    def test_after_the_gate_only_flagged_episodes_wait(self):
        queue = self.producer.queue
        for n in range(10):
            queue.save({"id": f"office_hours_old_{n}", "status": "rendered", "reviewer": "Jane Doe", "segments": []})
        quiet = self.producer.draft(self.airing("office_hours", MONDAY))
        self.assertEqual(quiet["status"], "approved")
        plans = self.ctx.buffer_dir / "plans"
        plans.mkdir(parents=True)
        (plans / "_ask_the_professor.json").write_text(json.dumps({"answered": [
            {"id": 7, "name": "Dana", "city": "Norfolk", "question": "Does homeowners insurance cover a sewer backup?",
             "notes": ["Sewer and drain backup is commonly excluded unless the policy adds it by endorsement."]}]}))
        flagged = self.producer.draft(self.airing("office_hours", datetime(2026, 12, 8, 18, 0)))
        self.assertEqual(flagged["status"], "pending_review")
        self.assertIn("listener questions", flagged["flags"])
        ask = next(s for s in flagged["segments"] if s["name"] == "ask_the_professor")
        self.assertIn("Dana", json.dumps(ask))
        self.assertEqual(self.producer.listener_questions(), [])  # it won't air twice

    def test_war_stories_never_carry_personal_details(self):
        stories = self.private / "war_stories"
        stories.mkdir(parents=True)
        (stories / "frozen-pipe.md").write_text(
            "---\ntitle: \"The frozen pipe\"\nsource: wit_claim\nlesson: \"Sudden bursts are commonly covered; "
            "slow leaks commonly aren't.\"\n---\n- A pipe froze and burst in an empty house in January.\n"
            "- The water ran for two days before a neighbor noticed.\n")
        (stories / "leaky.md").write_text(
            "---\ntitle: \"Leaky\"\nsource: wit_claim\nlesson: \"x\"\n---\n- The owner at 1234 Elm Street called.\n")
        library = oh.Library()
        for item in library.ready("story") + [i for i in library.waiting() if i.kind == "story"]:
            library.sign(item, "Jane Doe")
        episode = self.producer.draft(self.airing("office_hours", MONDAY))
        names = [s["name"] for s in episode["segments"]]
        self.assertIn("war_stories", names)
        self.assertIn("a story from WIT's own claims", episode["flags"])
        self.assertTrue(any("war story leaky skipped" in p and "a street address" in p for p in episode["problems"]))
        story = next(s for s in episode["segments"] if s["name"] == "war_stories")
        self.assertTrue(self.text(story).endswith(DISCLAIMER))

    def test_exam_cram_runs_the_questions_back_to_back(self):
        airing = self.airing("exam_cram", datetime(2026, 12, 12, 13, 0))
        episode = self.producer.draft(airing)
        self.assertEqual(episode["kind"], "exam_cram")
        self.assertEqual(episode["topic_title"], "Exam Cram (10 questions)")  # ten signed questions in the bank
        names = [s["name"] for s in episode["segments"]]
        self.assertEqual(names, ["roll_call", "questions_1", "recruiting", "questions_2", "dismissal"])
        second = episode["segments"][3]
        self.assertTrue(second["parts"][0]["script"]["lines"][0]["text"].startswith("Question six."))
        self.assertNotIn("Licensing Lab", json.dumps(episode))
        self.assertIn("Ten practice questions, back to back.", self.text(episode["segments"][0]))

    def test_nothing_signed_means_no_episode_and_no_encore(self):
        shutil.rmtree(self.private)
        result = self.producer.produce(self.airing("office_hours", MONDAY), datetime(2026, 12, 7, 17, 0))
        self.assertEqual(result.status, "skipped")
        self.assertFalse(list(self.ctx.buffer_dir.rglob("*.flac")))


class AskTheProfessorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_questions_are_screened_answered_and_exported(self):
        ok = {"question": "Does my renters policy cover a stolen bike?", "name": "Dana", "city": "Norfolk",
              "age_13_plus": True}
        self.assertEqual(ask_professor.clean_question(ok)["question"], ok["question"])
        for bad, message in (({**ok, "age_13_plus": False}, "13"), ({**ok, "question": "Why?"}, "between 10"),
                             ({**ok, "question": "Call me at 757-555-0123 about my claim"}, "personal details"),
                             ({**ok, "question": "My policy number: HO-1234567 was denied, why?"}, "personal details"),
                             ({**ok, "question": "See www.example.com for my question"}, "personal details"),
                             ({**ok, "name": "Dana 99"}, "first name")):
            with self.assertRaises(ask_professor.AskError) as caught:
                ask_professor.clean_question(bad)
            self.assertIn(message, str(caught.exception))
        with ask_professor.Mailbag(self.root / "ask.db") as mailbag:
            first = mailbag.submit(ok)
            second = mailbag.submit({**ok, "question": "Is flood covered by homeowners insurance?", "name": ""})
            self.assertEqual(len(mailbag.pending()), 2)
            mailbag.answer(first, ["Theft of personal property is commonly covered, subject to the deductible."], "Jane Doe")
            mailbag.reject(second)
            with self.assertRaises(ask_professor.AskError):
                mailbag.answer(second, ["  "], "Jane Doe")
            dest = mailbag.export(self.root / "plans" / "_ask_the_professor.json")
        data = json.loads(dest.read_text())
        self.assertEqual([q["id"] for q in data["answered"]], [first])
        self.assertEqual(data["answered"][0]["notes"], ["Theft of personal property is commonly covered, subject to the deductible."])

    def test_the_station_api_takes_questions(self):
        import api_server

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), api_server.NowPlayingHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(data):
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/ask-the-professor",
                                             data=json.dumps(data).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        with patch.dict(os.environ, {"WRIT_ASK_PROFESSOR_DB": str(self.root / "ask.db")}), \
                patch.dict(api_server.form_times, clear=True):
            status, body = post({"question": "What is an endorsement on a policy?", "age_13_plus": True})
            self.assertEqual(status, 200)
            self.assertIn("reviewer", body["note"])
            status, body = post({"question": "Email me at a@b.co please, about deductibles", "age_13_plus": True})
            self.assertEqual(status, 400)
        with ask_professor.Mailbag(self.root / "ask.db") as mailbag:
            self.assertEqual([r["question"] for r in mailbag.pending()], ["What is an endorsement on a policy?"])


if __name__ == "__main__":
    unittest.main()
