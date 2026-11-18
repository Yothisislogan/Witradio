"""The OpenAI-compatible client, against a local fake server."""

import http.server
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mac.llm_client import LLMClient, LLMError
from mac.station_config import LLMConfig


class FakeServer(http.server.ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.requests = []  # (path, headers, body)
        self.replies = []  # queued (status, payload) for chat/completions

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers), None))
        if self.path == "/v1/models":
            return self._reply(200, {"data": [{"id": "qwen3-35b-a3b"}, {"id": "gpt-oss-120b"}]})
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append((self.path, dict(self.headers), body))
        status, payload = self.server.replies.pop(0) if self.server.replies else (
            200, {"choices": [{"message": {"content": "ok"}}]}
        )
        self._reply(status, payload)

    def log_message(self, *args):
        pass


def chat_reply(text, finish="stop"):
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}]}


class LLMClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = LLMClient(base_url=self.server.base_url, model="qwen3-35b-a3b",
                                api_key="local-key", timeout=5, backoff_seconds=0.01)

    def test_chat_sends_openai_payload_with_bearer_key(self):
        self.server.replies.append(chat_reply("Good evening, night creatures."))
        text = self.client.complete("Open the show.", system="You are the Count.", max_tokens=300)

        path, headers, body = self.server.requests[-1]
        self.assertEqual(text, "Good evening, night creatures.")
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer local-key")
        self.assertEqual(body["model"], "qwen3-35b-a3b")
        self.assertEqual(body["max_tokens"], 300)
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"], [
            {"role": "system", "content": "You are the Count."},
            {"role": "user", "content": "Open the show."},
        ])

    def test_model_and_key_are_optional(self):
        client = LLMClient(base_url=self.server.base_url, timeout=5)
        self.server.replies.append(chat_reply("fine"))
        client.complete("hi")
        _, headers, body = self.server.requests[-1]
        self.assertNotIn("model", body)
        self.assertNotIn("Authorization", headers)

    def test_reasoning_is_stripped(self):
        self.server.replies.append(chat_reply("<think>The listener wants brevity.</think>\nLet's read the tape."))
        self.assertEqual(self.client.complete("go"), "Let's read the tape.")

    def test_retries_overload_then_succeeds(self):
        self.server.replies += [(503, {"error": "loading model"}), chat_reply("Respectfully.")]
        self.assertEqual(self.client.complete("go"), "Respectfully.")
        posts = [r for r in self.server.requests if r[0].endswith("/chat/completions")]
        self.assertEqual(len(posts), 2)

    def test_client_errors_are_not_retried(self):
        self.server.replies += [(400, {"error": "bad request"}), chat_reply("never sent")]
        with self.assertRaises(LLMError) as ctx:
            self.client.complete("go")
        self.assertIn("HTTP 400", str(ctx.exception))
        posts = [r for r in self.server.requests if r[0].endswith("/chat/completions")]
        self.assertEqual(len(posts), 1)

    def test_empty_reply_is_an_error(self):
        self.server.replies.append(chat_reply("<think>...</think>", finish="length"))
        with self.assertRaises(LLMError) as ctx:
            self.client.complete("go")
        self.assertIn("finish_reason=length", str(ctx.exception))

    def test_unreachable_server_is_an_error(self):
        client = LLMClient(base_url="http://127.0.0.1:9/v1", timeout=1, retries=0)
        with self.assertRaises(LLMError):
            client.complete("go")
        self.assertFalse(client.healthy())

    def test_models_and_health(self):
        self.assertEqual(self.client.models(), ["qwen3-35b-a3b", "gpt-oss-120b"])
        self.assertTrue(self.client.healthy())

    def test_from_station_reads_llm_config(self):
        station = SimpleNamespace(llm=LLMConfig(base_url=self.server.base_url, model="m", timeout=9.0,
                                                max_tokens=99, api_key_env="TEST_LLM_KEY"))
        with patch.dict("os.environ", {"TEST_LLM_KEY": "k"}):
            client = LLMClient.from_station(station)
        self.assertEqual((client.base_url, client.model, client.api_key, client.timeout, client.max_tokens),
                         (self.server.base_url, "m", "k", 9.0, 99))


class RunClaudeDispatchTests(unittest.TestCase):
    def test_openai_station_uses_the_http_client_not_a_cli(self):
        from mac.content_generator import helpers

        server = FakeServer()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        server.replies.append(chat_reply('"*Stay curious. Stay covered. Stay weird.*"'))
        station = SimpleNamespace(
            agent=SimpleNamespace(kind="openai", command="openai", args=()),
            llm=LLMConfig(base_url=server.base_url, timeout=5.0),
        )
        with (
            patch.object(helpers, "STATION", station),
            patch.object(helpers.subprocess, "run", side_effect=AssertionError("no CLI for openai")),
        ):
            text = helpers.run_claude("Sign off, Wit.", timeout=1)
        self.assertEqual(text, "Stay curious. Stay covered. Stay weird.")


if __name__ == "__main__":
    unittest.main()
