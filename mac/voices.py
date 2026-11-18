#!/usr/bin/env python3
"""Voice scripts to audio, through whichever engine suits each host.

The script model writes a voice script, not plain text (show bible: Local AI
stack). Each line carries a speaker and text, and optionally an emotion,
intensity, pace, a nonverbal cue and a pause:

    {"segment": "yy_cold_open", "lines": [
      {"speaker": "ying", "text": "They lost by thirty. Thirty!",
       "emotion": "outraged", "intensity": 0.9, "pace": "fast"},
      {"speaker": "yang", "text": "Breathe with me.", "emotion": "serene",
       "intensity": 0.3, "pace": "slow", "pause_before_ms": 400}]}

config/voices.yaml gives each host a card (engine, voice, reference clip,
delivery, nonverbals, fallback) and each engine an adapter style. Hard names
are respelled from config/pronunciations.yaml before synthesis. Every chunk
is resampled and loudness-matched, then the segment is joined with its
pauses and normalized again.

    python3 mac/voices.py say --host count "Good evening, night creatures."
    python3 mac/voices.py check
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_tools as at  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VOICES_PATH = PROJECT_ROOT / "config" / "voices.yaml"
PRONUNCIATIONS_PATH = PROJECT_ROOT / "config" / "pronunciations.yaml"
KOKORO_TTS = PROJECT_ROOT / "mac" / "kokoro" / "tts.py"
STYLES = ("plain", "qwen3", "chatterbox", "dia", "dramabox")
TRANSPORTS = ("openai", "command", "kokoro")
DEFAULT_GAP_MS = 250


class VoiceError(RuntimeError):
    pass


# ── voice scripts ────────────────────────────────────────────────────────────


@dataclass
class Line:
    speaker: str
    text: str
    emotion: str = ""
    intensity: float | None = None
    pace: str = ""
    nonverbal: str = ""
    pause_before_ms: int = 0

    @classmethod
    def from_json(cls, data: Any) -> "Line":
        if not isinstance(data, dict):
            raise VoiceError(f"a line must be an object, got {data!r}")
        speaker, text = str(data.get("speaker", "")).strip().lower(), str(data.get("text", "")).strip()
        if not speaker or not text:
            raise VoiceError(f"a line needs a speaker and text: {data!r}")
        intensity = data.get("intensity")
        try:
            intensity = None if intensity in (None, "") else max(0.0, min(1.0, float(intensity)))
        except (TypeError, ValueError):
            intensity = None
        try:
            pause = max(0, min(5000, int(data.get("pause_before_ms") or 0)))
        except (TypeError, ValueError):
            pause = 0
        return cls(speaker=speaker, text=text, emotion=str(data.get("emotion") or "").strip(),
                   intensity=intensity, pace=str(data.get("pace") or "").strip(),
                   nonverbal=str(data.get("nonverbal") or "").strip().lower(), pause_before_ms=pause)

    def to_json(self) -> dict:
        data = {"speaker": self.speaker, "text": self.text}
        for key in ("emotion", "pace", "nonverbal"):
            if getattr(self, key):
                data[key] = getattr(self, key)
        if self.intensity is not None:
            data["intensity"] = self.intensity
        if self.pause_before_ms:
            data["pause_before_ms"] = self.pause_before_ms
        return data

    @property
    def words(self) -> int:
        return len(self.text.split())


@dataclass
class VoiceScript:
    segment: str
    lines: list[Line]
    title: str = ""
    extra: dict = field(default_factory=dict)  # anything else the model returned, e.g. a prediction

    @classmethod
    def from_json(cls, data: Any, segment: str = "") -> "VoiceScript":
        if not isinstance(data, dict) or not isinstance(data.get("lines"), list) or not data["lines"]:
            raise VoiceError("a voice script needs a non-empty \"lines\" list")
        extra = {k: v for k, v in data.items() if k not in ("segment", "title", "lines")}
        return cls(segment=str(data.get("segment") or segment), title=str(data.get("title") or ""),
                   lines=[Line.from_json(line) for line in data["lines"]], extra=extra)

    def to_json(self) -> dict:
        return {"segment": self.segment, "title": self.title, "lines": [l.to_json() for l in self.lines], **self.extra}

    @property
    def text(self) -> str:
        return " ".join(line.text for line in self.lines)

    @property
    def words(self) -> int:
        return sum(line.words for line in self.lines)

    @property
    def speakers(self) -> list[str]:
        return sorted({line.speaker for line in self.lines})


# ── configuration ────────────────────────────────────────────────────────────


@dataclass
class Engine:
    name: str
    transport: str
    style: str = "plain"
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    command: str = ""
    timeout: float = 300.0
    nonverbal_format: str = ""
    max_dialogue_words: int = 70
    extra_fields: bool = True


@dataclass
class VoiceCard:
    host: str
    engine: str
    voice: str = ""
    reference: str = ""
    delivery: str = ""
    nonverbals: tuple[str, ...] = ()
    speed: float = 1.0
    words_per_second: float = 2.5
    fallback_engine: str = ""
    fallback_voice: str = ""


def load_voices(path: Path = VOICES_PATH) -> tuple[dict[str, Engine], dict[str, VoiceCard]]:
    import yaml

    data = yaml.safe_load(Path(path).read_text()) or {}
    engines: dict[str, Engine] = {}
    for name, raw in (data.get("engines") or {}).items():
        raw = dict(raw or {})
        engine = Engine(name=name, transport=str(raw.get("transport", "")), style=str(raw.get("style", "plain")),
                        base_url=str(raw.get("base_url", "")), model=str(raw.get("model", "")),
                        api_key_env=str(raw.get("api_key_env", "")), command=str(raw.get("command", "")),
                        timeout=float(raw.get("timeout", 300)), nonverbal_format=str(raw.get("nonverbal_format", "")),
                        max_dialogue_words=int(raw.get("max_dialogue_words", 70)),
                        extra_fields=bool(raw.get("extra_fields", True)))
        if engine.transport not in TRANSPORTS:
            raise VoiceError(f"engine {name}: transport must be one of {', '.join(TRANSPORTS)}")
        if engine.style not in STYLES:
            raise VoiceError(f"engine {name}: style must be one of {', '.join(STYLES)}")
        engines[name] = engine
    cards: dict[str, VoiceCard] = {}
    for host, raw in (data.get("voices") or {}).items():
        raw = dict(raw or {})
        fallback = raw.get("fallback") or {}
        card = VoiceCard(host=host, engine=str(raw.get("engine", "")), voice=str(raw.get("voice", "")),
                         reference=str(raw.get("reference", "")), delivery=str(raw.get("delivery", "")),
                         nonverbals=tuple(str(n).lower() for n in raw.get("nonverbals") or ()),
                         speed=float(raw.get("speed", 1.0)), words_per_second=float(raw.get("words_per_second", 2.5)),
                         fallback_engine=str(fallback.get("engine", "")), fallback_voice=str(fallback.get("voice", "")))
        for name in filter(None, (card.engine, card.fallback_engine)):
            if name not in engines:
                raise VoiceError(f"voice {host}: no engine called {name}")
        cards[host] = card
    return engines, cards


def load_pronunciations(path: Path = PRONUNCIATIONS_PATH) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(Path(path).read_text()) if Path(path).exists() else {}
    data = data or {}
    return {"global": dict(data.get("global") or {}), "abbreviations": dict(data.get("abbreviations") or {}),
            "hosts": {k: dict(v or {}) for k, v in (data.get("hosts") or {}).items()}}


def respell(text: str, table: dict[str, str]) -> str:
    """Whole-word replacements, longest first; the replacement is never re-scanned.

    A key ending in a period ("No.", "Op.") is a catalog abbreviation and only
    expands before a number, so "No. I disagree." stays as written."""
    if not table:
        return text
    keys = sorted(table, key=len, reverse=True)

    def pattern_for(key: str) -> str:
        if key.endswith("."):
            return rf"(?<![\w-]){re.escape(key)}(?=\s*\d)"
        return rf"(?<![\w-]){re.escape(key)}(?![\w-])"

    pattern = re.compile("|".join(pattern_for(k) for k in keys))
    return pattern.sub(lambda m: str(table[m.group(0)]), text)


def _load_kokoro():
    spec = importlib.util.spec_from_file_location("witfm_kokoro_tts", KOKORO_TTS)
    if spec is None or spec.loader is None:
        raise VoiceError("mac/kokoro/tts.py is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── rendering ────────────────────────────────────────────────────────────────


@dataclass
class Rendered:
    path: Path
    seconds: float
    engines: list[str]
    words: int


class Renderer:
    def __init__(self, engines: dict[str, Engine], cards: dict[str, VoiceCard], pronunciations: dict[str, Any],
                 voices_dir: Path | None = None):
        self.engines = engines
        self.cards = cards
        self.pron = pronunciations
        self.voices_dir = Path(voices_dir or os.environ.get("WRIT_VOICES_DIR") or Path.home() / ".writ" / "voices")

    @classmethod
    def from_config(cls, voices_path: Path = VOICES_PATH, pronunciations_path: Path = PRONUNCIATIONS_PATH,
                    voices_dir: Path | None = None) -> "Renderer":
        engines, cards = load_voices(voices_path)
        return cls(engines, cards, load_pronunciations(pronunciations_path), voices_dir)

    def card(self, speaker: str) -> VoiceCard:
        try:
            return self.cards[speaker]
        except KeyError:
            raise VoiceError(f"no voice card for {speaker!r}") from None

    def words_per_second(self, speaker: str) -> float:
        return self.card(speaker).words_per_second

    def render(self, script: VoiceScript, dest: Path, *, target_lufs: float = -16.0) -> Rendered:
        """Render to dest (FLAC or WAV by suffix)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="voice-") as tmp:
            parts, gaps, used = [], [], []
            for index, (engine_name, lines) in enumerate(self._chunks(script)):
                raw = Path(tmp) / f"chunk{index:03d}.wav"
                used.append(self._synthesize(engine_name, lines, raw))
                parts.append(at.normalize(raw, Path(tmp) / f"level{index:03d}.wav", target_lufs))
                gaps.append(lines[0].pause_before_ms or (DEFAULT_GAP_MS if index else 0))
            joined = at.concat(parts, Path(tmp) / "joined.wav", gaps)
            partial = dest.with_name(f".{dest.name}.partial{dest.suffix}")
            at.normalize(joined, partial, target_lufs)
            partial.replace(dest)
        return Rendered(path=dest, seconds=at.duration(dest), engines=used, words=script.words)

    def _chunks(self, script: VoiceScript) -> list[tuple[str, list[Line]]]:
        """One chunk per line, except that a dialogue engine takes consecutive lines of its speakers."""
        chunks: list[tuple[str, list[Line]]] = []
        for line in script.lines:
            engine = self.engines[self.card(line.speaker).engine]
            if engine.style == "dia" and chunks and chunks[-1][0] == engine.name:
                current = chunks[-1][1]
                speakers = {l.speaker for l in current} | {line.speaker}
                if (len(speakers) <= 2 and line.pause_before_ms < 1000
                        and sum(l.words for l in current) + line.words <= engine.max_dialogue_words):
                    current.append(line)
                    continue
            chunks.append((engine.name, [line]))
        return chunks

    def _synthesize(self, engine_name: str, lines: list[Line], wav: Path) -> str:
        try:
            self._call(self.engines[engine_name], lines, wav)
            return engine_name
        except VoiceError as exc:
            problem = str(exc)
        parts, gaps, names = [], [], []
        for index, line in enumerate(lines):  # fall back line by line
            card = self.card(line.speaker)
            if not card.fallback_engine or card.fallback_engine == engine_name:
                raise VoiceError(f"{engine_name} failed for {line.speaker} and there is no fallback: {problem}")
            part = wav.with_name(f"{wav.stem}-fallback{index}.wav")
            self._call(self.engines[card.fallback_engine], [line], part, voice=card.fallback_voice)
            parts.append(part)
            gaps.append(line.pause_before_ms or (DEFAULT_GAP_MS if index else 0))
            names.append(card.fallback_engine)
        at.concat(parts, wav, gaps)
        return f"{'+'.join(sorted(set(names)))} (fallback; {engine_name}: {problem[:120]})"

    def _call(self, engine: Engine, lines: list[Line], wav: Path, voice: str | None = None) -> None:
        text, extras, chosen = self.translate(engine, lines, voice)
        if engine.transport == "openai":
            self._openai(engine, chosen, text, extras, wav)
        elif engine.transport == "command":
            self._command(engine, lines, chosen, text, extras, wav)
        else:
            self._kokoro(chosen, text, wav)
        if not wav.exists() or wav.stat().st_size < 512:
            raise VoiceError(f"{engine.name} produced no audio")

    def spoken(self, line: Line, engine: Engine) -> str:
        """The text the engine reads: abbreviations expanded, names respelled, nonverbal tagged."""
        card = self.card(line.speaker)
        text = respell(line.text, {str(k): str(v) for k, v in self.pron["abbreviations"].items()})
        text = respell(text, {**self.pron["global"], **self.pron["hosts"].get(line.speaker, {})})
        # Only the card's own nonverbals: an engine handed an unknown cue may read it aloud.
        if line.nonverbal and line.nonverbal in card.nonverbals and engine.nonverbal_format:
            text = f"{engine.nonverbal_format.format(tag=line.nonverbal)} {text}"
        return text

    def translate(self, engine: Engine, lines: list[Line], voice: str | None = None) -> tuple[str, dict, str]:
        first, card = lines[0], self.card(lines[0].speaker)
        extras: dict[str, Any] = {}
        if engine.style == "dia":
            text = " ".join(f"[{self.card(l.speaker).voice or 'S1'}] {self.spoken(l, engine)}" for l in lines)
        elif engine.style == "dramabox":
            text = "\n".join(f"{l.speaker.upper()}{f' ({l.emotion})' if l.emotion else ''}: {self.spoken(l, engine)}"
                             for l in lines)
        else:
            text = " ".join(self.spoken(l, engine) for l in lines)
        if engine.style == "qwen3":
            extras["instructions"] = ", ".join(x for x in (first.pace, first.emotion, card.delivery) if x)
        if engine.style == "chatterbox" and first.intensity is not None:
            extras["exaggeration"] = round(0.25 + 0.75 * first.intensity, 2)
        if card.speed != 1.0:
            extras["speed"] = card.speed
        return text, extras, voice or card.voice

    def _openai(self, engine: Engine, voice: str, text: str, extras: dict, wav: Path) -> None:
        payload = {"model": engine.model, "voice": voice, "input": text, "response_format": "wav"}
        if engine.extra_fields:
            payload.update(extras)
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(engine.api_key_env, "") if engine.api_key_env else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(f"{engine.base_url.rstrip('/')}/audio/speech", data=json.dumps(payload).encode(),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=engine.timeout) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            raise VoiceError(f"{engine.name}: HTTP {exc.code} {exc.read()[:200]!r}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise VoiceError(f"{engine.name}: {getattr(exc, 'reason', exc)}") from exc
        raw = wav.with_suffix(".download")
        raw.write_bytes(data)
        try:
            at.to_wav(raw, wav)
        except at.AudioError as exc:
            raise VoiceError(f"{engine.name} returned something that isn't audio") from exc
        finally:
            raw.unlink(missing_ok=True)

    def _command(self, engine: Engine, lines: list[Line], voice: str, text: str, extras: dict, wav: Path) -> None:
        card = self.card(lines[0].speaker)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write(text)
        values = {"text_file": handle.name, "out": str(wav), "voice": voice,
                  "reference": str(self.voices_dir / card.reference) if card.reference else "",
                  "instructions": str(extras.get("instructions", "")), "exaggeration": str(extras.get("exaggeration", ""))}
        try:
            args = [part.format(**values) for part in shlex.split(engine.command)]
            result = subprocess.run(args, capture_output=True, text=True, timeout=engine.timeout)
        except (OSError, subprocess.TimeoutExpired, KeyError) as exc:
            raise VoiceError(f"{engine.name}: {exc}") from exc
        finally:
            Path(handle.name).unlink(missing_ok=True)
        if result.returncode != 0:
            raise VoiceError(f"{engine.name} exited {result.returncode}: {result.stderr.strip()[-200:]}")

    def _kokoro(self, voice: str, text: str, wav: Path) -> None:
        try:
            ok = _load_kokoro().render_speech(text, wav, voice=voice or "am_michael")
        except Exception as exc:  # the Kokoro wrapper reports failures in many ways
            raise VoiceError(f"kokoro: {exc}") from exc
        if not ok:
            raise VoiceError("kokoro could not render (is mac/kokoro/.venv set up?)")


# ── command line ─────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render host voices on the Halo")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_say = sub.add_parser("say", help="render one line for a host, to audition a voice")
    p_say.add_argument("--host", required=True)
    p_say.add_argument("--out", type=Path)
    p_say.add_argument("--emotion", default="")
    p_say.add_argument("--intensity", type=float)
    p_say.add_argument("text")
    sub.add_parser("check", help="see which engine servers answer")
    args = parser.parse_args(argv)

    renderer = Renderer.from_config()
    if args.cmd == "say":
        out = args.out or Path(f"{args.host}-say.flac")
        line = Line(speaker=args.host, text=args.text, emotion=args.emotion, intensity=args.intensity)
        result = renderer.render(VoiceScript(segment="say", lines=[line]), out)
        print(f"{result.path}: {result.seconds:.1f} s via {', '.join(result.engines)}")
        return 0
    for name, engine in renderer.engines.items():
        if engine.transport == "openai":
            try:
                with urllib.request.urlopen(f"{engine.base_url.rstrip('/')}/models", timeout=5):
                    status = "answers"
            except urllib.error.HTTPError as exc:
                status = f"answers (HTTP {exc.code} on /models)"
            except Exception as exc:
                status = f"not reachable ({getattr(exc, 'reason', exc)})"
        elif engine.transport == "command":
            status = f"command: {engine.command.split()[0] if engine.command else '(none)'}"
        else:
            status = "kokoro (local)" + ("" if (KOKORO_TTS.parent / ".venv").exists() else ": venv not set up")
        print(f"{name:12s} {engine.base_url or '':32s} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
