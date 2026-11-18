#!/usr/bin/env python3
"""One client for any OpenAI-compatible server.

llama.cpp's server, Lemonade, LM Studio and Ollama all expose the same
`/v1/chat/completions` API, so producer code never changes when the model
does (show bible, Local AI stack). This replaces the Claude CLI subprocess in
`run_claude` and tools/gemini_prompt_cli.py for stations whose agent kind is
`openai`.

Settings come from the station's `llm:` block in config/stations.yaml, with
WRIT_LLM_BASE_URL and WRIT_LLM_MODEL overriding it. If the server wants a key,
put it in WRIT_LLM_API_KEY (or the variable named by `api_key_env`); it is
sent as a Bearer header, never in the URL.

    python mac/llm_client.py -p "Write a 20-second station ID."
    python mac/llm_client.py --models
    python mac/llm_client.py --health
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reasoning models (Qwen3 in thinking mode, DeepSeek-R1 distills) may put
# their chain of thought inline; the script is what comes after it.
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


@dataclass
class LLMClient:
    base_url: str
    model: str = ""
    api_key: str = ""
    timeout: float = 300.0
    temperature: float = 0.8
    max_tokens: int = 2048
    retries: int = 2
    backoff_seconds: float = 2.0

    @classmethod
    def from_station(cls, station: Any = None) -> "LLMClient":
        if station is None:
            from station_config import load_station_config

            station = load_station_config()
        llm = station.llm
        return cls(
            base_url=llm.base_url,
            model=llm.model,
            api_key=llm.api_key,
            timeout=llm.timeout,
            temperature=llm.temperature,
            max_tokens=llm.max_tokens,
        )

    # ── HTTP ──

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: float | None = None, retries: int | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        attempts = (self.retries if retries is None else retries) + 1
        last_error = "no attempt made"
        for attempt in range(attempts):
            request = urllib.request.Request(self._url(path), data=data, headers=self._headers(), method=method)
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    return json.loads(response.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = f"HTTP {exc.code} from {path}: {body}"
                if exc.code not in RETRY_STATUSES:
                    raise LLMError(last_error) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = f"cannot reach {self.base_url}: {reason}"
            except json.JSONDecodeError as exc:
                raise LLMError(f"Invalid JSON from {path}: {exc}") from exc
            if attempt + 1 < attempts:
                time.sleep(self.backoff_seconds * (2 ** attempt))
        raise LLMError(last_error)

    # ── API ──

    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
        timeout: float | None = None,
        json_mode: bool = False,
    ) -> str:
        """Return the assistant's text for a list of chat messages."""
        payload: dict[str, Any] = {
            "messages": list(messages),
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }
        chosen = model or self.model
        if chosen:  # llama.cpp ignores it; LM Studio and Ollama need it
            payload["model"] = chosen
        if stop:
            payload["stop"] = list(stop)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        result = self._request("POST", "chat/completions", payload, timeout=timeout)
        choices = result.get("choices") or []
        if not choices:
            raise LLMError(f"No choices in response: {str(result)[:300]}")
        message = choices[0].get("message") or {}
        text = message.get("content")
        if isinstance(text, list):  # some servers return content parts
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        text = _THINK_RE.sub("", text or "").strip()
        if not text:
            reason = choices[0].get("finish_reason")
            raise LLMError(f"Empty reply (finish_reason={reason}); raise max_tokens for reasoning models")
        return text

    def complete(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, **kwargs)

    def models(self) -> list[str]:
        result = self._request("GET", "models", timeout=10)
        return [str(m.get("id")) for m in result.get("data") or [] if isinstance(m, dict)]

    def healthy(self) -> bool:
        """One quick probe, no retries: for monitoring and the CLI."""
        try:
            self._request("GET", "models", timeout=5, retries=0)
            return True
        except LLMError:
            return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Talk to the station's OpenAI-compatible LLM")
    parser.add_argument("-p", "--prompt", help="prompt text (prints only the reply)")
    parser.add_argument("--system", help="system prompt, e.g. the show's bible section")
    parser.add_argument("--model", help="override the configured model")
    parser.add_argument("--station", help="station id (default: WRIT_STATION_ID)")
    parser.add_argument("--models", action="store_true", help="list the server's models")
    parser.add_argument("--health", action="store_true", help="exit 0 if the server answers")
    args = parser.parse_args(argv)

    from station_config import load_station_config

    client = LLMClient.from_station(load_station_config(args.station))
    try:
        if args.health:
            ok = client.healthy()
            print("ok" if ok else f"unreachable: {client.base_url}")
            return 0 if ok else 1
        if args.models:
            print("\n".join(client.models()))
            return 0
        if not args.prompt:
            parser.error("give -p PROMPT, --models or --health")
        print(client.complete(args.prompt, system=args.system, model=args.model))
        return 0
    except LLMError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
