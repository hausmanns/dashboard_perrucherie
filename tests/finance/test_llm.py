"""app/llm.py — the debug trace behind 🔍 « Sous le capot » (no real model is called)."""
import json

import pytest

from app import config, llm

LOCAL = {"llm": {"mode": "local_only", "local_base_url": "http://local.test/v1", "local_model": "m"}}


class FakeResponse:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._body


def answer(content: str, **message) -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": content, **message}}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 3}})


def test_trace_keeps_what_was_sent_thinking_and_tokens(monkeypatch):
    monkeypatch.setattr(llm.httpx, "post", lambda url, **kw: answer('{"a": 1}', reasoning="Je lis le brut."))
    trace: dict = {}
    data, engine = llm.chat_json(llm.image_message("Lis ceci", [(b"x", "image/png")] * 2), LOCAL, trace=trace)
    assert data == {"a": 1} and engine == "local:m"
    assert trace["prompt"] == "Lis ceci" and trace["images"] == 2      # images counted, not stored
    (a,) = trace["attempts"]
    assert a["engine"] == "local:m" and "error" not in a and a["seconds"] >= 0
    assert a["reasoning"] == "Je lis le brut." and a["raw"] == '{"a": 1}'
    assert a["usage"] == {"prompt_tokens": 10, "completion_tokens": 3}


def test_thinking_left_inline_is_split_from_the_answer(monkeypatch):
    monkeypatch.setattr(llm.httpx, "post", lambda url, **kw: answer('<think>hmm {pas du json}</think>\n{"a": 2}'))
    trace: dict = {}
    data, _ = llm.chat_json(llm.text_message("x"), LOCAL, trace=trace)
    assert data == {"a": 2}
    assert trace["attempts"][0]["reasoning"] == "hmm {pas du json}"
    assert trace["attempts"][0]["raw"] == '{"a": 2}'


def test_trace_shows_why_local_failed_before_the_cloud_answered(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "k")

    def post(url, **kw):
        if url.startswith("http://local.test"):
            return FakeResponse(400, "request (4748 tokens) exceeds the available context size (4096 tokens)")
        return answer('{"a": 3}')

    monkeypatch.setattr(llm.httpx, "post", post)
    trace: dict = {}
    settings = {"llm": {**LOCAL["llm"], "mode": "local_first"}}
    data, engine = llm.chat_json(llm.text_message("x"), settings, trace=trace)
    assert data == {"a": 3} and engine.startswith("cloud:")
    local, cloud = trace["attempts"]
    assert "4096" in local["error"] and "error" not in cloud


def test_trace_is_filled_even_when_nobody_answers(monkeypatch):
    monkeypatch.setattr(llm.httpx, "post", lambda url, **kw: answer("pas du json", reasoning="…"))
    trace: dict = {}
    with pytest.raises(llm.LLMError):
        llm.chat_json(llm.text_message("x"), LOCAL, trace=trace)
    (a,) = trace["attempts"]
    assert "illisible" in a["error"] and a["raw"] == "pas du json" and a["reasoning"] == "…"
