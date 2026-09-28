"""Known non-Project work retains its original scope across authority outages."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from ouroboros import projects_registry as registry
from ouroboros.task_results import load_task_result, write_task_result
from supervisor import queue, workers
from tests.test_project_hold_recovery import worker
from tests.test_swarm_host_admission import host  # noqa: F401

pytestmark = pytest.mark.serial


def admit_main(host, producer="promotion", tid="main"):  # noqa: F811
    if producer == "promotion":
        from supervisor.events_project_routing import _handle_promote_chat_to_task
        result = _handle_promote_chat_to_task({"task_id": tid, "routing_token": tid + "-token",
            "objective": "Ordinary Main work", "chat_id": 1}, host.ctx)
        assert result["status"] == "scheduled"
    else:
        from starlette.requests import Request

        from ouroboros.gateway.tasks import _create_task_from_body
        request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(
            drive_root=host.root, repo_dir=workers.REPO_DIR))})
        response = _create_task_from_body(request, {"task_id": tid, "description": "Ordinary Main work"})
        assert response.status_code == 200, response.body
    return next(row for row in host.pending if row["id"] == tid)


@pytest.mark.parametrize("producer", ["promotion", "api"])
def test_known_none_recovers_same_id_after_bindings_outage(host, monkeypatch, producer):  # noqa: F811
    from ouroboros.project_admission import project_hold_fact

    original = copy.deepcopy(admit_main(host, producer))
    assert "_project_admission" not in original
    assert queue.persist_queue_snapshot()
    path = registry._bindings_path(host.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{torn")
    host.pending.clear()
    queue.restore_pending_from_snapshot()
    sent = worker(host, monkeypatch)
    workers.assign_tasks()
    assert not sent and not host.attempts
    assert project_hold_fact(host.pending[0])["label"] == "Waiting for task scope verification"
    assert queue.persist_queue_snapshot()
    host.pending.clear()
    queue.restore_pending_from_snapshot()
    path.write_text('{"bindings": {}}')
    registry._registry_path(host.root).write_text("{still torn")
    workers.assign_tasks()
    workers.assign_tasks()
    assert [row["id"] for row in sent] == [original["id"]]
    assert sent[0].get("drive_root") == original.get("drive_root")
    assert "_project_admission" not in sent[0]
    assert not host.attempts


@pytest.mark.parametrize("producer", ["promotion", "api"])
def test_unscoped_producers_preserve_absence_and_stale_restore_policy(host, producer):  # noqa: F811
    row = admit_main(host, producer)
    assert "_project_admission" not in row
    assert "_project_admission" not in load_task_result(host.root, "main")
    assert queue.persist_queue_snapshot()
    snap = json.loads(queue.QUEUE_SNAPSHOT_PATH.read_text())
    assert "_project_admission" not in snap["pending"][0]["task"]
    snap["ts"] = "2000-01-01T00:00:00Z"
    queue.QUEUE_SNAPSHOT_PATH.write_text(json.dumps(snap))
    host.pending.clear()
    queue.restore_pending_from_snapshot()
    assert not host.pending


@pytest.mark.parametrize("veto", ["legacy", "null", "malformed", "dispatch", "stopped", "binding", "origin", "folder"])
def test_no_scope_recovery_requires_original_positive_authority(host, monkeypatch, veto):  # noqa: F811
    row = admit_main(host)
    if veto == "legacy":
        row.pop("_project_scope_none", None)
    elif veto in {"null", "malformed"}:
        row["_project_admission"] = None if veto == "null" else []
    elif veto == "dispatch":
        write_task_result(host.root, "main", "scheduled", admitted_dispatch="possible")
    elif veto == "stopped":
        write_task_result(host.root, "main", "cancelled")
    elif veto == "origin":
        row["origin_message_ref"] = {"chat_id": 1, "client_message_id": "origin"}
    elif veto == "folder":
        row["workspace_root"] = str(host.root)
    row["_project_admission_restore_hold"] = {"reason": "project_routing_fence_lookup_failed"}
    if veto in {"binding", "origin"}:
        registry.create_project(host.root, "new-room")
        registry.bind_task_to_project(host.root, "main" if veto == "binding" else "sibling", "new-room",
            origin={"absent": "system"} if veto == "binding" else {"ref": {
                "chat_id": 1, "client_message_id": "origin", "ts": "2026-09-28T00:00:00Z",
                "text_sha256": hashlib.sha256(b"Owner work").hexdigest()}, "text": "Owner work"})
    sent = worker(host, monkeypatch)
    workers.assign_tasks()
    assert not sent and not host.attempts
    assert not any(r.get("project_id") == "new-room" for r in host.pending)


def test_old_main_snapshot_cannot_replay_after_handoff(host, monkeypatch):  # noqa: F811
    admit_main(host)
    assert queue.persist_queue_snapshot()
    old = queue.QUEUE_SNAPSHOT_PATH.read_bytes()
    sent = worker(host, monkeypatch)
    monkeypatch.setattr("supervisor.worker_assignment._mirror_assigned_running_status", lambda _t: None)
    workers.assign_tasks()
    assert len(sent) == 1
    assert load_task_result(host.root, "main")["admitted_dispatch"] == "possible"
    queue.RUNNING.clear()
    workers.WORKERS[0].busy_task_id = None
    queue.QUEUE_SNAPSHOT_PATH.write_bytes(old)
    registry._bindings_path(host.root).write_text("{torn")
    queue.restore_pending_from_snapshot()
    registry._bindings_path(host.root).write_text('{"bindings": {}}')
    workers.assign_tasks()
    assert len(sent) == 1


@pytest.mark.parametrize("project_room", [False, True])
def test_corrupt_registry_keeps_main_dialogue_available(tmp_path, monkeypatch, project_room):
    import server
    from tests.test_project_routing_v664 import _ctx, _ImmediateThread

    project = registry.create_project(tmp_path, "target")
    chat_id = project["chat_id"] if project_room else 1
    direct, receipts = [], []
    ctx = _ctx(tmp_path, direct=lambda *_a, **_k: direct.append(True))
    registry._registry_path(tmp_path).write_text("{torn")
    monkeypatch.setattr("ouroboros.server_owner_routing.threading", SimpleNamespace(Thread=_ImmediateThread))
    class Bridge:
        def get_updates(self, **_kwargs):
            return [{"update_id": 1, "message": {"chat": {"id": chat_id}, "from": {"id": 1},
                "text": "Repair the project registry", "source": "web", "client_message_id": "repair"}}]
        def send_routing_ack(self, *_args, **kwargs):
            receipts.append(kwargs)
        def broadcast(self, _payload):
            pass
    monkeypatch.setattr("supervisor.message_bus.log_chat", lambda *_a, **_k: None)
    server._process_bridge_updates(Bridge(), 0, ctx)
    assert direct == ([] if project_room else [True])
    if project_room:
        assert receipts[-1]["status"] == "project_unavailable"


@pytest.mark.parametrize("project_room", [False, True])
def test_main_steers_healthy_foreign_task_despite_corrupt_registry(host, monkeypatch, project_room):  # noqa: F811
    from ouroboros.owner_mailbox import drain_owner_messages
    from ouroboros.tools.control import _steer_task
    from tests.test_steer_relay import _live_tool_ctx, _supervisor_ctx

    project = registry.create_project(host.root, "target")
    ctx = _supervisor_ctx(host.root, [])
    ctx.RUNNING["t-target"]["task"]["chat_id"] = 7
    issuer = _live_tool_ctx(host.root, ctx, [], task_id="owner-turn", metadata={
        "client_message_id": "steer", "origin_message_text": "Continue original task"})
    if project_room:
        issuer.current_chat_id = project["chat_id"]
        issuer.task_metadata["origin_message_ref"]["chat_id"] = project["chat_id"]
    registry._registry_path(host.root).write_text("{torn")
    _steer_task(issuer, "t-target", "Continue original task")
    assert drain_owner_messages(host.root, "t-target") == ([] if project_room else ["Continue original task"])


@pytest.mark.parametrize("producer", ["promotion", "api"])
@pytest.mark.parametrize("authority", ["registry", "bindings"])
def test_fresh_main_admission_distinguishes_irrelevant_registry_from_unknown_scope(host, producer, authority):  # noqa: F811
    registry.create_project(host.root, "neighbor")
    path = registry._registry_path(host.root) if authority == "registry" else registry._bindings_path(host.root)
    path.write_text("{torn")
    if authority == "registry":
        row = admit_main(host, producer)
        assert row["_project_scope_none"] is True and row["admitted_dispatch"] == "none"
    else:
        from starlette.requests import Request

        from ouroboros.gateway.tasks import _create_task_from_body
        from supervisor.events_project_routing import _handle_promote_chat_to_task
        if producer == "promotion":
            result = _handle_promote_chat_to_task({"task_id": "main", "routing_token": "ours",
                "objective": "Ordinary work", "chat_id": 1}, host.ctx)
            assert result["status"] != "scheduled"
        else:
            request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(
                drive_root=host.root, repo_dir=workers.REPO_DIR))})
            assert _create_task_from_body(request, {"task_id": "main", "description": "Ordinary work"}).status_code >= 400
        assert not host.pending and not host.attempts


@pytest.mark.parametrize("reason", ["source", "pool", "attachments"])
@pytest.mark.parametrize("stub", [False, True])
def test_early_promotion_refusals_have_positive_never_admitted_evidence(host, reason, stub):  # noqa: F811
    from ouroboros.terminal_projection import _settled
    from supervisor.events_project_routing import _handle_promote_chat_to_task

    event = {"task_id": "refused", "routing_token": "ours", "objective": "Original work", "chat_id": 1}
    if stub:
        write_task_result(host.root, "refused", "requested", promotion_admission={
            "status": "emitted", "routing_token": "ours"})
    if reason == "source":
        event["_source_error"] = "Source checkout failed"
    elif reason == "pool":
        host.ctx.WORKERS.clear()
    else:
        event["attachment_uploads"] = [{"path": str(host.root / "missing.png"), "label": "missing.png"}]
    outcome = _handle_promote_chat_to_task(event, host.ctx)
    assert outcome["status"] == "needs_manual_target"
    stored = load_task_result(host.root, "refused")
    assert stored["admission_outcome"] == "never_admitted"
    assert not _settled(stored) and not host.pending and not host.attempts


def test_admission_and_revalidation_read_bindings_once_per_operation(host, monkeypatch):  # noqa: F811
    from supervisor.task_admission import revalidate_project_holds

    reads = []
    original = registry._load_bindings
    def counted(root, **kwargs):
        reads.append(kwargs.get("strict"))
        return original(root, **kwargs)
    monkeypatch.setattr(registry, "_load_bindings", counted)
    for i in range(8):
        row = {"id": f"main-{i}", "type": "task", "chat_id": 1, "root_task_id": f"main-{i}",
               "origin_message_ref": {"chat_id": 1, "client_message_id": f"origin-{i}"}}
        reads.clear()
        admitted = queue.enqueue_task(row)
        assert not admitted.get("_admission_blocked") and reads == [True]
        admitted["_project_admission_restore_hold"] = {"reason": "project_routing_fence_lookup_failed"}
        write_task_result(host.root, row["id"], "scheduled")
    reads.clear()
    with queue._queue_lock:
        revalidate_project_holds()
    assert reads == [True]
    assert all(not row.get("_project_admission_restore_hold") for row in host.pending)
