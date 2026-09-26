"""
Tests for the --explain module. Uses unittest.mock to fake HTTP
responses so these tests are hermetic (no real network call, no real
API key needed) and fast.
"""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from logometer.explain import ExplainError, explain_window


def _fake_success_response(text: str) -> io.BytesIO:
    payload = {"content": [{"type": "text", "text": text}]}
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class TestExplainAnthropic(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_api_key_raises_clear_error(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ExplainError) as ctx:
                explain_window(["ERROR boom"], provider="anthropic")
            self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_successful_call_returns_text(self, mock_urlopen):
        mock_urlopen.return_value.__enter__.return_value = _fake_success_response(
            "Upstream connection pool exhausted."
        )
        result = explain_window(["ERROR: pool exhausted"], provider="anthropic")
        self.assertEqual(result, "Upstream connection pool exhausted.")

    @patch("urllib.request.urlopen")
    def test_http_error_wrapped_as_explain_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"error": "invalid key"}'),
        )
        with self.assertRaises(ExplainError) as ctx:
            explain_window(["ERROR boom"], provider="anthropic")
        self.assertIn("401", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_network_error_wrapped_as_explain_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("no route to host")
        with self.assertRaises(ExplainError):
            explain_window(["ERROR boom"], provider="anthropic")

    @patch("urllib.request.urlopen")
    def test_empty_response_raises(self, mock_urlopen):
        mock_urlopen.return_value.__enter__.return_value = _fake_success_response("")
        with self.assertRaises(ExplainError):
            explain_window(["ERROR boom"], provider="anthropic")

    def test_unknown_provider_raises(self):
        with self.assertRaises(ExplainError):
            explain_window(["ERROR boom"], provider="not-a-real-provider")


if __name__ == "__main__":
    unittest.main()
