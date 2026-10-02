"""Owner D10: Pause of an answered root while its retained late acceptance is live.

The real historical review owner, its retained operation and settlement, the
owner's Pause ingress, the assignment tick's Pause census and the Resume seam.
The reviewer transport is the shared synthetic one (``late`` fixture): it still
reserves, dispatches and settles every send through the usage ledger, so the
owner fence gates new sends exactly as in production.
"""

from __future__ import annotations

import queue
import threading
import time

import pytest

from ouroboros import review_operation
from ouroboros.task_results import load_task_result
from supervisor import events_chat_delivery as chat
from tests.test_acceptance_history import _caller, _request, _source
from tests.test_acceptance_late_consumers import delivered
from tests.test_acceptance_late_consumers import late as late  # noqa: F401 — fixture
from tests.test_review_operation_collection import _send_ctx
from tests.test_review_operation_collection import fresh_sends as fresh_sends  # noqa: F401 — fixture
from tests.test_review_operation_lifetime import until


def _census(root) -> dict:
    from ouroboros.gateway.state import _chat_activities_snapshot_safe

    return {row["activity_id"]: row["phase"] for row in _chat_activities_snapshot_safe(root, {}, direct_turns=[])}


def _tick(q):
    """The assignment tick's observe-only Pause census (its per-root throttle reset)."""
    from supervisor import owner_pause_control

    owner_pause_control._LAST_SETTLE_CHECK.clear()
    return owner_pause_control.settle_requested_owner_pauses(q)


@pytest.mark.parametrize("registered", [False, True])
def test_pause_during_sent_late_acceptance_collects_it_once_and_releases_when_nothing_remains(
        late, tmp_path, monkeypatch, registered):  # noqa: F811
    """Pause is accepted after delivery (with and without a RUNNING row); the already
    dispatched panel finishes and is collected once — never re-sent — and the delivered
    answer stays exactly as it was. Nothing was left to defer, so the Pause releases itself."""
    from ouroboros.owner_pause import read_fence
    from supervisor.owner_pause_control import request_owner_pause
    from tests._budget_pause_exact_helpers import _install_queue

    f = delivered(tmp_path, monkeypatch)
    q, _state, workers = _install_queue(f.root, monkeypatch)
    ctx = _caller(f)
    ctx.event_queue = queue.Queue()
    release = threading.Event()
    late.gates.append(release)
    before = load_task_result(f.root, f.tid)
    try:
        start = _request(f, ctx, _source(ctx, text="Review this delivered historical answer."))
        assert start["status"] == "pending", start
        until(lambda: len(late.calls) == 3)
        if registered:
            workers.RUNNING[f.tid] = {"task": f.task, "attempt": 1, "worker_id": 0}
        pause = request_owner_pause(f.tid, request_id="late-acceptance-pause")
        assert pause["ok"] and pause["state"] == "requested", pause
        assert _census(f.root)[f.tid] == "budget_pausing"
        assert _tick(q) == [] and read_fence(f.root, f.tid)["state"] == "requested"  # sent work still settling
    finally:
        workers.RUNNING.clear()
        release.set()
    until(lambda: not review_operation._LIVE)
    after = load_task_result(f.root, f.tid)
    assert after["result"] == before["result"] and after["status"] == before["status"]
    assert len(late.calls) == 3  # the dispatched panel was collected, never reissued
    panels = (after.get("review_projection") or {}).get("panels") or []
    assert panels and all(actor.get("operation_state") != "not_dispatched"
                          for panel in panels for actor in panel.get("actors") or [])
    _tick(q)
    assert read_fence(f.root, f.tid)["state"] == "released" and f.tid not in q.BUDGET_ROOT_FENCES
    assert f.tid not in _census(f.root)
    # The same frozen subject keeps its single paid identity: a new owner request only collects.
    again = _request(f, ctx, _source(ctx, text="Review the same delivered answer again."))
    until(lambda: not review_operation._LIVE)
    assert again["status"] != "pending" and len(late.calls) == 3, again


def test_pause_defers_an_unsent_automatic_late_review_until_resume(late, tmp_path, monkeypatch):  # noqa: F811
    """An automatic late review still preparing (nothing sent) is deferred by the Pause —
    not cancelled — shows as Paused, and runs exactly once after the owner's Resume."""
    from ouroboros.owner_pause import read_fence
    from supervisor.owner_pause_control import request_owner_pause
    from tests._budget_pause_exact_helpers import _install_queue

    f = delivered(tmp_path, monkeypatch, receipt="owed")
    q, _state, workers = _install_queue(f.root, monkeypatch)
    workers.RUNNING[f.tid] = {"task": f.task, "attempt": 1, "worker_id": 0}  # its writer still runs
    chat._handle_send_message(f.event, _send_ctx(f.root, []))
    until(lambda: bool(review_operation._LIVE))
    operation = next(iter(review_operation._LIVE.values()))
    pause = request_owner_pause(f.tid, request_id="defer-automatic-review")
    assert pause["ok"], pause
    workers.RUNNING.clear()  # the original writer ends while the Pause stands
    time.sleep(0.6)  # several preparation polls and control re-reads
    assert operation.control() is None and not operation.closed and not late.calls
    pointer = next(iter(load_task_result(f.root, f.tid)["review_operations"].values()))
    assert pointer["state"] == "preparing"
    _tick(q)
    assert read_fence(f.root, f.tid)["state"] == "paused"  # nothing sent is in flight
    assert _census(f.root)[f.tid] == "budget_paused"
    resumed = q.resume_budget_paused_task(f.tid)
    assert resumed["ok"] and resumed["owner_pause_released"], resumed
    until(lambda: len(late.calls) == 3)
    until(lambda: not review_operation._LIVE)
    assert len(late.calls) == 3 and read_fence(f.root, f.tid)["state"] == "released"
