"""I7: what a project room may continue, and what stamps its last-result pointer.

On 19.09 a room offered exactly ONE predecessor candidate (the project's
last-result pointer, which a CHILD had overwritten), the model named the
interrupted root itself and was refused `AUTHORITY_SOURCE_UNAVAILABLE`, and the
retry without a predecessor minted a duplicate root. The list is a HINT; the
door is a predicate: same project, a root, a readable result, not live.
"""

from __future__ import annotations

import types

import pytest


def _room_ctx(tmp_path, metadata, *, project_id: str = "racer"):
    """A project room's routing ctx: the host facts of this turn plus the room."""
    return types.SimpleNamespace(
        task_metadata=metadata, drive_root=tmp_path, budget_drive_root=str(tmp_path),
        project_id=project_id, current_chat_id=7, pending_events=[], event_queue=None,
    )


def _host_ctx(tmp_path, *, pending=None, running=None):
    return types.SimpleNamespace(
        DRIVE_ROOT=tmp_path, PENDING=list(pending or []), RUNNING=dict(running or {}),
    )


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """`_durable_project_of_request` reads the registry through config.DATA_DIR."""
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)


class _RecordingQueue:
    """A supervisor that would accept the event, so an emission cannot hide."""

    def __init__(self):
        self.events = []

    def put_nowait(self, event):
        self.events.append(event)


def _door(ctx, task_id, evt=None):
    from ouroboros.tools.control_routing import _attach_predecessor_authority_from_metadata

    return _attach_predecessor_authority_from_metadata(ctx, evt if evt is not None else {}, task_id)


def test_a_root_the_room_manifest_lists_is_still_addressable(tmp_path):
    """I29 positive path (owner batch 3, answer 6b=A): the narrowing removed
    CHILDREN from the window, and a listed owner root stays promotable."""
    import server
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    project = create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-root", "completed", project_id="racer",
                      objective="the room's own finished work", ts="2026-08-10T00:00:01Z")

    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-1", {"project_id": "racer"},
    )
    listed = [row["task_id"] for row in metadata["project_routing_manifest"]["final_results"]]
    assert listed == ["racer-root"]

    evt: dict = {}
    assert _door(_room_ctx(tmp_path, metadata), "racer-root", evt) == ""
    assert evt["predecessor_task_id"] == "racer-root"
    assert evt["predecessor_authority_source"]["tool"] == "get_task_result"


def test_a_room_root_older_than_the_list_is_addressable_all_the_same(tmp_path, monkeypatch):
    """The 16-row cap is a HINT window, never the door: the night's root was a
    finished root of this very project, and only the cap hid it."""
    import server
    from ouroboros import runtime_limits
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    project = create_project(tmp_path, "racer", name="Racer")
    monkeypatch.setattr(runtime_limits, "ROUTING_MANIFEST_RESULT_ROWS", 2)
    write_task_result(tmp_path, "racer-old", "completed", project_id="racer",
                      objective="the interrupted work", ts="2026-08-10T00:00:01Z")
    for index in range(2):
        write_task_result(tmp_path, f"racer-new{index}", "completed", project_id="racer",
                          objective="newer work", ts=f"2026-08-11T00:00:0{index}Z")

    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-2", {"project_id": "racer"},
    )
    manifest = metadata["project_routing_manifest"]
    assert [row["task_id"] for row in manifest["final_results"]] == ["racer-new1", "racer-new0"]
    assert manifest["omissions"]["final_results"] == 1

    evt: dict = {}
    assert _door(_room_ctx(tmp_path, metadata), "racer-old", evt) == ""
    assert evt["predecessor_task_id"] == "racer-old"
    assert evt["predecessor_authority_source"] == {
        "kind": "task_result", "task_id": "racer-old", "human_label": "the interrupted work",
        "tool": "get_task_result",
        "arguments": {"task_id": "racer-old", "include_authority": True},
    }


def test_a_child_result_is_never_the_continuation_and_the_host_stops_offering_it(tmp_path):
    """A pointer stamped by a child before this release still names a child: the
    host offers the ROOT instead, and the door refuses the child if the model names
    it all the same, saying where the work is reachable (I29 stays closed)."""
    import server
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.project_journal import record_project_last_result

    project = create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-root", "completed", project_id="racer",
                      objective="the root", ts="2026-08-10T00:00:01Z")
    write_task_result(tmp_path, "racer-child", "completed", project_id="racer",
                      objective="helper work", parent_task_id="racer-root",
                      root_task_id="racer-root", delegation_role="subagent",
                      ts="2026-08-10T00:00:02Z")
    record_project_last_result("racer", "racer-child", tmp_path)

    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-3", {"project_id": "racer"},
    )
    assert metadata["project_last_task_result"]["task_id"] == "racer-root"
    assert [row["task_id"] for row in
            metadata["project_routing_manifest"]["final_results"]] == ["racer-root"]

    evt: dict = {}
    refusal = _door(_room_ctx(tmp_path, metadata), "racer-child", evt)
    assert "delegated child result" in refusal and "root" in refusal
    assert evt == {}
    assert _door(_room_ctx(tmp_path, metadata), "racer-root") == ""


def test_another_projects_root_is_not_this_rooms_continuation(tmp_path):
    import server
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    project = create_project(tmp_path, "racer", name="Racer")
    create_project(tmp_path, "tower", name="Tower")
    write_task_result(tmp_path, "tower-root", "completed", project_id="tower",
                      objective="another room's work", ts="2026-08-10T00:00:01Z")

    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-4", {"project_id": "racer"},
    )
    assert metadata["project_routing_manifest"]["final_results"] == []

    evt: dict = {}
    refusal = _door(_room_ctx(tmp_path, metadata), "tower-root", evt)
    assert "not an addressable result in the host routing manifest" in refusal
    assert evt == {}


def test_a_live_root_is_steer_territory_not_a_predecessor(tmp_path):
    """Accepting a PENDING/RUNNING root would mint the second root the night
    produced; the room still SEES it, with the status that says `steer_task`."""
    import server
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    project = create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-live", "running", project_id="racer",
                      objective="work in flight", ts="2026-08-10T00:00:01Z")
    running = {"racer-live": {"task": {"id": "racer-live", "project_id": "racer",
                                       "title": "Live work", "objective": "work in flight"},
                              "started_at": "2026-08-10T00:00:01Z"}}

    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path, running=running), int(project["chat_id"]), "room-5",
        {"project_id": "racer"},
    )
    manifest = metadata["project_routing_manifest"]
    assert [row["task_id"] for row in manifest["active_roots"]] == ["racer-live"]
    assert manifest["active_roots"][0]["status"] == "running"

    evt: dict = {}
    refusal = _door(_room_ctx(tmp_path, metadata), "racer-live", evt)
    assert "steer_task" in refusal and "running" in refusal
    assert evt == {}


def test_an_unreadable_predecessor_still_answers_authority_source_unavailable(tmp_path):
    """A collected result is the one case the predicate cannot rescue, and the
    promote says so in its own refusal instead of emitting anything."""
    import server
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.control_routing import _promote_chat_to_task

    project = create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-gone", "completed", project_id="racer",
                      objective="collected work", ts="2026-08-10T00:00:01Z")
    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-6", {"project_id": "racer"},
    )
    (tmp_path / "task_results" / "racer-gone.json").unlink()

    evt: dict = {}
    refusal = _door(_room_ctx(tmp_path, metadata), "racer-gone", evt)
    assert refusal == "the selected predecessor task result is missing or unreadable"
    assert evt == {}

    ctx = _room_ctx(tmp_path, metadata)
    ctx.event_queue = _RecordingQueue()
    out = _promote_chat_to_task(ctx, "Continue the interrupted work", workspace="none",
                                predecessor_task_id="racer-gone")
    assert out.startswith("\u26a0\ufe0f AUTHORITY_SOURCE_UNAVAILABLE (promote_chat_to_task):")
    assert "missing or unreadable" in out
    # Nothing was emitted, so no id was reserved and no second root can follow.
    assert ctx.event_queue.events == [] and ctx.pending_events == []
    assert not (tmp_path / "task_results" / "racer-gone.json").exists()


def test_outside_a_room_the_host_list_still_decides(tmp_path):
    """The quiet direction of the same guard: with no room project there is no
    `same project` to evaluate, so an unlisted id stays unaddressable."""
    import server
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-root", "completed", project_id="racer",
                      objective="the room's work", ts="2026-08-10T00:00:01Z")

    metadata = server._decision_turn_metadata(_host_ctx(tmp_path), 1, "main-1", {})
    main_ctx = _room_ctx(tmp_path, {"client_message_id": "main-1"}, project_id="")
    refusal = _door(main_ctx, "racer-root")
    assert "not an addressable result in the host routing manifest" in refusal

    listed_ctx = _room_ctx(tmp_path, metadata, project_id="")
    assert _door(listed_ctx, "racer-root") == ""


def test_only_a_root_finalization_moves_the_projects_pointer(tmp_path):
    """The pointer answers "continue from here" for the ROOM; a child that
    finalizes later must not move it onto work no owner ever addressed."""
    from ouroboros.projects_registry import create_project, get_project
    from ouroboros.tools.project_journal import record_task_finalization

    create_project(tmp_path, "racer", name="Racer")
    record_task_finalization(
        "racer", {"id": "racer-root", "root_task_id": "racer-root"},
        objective="the root", kind="task", exec_status="completed", drive_root=tmp_path,
    )
    assert get_project(tmp_path, "racer")["last_task_result_id"] == "racer-root"

    record_task_finalization(
        "racer", {"id": "racer-child", "parent_task_id": "racer-root",
                  "root_task_id": "racer-root", "delegation_role": "subagent"},
        objective="helper work", kind="task", exec_status="completed", drive_root=tmp_path,
    )
    assert get_project(tmp_path, "racer")["last_task_result_id"] == "racer-root"


def test_the_self_heal_scan_never_offers_or_stamps_a_child(tmp_path):
    """The lookup's fallback scan is the pointer's SECOND writer: with no pointer
    yet and a child as the project's newest result, it answers with the newest ROOT
    and stamps that, never the child the door would refuse."""
    import os

    import server
    from ouroboros.projects_registry import create_project, get_project
    from ouroboros.task_results import task_results_dir, write_task_result

    create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-root", "completed", project_id="racer", result="root answer")
    write_task_result(tmp_path, "racer-child", "completed", project_id="racer", result="helper answer",
                      parent_task_id="racer-root", root_task_id="racer-root", delegation_role="subagent")
    results = task_results_dir(tmp_path, create=False)
    os.utime(results / "racer-root.json", (1_000, 1_000))
    os.utime(results / "racer-child.json", (2_000, 2_000))  # the child is the newest file
    assert not get_project(tmp_path, "racer").get("last_task_result_id")

    row = server._latest_project_task_result(_host_ctx(tmp_path), "racer")
    assert row["task_id"] == "racer-root"
    assert get_project(tmp_path, "racer")["last_task_result_id"] == "racer-root"


def test_a_pointer_a_child_stamped_before_the_rule_heals_onto_the_root(tmp_path):
    """A pointer written before "only a root stamps it" may name a child. That is
    provably wrong (not a copy-back in flight), so the lookup answers with the root
    and repairs the pointer; a pointer naming a ROOT is served as it is."""
    import server
    from ouroboros.projects_registry import create_project, get_project, update_project
    from ouroboros.task_results import write_task_result

    create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-root", "completed", project_id="racer", result="root answer")
    write_task_result(tmp_path, "racer-child", "completed", project_id="racer", result="helper answer",
                      parent_task_id="racer-root", root_task_id="racer-root", delegation_role="subagent")
    update_project(tmp_path, "racer", last_task_result_id="racer-child")

    assert server._latest_project_task_result(_host_ctx(tmp_path), "racer")["task_id"] == "racer-root"
    assert get_project(tmp_path, "racer")["last_task_result_id"] == "racer-root"

    # The quiet direction: a root pointer is one direct fetch and is left alone.
    write_task_result(tmp_path, "racer-root-2", "completed", project_id="racer", result="later root")
    update_project(tmp_path, "racer", last_task_result_id="racer-root")
    assert server._latest_project_task_result(_host_ctx(tmp_path), "racer")["task_id"] == "racer-root"
    assert get_project(tmp_path, "racer")["last_task_result_id"] == "racer-root"


def test_the_room_manifest_carries_cancel_facts_and_an_honest_omission_count(tmp_path):
    import server
    from ouroboros.cancel_intents import request_cancel
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    project = create_project(tmp_path, "racer", name="Racer")
    write_task_result(
        tmp_path, "racer-cancelled", "cancelled", project_id="racer",
        objective="stopped work", ts="2026-08-10T00:00:01Z",
        cancel_origin={"source": "owner", "reason": "owner stopped it",
                       "requested_at": "2026-08-10T00:00:00Z"},
    )
    write_task_result(tmp_path, "racer-child", "completed", project_id="racer",
                      objective="helper", parent_task_id="racer-cancelled",
                      root_task_id="racer-cancelled", delegation_role="subagent",
                      ts="2026-08-10T00:00:02Z")
    running = {"racer-live": {"task": {"id": "racer-live", "project_id": "racer",
                                       "title": "Live", "objective": "in flight"},
                              "started_at": "2026-08-10T00:00:03Z"}}
    request_cancel(tmp_path, "racer-live", source="owner", reason="stop it")

    manifest = server._decision_turn_metadata(
        _host_ctx(tmp_path, running=running), int(project["chat_id"]), "room-7",
        {"project_id": "racer"},
    )["project_routing_manifest"]

    [row] = manifest["final_results"]
    assert row["task_id"] == "racer-cancelled"
    assert row["cancel_origin"] == {"source": "owner", "reason": "owner stopped it",
                                    "requested_at": "2026-08-10T00:00:00Z"}
    [live] = manifest["active_roots"]
    assert live["task_id"] == "racer-live" and live["cancel_state"] == "pending"
    assert manifest["omissions"]["children"] == 1
    assert manifest["omissions"]["final_results"] == 0


def test_the_manifest_row_cap_is_one_runtime_limit(tmp_path, monkeypatch):
    """Both lanes read the same bound; no call-site literal decides the window."""
    import server
    from ouroboros import runtime_limits
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    project = create_project(tmp_path, "racer", name="Racer")
    for index in range(3):
        write_task_result(tmp_path, f"root{index}", "completed", project_id="racer",
                          objective="work", ts=f"2026-08-10T00:00:0{index}Z")
    monkeypatch.setattr(runtime_limits, "ROUTING_MANIFEST_RESULT_ROWS", 1)

    room = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-8", {"project_id": "racer"},
    )["project_routing_manifest"]
    main = server._main_routing_manifest(_host_ctx(tmp_path))

    assert len(room["final_results"]) == 1 and room["omissions"]["final_results"] == 2
    assert len(main["final_results"]) == 1 and main["omissions"]["final_results"] == 2
