"""The voice layer: voice scripts through engine adapters, with fallbacks."""

import http.server
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mac import audio_tools as at
from mac import voices as v

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


class FakeSpeech(http.server.ThreadingHTTPServer):
    """An OpenAI-compatible /v1/audio/speech server: 0.25 s of tone per word."""

    def __init__(self, root: Path):
        super().__init__(("127.0.0.1", 0), FakeSpeechHandler)
        self.root = root
        self.requests: list[dict] = []
        self.fail_models: set[str] = set()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class FakeSpeechHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        if body.get("model") in self.server.fail_models:
            self.send_response(500)
            self.end_headers()
            return
        path = self.server.root / f"r{len(self.server.requests)}.wav"
        at.tone(path, max(0.3, 0.25 * len(body["input"].split())), 300 + 50 * len(self.server.requests))
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def write_config(root: Path, base: str) -> tuple[Path, Path]:
    voices = root / "voices.yaml"
    tone_script = root / "tone.py"
    tone_script.write_text("import subprocess, sys\n"
                           "subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', "
                           "'sine=frequency=500:duration=1', sys.argv[1]], check=True)\n")
    voices.write_text(f"""
engines:
  qwen3: {{transport: openai, base_url: "{base}", model: qwen3-tts, style: qwen3}}
  chatterbox: {{transport: openai, base_url: "{base}", model: chatterbox, style: chatterbox, nonverbal_format: "[{{tag}}]"}}
  dia: {{transport: openai, base_url: "{base}", model: dia, style: dia, nonverbal_format: "({{tag}})", max_dialogue_words: 40}}
  dramabox: {{transport: command, command: "{sys.executable} {tone_script} {{out}}", style: dramabox}}
  kokoro: {{transport: kokoro, style: plain}}
voices:
  count: {{engine: chatterbox, voice: count, delivery: "slow, velvet", nonverbals: [chuckle], words_per_second: 2.2,
          fallback: {{engine: kokoro, voice: bm_george}}}}
  dottie: {{engine: qwen3, voice: dottie, delivery: "brisk, wry", fallback: {{engine: kokoro, voice: af_sarah}}}}
  ying: {{engine: dia, voice: S1, nonverbals: [laughs], fallback: {{engine: kokoro, voice: am_puck}}}}
  yang: {{engine: dia, voice: S2, fallback: {{engine: kokoro, voice: am_michael}}}}
  hushworth: {{engine: dramabox, voice: hushworth, reference: hushworth.wav}}
""")
    pron = root / "pronunciations.yaml"
    pron.write_text("global: {Dvořák: DVOR-zhahk}\nabbreviations: {'No.': Number, 'Op.': Opus, 'S&P': S and P}\n")
    return voices, pron


class ScriptTests(unittest.TestCase):
    def test_voice_script_validation(self):
        script = v.VoiceScript.from_json({"title": "Cold open", "prediction": {"claim": "x"}, "lines": [
            {"speaker": "Ying", "text": "Thirty!", "intensity": 7, "pause_before_ms": -5}]}, segment="yy")
        line = script.lines[0]
        self.assertEqual((line.speaker, line.intensity, line.pause_before_ms), ("ying", 1.0, 0))
        self.assertEqual(script.extra, {"prediction": {"claim": "x"}})
        self.assertEqual(script.to_json()["segment"], "yy")
        for bad in ({}, {"lines": []}, {"lines": [{"speaker": "ying"}]}):
            with self.assertRaises(v.VoiceError):
                v.VoiceScript.from_json(bad)

    def test_respell(self):
        table = {"Dvořák": "DVOR-zhahk", "No.": "Number", "Op.": "Opus"}
        self.assertEqual(v.respell("Dvořák's Symphony No. 9, Op. 95", table), "DVOR-zhahk's Symphony Number 9, Opus 95")
        self.assertEqual(v.respell("No. I disagree.", table), "No. I disagree.")

    def test_real_config_is_complete(self):
        engines, cards = v.load_voices()
        for host in ("count", "dottie", "ying", "yang", "wit", "steve", "professor_ex", "hushworth", "logan", "station"):
            self.assertIn(host, cards)
            self.assertEqual(cards[host].fallback_engine, "kokoro")
        self.assertEqual(engines["dia"].style, "dia")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class RenderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.server = FakeSpeech(self.root / "served")
        self.server.root.mkdir()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        voices, pron = write_config(self.root, self.server.base)
        self.renderer = v.Renderer.from_config(voices, pron, voices_dir=self.root / "refs")

    def test_styles_translate_each_line(self):
        r = self.renderer
        count = v.Line("count", "That was Dvořák, No. 9.", emotion="amused", intensity=0.6, nonverbal="chuckle")
        text, extras, voice = r.translate(r.engines["chatterbox"], [count])
        self.assertEqual((text, extras, voice), ("[chuckle] That was DVOR-zhahk, Number 9.", {"exaggeration": 0.7}, "count"))
        dottie = v.Line("dottie", "The S&P closed up.", emotion="wry", pace="brisk")
        self.assertEqual(r.translate(r.engines["qwen3"], [dottie])[1], {"instructions": "brisk, wry, brisk, wry"})
        lines = [v.Line("ying", "Thirty!", nonverbal="laughs"), v.Line("yang", "Breathe.", nonverbal="sneezes")]
        self.assertEqual(r.translate(r.engines["dia"], lines)[0], "[S1] (laughs) Thirty! [S2] Breathe.")
        reading = v.Line("hushworth", "Once upon a time.", emotion="whispering")
        self.assertEqual(r.translate(r.engines["dramabox"], [reading])[0], "HUSHWORTH (whispering): Once upon a time.")

    def test_dialogue_renders_together_and_is_loudness_matched(self):
        script = v.VoiceScript("yy", [
            v.Line("ying", "They lost by thirty. Thirty!", intensity=0.9),
            v.Line("yang", "Breathe with me.", pause_before_ms=400),
            v.Line("ying", "I am breathing. Angrily."),
            v.Line("dottie", "Here's the number.", pause_before_ms=600),
        ])
        result = self.renderer.render(script, self.root / "out" / "yy.flac")
        self.assertEqual([r["model"] for r in self.server.requests], ["dia", "qwen3-tts"])
        self.assertEqual(self.server.requests[0]["input"],
                         "[S1] They lost by thirty. Thirty! [S2] Breathe with me. [S1] I am breathing. Angrily.")
        self.assertEqual(result.engines, ["dia", "qwen3"])
        self.assertAlmostEqual(float(at.measure(result.path)["input_i"]), -16.0, delta=0.6)
        self.assertGreater(result.seconds, 3.0)
        self.assertFalse(list(result.path.parent.glob(".*partial*")))

    def test_engine_down_falls_back_line_by_line(self):
        self.server.fail_models.add("dia")

        def fake_kokoro(text, path, voice="am_michael", speed=1.0):
            at.tone(Path(path), 0.5)
            fake_kokoro.calls.append(voice)
            return True

        fake_kokoro.calls = []
        script = v.VoiceScript("yy", [v.Line("ying", "Thirty!"), v.Line("yang", "Breathe.")])
        with patch.object(v, "_load_kokoro", return_value=type("K", (), {"render_speech": staticmethod(fake_kokoro)})):
            result = self.renderer.render(script, self.root / "fallback.flac")
        self.assertEqual(fake_kokoro.calls, ["am_puck", "am_michael"])
        self.assertTrue(result.engines[0].startswith("kokoro (fallback; dia: dia: HTTP 500"))

    def test_command_engine_and_unknown_speaker(self):
        result = self.renderer.render(v.VoiceScript("story", [v.Line("hushworth", "Once upon a time.")]),
                                      self.root / "story.wav")
        self.assertEqual(result.engines, ["dramabox"])
        self.assertAlmostEqual(result.seconds, 1.0, delta=0.1)
        with self.assertRaises(v.VoiceError):
            self.renderer.render(v.VoiceScript("x", [v.Line("nobody", "Hello.")]), self.root / "x.wav")


if __name__ == "__main__":
    unittest.main()
