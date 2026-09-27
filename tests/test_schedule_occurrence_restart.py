"""Durable enqueue refusal, consumed before retry; absence never proves unrun."""
from __future__ import annotations

import copy

import pytest

from ouroboros.task_results import load_task_result, write_task_result
from supervisor import queue_schedules
from supervisor import schedule_occurrence as occurrence
from tests import test_schedule_occurrence as fixtures

_row, _rows, q = fixtures._row, fixtures._rows, fixtures.q

pytestmark = pytest.mark.serial


def _capacity_refusal(q, monkeypatch):
    from ouroboros import consciousness_allowance

    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_MAX_TASKS", "0")
    monkeypatch.setattr(consciousness_allowance, "allowance_window", lambda _root: {
        "status": "available", "limit_usd": 10.0, "accounted_usd": 0.0, "unknown_unmetered": 0, "resets_at": ""})
    _row(q, intent={"kind": "system_repo"}, metadata={"initiator": "consciousness"})
    q.queue.check_scheduled_tasks()
    row = _rows(q)["s1"]
    assert row["occurrence"]["admission"] == "refused" and not q.pending
    assert not load_task_result(q.root, row["occurrence"]["task_id"])
    occurrence._FRESH_CLAIMS.clear()
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_MAX_TASKS", "1")
    return copy.deepcopy(row["occurrence"])


@pytest.mark.parametrize("failure", ["false", "raise", "no_write"])
def test_refusal_must_be_durably_consumed_before_enqueue(q, monkeypatch, failure):  # noqa: F811
    held = _capacity_refusal(q, monkeypatch)
    original = queue_schedules._write_scheduled_tasks

    def fail_consumption(data, *args, **kwargs):
        current = data["tasks"][0]["occurrence"]
        if "admission" not in current:
            if failure == "raise":
                raise OSError("isolated refusal consumption failed")
            return False if failure == "false" else None
        return original(data, *args, **kwargs)

    monkeypatch.setattr(queue_schedules, "_write_scheduled_tasks", fail_consumption)
    monkeypatch.setattr(q.queue, "enqueue_task", lambda *_a, **_k: pytest.fail("unconsumed witness admitted"))
    q.queue.check_scheduled_tasks()
    assert not q.pending and _rows(q)["s1"]["occurrence"] == held


def test_crash_after_consumption_and_enqueue_without_receipt_stays_unknown(q, monkeypatch):  # noqa: F811
    held = _capacity_refusal(q, monkeypatch)
    original = occurrence._write_receipt

    def crash(*_args):
        assert [task["id"] for task in q.pending] == [held["task_id"]]
        assert "admission" not in _rows(q)["s1"]["occurrence"]
        raise RuntimeError("process lost before receipt")

    monkeypatch.setattr(occurrence, "_write_receipt", crash)
    with pytest.raises(RuntimeError, match="process lost"):
        q.queue.check_scheduled_tasks()
    occurrence._FRESH_CLAIMS.clear()
    q.pending.clear()
    monkeypatch.setattr(occurrence, "_write_receipt", original)
    q.queue.check_scheduled_tasks()
    assert not q.pending
    assert _rows(q)["s1"]["hold"]["reason"] == "occurrence_evidence_missing"
    assert _rows(q)["s1"]["occurrence"]["task_id"] == held["task_id"]


@pytest.mark.parametrize("case", ["foreign", "unreadable", "dispatch_row", "accepted", "owner_hold"])
def test_refusal_never_overrides_current_admission_or_unknown_source(q, monkeypatch, case):  # noqa: F811
    held = _capacity_refusal(q, monkeypatch)
    task_id, token = held["task_id"], held["token"]
    if case == "foreign":
        write_task_result(q.root, task_id, "scheduled", schedule_admission={"token": "foreign"})
    elif case == "unreadable":
        path = q.root / f"task_results/{task_id}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text("{not-json")
    elif case == "dispatch_row":
        with queue_schedules.schedule_transaction(q.root):
            data = queue_schedules.load_schedule_store(q.root)
            data["tasks"][0]["occurrence"]["dispatch"] = "possible"
            queue_schedules._write_scheduled_tasks(data)
    else:
        frozen = {"id": task_id, "type": "task", "text": "ACCEPTED ORIGINAL", "chat_id": 42,
                  "metadata": {"schedule_occurrence": {"schedule_id": "s1", "token": token}}}
        write_task_result(q.root, task_id, "scheduled", schedule_admission={
            "schedule_id": "s1", "token": token, "dispatch": "none", "task": frozen},
            **({"_owner_hold": {"source": "owner", "revision": "latest"}} if case == "owner_hold" else {}))
    for _ in range(2):
        q.queue.check_scheduled_tasks()
    if case in {"accepted", "owner_hold"}:
        [task] = q.pending
        assert task["id"] == task_id and task["text"] == "ACCEPTED ORIGINAL"
        if case == "owner_hold":
            assert task["_owner_hold"]["revision"] == "latest"
            assert occurrence.record_dispatch_possible(task) is False
    else:
        assert not q.pending
        if case != "dispatch_row":
            assert _rows(q)["s1"]["hold"]["reason"] == (
                "occurrence_task_conflict" if case == "foreign" else "occurrence_result_unreadable")


def test_receipt_loss_after_dispatch_cannot_replay_old_refusal(q, monkeypatch):  # noqa: F811
    held = _capacity_refusal(q, monkeypatch)
    q.queue.check_scheduled_tasks()
    [task] = q.pending
    assert occurrence.record_dispatch_possible(task)
    q.pending.clear()
    occurrence._FRESH_CLAIMS.clear()
    (q.root / f'task_results/{held["task_id"]}.json').unlink()
    for _ in range(2):
        q.queue.check_scheduled_tasks()
    assert not q.pending and "occurrence" not in _rows(q)["s1"]
    assert _rows(q)["s1"]["last_task_id"] == held["task_id"]


def test_accepted_republish_refusal_does_not_claim_never_admitted(q, monkeypatch):  # noqa: F811
    held = _capacity_refusal(q, monkeypatch)
    q.queue.check_scheduled_tasks()
    q.pending.clear()
    occurrence._FRESH_CLAIMS.clear()
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_MAX_TASKS", "0")
    q.queue.check_scheduled_tasks()
    current = _rows(q)["s1"]["occurrence"]
    assert current["phase"] == "admitted" and "admission" not in current
    (q.root / f'task_results/{held["task_id"]}.json').unlink()
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_MAX_TASKS", "1")
    q.queue.check_scheduled_tasks()
    assert not q.pending and _rows(q)["s1"]["hold"]["reason"] == "occurrence_evidence_missing"


def test_stale_preparation_cannot_duplicate_or_replace_accepted_source(q, monkeypatch):  # noqa: F811
    held = _capacity_refusal(q, monkeypatch)
    stale = occurrence.prepare(occurrence.view(_rows(q)["s1"]))
    q.queue.check_scheduled_tasks()
    occurrence.admit([stale])
    assert [task["id"] for task in q.pending] == [held["task_id"]]
    q.pending.clear()
    stale["task"]["text"] = "STALE PREPARATION"
    occurrence.admit([stale])
    assert not q.pending
    q.queue.check_scheduled_tasks()
    [task] = q.pending
    assert task["id"] == held["task_id"] and task["text"] != "STALE PREPARATION"
