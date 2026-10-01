"""Keyless direct-MiniMax SDK transport, not recordings or live dialect evidence.

The M3 tool guide uses reasoning_details; the current M3.1 OpenAI reference
uses reasoning_content. Keep both forms distinct. These synthetic SSE deltas
exercise existing assembly only; they establish no MiniMax deduplication rule.
"""

import asyncio
import copy
import json
import queue

import httpx
import openai
import pytest

from ouroboros.llm import LLMClient
from ouroboros.loop_messages import _emit_round_progress, _visible_round_text
from tests.test_transport_b_stream_deadlines import (
    TOOLS, WireResponse, chunk, completion, rows, sse,
)
from tests.test_transport_b_stream_deadlines import isolated as isolated


CALL = {"id": "lookup-1", "type": "function",
        "function": {"name": "lookup", "arguments": '{"q":"weather"}'}}
DETAILS = [
    {"type": "reasoning.text", "text": "Check the weather.", "id": "reason-1",
     "format": "MiniMax-response-v1", "index": 0},
    {"type": "reasoning.encrypted", "id": "sealed-1", "data": "opaque=="},
    {"type": "future.continuation", "id": "future-1", "payload": {"bytes": [0, 1]}},
]


@pytest.fixture
def make_client(monkeypatch):
    """Keep the real SDK serializer; only the HTTP peer is synthetic."""
    monkeypatch.setenv("MINIMAX_API_KEY", "minimax-fixture-key")
    monkeypatch.setenv("MINIMAX_REGION", "global_en")
    clients = []

    def make(script, *, asynchronous=False, expected_replay=None):
        client, sent = LLMClient(), []

        def respond(request):
            payload = json.loads(request.content)
            sent.append(payload)
            assert request.url.path == "/v1/chat/completions"
            assert "extra_body" not in payload, "the SDK must flatten extensions onto HTTP JSON"
            assistant = next((m for m in payload["messages"] if m.get("tool_calls")), {})
            missing_echo = expected_replay is not None and any(
                m.get("role") == "tool" for m in payload["messages"]
            ) and any(k not in assistant or assistant[k] != v for k, v in expected_replay.items())
            if payload.get("reasoning_split") is not True or missing_echo:
                return httpx.Response(400, json={"error": {
                    "type": "invalid_request_error", "code": "fixture_contract",
                    "message": "split output and unchanged continuation are required"}})
            result = script.pop(0)
            if isinstance(result, httpx.Response):
                return result
            if isinstance(result, WireResponse):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=result.wire)
            return httpx.Response(200, json=result)

        transport = httpx.MockTransport(respond)
        remote = (openai.AsyncOpenAI if asynchronous else openai.OpenAI)(
            api_key="fixture-not-live", base_url="https://minimax.invalid/v1", max_retries=0,
            http_client=(httpx.AsyncClient if asynchronous else httpx.Client)(transport=transport))
        clients.append(remote)
        monkeypatch.setattr(client, "_get_remote_client", lambda *_a, **_kw: remote)
        monkeypatch.setattr(client, "_get_async_remote_client", lambda *_a, **_kw: remote)
        return client, sent

    yield make
    for remote in clients:
        if isinstance(remote, openai.AsyncOpenAI):
            asyncio.run(remote.close())
        else:
            remote.close()


def _call(client, history, *, stream=False, asynchronous=False, model="minimax::MiniMax-M3.1"):
    kwargs = dict(messages=history, model=model, tools=TOOLS, stream=stream,
                  max_tokens=1024, wait_for_resources=False)
    return asyncio.run(client.chat_async(**kwargs)) if asynchronous else client.chat(**kwargs)


def _response(message, finish="tool_calls"):
    return completion(choices=[{"index": 0, "message": message, "finish_reason": finish}])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("carrier", ["reasoning_content", "reasoning_details", "both"])
def test_same_route_tool_continuation_keeps_exact_carriers(isolated, make_client, asynchronous, stream, carrier):
    reasoning = {}
    if carrier in {"reasoning_content", "both"}:
        reasoning["reasoning_content"] = "\n Check the weather.  "
    if carrier in {"reasoning_details", "both"}:
        reasoning["reasoning_details"] = copy.deepcopy(DETAILS)
    tool_message = {"role": "assistant", "content": "", "tool_calls": [CALL], **reasoning}
    first = _response(tool_message)
    if stream:
        deltas = [{"role": "assistant", "content": ""}]
        if "reasoning_content" in reasoning:
            deltas.extend([{"reasoning_content": "\n Check the "}, {"reasoning_content": "weather.  "}])
        if "reasoning_details" in reasoning:
            deltas.append({"reasoning_details": copy.deepcopy(DETAILS)})
        deltas.append({"tool_calls": [{"index": 0, **CALL}]})
        first = WireResponse(sse(*(chunk(delta) for delta in deltas),
                                 chunk(finish="tool_calls", usage=completion()["usage"])))
    final = _response({"role": "assistant", "content": "Sunny.", **reasoning}, "stop")
    client, sent = make_client([first, final], asynchronous=asynchronous, expected_replay=reasoning)
    history = [{"role": "user", "content": "Weather?"}]
    msg, usage = _call(client, history, stream=stream, asynchronous=asynchronous)
    assert {key: msg.get(key) for key in reasoning} == reasoning
    assert msg["content"] == "" and msg["tool_calls"] == [CALL]
    assert usage["provider"] == "minimax"
    if stream:
        assert usage["stream_receipt"]["complete"] is True
        assert usage["stream_receipt"]["anomalies"]["count"] == 0
        assert usage["stream_receipt"]["manifest_ref"]
    history.extend([msg, {"role": "tool", "tool_call_id": CALL["id"], "content": "Sunny"}])
    canonical = copy.deepcopy(history)
    result, _ = _call(client, history, asynchronous=asynchronous)
    assert result["content"] == "Sunny."
    assert _visible_round_text(result["content"]) == "Sunny."
    assert history == canonical
    assert len(sent) == 2
    assert all(payload["reasoning_split"] is True for payload in sent)
    replay = sent[1]["messages"][1]
    assert {key: replay.get(key) for key in reasoning} == reasoning
    assert replay["tool_calls"] == [CALL]
    assert "response_id" not in replay
    assert [row["state"] for row in rows(isolated)] == ["reserved", "dispatched", "settled"] * 2


@pytest.mark.parametrize("reasoning", [
    {"reasoning_content": {"future": ["retained", 42]}},
    {"reasoning_content": None},
    {"reasoning_content": ""},
    {"reasoning_details": {"future": "not a list"}},
    {"reasoning_details": [None, 17, {"type": "new", "data": "opaque"}]},
    {"reasoning_details": None},
    {"reasoning_details": []},
])
def test_unknown_and_empty_shapes_are_retained_without_display_coercion(isolated, make_client, reasoning):
    body = _response({"role": "assistant", "content": "", "tool_calls": [CALL], **reasoning})
    original = copy.deepcopy(body)
    client, sent = make_client([body, _response({"role": "assistant", "content": "done"}, "stop")],
                               expected_replay=reasoning)
    msg, _ = _call(client, [{"role": "user", "content": "lookup"}])
    assert {key: msg[key] for key in reasoning} == reasoning
    assert LLMClient.extract_display_reasoning(msg) == ""
    _call(client, [msg, {"role": "tool", "tool_call_id": CALL["id"], "content": "ok"}])
    assert {key: sent[1]["messages"][0][key] for key in reasoning} == reasoning
    assert body == original


@pytest.mark.parametrize("carrier", ["reasoning_content", "reasoning_details"])
def test_existing_details_narration_stays_display_only(isolated, make_client, carrier):
    reasoning = {carrier: "Check the weather." if carrier == "reasoning_content" else DETAILS}
    client, _ = make_client([_response({"role": "assistant", "content": "", **reasoning})])
    msg, _ = _call(client, [{"role": "user", "content": "Weather?"}])
    original, emitted, trace = copy.deepcopy(msg), [], {"reasoning_notes": []}
    _emit_round_progress(msg["content"], msg, lambda text, **meta: emitted.append((text, meta)), trace)
    assert emitted == ([("Check the weather.", {"narration": True})] if carrier == "reasoning_details" else [])
    assert trace == {"reasoning_notes": []}
    assert msg == original and _visible_round_text(msg["content"]) == ""


def test_literal_think_tags_remain_evidence(isolated, make_client):
    text = "A literal <think>example</think> belongs in this answer."
    client, _ = make_client([_response({"role": "assistant", "content": text}, "stop")])
    msg, _ = _call(client, [{"role": "user", "content": "Quote an example"}])
    assert msg["content"] == text
    assert LLMClient.extract_display_reasoning(msg) == ""


def test_loop_retains_continuation_and_delivers_only_final_content(isolated, monkeypatch, make_client):
    from ouroboros import loop
    from ouroboros.tools.registry import ToolRegistry

    first = WireResponse(sse(chunk({"role": "assistant", "content": "", "reasoning_content": "Check first.",
                                    "tool_calls": [{"index": 0, **CALL}]}),
                             chunk(finish="tool_calls", usage=completion()["usage"])))
    final = WireResponse(sse(chunk({"role": "assistant", "content": "Sunny.", "reasoning_content": "Now answer."}),
                             chunk(finish="stop", usage=completion()["usage"])))
    client, sent = make_client([first, final], expected_replay={"reasoning_content": "Check first."})
    monkeypatch.setattr(client, "default_model", lambda: "minimax::MiniMax-M3.1")
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")

    def handle(calls, _tools, _logs, _task, _executor, messages, _trace, _progress):
        messages.append({"role": "tool", "tool_call_id": calls[0]["id"], "content": "Sunny"})
        return 0

    monkeypatch.setattr(loop, "handle_tool_calls", handle)
    history, progress = [{"role": "user", "content": "Weather?"}], []
    result, _, trace = loop.run_llm_loop(
        messages=history, tools=ToolRegistry(repo_dir=isolated, drive_root=isolated), llm=client,
        drive_logs=isolated, emit_progress=lambda text, **_meta: progress.append(text),
        incoming_messages=queue.Queue(), task_id="minimax-loop", drive_root=isolated,
    )
    assert result == "Sunny."
    assert "Check first." not in progress  # no expansion of shared reasoning_content narration
    assert "Check first." not in trace["reasoning_notes"]
    assert len(sent) == 2
    assistant = next(msg for msg in sent[1]["messages"] if msg.get("tool_calls"))
    assert assistant["reasoning_content"] == "Check first."
    assert next(msg for msg in history if msg.get("tool_calls"))["reasoning_content"] == "Check first."


def test_repeated_reasoning_fragments_are_not_guessed_duplicates(isolated, make_client):
    response = WireResponse(sse(chunk({"role": "assistant", "reasoning_content": "again "}),
                                chunk({"reasoning_content": "again "}),
                                chunk({"content": "done"}, "stop", usage=completion()["usage"])))
    client, _ = make_client([response])
    msg, _ = _call(client, [{"role": "user", "content": "repeat"}], stream=True)
    assert msg["reasoning_content"] == "again again "


@pytest.mark.parametrize("asynchronous", [False, True])
def test_incomplete_minimax_stream_keeps_unknown_and_never_resends(isolated, make_client, asynchronous):
    from ouroboros.transport_custody import _capture_on_chain

    response = httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse(chunk({"reasoning_content": "partial"}), done=False))
    client, sent = make_client([response], asynchronous=asynchronous)
    with pytest.raises(Exception) as caught:
        _call(client, [{"role": "user", "content": "lookup"}], stream=True, asynchronous=asynchronous)
    assert len(sent) == 1 and response.is_closed
    assert _capture_on_chain(caught.value).state == "unresolved"
    assert [row["state"] for row in rows(isolated)] == ["reserved", "dispatched", "unresolved"]


@pytest.mark.parametrize("missing", ["split", "continuation"])
def test_http_fixture_rejects_missing_split_or_continuation(make_client, missing):
    """The fake peer itself refuses the broken shape, before any scripted success."""
    client, sent = make_client([], expected_replay={"reasoning_details": DETAILS})
    assistant = {"role": "assistant", "content": "", "tool_calls": [CALL]}
    if missing != "continuation":
        assistant["reasoning_details"] = DETAILS
    kwargs = {"model": "MiniMax-fixture", "messages": [assistant,
              {"role": "tool", "tool_call_id": CALL["id"], "content": "Sunny"}]}
    if missing != "split":
        kwargs["extra_body"] = {"reasoning_split": True}
    with pytest.raises(openai.BadRequestError, match="unchanged continuation"):
        client._get_remote_client({}).chat.completions.create(**kwargs)
    assert len(sent) == 1
