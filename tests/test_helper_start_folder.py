"""The named folder is the exact child start, not write or read authority."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros.contracts.task_contract import build_task_contract
from ouroboros.contracts.task_constraint import TaskConstraint
from ouroboros.subagent_work_order import compile_external_work_order
from ouroboros.subagents import delegated_run_shape
from ouroboros.tools.delegate_integration import _mutation_authority
from ouroboros.tools.registry import ToolContext, ToolRegistry
from supervisor.events_subagent_admission import _resolve_subagent_constraint
from supervisor.task_dispatch import build_scheduled_task_payload
from tests._shared import configure_test_subagent


@pytest.mark.parametrize("named", [True, False])
def test_schedule_preserves_parent_source_and_exact_child_folder(tmp_path, monkeypatch, named):
    import ouroboros.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setenv("OUROBOROS_MAX_SUBAGENT_DEPTH", "4")
    home = tmp_path / "home"
    repo, data = home / "Ouroboros" / "repo", home / "Ouroboros" / "data"
    parent, selected = home / "parent", home / "selected"
    for path in (repo, data, parent, selected):
        path.mkdir(parents=True)
    monkeypatch.setenv("OUROBOROS_USER_FILES_ROOT", str(home))
    subprocess.run(["git", "init", "--quiet", str(selected)], check=True, capture_output=True)
    (selected / "README.md").write_text("selected project", encoding="utf-8")
    subprocess.run(["git", "-C", str(selected), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(selected), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                    "commit", "--quiet", "-m", "Seed fixture"], check=True, capture_output=True)
    subdir = selected / "packages" / "component"
    subdir.mkdir(parents=True)
    ctx = ToolContext(repo_dir=repo, drive_root=data, task_id="parent-task", is_direct_chat=True)
    ctx.task_metadata = {"_project_room_dir": str(parent)}
    ctx.task_contract = build_task_contract({"workspace_root": str(parent), "workspace_mode": "external",
                                            "disabled_tools": ["web_search"]})
    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    registry.set_context(ctx)
    actor = configure_test_subagent(monkeypatch)
    result = registry.execute_result("schedule_subagent", {
        "subagent_id": actor, "objective": "Inspect source", "expected_output": "Evidence",
        **({"workspace_root": str(subdir)} if named else {}),
    })
    assert result.status == "ok", result.text
    event = ctx.pending_events[-1]
    expected = subdir if named else parent
    assert event["workspace_root"] == str(expected)
    assert event["task_contract"]["workspace"]["root"] == str(expected)
    assert event["task_contract"]["disabled_tools"] == ["web_search"]
    assert event["parent_workspace"] == {"root": str(parent), "mode": "", "source": "project_room"}
    saved = json.loads((data / "task_results" / f"{event['task_id']}.json").read_text(encoding="utf-8"))
    assert saved["parent_workspace"] == event["parent_workspace"]
    task = build_scheduled_task_payload({**event, "tid": event["task_id"], "parent_id": "parent-task"})
    assert task["metadata"]["parent_workspace"] == event["parent_workspace"]
    constraint, folder, mode, refusal = _resolve_subagent_constraint(
        SimpleNamespace(), tid=task["id"], requested_constraint=task["task_constraint"],
        workspace_root=task["workspace_root"], workspace_mode=task["workspace_mode"],
        base_sha="", parent_task_id="parent-task")
    assert not refusal and Path(folder) == expected
    child = ToolContext(repo_dir=repo, drive_root=data, task_id=task["id"],
                        workspace_root=Path(folder), workspace_mode=mode,
                        task_constraint=TaskConstraint(mode=constraint["mode"]), task_metadata=task["metadata"],
                        task_contract=task["task_contract"])
    assert child.active_repo_dir() == expected
    authority, error = _mutation_authority(child, delegated_run_shape(False))
    assert error is None and authority["target_root"] == str(expected)
    work_order = compile_external_work_order(task)
    assert str(expected) in work_order
    assert "web_search" in work_order


def test_bad_named_folder_is_refused_before_enqueue(tmp_path, monkeypatch):
    actor = configure_test_subagent(monkeypatch)
    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    registry._ctx.task_id = "parent"
    result = registry.execute_result("schedule_subagent", {
        "subagent_id": actor, "objective": "Read source", "expected_output": "Evidence",
        "workspace_root": str(tmp_path / "missing"),
    })
    assert result.status != "ok" and "not a directory" in result.text
    assert not registry._ctx.pending_events


@pytest.fixture
def start_registry(tmp_path, monkeypatch):
    import ouroboros.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setenv("OUROBOROS_MAX_SUBAGENT_DEPTH", "4")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    home, repo, data, outside = [tmp_path / name for name in ("home", "repo", "data", "outside")]
    for path in (home, repo, data, outside):
        path.mkdir()
    monkeypatch.setenv("OUROBOROS_USER_FILES_ROOT", str(home))
    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    registry._ctx.task_id = "parent"
    actor = configure_test_subagent(monkeypatch)
    args = {"subagent_id": actor, "objective": "Inspect source", "expected_output": "Evidence"}
    return registry, args, home, repo, data, outside


@pytest.mark.parametrize("mode", ["light", "advanced", "pro"])
def test_named_folder_cannot_mint_off_home_read_authority(start_registry, monkeypatch, mode):
    registry, args, _home, _repo, _data, outside = start_registry
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", mode)
    result = registry.execute_result("schedule_subagent", {**args, "workspace_root": str(outside)})
    assert result.status != "ok" and result.code == "TOOL_ARG_ERROR", result
    assert "outside the user_files home" in result.text
    assert not registry._ctx.pending_events


@pytest.mark.parametrize("parent_kind", ["external", "cyber_pro"])
def test_existing_parent_off_home_read_authority_can_select_folder(start_registry, monkeypatch, parent_kind):
    registry, args, home, _repo, _data, outside = start_registry
    if parent_kind == "external":
        parent = home / "parent-project"
        parent.mkdir()
        registry._ctx.workspace_root = parent
        registry._ctx.workspace_mode = "external"
    else:
        monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "cyber_pro")
    result = registry.execute_result("schedule_subagent", {**args, "workspace_root": str(outside)})
    assert result.status == "ok", result.text
    assert registry._ctx.pending_events[-1]["workspace_root"] == str(outside)


@pytest.mark.parametrize("target", ["home", "repo", "data"])
def test_parent_readable_folders_are_admitted_without_mutation_geometry(start_registry, target):
    registry, args, home, repo, data, _outside = start_registry
    folder = {"home": home, "repo": repo, "data": data}[target] / "inspect"
    folder.mkdir()
    result = registry.execute_result("schedule_subagent", {**args, "workspace_root": str(folder)})
    assert result.status == "ok", result.text
    assert registry._ctx.pending_events[-1]["workspace_root"] == str(folder)


@pytest.mark.parametrize("surface", ["external_workspace", "self_worktree", "genesis"])
def test_contradictory_named_start_is_refused_before_enqueue(start_registry, surface):
    registry, args, home, _repo, _data, _outside = start_registry
    selected, other = home / "selected", home / "other"
    selected.mkdir()
    other.mkdir()
    options = {"write_surface": surface, "workspace_root": str(selected)}
    if surface != "genesis":
        options["write_root"] = str(other)
    result = registry.execute_result("schedule_subagent", {**args, **options})
    assert result.status != "ok" and result.code == "TOOL_ARG_ERROR", result
    assert ("genesis" if surface == "genesis" else "different folders") in result.text
    assert not registry._ctx.pending_events


def test_matching_named_start_and_write_root_preserve_existing_external_surface(start_registry):
    registry, args, home, _repo, _data, _outside = start_registry
    folder = home / "selected"
    folder.mkdir()
    result = registry.execute_result("schedule_subagent", {
        **args, "workspace_root": str(folder), "write_surface": "external_workspace", "write_root": str(folder),
    })
    assert result.status == "ok", result.text
    event = registry._ctx.pending_events[-1]
    assert event["task_constraint"]["mode"] == "acting_subagent"
    assert event["task_constraint"]["write_root"] == str(folder)
