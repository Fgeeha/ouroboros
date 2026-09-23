"""Exact mid-run budget pause and owner-granted same-ID Resume (#1196).

Static authoring note: these tests were WRITTEN against the candidate but NOT
RUN by their author (no runtime imports were permitted in that lane); the
parent's isolated harness is the first execution.

The pausing half is a HOLD, not a fallback: once the dispatch fence closes the
task stays fenced and nonterminal until its producers are quiescent and its
continuation is stored. These tests therefore drive the hold with a stubbed
``_hold_control_reason`` (the task's existing control rail) rather than waiting
on a real clock, and pin a shortened ``_HOLD_POLL_SEC``.
"""

from __future__ import annotations

import json
import pathlib
import time
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------- helpers

def _install_queue(tmp_path, monkeypatch):
    from supervisor import queue, state, workers

    state.init(tmp_path, total_budget_limit=10.0)
    queue.init(tmp_path)
    workers.DRIVE_ROOT = tmp_path
    queue.DRIVE_ROOT = tmp_path
    workers.PENDING[:] = []
    workers.RUNNING.clear()
    workers.WORKERS.clear()
    queue.BUDGET_ROOT_FENCES.clear()
    queue.init_queue_refs(workers.PENDING, workers.RUNNING, workers.QUEUE_SEQ_COUNTER_REF)
    monkeypatch.setattr(workers, "load_state", lambda: {"owner_chat_id": 0})
    monkeypatch.setattr(queue, "load_state", lambda: {"owner_chat_id": 0}, raising=False)
    return queue, state, workers


def _running_row(root, task_id):
    from ouroboros.task_results import STATUS_RUNNING, write_task_result

    write_task_result(root, task_id, STATUS_RUNNING, result="running")


def _loop_ctx(root, task_id="pause-task", *, direct=False, attempt=1):
    ctx = SimpleNamespace(
        task_id=task_id, task_attempt=attempt, drive_root=root, budget_drive_root=root,
        is_direct_chat=direct, owner_wait_callback=(lambda *_a, **_k: "owner_input"),
        task_started_at=time.time() - 100.0, root_task_id=task_id,
        _cost_ceiling=None, model_wait_context=None, context_fit_plan=None,
        _owner_directives=[], _delivery_candidate=None, _accumulated_usage={},
        task_metadata={}, active_model="m", active_effort="high", active_use_local=False,
        active_context_mode="max", event_queue=None,
    )
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
            {"id": "call_b", "type": "function", "function": {"name": "y", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_a", "content": "done a"},
    ]
    limit_ctx = SimpleNamespace(
        tools=SimpleNamespace(_ctx=ctx), accumulated_usage={"cost": 1.25, "execution_status": "failed",
                                                              "reason_code": "budget_exhausted"},
        messages=messages, llm_trace={"tool_calls": [1]}, owner_msg_seen={"m1"}, round_idx=4,
        tool_schemas=[{"type": "function", "function": {"name": "x"}}],
    )
    return ctx, limit_ctx


def _controls(*reasons):
    """A ``_hold_control_reason`` stub: these reasons in order, then silence."""
    remaining = list(reasons)
    return lambda _ctx: remaining.pop(0) if remaining else ""


def _fast_hold(monkeypatch, budget_pause):
    """Poll fast and leave the task's controls quiet unless a test says otherwise."""
    monkeypatch.setattr(budget_pause, "_HOLD_POLL_SEC", 0.01)
    monkeypatch.setattr(budget_pause, "_hold_control_reason", lambda _ctx: "")


def _pause(tmp_path, monkeypatch, *, task_id="pause-task", rail=None, scope="global",
           root_task_id=None):
    from ouroboros import budget_pause

    rail = rail or budget_pause.RAIL_GLOBAL_EXHAUSTED
    _running_row(tmp_path, task_id)
    ctx, limit_ctx = _loop_ctx(tmp_path, task_id)
    ctx.root_task_id = root_task_id or task_id
    _fast_hold(monkeypatch, budget_pause)
    monkeypatch.setattr(budget_pause, "observe_external_runs", lambda _ctx, request_stop=True: {
        "runs": [{"run_id": "run-1", "state": "stop_requested", "stop_outcome": "requested"}],
        "observed_at": time.time(), "custody_read": "ok", "coverage_basis": "test"})
    with pytest.raises(budget_pause.BudgetPauseRequested) as raised:
        budget_pause.request_pause(limit_ctx, rail=rail, scope=scope, reason_text="money gone",
                                   root_task_id=root_task_id or task_id)
    return ctx, limit_ctx, raised.value.pause


# --------------------------------------------------------------------------- program counter

def test_unanswered_tool_calls_are_execution_unknown_not_replayed():
    from ouroboros import budget_pause

    messages = [
        {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}, {"id": "c"}]},
        {"role": "tool", "tool_call_id": "b", "content": "ok"},
    ]
    assert budget_pause.pending_tool_call_ids(messages) == ["a", "c"]
    point = budget_pause.resume_point(messages, 7)
    assert point["phase"] == "partial_tool_batch_unknown"
    assert point["unanswered_policy"] == "not_re_executed_execution_unknown"
    assert budget_pause.resume_point([{"role": "assistant", "content": "hi"}], 1)["phase"] == "boundary"


# --------------------------------------------------------------------------- loop-side pause

def test_direct_actor_is_excluded_loudly_and_fence_stays_open(tmp_path):
    from ouroboros import budget_pause

    ctx, limit_ctx = _loop_ctx(tmp_path, direct=True)
    assert budget_pause.request_pause(limit_ctx, rail=budget_pause.RAIL_GLOBAL_EXHAUSTED,
                                      scope="global", reason_text="x") is None
    assert limit_ctx.accumulated_usage["exact_pause_unavailable"] == "direct_actor"
    assert not budget_pause.dispatch_fenced(ctx.task_id)


def test_pause_writes_source_and_row_before_raising_and_closes_fence(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    from ouroboros.artifacts import read_actor_source_bytes

    ctx, limit_ctx, pause = _pause(tmp_path, monkeypatch)
    try:
        assert pause["state"] == budget_pause.STATE_PAUSING
        assert pause["exact_continuation"] is True and pause["replay_safe"] is False
        assert pause["resume_point"]["unanswered_tool_call_ids"] == ["call_b"]
        row = budget_pause.budget_pause_row(tmp_path, ctx.task_id)
        assert row["pause_id"] == pause["pause_id"] and row["task_attempt"] == 1
        state = json.loads(read_actor_source_bytes(tmp_path, ctx.task_id, row["source_ref"]))
        # The rail's terminal projection must not travel into the continuation.
        assert "execution_status" not in state["usage"] and "reason_code" not in state["usage"]
        assert state["usage"]["cost"] == 1.25 and state["round_idx"] == 4
        assert state["messages"] == limit_ctx.messages and state["seen"] == ["m1"]
        assert limit_ctx.accumulated_usage["reason_code"] == "budget_paused"
        assert budget_pause.dispatch_fenced(ctx.task_id)
        # Ledger seam: no NEW send under the fenced task.
        from ouroboros import usage_accounting as ua

        with pytest.raises(ua.DispatchFenced):
            ua.reserve_attempt(ua.AttemptRequest(model="m", provider="test", drive_root=tmp_path,
                                                 task_id=ctx.task_id, root_task_id=ctx.task_id))
    finally:
        budget_pause.end_dispatch_fence(ctx.task_id)


def test_storage_failure_keeps_a_fenced_nonterminal_hold_and_buys_no_model_call(tmp_path, monkeypatch):
    """A write that fails is a RETAINED hold, never a claimed pause and never
    the old paid terminal rail: the fence stays closed and the task keeps its
    worker until its own control ends the hold."""
    from ouroboros import budget_pause, utils
    from ouroboros.model_wait import ModelWaitInterrupted

    _running_row(tmp_path, "hold-store")
    ctx, limit_ctx = _loop_ctx(tmp_path, "hold-store")
    monkeypatch.setattr(utils, "update_json_locked",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("read-only file system")))
    published = []
    monkeypatch.setattr(budget_pause, "_publish_hold", lambda _c, row: published.append(row))
    monkeypatch.setattr(budget_pause, "_HOLD_POLL_SEC", 0.01)
    monkeypatch.setattr(budget_pause, "_hold_control_reason", _controls("", "", "cancelled"))
    with pytest.raises(ModelWaitInterrupted):
        budget_pause.request_pause(limit_ctx, rail=budget_pause.RAIL_GRACEFUL_CEILING,
                                   scope="root", reason_text="x")
    usage = limit_ctx.accumulated_usage
    assert usage["exact_pause_unavailable"] == "hold_ended_by_control"
    assert usage["budget_pause_hold"]["hold_reason"] == budget_pause.HOLD_PAUSE_RECORD_UNWRITABLE
    assert "read-only file system" in usage["budget_pause_hold"]["error"]
    assert usage["budget_pause_hold"]["ended_by"] == "cancelled"
    # The hold was owner-visible while it lasted, and announced once per reason
    # (a poll interval is not a ledger cadence), then closed explicitly.
    still_held = [row for row in published if row["state"] == budget_pause.STATE_PAUSING]
    assert [row["hold_reason"] for row in still_held] == [budget_pause.HOLD_PAUSE_RECORD_UNWRITABLE]
    assert published[-1]["state"] == "hold_ended" and published[-1]["ended_by"] == "cancelled"
    # No pause was claimed, no wrap-up was bought, and the fence never reopened.
    assert "budget_pause" not in usage and usage.get("reason_code") == "budget_exhausted"
    assert budget_pause.dispatch_fenced("hold-store")
    budget_pause.end_dispatch_fence("hold-store")


def test_stop_during_a_hold_ends_it_through_the_existing_control_rail(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    from ouroboros.model_wait import ModelWaitInterrupted

    _running_row(tmp_path, "hold-stop")
    ctx, limit_ctx = _loop_ctx(tmp_path, "hold-stop")
    monkeypatch.setattr(budget_pause, "_HOLD_POLL_SEC", 0.01)
    monkeypatch.setattr(budget_pause, "local_producer_observation",
                        lambda _c, timeout_sec: {"quiescent": False, "review_attempts": {},
                                                 "tool_futures": {}})
    monkeypatch.setattr(budget_pause, "_hold_control_reason", _controls("", "", "cancelled"))
    with pytest.raises(ModelWaitInterrupted) as raised:
        budget_pause.request_pause(limit_ctx, rail=budget_pause.RAIL_GLOBAL_EXHAUSTED,
                                   scope="global", reason_text="x")
    assert raised.value.control_reason == "cancelled"
    # The pausing row opened; the control closed it as abandoned rather than
    # leaving a half-written pause claiming to be durable.
    row = budget_pause.budget_pause_row(tmp_path, "hold-stop")
    assert row["state"] == budget_pause.STATE_ABANDONED
    assert row["abandon_reason"] == "hold_ended_by_control:cancelled"
    assert budget_pause.has_budget_pause_checkpoint(tmp_path, "hold-stop", 1) is False
    assert budget_pause.dispatch_fenced("hold-stop")  # no fence reopen
    budget_pause.end_dispatch_fence("hold-stop")


def test_hold_controls_are_the_existing_ones_and_a_finalize_request_is_not_one(tmp_path, monkeypatch):
    """The hold borrows the task's own control rail and invents none: Stop,
    Panic, an explicit deadline and the finite lifetime end it; 'finalize now'
    and a closed wait do not abort a pause the fence already committed to."""
    from ouroboros import budget_pause, cancel_intents, model_wait

    ctx, _limit = _loop_ctx(tmp_path, "controls-1")
    monkeypatch.setattr(cancel_intents, "cancel_pending", lambda *_a, **_k: False)
    monkeypatch.setattr(model_wait, "current_model_wait",
                        lambda: SimpleNamespace(control_reason=lambda: "finalize_requested"))
    assert budget_pause._hold_control_reason(ctx) == ""
    monkeypatch.setattr(model_wait, "current_model_wait",
                        lambda: SimpleNamespace(control_reason=lambda: "absolute_ceiling"))
    assert budget_pause._hold_control_reason(ctx) == "absolute_ceiling"
    # No bound wait: the owner-stop flags the restore gate reads answer instead.
    monkeypatch.setattr(model_wait, "current_model_wait", lambda: None)
    assert budget_pause._hold_control_reason(ctx) == ""
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "panic_stop.flag").write_text("panic")
    assert budget_pause._hold_control_reason(ctx) == "panic"


def test_pending_review_attempt_blocks_release_then_pauses_once_it_settles(tmp_path, monkeypatch):
    """Quiescence is the release gate, not a refusal: the task holds while an
    already-sent review attempt is open and pauses exactly when it settles."""
    from ouroboros import budget_pause, review_custody as rc

    _running_row(tmp_path, "busy-1")
    ctx, limit_ctx = _loop_ctx(tmp_path, "busy-1")
    monkeypatch.setattr(budget_pause, "_HOLD_POLL_SEC", 0.01)
    monkeypatch.setattr(budget_pause, "observe_external_runs", lambda _c, request_stop=True: {
        "runs": [], "observed_at": time.time(), "custody_read": "ok", "coverage_basis": "test"})
    holds = []
    monkeypatch.setattr(budget_pause, "_publish_hold", lambda _c, row: holds.append(row))
    open_attempt = rc.ActiveReviewAttempt(key="k", operation_id="op-open", wave_key="task_acceptance|busy-1|r")
    with rc._ACTIVE_LOCK:
        rc._ACTIVE["busy-k"] = open_attempt
    # The attempt settles through its OWN custody path after the first hold.
    monkeypatch.setattr(budget_pause, "_hold_control_reason",
                        lambda _ctx: (open_attempt.event.set() if holds else None) or "")
    try:
        with pytest.raises(budget_pause.BudgetPauseRequested) as raised:
            budget_pause.request_pause(limit_ctx, rail=budget_pause.RAIL_GLOBAL_EXHAUSTED,
                                       scope="global", reason_text="x")
    finally:
        with rc._ACTIVE_LOCK:
            rc._ACTIVE.pop("busy-k", None)
        budget_pause.end_dispatch_fence("busy-1")
    assert holds[0]["hold_reason"] == budget_pause.HOLD_PRODUCERS_UNSETTLED
    assert holds[0]["unsettled"]["review_attempts"][0]["operation_id"] == "op-open"
    assert holds[0]["state"] == budget_pause.STATE_PAUSING  # nonterminal throughout
    # The row was never abandoned: the same pause id carries through to the row.
    row = budget_pause.budget_pause_row(tmp_path, "busy-1")
    assert row["state"] == budget_pause.STATE_PAUSING and row["source_ref"]
    assert row["pause_id"] == raised.value.pause["pause_id"]
    assert "budget_pause_hold" not in limit_ctx.accumulated_usage


def test_timed_out_tool_future_blocks_release_until_its_settlement_callback_finishes(tmp_path):
    """``future.done()`` is not callback-complete: a call abandoned at its own
    timeout keeps the task unquiescent until the late settlement callback that
    owns its effects has finished."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from ouroboros import budget_pause

    ctx, _limit = _loop_ctx(tmp_path, "tool-1")
    gate = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(gate.wait, 5.0)
        budget_pause.register_tool_future(ctx, "call_slow", "run_command", future)
        drain = budget_pause.drain_local_tool_futures(ctx, timeout_sec=0.05)
        assert drain["drained"] is False
        assert drain["unsettled"] == [{"operation_id": "call_slow", "tool": "run_command",
                                       "state": "running"}]
        # The timeout path claims the row BEFORE the worker settles.
        release = budget_pause.hold_tool_settlement(ctx, "call_slow")
        gate.set()
        assert future.result(timeout=5.0) is True
        assert budget_pause.drain_local_tool_futures(ctx, timeout_sec=0.05)["drained"] is False
        release()
        settled = budget_pause.drain_local_tool_futures(ctx, timeout_sec=1.0)
        assert settled["drained"] is True
        assert settled["settled"] == [{"operation_id": "call_slow", "tool": "run_command"}]
    finally:
        gate.set()
        executor.shutdown(wait=True)
        budget_pause.forget_tool_scope(ctx)


def test_tool_future_quiescence_is_scoped_to_one_attempt_and_prunes_itself(tmp_path):
    from concurrent.futures import Future

    from ouroboros import budget_pause

    ctx, _limit = _loop_ctx(tmp_path, "tool-2", attempt=1)
    later, _l = _loop_ctx(tmp_path, "tool-2", attempt=2)
    done = Future()
    done.set_result("x")
    try:
        budget_pause.register_tool_future(ctx, "call_a", "read_file", done)
        assert budget_pause.drain_local_tool_futures(ctx, timeout_sec=0.1)["drained"] is True
        # A later attempt never inherits a previous attempt's observations.
        assert budget_pause.drain_local_tool_futures(later, timeout_sec=0.0) == {
            "drained": True, "registry": "ok", "settled": [], "unsettled": []}
        # A settled, unheld row is pruned by the next registration.
        second = Future()
        second.set_result("y")
        budget_pause.register_tool_future(ctx, "call_b", "read_file", second)
        assert [row["operation_id"] for row in
                budget_pause.drain_local_tool_futures(ctx, timeout_sec=0.1)["settled"]] == ["call_b"]
    finally:
        budget_pause.forget_tool_scope(ctx)
        budget_pause.forget_tool_scope(later)


def test_unreadable_external_custody_is_held_as_unknown_not_clean(tmp_path, monkeypatch):
    from ouroboros import budget_pause

    ctx, _limit = _loop_ctx(tmp_path)
    monkeypatch.setattr(budget_pause, "_external_run_rows", lambda _c: ([], "OSError: boom"))
    observed = budget_pause.observe_external_runs(ctx)
    assert observed["custody_read"] == "failed" and observed["runs"] == []
    assert "boom" in observed["error"]


def test_pausing_row_exists_before_any_wait_and_fences_crash_retry(tmp_path, monkeypatch):
    """The durable ``pausing`` row opens BEFORE the task waits on anything, so a
    death while it is still settling meets the crash-retry fence, not a replay."""
    from ouroboros import budget_pause
    from ouroboros.model_wait import ModelWaitInterrupted

    _running_row(tmp_path, "drain-1")
    ctx, limit_ctx = _loop_ctx(tmp_path, "drain-1")
    monkeypatch.setattr(budget_pause, "_HOLD_POLL_SEC", 0.01)
    seen = {}

    def _observe_during_hold(_ctx, *, timeout_sec):
        seen["row"] = budget_pause.budget_pause_row(tmp_path, "drain-1")
        raise RuntimeError("simulated death while settling")

    monkeypatch.setattr(budget_pause, "local_producer_observation", _observe_during_hold)
    monkeypatch.setattr(budget_pause, "_hold_control_reason", _controls("", "", "panic"))
    with pytest.raises(ModelWaitInterrupted):
        budget_pause.request_pause(limit_ctx, rail=budget_pause.RAIL_GLOBAL_EXHAUSTED,
                                   scope="global", reason_text="x")
    assert seen["row"]["state"] == budget_pause.STATE_PAUSING and seen["row"]["source_ref"] is None
    # An observation that RAISED proves nothing about quiescence: it holds.
    held = limit_ctx.accumulated_usage["budget_pause_hold"]
    assert held["hold_reason"] == budget_pause.HOLD_PRODUCERS_UNSETTLED
    assert "simulated death" in held["unsettled"]["observation_error"]
    budget_pause.end_dispatch_fence("drain-1")
    # While that row was live (no source yet) the crash-retry fence already held;
    # an unreadable record fails closed the same way.
    monkeypatch.setattr(budget_pause, "budget_pause_row",
                        lambda *_a: (_ for _ in ()).throw(OSError("unreadable")))
    assert budget_pause.has_budget_pause_checkpoint(tmp_path, "drain-1", 1) is True


def test_light_extraction_is_not_dispatched_under_the_fence(monkeypatch):
    from ouroboros import budget_pause, review_verdict_extraction as rve
    from ouroboros import usage_accounting as ua

    monkeypatch.setattr(ua, "current_usage_scope", lambda: ua.UsageScope(task_id="fenced-task"))
    budget_pause.begin_dispatch_fence("fenced-task")
    try:
        canonical, usage = rve._extract_verdict_via_light_model("free text verdict", contract="c")
        assert canonical is None
        assert usage["reason_code"] == "budget_pausing_no_extraction"
        assert usage["dispatch"] == "not_dispatched"
    finally:
        budget_pause.end_dispatch_fence("fenced-task")


def test_local_review_drain_lists_unsettled_attempts_without_settling_them():
    import threading

    from ouroboros import budget_pause, review_custody as rc

    settled = rc.ActiveReviewAttempt(key="triad|t9|r", operation_id="op-settled", wave_key="task_acceptance|t9|r")
    settled.event.set()
    open_attempt = rc.ActiveReviewAttempt(key="triad|t9|r2", operation_id="op-open", wave_key="task_acceptance|t9|r2")
    foreign = rc.ActiveReviewAttempt(key="triad|other|r", operation_id="op-foreign", wave_key="task_acceptance|other|r")
    with rc._ACTIVE_LOCK:
        rc._ACTIVE.update({"k1": settled, "k2": open_attempt, "k3": foreign})
    try:
        drain = budget_pause.drain_local_review_attempts("t9", timeout_sec=0.05)
        assert [row["operation_id"] for row in drain["settled"]] == ["op-settled"]
        assert [row["operation_id"] for row in drain["unsettled"]] == ["op-open"]
        assert drain["drained"] is False
        assert not open_attempt.event.is_set()  # never settled by the drain
    finally:
        with rc._ACTIVE_LOCK:
            for key in ("k1", "k2", "k3"):
                rc._ACTIVE.pop(key, None)


# --------------------------------------------------------------------------- supervisor: park

def _supervisor_ctx(tmp_path, workers, queue, persisted, pushed):
    return SimpleNamespace(
        DRIVE_ROOT=tmp_path, RUNNING=workers.RUNNING, PENDING=workers.PENDING, WORKERS=workers.WORKERS,
        sort_pending=lambda: None,
        persist_queue_snapshot=lambda reason="": (persisted.append(reason) or True),
        bridge=SimpleNamespace(push_log=lambda event: pushed.append(event)),
    )


def test_exact_pause_event_parks_same_task_id_and_confirms_row(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    from supervisor.events import _handle_budget_pause

    queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    ctx, _limit, pause = _pause(tmp_path, monkeypatch, scope="root")
    budget_pause.end_dispatch_fence(ctx.task_id)
    task = {"id": ctx.task_id, "type": "task", "chat_id": 3, "root_task_id": ctx.task_id, "_attempt": 1}
    worker = SimpleNamespace(busy_task_id=ctx.task_id)
    workers.RUNNING[ctx.task_id] = {"task": task, "worker_id": 0, "attempt": 1}
    workers.WORKERS[0] = worker
    persisted, pushed = [], []
    sctx = _supervisor_ctx(tmp_path, workers, queue, persisted, pushed)

    event = budget_pause.pause_event(task, pause)
    assert event["resource_limit"]["exact_continuation"] is True
    _handle_budget_pause({**event, "worker_id": 0}, sctx)

    assert workers.RUNNING == {} and worker.busy_task_id is None
    marker = workers.PENDING[0]["_budget_pause"]
    assert marker["exact_continuation"] is True and marker["checkpoint"]["pause_id"] == pause["pause_id"]
    assert marker["fence_id"] and queue.BUDGET_ROOT_FENCES[ctx.task_id]["status"] == "paused"
    assert budget_pause.budget_pause_row(tmp_path, ctx.task_id)["state"] == budget_pause.STATE_PAUSED
    assert persisted == ["budget_pause_exact_continuation"]
    assert pushed[0]["type"] == "budget_scope_paused" and pushed[0]["pause_id"] == pause["pause_id"]
    assert "checkpoint" not in pushed[0]
    # The queue predicate already reads the marker: no false "queued".
    from supervisor.queue_transitions import budget_pause_fact

    assert budget_pause_fact(workers.PENDING[0])["exact_continuation"] is True


def test_exact_pause_event_without_durable_row_is_refused(tmp_path, monkeypatch):
    from supervisor.events import _handle_budget_pause

    queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    _running_row(tmp_path, "ghost")
    workers.RUNNING["ghost"] = {"task": {"id": "ghost", "type": "task"}, "worker_id": 0, "attempt": 1}
    sctx = _supervisor_ctx(tmp_path, workers, queue, [], [])
    with pytest.raises(ValueError):
        _handle_budget_pause({"type": "budget_pause", "task_id": "ghost", "worker_id": 0,
                              "resource_limit": {"exact_continuation": True,
                                                 "checkpoint": {"pause_id": "nope"}}}, sctx)
    assert "ghost" in workers.RUNNING  # nothing moved without a record


def test_worker_death_during_pausing_completes_the_park_not_a_retry(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    from supervisor import worker_health

    queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    ctx, _limit, pause = _pause(tmp_path, monkeypatch)
    budget_pause.end_dispatch_fence(ctx.task_id)
    task = {"id": ctx.task_id, "type": "task", "_attempt": 1, "budget_drive_root": str(tmp_path)}
    meta = {"task": task, "worker_id": 0, "attempt": 1}
    workers.RUNNING[ctx.task_id] = meta
    workers.WORKERS[0] = SimpleNamespace(busy_task_id=ctx.task_id)
    monkeypatch.setattr(worker_health, "_dead_job_is_current", lambda job: True)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": True)
    job = {"worker": workers.WORKERS[0], "task_id": ctx.task_id, "task": task, "meta": meta,
           "worker_id": 0, "exitcode": 1, "drive_root": str(tmp_path)}
    assert worker_health._complete_exact_budget_pause_after_death(job, tmp_path, task, ctx.task_id, 1) is True
    assert workers.RUNNING == {} and workers.PENDING[0]["_budget_pause"]["exact_continuation"] is True
    assert budget_pause.budget_pause_row(tmp_path, ctx.task_id)["pause_source"] == "worker_death_during_pausing"
    # A different attempt (a retry that never saw the checkpoint) is not adopted.
    assert worker_health._complete_exact_budget_pause_after_death(job, tmp_path, task, ctx.task_id, 2) is False


# --------------------------------------------------------------------------- supervisor: resume

def _parked(tmp_path, monkeypatch, *, task_id="pause-task", scope="global", root_task_id=None, extra=None):
    from ouroboros import budget_pause
    from supervisor import queue, workers

    # ONE lineage: the durable row, its queue marker and the pending task row all
    # name the same root, or a descendant's resume cannot see its paused root.
    ctx, _limit, pause = _pause(tmp_path, monkeypatch, task_id=task_id, scope=scope,
                                root_task_id=root_task_id or task_id)
    budget_pause.end_dispatch_fence(task_id)
    row = budget_pause.budget_pause_row(tmp_path, task_id)
    assert row["root_task_id"] == (root_task_id or task_id)
    marker = budget_pause.exact_pause_marker(row, default_root=root_task_id or task_id)
    if scope == "root":
        from supervisor.events_budget import _set_root_budget_pause_locked

        fence = _set_root_budget_pause_locked(marker["root_task_id"], marker)
        marker["fence_id"] = fence["fence_id"]
    task = {"id": task_id, "type": "task", "chat_id": 0, "root_task_id": root_task_id or task_id,
            "_attempt": 1, "_budget_pause": marker, **(extra or {})}
    workers.PENDING.append(task)
    budget_pause.set_budget_pause(tmp_path, task_id, {**row, "state": budget_pause.STATE_PAUSED})
    return task, row


def test_resume_refuses_while_money_is_still_exhausted(tmp_path, monkeypatch):
    queue, state, workers = _install_queue(tmp_path, monkeypatch)
    task, _row = _parked(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda _st, **_k: 0.0)
    result = queue.resume_budget_paused_task(task["id"])
    assert result["error"] == "budget_still_exhausted"
    assert "_budget_pause" in workers.PENDING[0] and "_budget_pause_resume" not in workers.PENDING[0]


def test_resume_refuses_cancel_intent_and_paused_root(tmp_path, monkeypatch):
    queue, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda _st, **_k: 5.0)
    import supervisor.queue_transitions as qt

    task, _row = _parked(tmp_path, monkeypatch)
    monkeypatch.setattr("ouroboros.cancel_intents.has_active_intent", lambda *_a, **_k: True)
    assert queue.resume_budget_paused_task(task["id"])["error"] == "cancel_intent_active"
    monkeypatch.setattr("ouroboros.cancel_intents.has_active_intent", lambda *_a, **_k: False)

    workers.PENDING[:] = []
    root, _r = _parked(tmp_path, monkeypatch, task_id="root-1", scope="root")
    child, _c = _parked(tmp_path, monkeypatch, task_id="child-1", root_task_id="root-1")
    refused = queue.resume_budget_paused_task("child-1")
    assert refused["error"] == "root_still_paused" and refused["action"] == "resume_root_first"


def test_root_resume_mints_single_use_grant_and_only_makes_children_eligible(tmp_path, monkeypatch):
    from ouroboros import budget_pause

    queue, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda _st, **_k: 5.0)
    root, root_row = _parked(tmp_path, monkeypatch, task_id="root-2", scope="root")
    child, _c = _parked(tmp_path, monkeypatch, task_id="child-2", root_task_id="root-2")
    # Backdate the pause so the paused interval is measurable and separate.
    budget_pause.set_budget_pause(tmp_path, "root-2", {**budget_pause.budget_pause_row(tmp_path, "root-2"),
                                                         "paused_at": time.time() - 30.0})

    granted = queue.resume_budget_paused_task("root-2")
    assert granted["ok"] is True and granted["exact_continuation"] is True
    assert granted["eligible_descendants"] == ["child-2"]
    assert granted["paused_duration_sec"] >= 29
    assert "root-2" not in queue.BUDGET_ROOT_FENCES
    handoff = root["_budget_pause_resume"]
    assert handoff["grant_id"] == granted["grant_id"] and handoff["pause"]["exact_continuation"] is True
    assert handoff["started_at"] == root_row["started_at"]  # never moved
    row = budget_pause.budget_pause_row(tmp_path, "root-2")
    assert row["state"] == budget_pause.STATE_RESUME_GRANTED and row["grant"]["single_use"] is True
    # The child stayed paused: eligibility is not release.
    assert "_budget_pause" in child and "_budget_pause_resume" not in child
    # Second grant for the same pause is refused (single use).
    workers.PENDING.remove(root)
    stale_root = {**root, "_budget_pause": handoff["pause"]}
    stale_root.pop("_budget_pause_resume", None)
    workers.PENDING.append(stale_root)
    assert queue.resume_budget_paused_task("root-2")["error"] == "resume_already_granted"
    # Now the child may be selected explicitly (Q9) — under the root that was
    # actually resumed, not the stale parked copy above.
    workers.PENDING.remove(stale_root)
    workers.PENDING.append(root)
    assert queue.resume_budget_paused_task("child-2")["ok"] is True


def test_grant_is_revoked_when_money_vanishes_before_dispatch(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    from supervisor.queue_transitions import revoke_exact_budget_resume

    queue, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda _st, **_k: 5.0)
    task, _row = _parked(tmp_path, monkeypatch, task_id="revoke-1")
    assert queue.resume_budget_paused_task("revoke-1")["ok"] is True
    monkeypatch.setattr(state, "budget_remaining", lambda _st, **_k: 0.0)
    sent = []
    workers.WORKERS[0] = SimpleNamespace(wid=0, busy_task_id=None, reaping=False,
                                         in_q=SimpleNamespace(put=lambda t: sent.append(t)))
    workers.assign_tasks()
    assert sent == []
    assert task.get("_budget_pause", {}).get("exact_continuation") is True
    assert "_budget_pause_resume" not in task
    row = budget_pause.budget_pause_row(tmp_path, "revoke-1")
    assert row["state"] == budget_pause.STATE_PAUSED and row["grant"]["revoke_reason"] == "budget_exhausted_before_dispatch"
    assert revoke_exact_budget_resume(task, "again") is False  # nothing granted now


def test_granted_task_dispatches_with_original_started_at_and_paused_carrier(tmp_path, monkeypatch):
    queue, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda _st, **_k: 5.0)
    task, row = _parked(tmp_path, monkeypatch, task_id="dispatch-1")
    assert queue.resume_budget_paused_task("dispatch-1")["ok"] is True
    sent = []
    workers.WORKERS[0] = SimpleNamespace(wid=0, busy_task_id=None, reaping=False,
                                         in_q=SimpleNamespace(put=lambda t: sent.append(dict(t))))
    workers.assign_tasks()
    assert [t["id"] for t in sent] == ["dispatch-1"]
    meta = workers.RUNNING["dispatch-1"]
    assert meta["started_at"] == pytest.approx(float(row["started_at"]))
    assert meta["budget_paused_sec"] > 0
    assert sent[0]["_budget_pause_resume"]["grant_id"]


def test_paused_row_survives_stale_snapshot_restore_without_waking(tmp_path, monkeypatch):
    queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    task, _row = _parked(tmp_path, monkeypatch, task_id="stale-1")
    queue.persist_queue_snapshot(reason="test")
    workers.PENDING[:] = []
    snap_path = queue.QUEUE_SNAPSHOT_PATH
    snap = json.loads(snap_path.read_text())
    snap["ts"] = "2000-01-01T00:00:00+00:00"  # far older than any freshness window
    snap_path.write_text(json.dumps(snap))
    assert queue.restore_pending_from_snapshot() == 1
    assert workers.PENDING[0]["id"] == "stale-1"
    assert workers.PENDING[0]["_budget_pause"]["exact_continuation"] is True  # still paused, not dispatched


def test_restore_returns_undispatched_grant_to_its_pause(tmp_path, monkeypatch):
    queue, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda _st, **_k: 5.0)
    task, _row = _parked(tmp_path, monkeypatch, task_id="restart-grant")
    assert queue.resume_budget_paused_task("restart-grant")["ok"] is True
    queue.persist_queue_snapshot(reason="test")
    workers.PENDING[:] = []
    assert queue.restore_pending_from_snapshot() == 1
    restored = workers.PENDING[0]
    assert "_budget_pause_resume" not in restored and restored["_budget_pause"]["exact_continuation"] is True


# --------------------------------------------------------------------------- loop-side resume

def test_resume_consumes_grant_restores_cognition_and_never_reexecutes(tmp_path, monkeypatch):
    from ouroboros import budget_pause, owner_wait

    queue, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda _st, **_k: 5.0)
    task, row = _parked(tmp_path, monkeypatch, task_id="loop-1")
    assert queue.resume_budget_paused_task("loop-1")["ok"] is True
    handoff = task["_budget_pause_resume"]
    ctx, _limit = _loop_ctx(tmp_path, "loop-1")
    ctx.budget_pause_resume = handoff
    tools = SimpleNamespace(_ctx=ctx)
    state_blob = budget_pause.load_budget_pause(ctx)
    assert state_blob["pause_id"] == row["pause_id"]
    monkeypatch.setattr(owner_wait, "rebind_restored_route", lambda *_a, **_k: (None, "max"))
    messages, trace, usage, seen = [], {}, {}, set()
    model, effort, use_local, mode, round_idx, plan = budget_pause.resume_paused_loop(
        tools, state_blob, messages, trace, usage, seen, budget_remaining_usd=5.0)
    assert (model, round_idx, mode) == ("m", 4, "max")
    assert usage["cost"] == 1.25 and "execution_status" not in usage
    # The unanswered call is closed as UNKNOWN, not re-run, not declared un-run.
    unknown = [m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "call_b"]
    assert len(unknown) == 1 and "UNKNOWN" in unknown[0]["content"] and "NOT re-executed" in unknown[0]["content"]
    assert "budget pause" in messages[-1]["content"] and "run-1" in messages[-1]["content"]
    consumed = budget_pause.budget_pause_row(tmp_path, "loop-1")
    assert consumed["state"] == budget_pause.STATE_RESUMED and consumed["grant"]["consumed_at"]
    assert not budget_pause.dispatch_fenced("loop-1")
    # The grant is single-use: a second load refuses.
    with pytest.raises(ValueError):
        budget_pause.load_budget_pause(ctx, handoff)


def test_load_refuses_foreign_or_missing_grant(tmp_path, monkeypatch):
    from ouroboros import budget_pause

    ctx, _limit, pause = _pause(tmp_path, monkeypatch, task_id="grant-1")
    budget_pause.end_dispatch_fence("grant-1")
    with pytest.raises(ValueError):
        budget_pause.load_budget_pause(ctx, {"pause_id": pause["pause_id"], "grant_id": "forged"})


def test_graceful_rail_refreshes_planning_threshold_within_authorized_money(tmp_path, monkeypatch):
    from ouroboros import budget_pause, task_pacing
    from ouroboros import loop_budget

    ctx, _limit = _loop_ctx(tmp_path)
    ctx._cost_ceiling = task_pacing.CostCeiling(state="active", ceiling_usd=7.0, root_cap_usd=10.0,
                                                planning_margin_usd=3.0, basis="root_cap_minus_margin")
    ctx._accumulated_usage = {"cost": 8.0}
    monkeypatch.setattr(loop_budget, "_loop_tree_accounting", lambda **_k: {"accounted_usd": 8.0})
    monkeypatch.setattr(task_pacing, "resolve_budget_profile", lambda _c: {"cost_hard_stop_pct": 50})
    disclosure = budget_pause._refresh_planning_threshold(ctx, budget_remaining_usd=100.0)
    assert disclosure["refreshed"] is True
    # min(cap - spent = 2, 50% of global remaining = 50) added on top of spend: no immediate re-pause.
    assert ctx._cost_ceiling.ceiling_usd == pytest.approx(10.0)
    assert ctx._cost_ceiling.root_cap_usd == 10.0 and ctx._cost_ceiling.basis.startswith("owner_resume_refresh")
    ctx._accumulated_usage = {"cost": 10.0}
    monkeypatch.setattr(loop_budget, "_loop_tree_accounting", lambda **_k: {"accounted_usd": 10.0})
    assert budget_pause._refresh_planning_threshold(ctx, budget_remaining_usd=100.0)["refreshed"] is False


# --------------------------------------------------------------------------- gateway / UI facts

def test_state_phase_budget_pausing_reads_the_durable_row(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    from ouroboros.gateway import state as gw_state

    ctx, _limit, _pause_row = _pause(tmp_path, monkeypatch, task_id="phase-1")
    budget_pause.end_dispatch_fence("phase-1")
    row = {"_attempt": 1, "budget_drive_root": str(tmp_path)}
    assert gw_state._managed_task_budget_pausing(tmp_path, row, "phase-1") is True
    assert gw_state._managed_task_budget_pausing(tmp_path, {"_attempt": 2}, "phase-1") is False
    assert gw_state._managed_task_budget_pausing(tmp_path, {"_attempt": 1}, "absent") is False


def test_resume_child_tool_only_targets_own_children(monkeypatch):
    from ouroboros.tools import join_ledger

    ctx = SimpleNamespace(task_id="parent-1", task_metadata={})
    monkeypatch.setattr(join_ledger, "_status_drive_root", lambda _c: pathlib.Path("/tmp"))
    monkeypatch.setattr(join_ledger, "_is_own_child", lambda _c, _r, tid: tid == "child-x")
    monkeypatch.setattr(join_ledger, "_publish_tool_result", lambda _c, result: result.text)
    monkeypatch.setattr(join_ledger, "_record_child_decision_beacon", lambda *_a, **_k: None)
    emitted = []
    monkeypatch.setattr("ouroboros.tools.control._emit_control_event",
                        lambda _c, evt: (emitted.append(evt) or "live"))
    assert "not a child" in join_ledger._resume_child_task(ctx, "stranger-1", "why")
    text = join_ledger._resume_child_task(ctx, "child-x", "still needed")
    assert "Resume requested" in text and "REQUEST" in text
    assert emitted[0]["type"] == "budget_resume_child" and emitted[0]["requested_by"] == "parent-1"
