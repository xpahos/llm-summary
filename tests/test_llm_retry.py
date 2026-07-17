"""_chat retries transient OpenAI auth errors instead of failing the run.

OpenAI intermittently returns 401 "insufficient permissions" on a key that
served the previous request seconds earlier. The SDK never retries 401s, so
OpenAISummarizer._chat must, or a single flake aborts the whole nightly run.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from llm_summary.llm import OpenAISummarizer


def _auth_error() -> openai.AuthenticationError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(401, request=request)
    return openai.AuthenticationError(
        "Error code: 401 - You have insufficient permissions for this operation.",
        response=response,
        body=None,
    )


class _FlakyClient:
    """Stands in for ChatOpenAI: fails `failures` times, then succeeds."""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls <= self.failures:
            raise _auth_error()

        class Resp:
            content = "summary text"

        return Resp()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("llm_summary.llm.time.sleep", lambda _s: None)


def _summarizer(config, client) -> OpenAISummarizer:
    s = OpenAISummarizer(config)
    s._client = client
    return s


def test_chat_retries_transient_401(config):
    client = _FlakyClient(failures=2)
    assert _summarizer(config, client)._chat("hello") == "summary text"
    assert client.calls == 3


def test_chat_gives_up_after_max_attempts(config):
    client = _FlakyClient(failures=10)
    with pytest.raises(openai.AuthenticationError):
        _summarizer(config, client)._chat("hello")
    assert client.calls == OpenAISummarizer._CHAT_ATTEMPTS


def test_chat_success_needs_no_retry(config):
    client = _FlakyClient(failures=0)
    assert _summarizer(config, client)._chat("hello") == "summary text"
    assert client.calls == 1
