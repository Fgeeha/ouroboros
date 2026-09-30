"""Producer-proven pre-effect refusals through registry and owner controls."""
from types import SimpleNamespace

import pytest

from tests._budget_pause_exact_helpers import _install_queue
from tests.test_owner_pause import _owner_park

pytestmark = pytest.mark.serial


def _world(tmp_path, monkeypatch):
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.registry import ToolRegistry

    queue, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda *_a, **_kw: 5.0)
    repo = tmp_path / "repo"
    repo.mkdir()
    for target in (repo / "notes.txt", tmp_path / "notes.txt"):
        target.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    write_task_result(tmp_path, "root", "running", root_task_id="root", chat_id=0)
    workers.RUNNING["root"] = {"task": {"id": "root", "root_task_id": "root",
        "type": "task", "chat_id": 0}, "worker_id": 0, "attempt": 1}
    registry = ToolRegistry(repo_dir=repo, drive_root=tmp_path)
    registry._ctx.task_id = registry._ctx.root_task_id = "root"
    return registry, queue, workers, repo


def _pause_and_resume(root, monkeypatch, queue, workers):
    from ouroboros import budget_pause, owner_pause
    from supervisor.events_budget import install_exact_budget_pause
    from supervisor.owner_pause_control import request_owner_pause

    assert request_owner_pause("root", request_id="P")["ok"]
    pause = _owner_park(root, monkeypatch, "root", [], root="root")
    ctx = SimpleNamespace(DRIVE_ROOT=root, RUNNING=workers.RUNNING, PENDING=workers.PENDING,
        WORKERS=workers.WORKERS, sort_pending=lambda: None,
        persist_queue_snapshot=queue.persist_queue_snapshot, bridge=None)
    install_exact_budget_pause(ctx, "root", budget_pause.exact_pause_marker(pause)["checkpoint"])
    assert owner_pause.read_fence(root, "root")["state"] == owner_pause.FENCE_PAUSED
    assert queue.resume_budget_paused_task("root")["ok"]


@pytest.mark.parametrize("root", ["active_workspace", "runtime_data"])
@pytest.mark.parametrize("case", ["missing_match", "duplicate_match", "missing_file"])
def test_editor_validation_finishes_without_a_writer_and_owner_can_resume(tmp_path, monkeypatch, root, case):
    from ouroboros.task_results import load_task_result

    registry, queue, workers, repo = _world(tmp_path, monkeypatch)
    target = (repo if root == "active_workspace" else tmp_path) / "notes.txt"
    before = target.read_bytes()
    result = registry.execute_result("edit_text", {"root": root,
        "path": "missing.txt" if case == "missing_file" else "notes.txt",
        "old_str": "not present" if case == "missing_match" else "alpha", "new_str": "gamma"})
    assert result.status != "ok", result
    assert result.meta["operation_outcome"] == "completed_no_effect", result
    assert target.read_bytes() == before
    assert load_task_result(tmp_path, "root")["launch_handoffs"] == {}
    _pause_and_resume(tmp_path, monkeypatch, queue, workers)


@pytest.mark.parametrize("tool,args", [
    ("edit_text", {"path": "notes.txt", "old_str": "", "new_str": "gamma"}),
    ("write_file", {"path": "bad.py", "content": "def broken(:"}),
    ("write_file", {}),
    ("schedule_followup", {"objective": "check", "relation": "independent"}),
    ("schedule_followup", {"objective": "check", "relation": "independent", "run_at": "invalid"}),
    ("schedule_followup", {"objective": "", "relation": "independent", "run_at": "2099-01-01T00:00:00Z"}),
    ("manage_schedules", {"action": "invalid"}),
    ("delegate_start", {"prompt": ""}),
    ("delegate_start", {"prompt": "check", "continue_from": "run-old", "retry_of": "inv-old"}),
])
def test_pre_effect_argument_refusals_do_not_hold_continue(tmp_path, monkeypatch, tool, args):
    from ouroboros.task_results import load_task_result
    from supervisor.continuation_admission import admit_continuation
    from tests.test_owner_continue import _interrupted, _owner_mail, NONCE

    registry, queue, workers, repo = _world(tmp_path, monkeypatch)
    effects = []
    monkeypatch.setattr("ouroboros.claudexor_daemon.ensure_owned_gateway",
                        lambda *_a, **_kw: effects.append("daemon"))
    monkeypatch.setattr(queue, "upsert_scheduled_task", lambda *_a, **_kw: effects.append("schedule"))
    before = (repo / "notes.txt").read_bytes()
    result = registry.execute_result(tool, args)
    assert result.status != "ok", result
    assert result.meta.get("operation_outcome") == "completed_no_effect", result
    assert not effects and not (repo / "bad.py").exists()
    assert (repo / "notes.txt").read_bytes() == before
    assert load_task_result(tmp_path, "root")["launch_handoffs"] == {}
    workers.RUNNING.clear()
    _interrupted(tmp_path, task_id="root")
    _owner_mail(tmp_path, task_id="root")
    admitted = admit_continuation("root", action_nonce=NONCE)
    assert admitted["ok"] and not admitted["held"], admitted


@pytest.mark.parametrize("root", ["active_workspace", "runtime_data"])
def test_actual_editor_write_still_completes_then_pauses(tmp_path, monkeypatch, root):
    registry, queue, workers, repo = _world(tmp_path, monkeypatch)
    result = registry.execute_result("edit_text", {"root": root, "path": "notes.txt",
        "old_str": "beta", "new_str": "gamma"})
    assert result.status == "ok", result
    assert result.meta.get("operation_outcome") != "completed_no_effect"
    assert ((repo if root == "active_workspace" else tmp_path) / "notes.txt").read_text(
        encoding="utf-8") == "alpha\ngamma\nalpha\n"
    _pause_and_resume(tmp_path, monkeypatch, queue, workers)


def test_a_write_then_error_is_not_mislabelled_a_pre_effect_refusal(tmp_path, monkeypatch):
    from ouroboros.task_results import load_task_result
    from ouroboros.tools import git

    registry, _queue, _workers, repo = _world(tmp_path, monkeypatch)
    def write_then_error(path, text):
        path.write_text(text, encoding="utf-8")
        raise OSError("injected failure after writing")
    monkeypatch.setattr(git, "write_text", write_then_error)
    result = registry.execute_result("edit_text", {"path": "notes.txt", "old_str": "beta", "new_str": "gamma"})
    assert result.status == "error"
    assert result.meta.get("operation_outcome") != "completed_no_effect"
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "alpha\ngamma\nalpha\n"
    assert load_task_result(tmp_path, "root")["launch_handoffs"]
