"""A transferred Main conversation retains the ordinary Project completion mirror."""
from __future__ import annotations

import pytest

from ouroboros.project_dialogue import build_owner_message_ref, enqueue_project_completion_summary
from ouroboros.projects_registry import bind_task_to_project, create_project


@pytest.mark.parametrize("source_chat", [1, "project", 0, -42, None])
@pytest.mark.parametrize("direct_carrier", ["event", "task", "result", "done"])
def test_direct_completion_requires_a_durable_main_origin(tmp_path, monkeypatch, source_chat, direct_carrier):
    project = create_project(tmp_path, "research", name="Research")
    chat = project["chat_id"] if source_chat == "project" else source_chat
    origin = {"absent": "system"} if chat is None else {
        "ref": build_owner_message_ref(chat_id=chat, client_message_id="request-one",
                                       ts="2026-09-21T12:00:00Z", text="Investigate"),
        "text": "Investigate",
    }
    bind_task_to_project(tmp_path, "turn-one", project["id"], project["chat_id"], origin=origin)
    rows = []
    monkeypatch.setattr("supervisor.terminal_delivery.enqueue_terminal_delivery",
                        lambda root, event: rows.append(event) or True)
    event = {}
    task = {"id": "turn-one", "chat_id": project["chat_id"], "project_id": project["id"]}
    result = {"status": "completed", "result": "# The complete answer\n\nEvidence remains intact.",
              "terminal_origin": "model_final", "project_id": project["id"]}
    done = {"status": "completed", "outcome_axes": {"execution": {"status": "ok"}}}
    {"event": event, "task": task, "result": result, "done": done}[direct_carrier]["_is_direct_chat"] = True
    admitted = enqueue_project_completion_summary(tmp_path, event, "turn-one", task, result, done)
    assert admitted is (source_chat == 1)
    assert len(rows) == int(source_chat == 1)
    if rows:
        assert rows[0]["system_type"] == "project_completion_summary"
        assert rows[0]["delivery_id"] == "project-completion:turn-one"
        assert rows[0]["chat_id"] == 1
        assert rows[0]["progress_meta"]["completion_answer"] == result["result"]


def test_handoff_identity_follows_origin_not_retry_task_or_text():
    from ouroboros.project_handoff import handoff_identity
    a = {"chat_id": 1, "client_message_id": "first"}
    assert handoff_identity("p", "root", a) == handoff_identity("p", "retry", {**a, "text_sha256": "other"})
    assert handoff_identity("p", "root", a) != handoff_identity("p", "root", {**a, "client_message_id": "second"})
    assert handoff_identity("p", "root", None) != handoff_identity("p", "retry", None)
    assert handoff_identity("p", "root", a) != handoff_identity("q", "root", a)


def test_handoff_delivery_requires_binding_and_main_origin(tmp_path, monkeypatch):
    from ouroboros.project_handoff import enqueue_project_handoff
    project = create_project(tmp_path, "research", name="Research")
    rows = []
    monkeypatch.setattr("supervisor.terminal_delivery.enqueue_terminal_delivery",
                        lambda root, event: rows.append(event) or True)
    assert not enqueue_project_handoff(tmp_path, "not-bound", origin_chat=1)
    origin = {"ref": build_owner_message_ref(chat_id=1, client_message_id="first",
              ts="2026-09-21T12:00:00Z", text="Investigate"), "text": "Investigate"}
    bind_task_to_project(tmp_path, "root", project["id"], project["chat_id"], origin=origin)
    assert enqueue_project_handoff(tmp_path, "root")
    assert rows[0]["system_type"] == "project_handoff"
    assert rows[0]["chat_id"] == 1
    assert "status" not in rows[0]["progress_meta"]
    assert not enqueue_project_handoff(tmp_path, "root", source_ref={"chat_id": project["chat_id"], "client_message_id": "inside"})


def test_handoff_is_a_main_only_history_row(tmp_path):
    from ouroboros.project_dialogue import room_membership
    row = {"type": "project_handoff", "task_id": "root"}
    assert room_membership(1, {22}, [], {"root": 22})(1, row)
    assert not room_membership(22, {22}, [], {"root": 22})(1, row)
