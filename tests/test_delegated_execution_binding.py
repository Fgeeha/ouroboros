"""Regression tests for the delegated authority/execution-root seam."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from ouroboros.delegate_start_instructions import (
    apply_execution_binding, execution_binding_instruction,
)
from ouroboros.tools.delegate_integration import (
    _capture_block, _target_drift_evidence, _target_drift_paths,
)


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=str(root), check=True,
                          capture_output=True, text=True)


def test_execution_binding_makes_private_root_the_only_write_target():
    text = execution_binding_instruction("/tmp/private-snapshot", "/tmp/authority")
    assert "/tmp/private-snapshot" in text
    assert "/tmp/authority" in text
    assert "sole writable execution root" in text
    assert "read-only identity/reference" in text
    assert "typed execution-root mismatch" in text


def test_runtime_child_environment_drops_launcher_authority(monkeypatch):
    from ouroboros.settings_integrity import runtime_environ

    monkeypatch.setenv("OUROBOROS_MANAGED_BY_LAUNCHER", "1")
    assert "OUROBOROS_MANAGED_BY_LAUNCHER" not in runtime_environ()


def test_compiled_work_order_rebinds_child_facing_write_fields():
    instructions = (
        "HOST TASK CONTRACT AUTHORITY (complete normalized JSON; exact strings are authority):\n"
        '{"workspace_root":"/authority","task_constraint":{"write_root":"/authority"}}'
    )
    bound, prompt = apply_execution_binding(
        instructions, instructions, True, "/private/snapshot", "/authority")
    assert '"workspace_root": "/private/snapshot"' in bound
    assert '"write_root": "/private/snapshot"' in bound
    assert '"authority_target_root": "/authority"' in bound
    assert "/private/snapshot" in prompt


def test_target_drift_is_detected_without_staging_or_rewriting_index(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "-q")
    _git(target, "config", "user.name", "test")
    _git(target, "config", "user.email", "test@example.invalid")
    (target / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(target, "add", "tracked.txt")
    _git(target, "commit", "-qm", "baseline")
    baseline = _git(target, "rev-parse", "HEAD").stdout.strip()
    (target / "tracked.txt").write_text("owner changed\n", encoding="utf-8")
    (target / "new.txt").write_text("owner file\n", encoding="utf-8")
    before = _git(target, "status", "--porcelain").stdout

    entry = SimpleNamespace(target_root=str(target), baseline_sha=baseline)
    changed = _target_drift_paths(entry)

    assert changed == ["new.txt", "tracked.txt"]
    assert _git(target, "status", "--porcelain").stdout == before


def test_capture_block_does_not_claim_private_only_after_target_drift(tmp_path):
    entry = SimpleNamespace(
        baseline_sha="base-sha",
        execution_root="/private/snapshot",
        target_root="/authority/tree",
    )
    block = _capture_block(
        entry,
        tmp_path,
        {"status": "ready_no_changes", "sha256": "", "diffstat": "0 files"},
        ["neighbor.txt"],
    )
    assert block["status"] == "failed"
    assert block["target_mutated_during_run"] == ["neighbor.txt"]
    assert "TARGET MUTATED DURING RUN" in block["note"]
    assert "private execution snapshot only" not in block["note"]


def test_target_drift_probe_failure_is_unknown_not_clean(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "-q")
    baseline = "missing-baseline"
    entry = SimpleNamespace(target_root=str(target), baseline_sha=baseline)

    def failing_git(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal: baseline unavailable")

    monkeypatch.setattr(subprocess, "run", failing_git)
    evidence = _target_drift_evidence(entry)

    assert evidence["checked"] is False
    assert evidence["paths"] == []
    assert "baseline unavailable" in evidence["error"]
    assert _target_drift_paths(entry) == []


def test_target_drift_includes_index_deletions(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "-q")
    _git(target, "config", "user.name", "test")
    _git(target, "config", "user.email", "test@example.invalid")
    (target / "gone.txt").write_text("baseline\n", encoding="utf-8")
    _git(target, "add", "gone.txt")
    _git(target, "commit", "-qm", "baseline")
    baseline = _git(target, "rev-parse", "HEAD").stdout.strip()
    _git(target, "rm", "gone.txt")
    entry = SimpleNamespace(target_root=str(target), baseline_sha=baseline)

    evidence = _target_drift_evidence(entry)

    assert evidence["checked"] is True
    assert evidence["paths"] == ["gone.txt"]
