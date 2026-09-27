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


def _fake_chat_response(content) -> io.BytesIO:
    payload = {"choices": [{"message": {"content": content}}]}
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class _OpenAICompatibleCases:
    """Shared by every provider that speaks the OpenAI chat-completions format."""

    provider: str
    key_env: str
    url: str
    default_model: str
    label: str

    def setUp(self):
        patcher = patch.dict("os.environ", {self.key_env: "test-key"}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_api_key_names_the_right_variable(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ExplainError) as ctx:
                explain_window(["ERROR boom"], provider=self.provider)
        self.assertIn(self.key_env, str(ctx.exception))
        self.assertIn(f"--explain-provider {self.provider}", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_request_goes_to_the_right_endpoint(self, mock_urlopen):
        mock_urlopen.return_value.__enter__.return_value = _fake_chat_response("Disk full.")
        result = explain_window(["ERROR: no space left"], provider=self.provider)
        self.assertEqual(result, "Disk full.")

        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.full_url, self.url)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(json.loads(request.data)["model"], self.default_model)

    @patch("urllib.request.urlopen")
    def test_model_override_is_sent(self, mock_urlopen):
        mock_urlopen.return_value.__enter__.return_value = _fake_chat_response("ok")
        explain_window(["ERROR boom"], provider=self.provider, model="some/other-model")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(json.loads(request.data)["model"], "some/other-model")

    @patch("urllib.request.urlopen")
    def test_http_error_names_the_provider(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url=self.url, code=401, msg="Unauthorized", hdrs=None,
            fp=io.BytesIO(b'{"error": "invalid key"}'),
        )
        with self.assertRaises(ExplainError) as ctx:
            explain_window(["ERROR boom"], provider=self.provider)
        self.assertIn(f"{self.label} API returned 401", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_null_content_raises_explain_error(self, mock_urlopen):
        mock_urlopen.return_value.__enter__.return_value = _fake_chat_response(None)
        with self.assertRaises(ExplainError):
            explain_window(["ERROR boom"], provider=self.provider)


class TestExplainOpenAI(_OpenAICompatibleCases, unittest.TestCase):
    provider = "openai"
    key_env = "OPENAI_API_KEY"
    url = "https://api.openai.com/v1/chat/completions"
    default_model = "gpt-4o-mini"
    label = "OpenAI"


class TestExplainOpenRouter(_OpenAICompatibleCases, unittest.TestCase):
    provider = "openrouter"
    key_env = "OPENROUTER_API_KEY"
    url = "https://openrouter.ai/api/v1/chat/completions"
    default_model = "anthropic/claude-haiku-4.5"
    label = "OpenRouter"


if __name__ == "__main__":
    unittest.main()
