"""The streaming reader: live thinking, and an answer that is not different.

The reply carries the model's THINKING in an Anthropic-compatible stream as
``content_block_delta`` events with ``delta.type == "thinking_delta"``.
Measured against MiniMax-M3.1-Flash (2026-10-19): the first chunk arrives
about a second in, and the assembled text is the same answer the single-body
call returns (22 rows / 18 columns / 3 legend entries, and the JSON differs
only where the model's own sampling differs).

So the streaming path has to deliver BOTH halves: the thinking WHILE the
model is thinking, and the same answer afterwards. A reader that dropped the
text, or a reader that only worked when the endpoint happened to stream,
would satisfy one and break the other.
"""
from __future__ import annotations

import io
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rca_core.llm as L                                     # noqa: E402


class _FakeResp:
    """The little of an HTTP response the SSE reader touches."""

    def __init__(self, lines, ctype="text/event-stream", status=200):
        self._buf = io.BytesIO("".join(lines).encode("utf-8"))
        self.headers = {"Content-Type": ctype}
        self.status = status

    def read(self, *a):
        return self._buf.read(*a)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for line in self._buf:
            yield line


def _sse(*events):
    """One SSE frame per event: an `event:` line then a `data:` line."""
    out = []
    for ev in events:
        out.append("event: %s\n" % ev.get("type", ""))
        out.append("data: %s\n\n" % json.dumps(ev))
    return out


def _call(lines, ctype="text/event-stream", thinking=None):
    progress = []
    resp = _FakeResp(lines, ctype=ctype)
    with patch.object(L.urllib.request, "urlopen", return_value=resp):
        text, truncated, status, err, usage = L._read_response_stream(
            "https://api.minimaxi.com/anthropic/messages", {}, {}, 30,
            progress.append, thinking)
    return text, truncated, status, err, usage, progress


def test_the_thinking_arrives_in_chunks():
    chunks = []
    lines = _sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
        {"type": "content_block_delta",
         "delta": {"type": "thinking_delta", "thinking": "We need inspect "}},
        {"type": "content_block_delta",
         "delta": {"type": "thinking_delta", "thinking": "the image carefully."}},
        {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": '{"rows": []}'}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    )
    text, truncated, status, err, usage, _p = _call(lines, thinking=chunks.append)
    assert chunks == ["We need inspect ", "the image carefully."]
    assert text == '{"rows": []}'
    assert status == 200
    assert not truncated
    assert err == ""


def test_the_text_is_assembled_from_the_text_deltas():
    lines = _sse(
        {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": '{"a":'}},
        {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": ' 1}'}},
    )
    text, _t, _s, _e, _u, _p = _call(lines)
    assert text == '{"a": 1}'


def test_max_tokens_is_reported_as_truncation():
    lines = _sse(
        {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": '{"a": 1}'}},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
    )
    text, truncated, _s, _e, _u, _p = _call(lines)
    assert truncated is True
    assert text == '{"a": 1}'


def test_a_thinking_only_reply_says_so():
    """The model spent the whole budget thinking and wrote nothing.

    That is the failure the "max" effort hits at a small max_tokens, and the
    message has to say WHICH it was -- an empty reply and a thinking-only
    reply are different problems.
    """
    lines = _sse(
        {"type": "content_block_delta",
         "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        {"type": "message_stop"},
    )
    text, _t, _s, err, _u, _p = _call(lines)
    assert text is None
    assert "thinking" in err


def test_an_endpoint_that_ignores_stream_is_still_answered():
    """A plain JSON body must be parsed the ordinary way."""
    body = json.dumps({"content": [{"type": "text", "text": "plain"}],
                       "stop_reason": "end_turn"})
    lines = [body]
    text, _t, status, _e, _u, _p = _call(lines, ctype="application/json")
    assert text == "plain"
    assert status == 200


def test_the_progress_stages_are_reported():
    lines = _sse({"type": "message_stop"})
    _text, _t, _s, _e, _u, progress = _call(lines)
    assert "submitting" in progress
    assert "thinking" in progress


def test_a_broken_event_does_not_kill_the_stream():
    """One malformed frame must not cost the whole answer."""
    lines = [
        "event: content_block_delta\n",
        "data: {not json}\n\n",
        "event: content_block_delta\n",
        "data: %s\n\n" % json.dumps({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "survived"}}),
    ]
    text, _t, _s, _e, _u, _p = _call(lines)
    assert text == "survived"


def test_the_stream_is_only_used_when_a_thinking_callback_wants_it():
    """The ordinary path must stay a single request."""
    seen = {}

    def fake_read(target, body, headers, timeout_sec, progress_callback=None):
        seen["stream"] = body.get("stream")
        return ("OK", False, 200, "", {"usage": {"input_tokens": 1,
                                                 "output_tokens": 1}})

    prov = L.LlmProvider(api_format=L.ApiFormat.ANTHROPIC,
                          endpoint="https://api.minimaxi.com/anthropic",
                          api_key="k", model="m")
    with patch.object(L, "_read_response", side_effect=fake_read), \
            patch.object(L, "_read_response_stream") as stream:
        L.call_llm_api(provider=prov, system_prompt="s", image_b64="QUFB",
                       media_type="image/png", user_text="u", max_tokens=64)
        assert stream.called is False
        assert seen.get("stream") is None

    with patch.object(L, "_read_response", side_effect=fake_read), \
            patch.object(L, "_read_response_stream",
                         return_value=("OK", False, 200, "", None)) as stream:
        L.call_llm_api(provider=prov, system_prompt="s", image_b64="QUFB",
                       media_type="image/png", user_text="u", max_tokens=64,
                       stream=True, thinking_callback=lambda c: None)
        assert stream.called is True
