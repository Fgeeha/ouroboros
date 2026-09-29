"""Source identity for task-owned isolated copies, independent of isolation.

The supervisor records this binding after provisioning. Legacy self_worktree
rows without it retain their original meaning: a copy of Ouroboros's body.
The working folder is a target address, not evidence that a foreign project is
Ouroboros source merely because both use the same isolated-copy mechanism.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any


def copy_binding(value: Any) -> dict:
    """Read the host's existing task metadata carrier, preserving legacy absence."""
    metadata = (value.get("metadata", value) if isinstance(value, dict)
                else getattr(value, "task_metadata", {}))
    binding = metadata.get("workspace_copy") if isinstance(metadata, dict) else None
    return binding if isinstance(binding, dict) else {}


def source_is_system_repo(source: Any, system_repo: Any) -> bool:
    """Admission-time Git identity, including nested/linked copies of the body."""
    source, system = Path(source).resolve(), Path(system_repo).resolve()
    if source == system:
        return True
    def common(root):
        try:
            proc = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=str(root),
                                  capture_output=True, text=True, encoding="utf-8", timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode:
            return None
        path = Path(proc.stdout.strip())
        return (path if path.is_absolute() else root / path).resolve()
    source_common = common(source)
    return source_common is not None and source_common == common(system)


def workspace_copy_source_is_system(ctx: Any, selected_root: str = "") -> bool:
    """The selected copy source before scheduling; absence selects the own body."""
    from ouroboros.tools.tool_resolution import system_repo_dir_for

    if not selected_root:
        return True
    binding = copy_binding(ctx)
    if binding and Path(selected_root).resolve() == Path(binding.get("execution_root") or ".").resolve():
        return binding.get("source_is_system_repo") is not False
    return source_is_system_repo(selected_root, system_repo_dir_for(ctx))


def is_system_copy(ctx: Any) -> bool:
    """Own-body policy of an already admitted isolated child; old rows stay old."""
    from ouroboros.contracts.task_constraint import normalize_task_constraint

    constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    isolated = ((constraint is not None and constraint.surface == "self_worktree")
                or getattr(ctx, "workspace_mode", "") == "self_worktree")
    if not isolated:
        return False
    binding = copy_binding(ctx)
    if not binding:
        return True
    selected = (constraint.write_root if constraint is not None else "") or getattr(ctx, "workspace_root", "")
    if not selected or Path(selected).resolve() != Path(binding.get("execution_root") or ".").resolve():
        return True
    return binding.get("source_is_system_repo") is not False


def admitted_copy_metadata(write_root: str, data_dir: Any = None) -> dict:
    """Materialize the newly provisioned host registry fact in durable task state."""
    from ouroboros.subagent_worktrees import list_worktrees

    for row in list_worktrees(data_dir):
        if row.get("path") != write_root or row.get("kind") == "delegated_exec":
            continue
        return {
            "source_root": row["repo_dir"], "execution_root": row["path"],
            "source_is_system_repo": row.get("source_is_system_repo", True),
            "baseline_sha": row["base_sha"],
            "file_baseline": row.get("file_baseline", {}),
        }
    raise ValueError("provisioned isolated child has no task-owned registry binding")
