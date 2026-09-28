"""Promotion, origin membership and pre-handoff failure through real queue owners."""
import copy
from types import SimpleNamespace

import pytest

from ouroboros import projects_registry as registry
from ouroboros.task_results import load_task_result, write_task_result
from supervisor import queue, workers
from tests.test_project_hold_recovery import accepted, restore_unreadable, worker
from tests.test_project_semantic_admission import room, task  # noqa: F401
from tests.test_schedule_occurrence import q  # noqa: F401
from tests.test_swarm_host_admission import host  # noqa: F401

pytestmark = pytest.mark.serial


@pytest.mark.parametrize("change", ["own_cas", "occupied", "rebind", "aba", "write_before", "write_after", "no_update"])
def test_explicit_promotion_carries_only_confirmed_own_folder_basis(host, tmp_path, monkeypatch, change):  # noqa: F811
    from supervisor.events_project_routing import _handle_promote_chat_to_task

    explicit, other = tmp_path / "explicit", tmp_path / "other"
    explicit.mkdir()
    other.mkdir()
    registry.create_project(host.root, "target", working_dir=str(other) if change == "occupied" else "")
    original = registry.project_admission_view(host.root, "target", frozen=True)
    update = registry.update_project

    def at_cas(*args, **kwargs):
        if kwargs.get("only_if_empty") != ("working_dir",):
            return update(*args, **kwargs)
        if change in {"rebind", "aba"}:
            update(host.root, "target", working_dir=str(other))
            if change == "aba":
                update(host.root, "target", working_dir="")
        if change == "no_update":
            return None
        if change == "write_before":
            raise OSError("synthetic failure before folder write")
        result = update(*args, **kwargs)
        if change == "write_after":
            raise OSError("synthetic unknown outcome after folder write")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(registry, "update_project", at_cas)
        answer = _handle_promote_chat_to_task({
            "task_id": "explicit", "routing_token": "explicit-token", "objective": "Use the explicit folder",
            "project_id": "target", "workspace_root": str(explicit), "chat_id": 1,
        }, host.ctx)
    assert answer["status"] == "scheduled"
    prepared = copy.deepcopy(host.pending[0])
    assert prepared["workspace_root"] == str(explicit)
    assert prepared["_project_admission"]["frozen"] is True
    path, committed = restore_unreadable(host)
    path.write_bytes(committed)
    sent = worker(host, monkeypatch)
    workers.assign_tasks()
    workers.assign_tasks()
    if change in {"rebind", "aba", "write_after"}:
        assert not sent
        assert prepared["_project_admission"]["project"] == original["project"]
        assert load_task_result(host.root, "explicit")["status"] == "failed"
    else:
        assert [row["id"] for row in sent] == ["explicit"]
        assert sent[0]["workspace_root"] == str(explicit)
        assert sent[0]["drive_root"] == prepared["drive_root"]
        assert sent[0]["_project_admission"] == prepared["_project_admission"]
        expected = registry.project_admission_view(host.root, "target", frozen=True)
        assert prepared["_project_admission"]["project"] == expected["project"]
    assert not host.attempts


@pytest.mark.parametrize("worker_count", [1, 3])
@pytest.mark.parametrize("boundary", ["snapshot", "result_false", "result_raise", "readback", "schedule"])
def test_failed_head_proof_keeps_custody_without_starving_other_work(host, tmp_path, monkeypatch, worker_count, boundary):  # noqa: F811
    from supervisor import task_admission, schedule_occurrence

    head = accepted(host, tmp_path)
    if boundary == "schedule":
        head["metadata"]["schedule_occurrence"] = {"schedule_id": "schedule", "token": "occurrence"}
        write_task_result(host.root, "held", "scheduled", schedule_admission={
            "schedule_id": "schedule", "token": "occurrence", "dispatch": "none", "status": "accepted"})
    original = copy.deepcopy(head)
    for i in range(worker_count):
        queue.enqueue_task({"id": f"main-{i}", "type": "task", "chat_id": 1, "text": "Independent work"})
    sent = worker(host, monkeypatch)
    for i in range(1, worker_count):
        workers.WORKERS[i] = SimpleNamespace(wid=i, busy_task_id=None, reaping=False,
            in_q=SimpleNamespace(put=lambda row: sent.append(copy.deepcopy(row))))
    attempts = []
    persist, write, read = queue.persist_queue_snapshot, task_admission.write_task_result, task_admission.load_task_result

    def snapshot(**kwargs):
        if (kwargs.get("reason") == "worker_launch_claimed"
                and not any(row["id"].startswith("main-") and row.get("admitted_dispatch") == "possible"
                            for row in host.pending)):
            attempts.append("held")
            return False
        return persist(**kwargs)

    def canonical(root, tid, *args, **kwargs):
        if tid == "held":
            attempts.append(tid)
            if boundary == "result_false":
                return False
            raise OSError("synthetic failed canonical proof")
        return write(root, tid, *args, **kwargs)

    def readback(root, tid, **kwargs):
        if tid == "held":
            attempts.append(tid)
            raise OSError("synthetic unavailable proof readback")
        return read(root, tid, **kwargs)

    def schedule_readback(tid):
        attempts.append(tid)
        raise OSError("synthetic unavailable schedule proof")

    with monkeypatch.context() as patch:
        if boundary == "snapshot":
            patch.setattr(queue, "persist_queue_snapshot", snapshot)
        elif boundary in {"result_false", "result_raise"}:
            patch.setattr(task_admission, "write_task_result", canonical)
        elif boundary == "readback":
            patch.setattr(task_admission, "load_task_result", readback)
        else:
            patch.setattr(schedule_occurrence, "_read_back", schedule_readback)
        workers.assign_tasks()
    assert [row["id"] for row in sent] == [f"main-{i}" for i in range(worker_count)]
    assert attempts == ["held"]
    assert host.pending == [head] and head["admitted_dispatch"] == "possible"
    assert "held" not in queue.RUNNING and not host.attempts
    workers.WORKERS[0].busy_task_id = None
    workers.assign_tasks()
    workers.assign_tasks()
    assert [row["id"] for row in sent].count("held") == 1
    assert sent[-1]["workspace_root"] == original["workspace_root"]
    assert sent[-1]["drive_root"] == original["drive_root"]
    assert sent[-1]["_project_admission"] == original["_project_admission"]


@pytest.mark.parametrize("bad_id", [7, " target ", None, "", "missing"])
@pytest.mark.parametrize("restoring", [False, True])
def test_origin_only_membership_cannot_normalize_corrupt_binding(room, bad_id, restoring):  # noqa: F811
    ref = {"chat_id": 1, "client_message_id": "source", "text_sha256": "a" * 64}
    registry.create_project(room.root, "7")
    binding = {"source_ref": ref}
    if bad_id != "missing":
        binding["project_id"] = bad_id
    registry._save_bindings(room.root, {"bindings": {"origin-sibling": binding}})
    rejected = room.queue.enqueue_task(task("fresh-sibling", project_id="", origin_message_ref=ref),
                                      restoring_snapshot=restoring)
    assert rejected.get("_admission_blocked") == "project_routing_fence_lookup_failed"
    assert not room.pending
    expected = "7" if bad_id == 7 else "target" if bad_id == " target " else ""
    assert registry.project_id_for_origin(room.root, ref) == expected  # tolerant display is unchanged


@pytest.mark.parametrize("same_origin", [False, True])
@pytest.mark.parametrize("bad_id", [7, " target ", None, "", "missing"])
def test_unrelated_corrupt_origin_does_not_block_healthy_membership(room, same_origin, bad_id):  # noqa: F811
    ref = {"chat_id": 1, "client_message_id": "source", "text_sha256": "a" * 64}
    unrelated = {**ref, "client_message_id": "unrelated"}
    malformed = {"source_ref": ref if same_origin else unrelated}
    if bad_id != "missing":
        malformed["project_id"] = bad_id
    registry._save_bindings(room.root, {"bindings": {
        "origin-sibling": {"project_id": "target", "source_ref": ref}, "corrupt": malformed}})
    result = room.queue.enqueue_task(task("fresh-sibling", project_id="", origin_message_ref=ref))
    if same_origin:
        assert result.get("_admission_blocked") == "project_routing_fence_lookup_failed"
        assert not room.pending
    else:
        assert not result.get("_admission_blocked")
        assert room.pending[0]["project_id"] == "target"


def test_known_registry_outage_skips_binding_reads_but_recovery_checks_them(host, tmp_path, monkeypatch):  # noqa: F811
    accepted(host, tmp_path)
    accepted(host, tmp_path, "second")
    path, committed = restore_unreadable(host)
    queue.enqueue_task({"id": "main", "type": "task", "chat_id": 1, "text": "Independent work"})
    lookup = registry.project_binding_for_task
    reads = []

    def observed(root, tid, **kwargs):
        reads.append(tid)
        return lookup(root, tid, **kwargs)

    monkeypatch.setattr(registry, "project_binding_for_task", observed)
    sent = worker(host, monkeypatch)
    workers.assign_tasks()
    assert not reads
    assert [row["id"] for row in sent] == ["main"]
    assert all(row["_project_admission_restore_hold"] for row in host.pending)
    path.write_bytes(committed)
    workers.WORKERS[0].busy_task_id = None
    workers.assign_tasks()
    assert {"held", "second"} <= set(reads)
    assert [row["id"] for row in sent] == ["main", "held"]


def test_global_snapshot_failure_blocks_every_candidate_without_dropping_rows(host, tmp_path, monkeypatch):  # noqa: F811
    head = accepted(host, tmp_path)
    queue.enqueue_task({"id": "main", "type": "task", "chat_id": 1, "text": "Independent work"})
    sent = worker(host, monkeypatch)
    attempts = []

    def unavailable(**kwargs):
        attempts.append(kwargs.get("reason"))
        return False

    with monkeypatch.context() as patch:
        patch.setattr(queue, "persist_queue_snapshot", unavailable)
        workers.assign_tasks()
    assert attempts == ["worker_launch_claimed", "worker_launch_claimed"]
    assert not sent and not queue.RUNNING and workers.WORKERS[0].busy_task_id is None
    assert [row["id"] for row in host.pending] == [head["id"], "main"]
    workers.assign_tasks()
    assert [row["id"] for row in sent] == [head["id"]]


@pytest.mark.parametrize("missing", [False, True])
def test_origin_only_membership_cannot_turn_closed_room_into_unregistered_work(room, missing):  # noqa: F811
    ref = {"chat_id": 1, "client_message_id": "source", "text_sha256": "a" * 64}
    registry._save_bindings(room.root, {"bindings": {
        "origin-sibling": {"project_id": "target", "source_ref": ref}}})
    if missing:
        registry._save(room.root, {"projects": []})
    else:
        registry.begin_project_deletion(room.root, "target")
    result = room.queue.enqueue_task(task("fresh-sibling", project_id="", origin_message_ref=ref))
    assert result.get("_admission_blocked") in {"project_routing_fence", "project_routing_fence_changed"}
    assert not room.pending
