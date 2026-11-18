"""The real liquidsoap/wit_next.liq, end to end, writing to files.

This is the Phase 1 gate from the development plan: a FLAC at unity gain
comes out of the lossless output bit for bit. (Every 16-bit value except
-32768, which Liquidsoap's float pipeline clamps to -32767; that is pinned
down here too.) It also checks the per-work
gain, the silence between works, movements played back to back, the
director callbacks, the fall back to the safe library when the director
stops answering, and that many short titled tracks don't crash the Ogg FLAC
encoder (Liquidsoap 2.2.4 segfaulted there until the lossless branch
dropped metadata).

Skipped where Liquidsoap or FFmpeg is missing. It plays about half a minute
in real time.
"""

import array
import json
import math
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from mac.alerts import Alerter
from mac.playout_director import Item, make_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "liquidsoap" / "wit_next.liq"
RATE = 44100
FRAME = 4  # bytes in one 16-bit stereo frame
GAIN_DB = -6.0
HAVE_TOOLS = all(shutil.which(tool) for tool in ("liquidsoap", "ffmpeg", "ffprobe"))


def noise(seconds: float, seed: int) -> array.array:
    """Full-scale white noise (-32767..32767) with no sample near zero, so
    silence boundaries stay exact even after -6 dB, and an asymmetric
    int/float conversion anywhere in the chain would show up as off-by-one
    samples."""
    rng = random.Random(seed)
    samples = array.array("h")
    for _ in range(round(seconds * RATE) * 2):
        value = rng.randint(-32767, 32767)
        samples.append(value if abs(value) >= 64 else 64)
    return samples


def write_flac(path: Path, samples: array.array) -> Path:
    data = array.array("h", samples)
    if sys.byteorder == "big":
        data.byteswap()
    wav = path.with_suffix(".wav")
    with wave.open(str(wav), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(data.tobytes())
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav), "-c:a", "flac", str(path)], check=True)
    wav.unlink()
    return path


def decode(path: Path) -> bytes:
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-acodec", "pcm_s16le", "-"],
                         capture_output=True).stdout
    if sys.byteorder == "big":
        swapped = array.array("h", pcm)
        swapped.byteswap()
        return swapped.tobytes()
    return pcm


def le_bytes(samples: array.array) -> bytes:
    data = array.array("h", samples)
    if sys.byteorder == "big":
        data.byteswap()
    return data.tobytes()


class ScriptedDirector:
    """Stands in for the real director behind the real HTTP service: serves a
    fixed list, then fails (so Liquidsoap falls back to the safe library),
    and records the /aired callbacks."""

    def __init__(self, state_dir: Path, items: list[Item]):
        self.state_dir = state_dir
        self.station = SimpleNamespace(id="wit-next", call_sign="WIT-FM Test")
        self.queue = list(items)
        self.aired_calls: list[tuple[str, str]] = []

    def next_item(self, now=None) -> Item:
        if not self.queue:
            raise LookupError("the scripted list is finished")  # answered with HTTP 503
        return self.queue.pop(0)

    def aired(self, entry: str, path: str, now=None) -> dict:
        self.aired_calls.append((entry, path))
        return {"entry": entry}


@unittest.skipUnless(HAVE_TOOLS, "needs liquidsoap, ffmpeg and ffprobe")
class LiquidsoapPlayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        media, library, out = root / "media", root / "library", root / "out"
        safe_dir = library / "air" / "safe" / "some-track"  # where the catalog puts the "safe" source
        for folder in (media, safe_dir, library / "ids", out):
            folder.mkdir(parents=True)

        context = dict(show_id="classical_day_shift", show_title="Classical: Day Shift", license="cc0",
                       lossless=True, composer="Test Composer", credit="Test Ensemble (CC0)")
        cls.audio: dict[str, array.array] = {}
        cls.items: list[Item] = []

        def work(name: str, seconds: float, seed: int, entry: str, gain: float = 0.0, title: str = "",
                 floor: bool = False) -> None:
            cls.audio[name] = noise(seconds, seed)
            if floor:  # a few samples at -32768, the one value the chain can't carry
                for index in range(1000, 1010):
                    cls.audio[name][index] = -32768
            path = write_flac(media / f"{name}.flac", cls.audio[name])
            cls.items.append(Item(kind="work", path=str(path), seconds=seconds, title=title or name,
                                  gain_db=gain, entry_id=entry, **context))

        def gap(seconds: float, entry: str) -> None:
            path = write_flac(media / f"gap_{len(cls.items)}.flac", array.array("h", bytes(round(seconds * RATE) * FRAME)))
            cls.items.append(Item(kind="gap", path=str(path), seconds=seconds, entry_id=entry,
                                  show_id=context["show_id"]))

        work("A", 3.0, 1, "test#0")
        gap(3.0, "test#1")
        work("B", 3.0, 2, "test#2", gain=GAIN_DB)
        gap(2.5, "test#3")
        work("C1", 1.5, 3, "test#4.0")
        work("C2", 1.5, 4, "test#4.1")
        gap(2.0, "test#5")
        cls.shorts = [f"S{i}" for i in range(16)]
        for i, name in enumerate(cls.shorts):
            # Quotes, a backslash, a colon and accents go through Liquidsoap's annotate parser.
            work(name, 0.5, 10 + i, f"short#{i}", title=f'Étude "{i}" in C: Dvořák\\Test',
                 floor=(i == 5))
        cls.audio["safe"] = noise(4.0, 99)
        write_flac(safe_dir / "safe.flac", cls.audio["safe"])

        cls.director = ScriptedDirector(root / "state", cls.items)
        server = make_server(cls.director, 0, alerter=Alerter(root / "alerts", env={}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {k: v for k, v in os.environ.items() if k not in ("WRIT_SAFE_DIR", "WRIT_IDS_DIR")}
        env = {**env, "WRIT_PLAYOUT_TO_FILES": str(out), "WRIT_PLAYOUT_ALLOW_ROOT": "1",
               "WRIT_DIRECTOR_PORT": str(server.server_address[1]), "WRIT_LIBRARY_DIR": str(library),
               "WRIT_PLAYOUT_LOG_LEVEL": "3"}
        log_path = root / "liquidsoap.log"
        with open(log_path, "w") as log:
            proc = subprocess.Popen(["liquidsoap", str(SCRIPT)], env=env, cwd=root, stdout=log,
                                    stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and proc.poll() is None:
                    if any(path.endswith("safe.flac") for _, path in cls.director.aired_calls):
                        time.sleep(2.5)  # let the safe library play for a moment
                        break
                    time.sleep(0.2)
                cls.alive = proc.poll() is None
                proc.send_signal(signal.SIGTERM)
                cls.returncode = proc.wait(timeout=30)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                server.shutdown()
                server.server_close()
        cls.log = log_path.read_text(errors="replace")
        cls.lossless = decode(out / "lossless.ogg") if (out / "lossless.ogg").exists() else b""
        cls.mp3_path = out / "next.mp3"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def tail(self) -> str:
        return self.log[-4000:]

    def zero_frames(self, pos: int) -> int:
        rest = self.lossless[pos:]
        return (len(rest) - len(rest.lstrip(b"\x00"))) // FRAME

    def test_ran_without_crashing_and_shut_down_cleanly(self):
        self.assertTrue(self.alive, f"Liquidsoap exited on its own with code {self.returncode} "
                                    f"(-11 is a segfault); log tail:\n{self.tail()}")
        self.assertEqual(self.returncode, 0, self.tail())
        self.assertNotIn("Segmentation fault", self.log)

    def test_unity_work_is_bit_exact_in_the_lossless_output(self):
        start = self.lossless.find(le_bytes(self.audio["A"]))
        self.assertGreaterEqual(start, 0, "the unity-gain FLAC is not in the lossless output bit for bit")
        self.assertEqual(start % FRAME, 0)

    def test_gain_gaps_movements_and_short_tracks_land_exactly(self):
        data = self.lossless
        pos = data.find(le_bytes(self.audio["A"]))
        self.assertGreaterEqual(pos, 0)
        pos += len(self.audio["A"]) * 2

        gap = self.zero_frames(pos)
        self.assertEqual(gap, 3 * RATE, "silence after the first work")
        pos += gap * FRAME

        # One fixed gain for the whole work: every sample within 1 LSB of
        # source * gain. No fades, no limiting.
        source = self.audio["B"]
        aired = array.array("h", data[pos:pos + len(source) * 2])
        if sys.byteorder == "big":
            aired.byteswap()
        gain = 10 ** (GAIN_DB / 20)
        worst = max(abs(a - s * gain) for a, s in zip(aired, source))
        self.assertLessEqual(worst, 1.0)
        rms = lambda xs: math.sqrt(sum(x * x for x in xs) / len(xs))  # noqa: E731
        self.assertAlmostEqual(20 * math.log10(rms(aired) / rms(source)), GAIN_DB, delta=0.02)
        pos += len(source) * 2

        gap = self.zero_frames(pos)
        self.assertEqual(gap, int(2.5 * RATE), "silence after the -6 dB work")
        pos += gap * FRAME

        movements = le_bytes(self.audio["C1"]) + le_bytes(self.audio["C2"])
        self.assertEqual(data[pos:pos + len(movements)], movements, "movements back to back, bit for bit")
        pos += len(movements)

        gap = self.zero_frames(pos)
        self.assertEqual(gap, 2 * RATE, "silence after the two-movement work")
        pos += gap * FRAME

        # Liquidsoap 2.2 carries samples as floats scaled by 32767 and clips
        # them to [-1, 1], so -32768 airs as -32767 (1 LSB, at negative full
        # scale). Every other value is exact; S5 holds a few -32768 samples.
        clamped = lambda xs: array.array("h", (max(x, -32767) for x in xs))  # noqa: E731
        shorts = b"".join(le_bytes(clamped(self.audio[name])) for name in self.shorts)
        self.assertEqual(data[pos:pos + len(shorts)], shorts,
                         "sixteen short titled tracks, bit for bit (with -32768 clamped to -32767)")
        pos += len(shorts)

        # The director then fails: the safe library takes over at once.
        safe = le_bytes(self.audio["safe"])
        found = data.find(safe[RATE * FRAME // 2: RATE * FRAME * 3 // 2], pos)
        self.assertGreaterEqual(found, 0, "the safe library never aired")
        self.assertLessEqual(found - pos, RATE * FRAME, "the safe library came in late")

    def test_director_hears_every_item_in_order(self):
        served = [(item.entry_id, item.path) for item in self.items]
        self.assertEqual(self.director.aired_calls[:len(served)], served)
        entry, path = self.director.aired_calls[len(served)]
        self.assertEqual((entry, Path(path).name), ("", "safe.flac"))

    def test_mp3_output_is_320_kbps_stereo(self):
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                "stream=codec_name,bit_rate,sample_rate,channels:format=duration",
                                "-of", "json", str(self.mp3_path)], capture_output=True, text=True)
        info = json.loads(probe.stdout or "{}")
        stream = info["streams"][0]
        self.assertEqual((stream["codec_name"], stream["sample_rate"], stream["channels"]), ("mp3", "44100", 2))
        self.assertEqual(stream["bit_rate"], "320000")
        self.assertGreater(float(info["format"]["duration"]), 24.5)


if __name__ == "__main__":
    unittest.main()
