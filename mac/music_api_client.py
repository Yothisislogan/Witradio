#!/usr/bin/env python3
"""API client for Gemini Lyria music generation.
"""

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MUSIC_MODEL = os.environ.get("GEMINI_MUSIC_MODEL", "lyria-3-pro-preview")


def is_api_available() -> bool:
    """Check if Gemini API key is configured."""
    return bool(GEMINI_API_KEY)


def generate_music_api(
    caption: str,
    output_path: Path,
    duration: float = 120.0,
    lyrics: str = "[Instrumental]",
    model: str = GEMINI_MUSIC_MODEL,
    timeout: float = 600.0,
) -> bool:
    """Generate music via Gemini API and save to output_path.

    Args:
        caption: Text description of the music style/mood.
        output_path: Where to save the generated audio file.
        duration: Requested length in seconds.
        lyrics: Optional lyrics.
        model: Gemini music model to use.
        timeout: HTTP request timeout.

    Returns:
        True if successful, False otherwise.
    """
    if not GEMINI_API_KEY:
        print("[music_api] Missing GEMINI_API_KEY")
        return False

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={GEMINI_API_KEY}"
    )

    # Prepare prompt
    if lyrics and lyrics.strip() != "[Instrumental]":
        prompt = f"{caption}\n\nLyrics:\n{lyrics}"
    else:
        prompt = f"{caption}\n\nInstrumental only, no vocals."

    if duration > 0:
        prompt = f"Generate a {int(duration)} second song. {prompt}"

    # Default to MP3 for consistency with file extensions
    mime_type = "audio/mpeg"

    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "responseModalities": ["AUDIO", "TEXT"],
            "responseFormat": {"audio": {"mime_type": mime_type}},
        }
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[music_api] HTTP {e.code}: {body[:200]}")
        return False
    except Exception as e:
        print(f"[music_api] Request failed: {e}")
        return False

    candidates = result.get("candidates", [])
    if not candidates:
        print(f"[music_api] No candidates in response: {result}")
        return False

    parts = candidates[0].get("content", {}).get("parts", [])
    audio_bytes = None
    for part in parts:
        if "inlineData" in part:
            audio_bytes = base64.b64decode(part["inlineData"]["data"])
            break

    if not audio_bytes:
        print("[music_api] No audio data in response")
        return False

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(audio_bytes)
        return True
    except Exception as e:
        print(f"[music_api] Failed to save audio: {e}")
        return False
