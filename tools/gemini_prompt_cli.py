#!/usr/bin/env python3
"""Small Gemini CLI-compatible wrapper for WRIT-FM.

The existing WRIT generator calls an agent like:

    command args -p "prompt"

This script accepts that same `-p` argument and prints only the generated text.

Required environment variable:

    GEMINI_API_KEY

Optional environment variable:

    GEMINI_MODEL=gemini-1.5-flash

"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
API_KEY = os.environ.get("GEMINI_API_KEY")


def call_gemini(prompt: str, model: str) -> str:
    if not API_KEY:
        raise RuntimeError("Missing GEMINI_API_KEY environment variable")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={API_KEY}"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": float(os.environ.get("GEMINI_TEMPERATURE", "0.8")),
            "maxOutputTokens": int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "350")),
        },
    }

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API HTTP error {exc.code}: {body}") from exc

    candidates = result.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini API returned no candidates: {result}")

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise RuntimeError(f"Gemini API returned empty text: {result}")

    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--prompt", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    try:
        print(call_gemini(args.prompt, args.model))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
