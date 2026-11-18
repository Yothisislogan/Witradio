import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "gemini_prompt_cli.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("gemini_prompt_cli", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class GeminiPromptCliTests(unittest.TestCase):
    def test_api_key_goes_in_header_not_url(self):
        module = _load_module()
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            body = {"candidates": [{"content": {"parts": [{"text": "Hello, night owls."}]}}]}
            return _FakeResponse(json.dumps(body).encode())

        with (
            patch.object(module, "API_KEY", "test-key-123"),
            patch.object(module.urllib.request, "urlopen", fake_urlopen),
        ):
            text = module.call_gemini("Say hello", "gemini-test")

        request = captured["request"]
        self.assertEqual(text, "Hello, night owls.")
        self.assertNotIn("test-key-123", request.full_url)
        self.assertNotIn("key=", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "test-key-123")


if __name__ == "__main__":
    unittest.main()
