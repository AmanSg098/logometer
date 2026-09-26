"""
Optional "--explain" support: sends an anomalous window's lines to an
LLM and asks for a one-sentence plain-English summary of the likely
cause.

Deliberately built on stdlib `urllib` rather than the `anthropic` or
`openai` SDKs, so `--explain` needs nothing beyond an API key — no
extra pip install, no risk of drifting out of sync with an SDK's API
surface. This is a deviation from the original plan (which sketched
"Anthropic or OpenAI SDK"); seemed like the better trade for a tool
whose whole pitch is "works with nothing installed."

This module must NEVER raise anything except ExplainError — callers
(cli.py) rely on that to fail soft (print a note, keep tailing) rather
than crashing the whole run because an anomaly happened to coincide
with a flaky network.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # fast + cheap, all we need for a one-liner

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_OPENAI_MODEL = "gpt-4o-mini"

_TIMEOUT_SECONDS = 15
_MAX_LINES_SENT = 30  # cap what we send: a window shouldn't need more context than this to explain

_SYSTEM_PROMPT = (
    "You are looking at a burst of anomalous lines from an application log. "
    "In ONE short sentence (under 25 words), state the most likely underlying cause. "
    "Be concrete and specific to what's in the lines. No preamble, no hedging, no markdown."
)


class ExplainError(Exception):
    """Raised for any failure in getting an explanation — missing API
    key, network failure, bad response, unknown provider. Callers
    should catch this and degrade gracefully rather than crash."""


def _build_prompt(lines: list[str]) -> str:
    sample = lines[:_MAX_LINES_SENT]
    joined = "\n".join(sample)
    if len(lines) > _MAX_LINES_SENT:
        joined += f"\n... ({len(lines) - _MAX_LINES_SENT} more similar lines omitted)"
    return f"Log lines:\n{joined}"


def _call_anthropic(lines: list[str], model: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ExplainError(
            "ANTHROPIC_API_KEY is not set. Set it to enable --explain, "
            "e.g. export ANTHROPIC_API_KEY=your-key-here"
        )

    body = json.dumps({
        "model": model,
        "max_tokens": 100,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": _build_prompt(lines)}],
    }).encode("utf-8")

    request = urllib.request.Request(
        _ANTHROPIC_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise ExplainError(f"Anthropic API returned {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise ExplainError(f"could not reach Anthropic API: {e.reason}") from e
    except TimeoutError as e:
        raise ExplainError("Anthropic API request timed out") from e

    try:
        blocks = payload["content"]
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    except (KeyError, TypeError) as e:
        raise ExplainError(f"unexpected response shape from Anthropic API: {payload!r}") from e

    if not text:
        raise ExplainError("Anthropic API returned an empty explanation")
    return text


def _call_openai(lines: list[str], model: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ExplainError(
            "OPENAI_API_KEY is not set. Set it to enable --explain with --explain-provider openai, "
            "e.g. export OPENAI_API_KEY=your-key-here"
        )

    body = json.dumps({
        "model": model,
        "max_tokens": 100,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(lines)},
        ],
    }).encode("utf-8")

    request = urllib.request.Request(
        _OPENAI_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise ExplainError(f"OpenAI API returned {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise ExplainError(f"could not reach OpenAI API: {e.reason}") from e
    except TimeoutError as e:
        raise ExplainError("OpenAI API request timed out") from e

    try:
        text = payload["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise ExplainError(f"unexpected response shape from OpenAI API: {payload!r}") from e

    if not text:
        raise ExplainError("OpenAI API returned an empty explanation")
    return text


_PROVIDERS = {
    "anthropic": (_call_anthropic, _ANTHROPIC_MODEL),
    "openai": (_call_openai, _OPENAI_MODEL),
}


def explain_window(lines: list[str], provider: str = "anthropic", model: str | None = None) -> str:
    """Get a one-sentence explanation for a burst of anomalous log
    lines. Raises ExplainError on any failure — callers should catch
    this and continue without the explanation rather than crash."""
    if provider not in _PROVIDERS:
        raise ExplainError(f"unknown --explain-provider {provider!r}, expected one of {list(_PROVIDERS)}")
    fn, default_model = _PROVIDERS[provider]
    return fn(lines, model or default_model)
