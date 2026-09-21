"""A read-only child reads what its parent points it to (owner T4=A, #1105).

Measured on a live install: a root task put files into its own ``task_drive``
and sent four read-only children to check them; all four were refused with
``outside selected root``. The recorded reason for hiding the orchestrator
roots from children (``tool_access.py``: "a child must not read sibling
projects") covers ``subagent_projects`` only. Now a child reads the
owner-visible Deliverables root and the ``task_drive``/``artifact_store`` of
its OWN lineage (parent and root ids from its own lineage fields), anchored on
the canonical data root while the child itself runs on a headless drive. A
sibling's or a stranger's task files stay refused; secret-named files in a
parent's drive stay denied by name; ``subagent_projects`` stays top-level only.
"""
from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

from ouroboros.contracts.task_constraint import TaskConstraint
from ouroboros.tool_access import (
    _POLICY,
    _TOP_LEVEL_PRINCIPAL_POLICY,
    decide_tool_access,
    summarize_subagent_profile,
)
from ouroboros.tools.registry import ToolContext, ToolRegistry

PARENT = "p07499dc017c01f83"
ROOT = "r5173b7c3c15d4c0b"
CHILD = "c11ae4fd0aa111111"
SIBLING = "s339e97de0b222222"
STRANGER = "x8b8af5e9cc333333"


@pytest.fixture
def geometry(tmp_path, monkeypatch):
    """The parent's task files live on the CANONICAL data root; the child runs
    on its own headless drive; the owner home is a fake tmp home."""
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    canonical = tmp_path / "data"
    headless = tmp_path / "headless"
    for path in (home, repo, canonical, headless):
        path.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", lambda: home)
    monkeypatch.setenv("OUROBOROS_USER_FILES_ROOT", str(home))
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setenv("OUROBOROS_SAFETY_MODE", "off")
    (repo / "README.md").write_text("repo readme\n", encoding="utf-8")
    parent_drive = canonical / "task_drives" / PARENT
    (parent_drive / "source" / "ouroboros").mkdir(parents=True)
    (parent_drive / "source" / "ouroboros" / "update_letter.py").write_text(
        "PARENT_DRIVE_BYTES = 1\n", encoding="utf-8")
    (parent_drive / "triage-draft.json").write_text('{"triage": "draft"}\n', encoding="utf-8")
    (parent_drive / ".env").write_text("SECRET_TOKEN=1\n", encoding="utf-8")
    (parent_drive / "settings.json").write_text('{"OPENAI_API_KEY": "sk-secret"}\n', encoding="utf-8")
    root_artifacts = canonical / "task_results" / "artifacts" / ROOT
    root_artifacts.mkdir(parents=True)
    (root_artifacts / "report.txt").write_text("ROOT_ARTIFACT_BYTES\n", encoding="utf-8")
    sibling_drive = canonical / "task_drives" / SIBLING
    sibling_drive.mkdir(parents=True)
    (sibling_drive / "notes.txt").write_text("SIBLING_BYTES\n", encoding="utf-8")
    stranger_artifacts = canonical / "task_results" / "artifacts" / STRANGER
    stranger_artifacts.mkdir(parents=True)
    (stranger_artifacts / "out.txt").write_text("STRANGER_BYTES\n", encoding="utf-8")
    deliverables = home / "Deliverables"
    deliverables.mkdir()
    (deliverables / "answer.txt").write_text("DELIVERABLE_BYTES needle\n", encoding="utf-8")
    return SimpleNamespace(
        home=home, repo=repo, canonical=canonical, headless=headless,
        parent_drive=parent_drive, root_artifacts=root_artifacts,
        sibling_drive=sibling_drive, stranger_artifacts=stranger_artifacts,
        deliverables=deliverables,
    )


def child_registry(geo, *, drive=None, acting=False):
    """A delegated child of PARENT under ROOT, through the real registry."""
    ctx = ToolContext(repo_dir=geo.repo, drive_root=drive or geo.headless, task_id=CHILD)
    ctx.budget_drive_root = str(geo.canonical)
    ctx.task_metadata = {
        "delegation_role": "subagent",
        "parent_task_id": PARENT,
        "root_task_id": ROOT,
        "budget_drive_root": str(geo.canonical),
    }
    if acting:
        work = geo.home / "work"
        work.mkdir(exist_ok=True)
        ctx.workspace_root = work
        ctx.workspace_mode = "external"
        ctx.task_constraint = TaskConstraint(
            mode="acting_subagent", allow_enable=False, surface="external_workspace")
    else:
        ctx.task_constraint = TaskConstraint(mode="local_readonly_subagent", allow_enable=False)
    registry = ToolRegistry(repo_dir=geo.repo, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    return registry, ctx


# --- the lineage read: parent's and root's task files, never a sibling's ------

def test_child_reads_its_parents_task_drive_from_a_headless_drive(geometry):
    registry, ctx = child_registry(geometry)
    target = geometry.parent_drive / "source" / "ouroboros" / "update_letter.py"

    out = registry.execute("read_file", {"root": "task_drive", "path": str(target)})

    assert "PARENT_DRIVE_BYTES" in out, out
    assert out.startswith("# task_drive:"), out
    assert ctx.last_read_view["opened_root"] == "task_drive"
    assert ctx.last_read_view["target"] == str(target.resolve())


def test_child_reads_the_root_tasks_artifact(geometry):
    registry, _ctx = child_registry(geometry)
    target = geometry.root_artifacts / "report.txt"

    out = registry.execute("read_file", {"root": "artifact_store", "path": str(target)})

    assert "ROOT_ARTIFACT_BYTES" in out, out
    assert out.startswith("# artifact_store:"), out


def test_single_drive_child_reads_the_parent_drive_too(geometry):
    registry, _ctx = child_registry(geometry, drive=geometry.canonical)
    out = registry.execute(
        "read_file", {"root": "task_drive", "path": str(geometry.parent_drive / "triage-draft.json")})
    assert '"triage": "draft"' in out, out


def test_an_acting_child_shares_the_lineage_read(geometry):
    registry, _ctx = child_registry(geometry, acting=True)
    out = registry.execute(
        "read_file", {"root": "task_drive", "path": str(geometry.parent_drive / "triage-draft.json")})
    assert '"triage": "draft"' in out, out


def test_a_siblings_drive_and_a_strangers_artifacts_stay_refused(geometry):
    registry, _ctx = child_registry(geometry)

    sibling = registry.execute(
        "read_file", {"root": "task_drive", "path": str(geometry.sibling_drive / "notes.txt")})
    stranger = registry.execute(
        "read_file", {"root": "artifact_store", "path": str(geometry.stranger_artifacts / "out.txt")})

    assert "SIBLING_BYTES" not in sibling and "outside selected root=task_drive" in sibling, sibling
    assert "STRANGER_BYTES" not in stranger and "outside selected root=artifact_store" in stranger, stranger


def test_lineage_is_read_only_even_for_a_top_level_parent_drive(geometry):
    """The rule is a READ rule: an acting child never writes into its parent's
    drive through the same path, and its own task_drive stays the write target
    the matrix says (none for an acting child)."""
    registry, _ctx = child_registry(geometry, acting=True)
    target = geometry.parent_drive / "triage-draft.json"
    before = target.read_text(encoding="utf-8")

    out = registry.execute("write_file", {"root": "task_drive", "path": str(target), "content": "x"})

    assert out.startswith("⚠️"), out
    assert target.read_text(encoding="utf-8") == before


# --- secrets in a parent's drive stay denied by NAME -------------------------

@pytest.mark.parametrize("name", [".env", "settings.json"])
def test_secret_named_files_in_the_parents_drive_stay_denied(geometry, name):
    registry, _ctx = child_registry(geometry)
    out = registry.execute("read_file", {"root": "task_drive", "path": str(geometry.parent_drive / name)})
    assert "READ_FILE_BLOCKED" in out and "secret" in out, out
    assert "SECRET_TOKEN" not in out and "sk-secret" not in out


def test_child_lists_the_parents_drive_with_secret_names_hidden(geometry):
    registry, _ctx = child_registry(geometry)

    out = registry.execute("list_files", {"root": "task_drive", "path": str(geometry.parent_drive)})

    items = json.loads(out)
    assert "triage-draft.json" in items and "source/" in items, items
    assert ".env" not in items and "settings.json" not in items, items
    assert any("hidden from this subagent" in item for item in items), items


# --- the pure lineage function ------------------------------------------------

def test_lineage_task_ids_are_own_parent_and_root_and_nothing_else(geometry):
    from ouroboros.tool_access import lineage_task_ids

    _registry, ctx = child_registry(geometry)
    assert lineage_task_ids(ctx) == (CHILD, PARENT, ROOT)

    ctx.task_metadata["root_task_id"] = PARENT  # parent IS the root: no duplicate
    assert lineage_task_ids(ctx) == (CHILD, PARENT)

    ctx.task_metadata["parent_task_id"] = "../escape"  # malformed ids are dropped, not guessed
    ctx.task_metadata["root_task_id"] = ""
    assert lineage_task_ids(ctx) == (CHILD,)

    top = ToolContext(repo_dir=geometry.repo, drive_root=geometry.canonical, task_id=ROOT)
    assert lineage_task_ids(top) == (ROOT,)


def test_lineage_read_base_names_the_containing_lineage_root(geometry):
    from ouroboros.tool_access import lineage_read_base

    _registry, ctx = child_registry(geometry)
    parent_file = geometry.parent_drive / "triage-draft.json"
    root_file = geometry.root_artifacts / "report.txt"

    assert lineage_read_base(ctx, "task_drive", parent_file) == geometry.parent_drive.resolve()
    assert lineage_read_base(ctx, "artifact_store", root_file) == geometry.root_artifacts.resolve()
    # The label must match the physical kind: a task_drive path is not an artifact base.
    assert lineage_read_base(ctx, "artifact_store", parent_file) is None
    assert lineage_read_base(ctx, "task_drive", geometry.sibling_drive / "notes.txt") is None
    assert lineage_read_base(ctx, "runtime_data", parent_file) is None
    # The child's OWN task root on its headless drive is a lineage base as well.
    own = geometry.headless / "task_drives" / CHILD / "scratch.txt"
    assert lineage_read_base(ctx, "task_drive", own) == (geometry.headless / "task_drives" / CHILD).resolve()


# --- Deliverables: a read-only child reads, never writes ---------------------

def test_deliverables_row_reads_only_and_only_for_the_readonly_child():
    for op in ("read", "list", "search"):
        assert decide_tool_access(profile="local_readonly_subagent", root="deliverables", operation=op).allow, op
    for op in ("write", "edit", "shell", "vcs", "service", "review", "delegate"):
        assert not decide_tool_access(profile="local_readonly_subagent", root="deliverables", operation=op).allow, op
    for profile in ("acting_subagent", "local_readonly_subagent"):
        assert not decide_tool_access(profile=profile, root="subagent_projects", operation="read").allow, profile
    assert not decide_tool_access(profile="acting_subagent", root="deliverables", operation="read").allow
    # Top-level principals are untouched: one shared matrix object, unchanged rows.
    for profile in ("workspace_task", "external_workspace_task", "self_modification"):
        assert _POLICY[profile] is _TOP_LEVEL_PRINCIPAL_POLICY
    assert _TOP_LEVEL_PRINCIPAL_POLICY["deliverables"] == {"read", "list", "search"}
    assert _TOP_LEVEL_PRINCIPAL_POLICY["subagent_projects"] == {"read", "list", "search"}


def test_child_reads_lists_and_searches_deliverables_but_cannot_touch_them(geometry):
    registry, _ctx = child_registry(geometry)
    answer = geometry.deliverables / "answer.txt"

    read = registry.execute("read_file", {"root": "deliverables", "path": "answer.txt"})
    listing = registry.execute("list_files", {"root": "deliverables", "path": "."})
    search = registry.execute("search_code", {"root": "deliverables", "query": "needle"})

    assert "DELIVERABLE_BYTES" in read and read.startswith("# deliverables:answer.txt"), read
    assert "answer.txt" in json.loads(listing), listing
    assert "deliverables:answer.txt:1:" in search, search

    write = registry.execute("write_file", {"root": "deliverables", "path": "answer.txt", "content": "x"})
    edit = registry.execute("edit_text", {"root": "deliverables", "path": "answer.txt",
                                          "old_str": "needle", "new_str": "x"})
    shell = registry.execute("run_command", {"command": "ls", "cwd": "deliverables"})
    for out in (write, edit, shell):
        assert out.startswith("⚠️"), out
    assert answer.read_text(encoding="utf-8") == "DELIVERABLE_BYTES needle\n"
    assert "⚠️" in registry.execute("list_files", {"root": "subagent_projects", "path": "."})


def test_readonly_child_schema_enums_follow_the_matrix(geometry):
    registry, _ctx = child_registry(geometry)

    def enum(name):
        return registry.get_schema_by_name(name)["function"]["parameters"]["properties"]["root"]["enum"]

    for name in ("read_file", "list_files"):
        assert "deliverables" in enum(name), name
        assert "subagent_projects" not in enum(name) and "user_files" not in enum(name), name
    assert set(enum("search_code")) == {"active_workspace", "system_repo", "skill_payload", "deliverables"}
    assert enum("query_code") == ["active_workspace", "system_repo"]


# --- both sides see what the child can read -----------------------------------

def test_profile_summary_names_readable_and_unreadable_roots(monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    readonly = summarize_subagent_profile("local_readonly_subagent", effective_lane="light").splitlines()
    assert len(readonly) == 2, readonly
    assert readonly[0].startswith("child capabilities — ") and "model_lane=light" in readonly[0]
    readable, unreadable = readonly[1].split(" · unreadable=")
    assert readable.startswith("readable=") and "deliverables" in readable and "task_drive" in readable
    assert "parent" in readable and "sibling" in readable, readable
    assert unreadable == "subagent_projects, user_files", unreadable

    acting = summarize_subagent_profile("acting_subagent").splitlines()
    assert len(acting) == 2, acting
    acting_readable, acting_unreadable = acting[1].split(" · unreadable=")
    assert "deliverables" not in acting_readable and "deliverables" in acting_unreadable
