"""Script writing against a fake OpenAI-compatible server, and the fact-check gate."""

import http.server
import json
import random
import tempfile
import threading
import unittest
from pathlib import Path

from mac import fact_check as fc
from mac.llm_client import LLMClient
from mac.script_writer import ScriptError, ScriptWriter, Segment, parse_reply

FACTS = ("The Tape, Tuesday November 17 2026. S&P 500 closed at 6712.34, change -1.23%. "
         "10-year Treasury yield 4.253%. The Federal Reserve held rates steady.")


class FakeChat(http.server.ThreadingHTTPServer):
    def __init__(self, replies):
        super().__init__(("127.0.0.1", 0), FakeChatHandler)
        self.replies = list(replies)
        self.requests: list[dict] = []

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class FakeChatHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        reply = self.server.replies.pop(0) if self.server.replies else "{}"
        data = json.dumps({"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def script(*lines, **extra) -> str:
    return json.dumps({"title": "The Tape", "lines": [{"speaker": s, "text": t} for s, t in lines], **extra})


GOOD = script(("dottie", "Let's read the tape. The S&P 500 closed at 6,712, down 1.2%. "
                         "The 10-year Treasury yield is 4.25%. The Federal Reserve held rates steady. "
                         "Here's the number. Here's why you care."))


class WriterTests(unittest.TestCase):
    def writer(self, replies, **kwargs):
        self.server = FakeChat(replies)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        llm = LLMClient(base_url=self.server.base, retries=0)
        return ScriptWriter(llm, words_per_second={"dottie": 2.7}, nonverbals={"dottie": []}, **kwargs)

    def segment(self, **kwargs):
        defaults = dict(id="closing_tape/tape", brief="Read the closes.", speakers=["dottie"], seconds=16, facts=FACTS)
        return Segment(**{**defaults, **kwargs})

    def test_good_reply_on_the_first_try(self):
        writer = self.writer([GOOD])
        written = writer.write(self.segment())
        self.assertEqual((written.attempts, written.script.lines[0].speaker), (1, "dottie"))
        request = self.server.requests[0]
        self.assertEqual(request["response_format"], {"type": "json_object"})
        system, user = request["messages"][0]["content"], request["messages"][1]["content"]
        self.assertIn("Every fact comes from the FACTS block", system)
        self.assertIn("Dottie Sterling", system)
        self.assertIn("FACTS (the only source of facts):\n" + FACTS, user)
        self.assertIn("about 43 words", user)

    def test_invented_number_goes_back_for_a_rewrite(self):
        bad = script(("dottie", "Let's read the tape. The S&P 500 closed at 6,800, down 1.2%. "
                                "The 10-year Treasury yield is 4.25%. The Federal Reserve held rates steady."))
        writer = self.writer([bad, GOOD])
        written = writer.write(self.segment())
        self.assertEqual(written.attempts, 2)
        feedback = self.server.requests[1]["messages"][-1]["content"]
        self.assertIn("These numbers are not in FACTS: 6,800", feedback)

    def test_gives_up_after_the_last_try(self):
        stranger = script(("ying", "Respectfully, the S&P 500 closed at 6,712 and fell 1.2% today, my friend."))
        writer = self.writer([stranger, "not json at all", script(("dottie", "Hi."))], attempts=3)
        with self.assertRaises(ScriptError) as caught:
            writer.write(self.segment())
        self.assertEqual(len(self.server.requests), 3)
        self.assertTrue(any("Too short" in p for p in caught.exception.problems))

    def test_review_catches_strangers_length_and_missing_fields(self):
        writer = self.writer([])
        segment = self.segment(fields={"prediction": "an object"}, fact_check=False)
        problems, _, _ = writer.review(script(("ying", "word " * 200)), segment)
        joined = " | ".join(problems)
        self.assertIn("Unknown speakers ying", joined)
        self.assertIn("Too long", joined)
        self.assertIn("Missing keys: prediction", joined)

    def test_parse_reply_tolerates_fences(self):
        self.assertEqual(parse_reply('Sure!\n```json\n{"lines": []}\n```'), {"lines": []})
        with self.assertRaises(ValueError):
            parse_reply("no json here")


class FactCheckTests(unittest.TestCase):
    def test_rounding_but_no_invention(self):
        facts = "S&P 500 6712.34. Yield 4.253%. Payrolls 1,234,567. Kickoff 8:30. Chiefs 27, Bills 24."
        good = "The S&P is at 6,712. The yield is 4.25%. Payrolls 1.2 million. Kickoff at 8:30. Final, 27-24."
        self.assertTrue(fc.check(good, facts).ok)
        for bad in ("The S&P is at 6,700.", "The yield is 4.26%.", "Payrolls 1.3 million.", "Kickoff at 9:30.",
                    "Final, 28-24."):
            self.assertFalse(fc.check(bad, facts).ok, bad)

    def test_names(self):
        facts = "Kansas City Chiefs 27, Buffalo Bills 24. Johann Sebastian Bach, Goldberg Variations."
        self.assertTrue(fc.check("The Chiefs' defense held. Bach's Goldberg Variations, all thirty.", facts).ok)
        result = fc.check("Respectfully, Patrick Mahomes was brilliant.", facts)
        self.assertEqual(result.missing_names, ["Patrick Mahomes"])
        self.assertTrue(fc.check("Breathe with me, Ying.", facts, allow_names=["Ying"]).ok)

    def test_fifty_planted_wrong_numbers_are_all_blocked(self):
        # The development plan's Phase 5 gate, in miniature.
        rng = random.Random(7)
        for case in range(50):
            close, change, yield_ = round(rng.uniform(4000, 7000), 2), round(rng.uniform(-3, 3), 2), round(rng.uniform(3, 5), 3)
            facts = f"S&P 500 closed at {close}, change {change}%. 10-year yield {yield_}%."
            honest = f"The S&P closed at {close:,.2f}, {abs(change)}% on the day. The ten-year is at {yield_}%."
            self.assertTrue(fc.check(honest, facts).ok, honest)
            planted = honest.replace(f"{close:,.2f}", f"{close + rng.choice([-10, 7, 1.5, 100]):,.2f}")
            self.assertFalse(fc.check(planted, facts).ok, planted)


if __name__ == "__main__":
    unittest.main()
