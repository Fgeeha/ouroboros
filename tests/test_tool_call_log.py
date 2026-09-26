"""#1316: every tool call the host begins processing leaves a durable start, and readers
count logical calls — through the real loop wrapper, memory, replay and the canonical copy."""
from __future__ import annotations

import json
import pathlib
import threading
import time
from types import SimpleNamespace

import pytest

import ouroboros.loop_tool_execution as execution
from ouroboros.tool_call_log import logical_calls
from ouroboros.tools.tool_result import ToolResult


def _rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _Registry:
    CODE_TOOLS = frozenset()

    def __init__(self, tmp_path, handler, *, round_id="exec:round:1"):
        self.handler = handler
        self.frames = []
        queue = SimpleNamespace(put_nowait=lambda env: self.frames.append(env["data"]), put=None)
        self._ctx = SimpleNamespace(
            task_attempt=2, event_queue=queue, task_metadata={"budget_drive_root": str(tmp_path / "canonical")},
            _current_llm_call_meta={"execution_id": "exec", "round_id": round_id, "llm_call_id": "llm-1"})

    def execute_result(self, name, args):
        return self.handler(name, args)


def _call(registry, tmp_path, call_id="call_0", name="read_file", args=None, timeout=5):
    logs = tmp_path / "child" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    tc = {"id": call_id, "function": {"name": name, "arguments": json.dumps(args or {"path": "a.txt"})}}
    return execution._execute_with_timeout(registry, tc, logs, timeout, "task-1"), logs


def test_start_is_durable_before_the_handler_and_the_settlement_shares_its_identity(tmp_path):
    seen_before_handler = []

    def handler(_name, _args):
        seen_before_handler.extend(_rows(tmp_path / "child" / "logs" / "tools.jsonl"))
        return ToolResult(status="ok", code="OK", text="contents")

    registry = _Registry(tmp_path, handler)
    result, logs = _call(registry, tmp_path, args={"path": "a.txt", "api_key": "sk-secret-value"})
    assert result["result"] == "contents"
    [start] = seen_before_handler
    assert start["type"] == "tool_call_started" and start["task_attempt"] == 2
    assert start["args"]["api_key"] != "sk-secret-value" and "sk-secret-value" not in json.dumps(start)
    rows = _rows(logs / "tools.jsonl")
    assert [row["type"] for row in rows] == ["tool_call_started", "tool_call"]
    assert {row["invocation_id"] for row in rows} == {start["invocation_id"]}
    assert rows[1]["elapsed_ms"] >= 0 and "sk-secret-value" not in json.dumps(rows[1])
    # The canonical copy survives the execution drive (e.g. its GC).
    canonical = tmp_path / "canonical" / "logs" / "tools.jsonl"
    (logs / "tools.jsonl").unlink()
    assert [row["type"] for row in _rows(canonical)] == ["tool_call_started", "tool_call"]
    # The live start frame IS the durable payload (same ts), so a backfill dedupes it.
    live_start = next(frame for frame in registry.frames if frame.get("type") == "tool_call_started")
    assert (live_start["ts"], live_start["invocation_id"]) == (start["ts"], start["invocation_id"])


def test_repeated_provider_ids_are_distinct_invocations(tmp_path):
    registry = _Registry(tmp_path, lambda *_: ToolResult(status="ok", code="OK", text="x"))
    _call(registry, tmp_path, call_id="call_0")
    registry._ctx._current_llm_call_meta = {"execution_id": "exec", "round_id": "exec:round:2", "llm_call_id": "llm-2"}
    _, logs = _call(registry, tmp_path, call_id="call_0")
    calls = logical_calls(_rows(logs / "tools.jsonl"))
    assert len(calls) == 2 and [call["state"] for call in calls] == ["settled", "settled"]
    assert [call["settled"]["round_id"] for call in calls] == ["exec:round:1", "exec:round:2"]


def test_a_body_that_starts_after_the_round_moved_keeps_its_frozen_correlation(tmp_path):
    registry = _Registry(tmp_path, lambda *_: ToolResult(status="ok", code="OK", text="x"))
    invocation = execution.new_invocation("call_0", registry._ctx._current_llm_call_meta, 2)
    registry._ctx._current_llm_call_meta = {"execution_id": "exec", "round_id": "exec:round:9", "llm_call_id": "llm-9"}
    logs = tmp_path / "logs"
    logs.mkdir()
    row = execution._execute_single_tool(
        registry, {"id": "call_0", "function": {"name": "read_file", "arguments": "{}"}}, logs, "task-1", invocation)
    [settled] = _rows(logs / "tools.jsonl")
    assert settled["round_id"] == row["round_id"] == "exec:round:1" and settled["llm_call_id"] == "llm-1"


@pytest.mark.parametrize("settle_first", [False, True])
def test_wait_end_and_settlement_are_independent_facts_in_either_order(settle_first):
    start = {"type": "tool_call_started", "invocation_id": "i", "tool": "run_command", "args": {"cmd": "x"}}
    settled = {"type": "tool_call", "invocation_id": "i", "tool": "run_command", "result_preview": "done"}
    waited = {"type": "tool_call_timeout", "invocation_id": "i", "tool": "run_command"}
    rows = [start, settled, waited] if settle_first else [start, waited, settled]
    [call] = logical_calls(rows)
    assert call["state"] == "settled" and call["wait_ended"] is waited and call["settled"] is settled
    assert logical_calls([start, waited])[0]["state"] == "wait_ended"
    assert logical_calls([start])[0]["state"] == "unknown"
    legacy = [{"type": "tool_call", "tool": "a"}, {"type": "tool_call", "tool": "a"}]
    assert [call["state"] for call in logical_calls(legacy)] == ["legacy", "legacy"]


def test_timeout_then_late_settlement_keeps_both_rows(tmp_path):
    release = threading.Event()

    def handler(_name, _args):
        release.wait(timeout=10)
        return ToolResult(status="ok", code="OK", text="late")

    registry = _Registry(tmp_path, handler)
    result, logs = _call(registry, tmp_path, timeout=1)
    assert result["tool_result"].code == "TOOL_TIMEOUT"
    assert [row["type"] for row in _rows(logs / "tools.jsonl")] == ["tool_call_started", "tool_call_timeout"]
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(_rows(logs / "tools.jsonl")) < 3:
        time.sleep(0.05)
    rows = _rows(logs / "tools.jsonl")
    [call] = logical_calls(rows)
    assert call["state"] == "settled" and call["settled"]["result_preview"] == "late"
    assert call["wait_ended"]["waited_ms"] >= 900 and call["settled"]["elapsed_ms"] >= call["wait_ended"]["waited_ms"]
    live_timeout = next(frame for frame in registry.frames if frame.get("type") == "tool_call_timeout")
    assert live_timeout["ts"] == call["wait_ended"]["ts"]


def test_early_exits_after_the_start_still_settle(tmp_path):
    from ouroboros.usage_accounting import UsageAccountingError

    registry = _Registry(tmp_path, lambda *_: ToolResult(status="ok", code="OK", text="x"))
    logs = tmp_path / "child" / "logs"
    logs.mkdir(parents=True)
    bad = {"id": "c", "function": {"name": "read_file", "arguments": "{not json"}}
    assert execution._execute_with_timeout(registry, bad, logs, 5, "task-1")["is_error"] is True

    def raising(_name, _args):
        raise UsageAccountingError("ledger unavailable")

    registry.handler = raising
    with pytest.raises(UsageAccountingError):
        _call(registry, tmp_path)
    calls = logical_calls(_rows(logs / "tools.jsonl"))
    assert [call["state"] for call in calls] == ["settled", "settled"]
    assert calls[1]["settled"]["status"] == "host_error" and calls[1]["settled"]["is_error"] is True


def test_a_failed_start_append_is_disclosed_and_never_vetoes_execution(tmp_path, monkeypatch):
    import ouroboros.tool_call_log as tool_call_log

    real = tool_call_log.append_jsonl
    ran = []

    def flaky(path, payload, **kwargs):
        if payload.get("type") == "tool_call_started" and "canonical" not in str(path):
            return False
        return real(path, payload, **kwargs)

    monkeypatch.setattr(tool_call_log, "append_jsonl", flaky)
    registry = _Registry(tmp_path, lambda *_: ran.append(1) or ToolResult(status="ok", code="OK", text="x"))
    _, logs = _call(registry, tmp_path)
    assert ran == [1]
    [settled] = _rows(logs / "tools.jsonl")
    assert settled["start_log"] == {"task_log": False, "canonical": True}
    canonical = _rows(tmp_path / "canonical" / "logs" / "tools.jsonl")
    assert [row["type"] for row in canonical] == ["tool_call_started", "tool_call"]


def test_memory_and_replay_count_logical_calls_and_never_revive_an_unfinished_start(tmp_path):
    from ouroboros.gateway.task_events import _event_from_log_entry
    from ouroboros.memory import Memory

    logs = tmp_path / "logs"
    logs.mkdir()
    rows = []
    for index in range(12):
        rows.append({"type": "tool_call_started", "task_id": "t", "invocation_id": f"i{index}", "tool": "read_file",
                     "args": {"path": f"f{index}"}})
        rows.append({"type": "tool_call", "task_id": "t", "invocation_id": f"i{index}", "tool": "read_file",
                     "args": {"path": f"f{index}"}, "result_preview": "ok"})
    rows.append({"type": "tool_call_started", "task_id": "t", "invocation_id": "dead", "tool": "run_command",
                 "args": {"cmd": "make"}})
    (logs / "tools.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    memory = Memory(drive_root=tmp_path)
    shown, coverage = memory.read_task_recent("tools.jsonl", "t", 5)
    assert len(logical_calls(shown)) == 5 and coverage["quota_met"] is True
    summary = memory.summarize_tools(shown)
    assert summary.splitlines()[-1] == "? run_command cmd=make (started; no outcome recorded)"
    assert len(summary.splitlines()) == 5
    types = [_event_from_log_entry("tools", n, row, tmp_path)["type"] for n, row in enumerate(rows[-3:])]
    assert types == ["tool_call_started", "tool_call", "tool_call_started"]
