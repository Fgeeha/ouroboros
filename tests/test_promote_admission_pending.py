"""#1160: an emitted promote is a durable pending fact, never "unknown".

A promote whose admission the supervisor has not confirmed inside the
confirmation window answered `PROMOTE_UNCONFIRMED`; asking `get_task_result`
for that id then said "unknown or not yet registered", which reads as "your
promote never happened" and invites a second promote — that is how one
conversation produced duplicate roots here and on a foreign Windows install.
"""

from __future__ import annotations

import types

import pytest


class _DeadQueue:
    """A supervisor that accepted the event and has not answered yet."""

    def __init__(self):
        self.events = []

    def put_nowait(self, event):
        self.events.append(event)


def _promote_ctx(tmp_path, event_queue):
    return types.SimpleNamespace(
        pending_events=[], event_queue=event_queue, current_chat_id=1,
        drive_root=tmp_path, budget_drive_root=str(tmp_path), project_id="",
        task_metadata={}, task_id="",
    )


@pytest.fixture
def _short_confirmation_window(tmp_path, monkeypatch):
    """The busy-supervisor case, without spending its real window in a test."""
    from ouroboros.tools import control_events

    monkeypatch.setattr(control_events, "_PROMOTE_CONFIRM_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(control_events, "_PROMOTE_CONFIRM_POLL_SEC", 0.005)
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)


def test_an_unconfirmed_promote_reads_as_a_pending_admission(tmp_path, _short_confirmation_window):
    from ouroboros.task_results import load_task_result
    from ouroboros.tools.control_routing import _promote_chat_to_task
    from ouroboros.tools.control_task_results import _get_task_result

    ctx = _promote_ctx(tmp_path, _DeadQueue())
    out = _promote_chat_to_task(ctx, "Continue the racer", workspace="none",
                                predecessor_task_id="")
    assert out.startswith("⚠️ PROMOTE_UNCONFIRMED")
    task_id = ctx.event_queue.events[0]["task_id"]
    assert f"get_task_result({task_id})" in out

    stub = load_task_result(tmp_path, task_id)
    assert stub["promotion_admission"]["status"] == "emitted"
    assert stub["promotion_admission"]["routing_token"] == ctx.event_queue.events[0]["routing_token"]
    emitted_at = stub["promotion_admission"]["emitted_at"]
    assert emitted_at

    read = _get_task_result(ctx, task_id)
    assert f"admission pending since {emitted_at}" in read
    assert "unknown or not yet registered" not in read
    assert f"get_task_result({task_id})" in read

    # The quiet direction: the pending sentence belongs to an emitted stub alone,
    # so an id nobody ever promoted is still honestly unknown.
    assert "unknown or not yet registered" in _get_task_result(ctx, "never-promoted")


def test_a_confirmed_admission_reads_as_the_ordinary_result(tmp_path, _short_confirmation_window):
    """The quiet direction: once the supervisor answers, the pending sentence is
    gone and the result reads exactly as before."""
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.control_routing import _promote_chat_to_task
    from ouroboros.tools.control_task_results import _get_task_result

    ctx = _promote_ctx(tmp_path, _DeadQueue())
    _promote_chat_to_task(ctx, "Continue the racer", workspace="none", predecessor_task_id="")
    event = ctx.event_queue.events[0]
    task_id = event["task_id"]

    write_task_result(
        tmp_path, task_id, "scheduled", root_task_id=task_id, delegation_role="root",
        description="Continue the racer",
        promotion_admission={"status": "scheduled", "routing_token": event["routing_token"],
                             "confirmed_at": "2026-09-21T00:00:00Z"},
        result="Task accepted and durably scheduled.",
    )

    read = _get_task_result(ctx, task_id)
    assert "admission pending since" not in read
    assert "Task accepted and durably scheduled." in read


def test_a_fast_admission_leaves_no_emitted_stub_behind(tmp_path, _short_confirmation_window):
    """Guard quiet: the stub only ever initializes ABSENCE, so a supervisor that
    answered before the tool wrote sees its scheduled receipt untouched."""
    from ouroboros.task_results import load_task_result, write_task_result
    from ouroboros.tools.control_routing import _promote_chat_to_task

    class _FastSupervisor:
        def __init__(self):
            self.events = []

        def put_nowait(self, event):
            self.events.append(event)
            write_task_result(
                tmp_path, event["task_id"], "scheduled", root_task_id=event["task_id"],
                delegation_role="root", description=event["objective"],
                promotion_admission={"status": "scheduled",
                                     "routing_token": event["routing_token"],
                                     "confirmed_at": "2026-09-21T00:00:00Z"},
                result="Task accepted and durably scheduled.",
            )

    ctx = _promote_ctx(tmp_path, _FastSupervisor())
    out = _promote_chat_to_task(ctx, "Build the racer", workspace="none",
                                predecessor_task_id="")
    assert out.startswith("OK: task")

    stored = load_task_result(tmp_path, ctx.event_queue.events[0]["task_id"])
    assert stored["status"] == "scheduled"
    assert stored["promotion_admission"]["status"] == "scheduled"
    assert "emitted_at" not in stored["promotion_admission"]


def test_a_late_admission_replaces_the_stub_it_finds(tmp_path, _short_confirmation_window):
    """The other side of the race: the admission receipt is a whole field, so an
    emitted stub written first leaves nothing in the scheduled row."""
    from ouroboros.task_results import load_task_result, write_task_result
    from ouroboros.tools.control_routing import _promote_chat_to_task

    ctx = _promote_ctx(tmp_path, _DeadQueue())
    _promote_chat_to_task(ctx, "Build the racer", workspace="none", predecessor_task_id="")
    event = ctx.event_queue.events[0]

    write_task_result(
        tmp_path, event["task_id"], "scheduled", root_task_id=event["task_id"],
        delegation_role="root",
        promotion_admission={"status": "scheduled", "routing_token": event["routing_token"],
                             "confirmed_at": "2026-09-21T00:00:00Z"},
    )
    stored = load_task_result(tmp_path, event["task_id"])
    assert stored["promotion_admission"] == {
        "status": "scheduled", "routing_token": event["routing_token"],
        "confirmed_at": "2026-09-21T00:00:00Z",
    }


def test_the_emitted_stub_never_owns_its_own_admissions_id(tmp_path, monkeypatch, _short_confirmation_window):
    """Positive scheduling authority stays with the supervisor: the stub must not
    make the promote it belongs to look like a duplicate id at any of the three
    admission gates, while a real durable row still does."""
    import supervisor.queue as supervisor_queue
    import supervisor.workers as workers
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.control_routing import _promote_chat_to_task

    ctx = _promote_ctx(tmp_path, _DeadQueue())
    _promote_chat_to_task(ctx, "Build the racer", workspace="none", predecessor_task_id="")
    event = ctx.event_queue.events[0]
    task_id, token = event["task_id"], event["routing_token"]

    monkeypatch.setattr(supervisor_queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(supervisor_queue, "PENDING", [])
    monkeypatch.setattr(supervisor_queue, "RUNNING", {})
    monkeypatch.setattr(supervisor_queue, "ADMISSION_RESERVATIONS", {})

    assert supervisor_queue.reserve_task_admission(
        task_id, token, drive_root=tmp_path,
    )["status"] == "reserved"
    assert workers._promote_duplicate_reason(
        task_id, types.SimpleNamespace(DRIVE_ROOT=tmp_path, PENDING=[], RUNNING={}),
        admission_token=token,
    ) == ""
    queued = supervisor_queue.enqueue_task({
        "id": task_id, "type": "task", "_require_unique_task_id": True,
        "_admission_token": token,
    })
    assert "_admission_blocked" not in queued

    write_task_result(tmp_path, "other-root", "completed", description="someone else's work")
    assert supervisor_queue.reserve_task_admission(
        "other-root", token, drive_root=tmp_path,
    ) == {"status": "blocked", "reason": "duplicate_task_id"}
    assert workers._promote_duplicate_reason(
        "other-root", types.SimpleNamespace(DRIVE_ROOT=tmp_path, PENDING=[], RUNNING={}),
        admission_token=token,
    ) == "duplicate_task_id"


def test_one_runtime_limit_bounds_both_promote_confirmation_waits():
    """The two confirmation windows were byte-identical literals in two modules."""
    from ouroboros import routing_wait, runtime_limits
    from ouroboros.tools import control_events

    assert runtime_limits.get_promote_confirm_wait_sec() == 15.0
    # The routing leaf reads the bound at the wait itself (no import-time edge): its
    # default IS the getter, and an explicit caller bound still wins, floored at zero.
    assert routing_wait._confirm_wait_sec(None) == runtime_limits.get_promote_confirm_wait_sec()
    assert routing_wait._confirm_wait_sec(0.05) == 0.05 and routing_wait._confirm_wait_sec(-1) == 0.0
    assert control_events._PROMOTE_CONFIRM_TIMEOUT_SEC == runtime_limits.get_promote_confirm_wait_sec()
