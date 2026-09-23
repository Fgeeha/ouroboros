"""Real parent/child writes and concurrent runs stay on disposable roots."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ouroboros.preflight_runner import _preflight_env
from ouroboros.settings_defaults import settings_env_keys
from ouroboros.test_environment import isolated_environment, settings_keys

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.serial


def test_pytest_cache_belongs_to_session_boundary(pytestconfig):
    from tests.conftest import _PYTEST_ROOT

    assert Path(pytestconfig.cache._cachedir).is_relative_to(_PYTEST_ROOT)


def test_static_settings_scrub_covers_runtime_vocabulary():
    assert set(settings_env_keys()) <= settings_keys()


_WRITE_ROOTS = """
import json, os, pathlib
from ouroboros import config
from supervisor import update_merge
roots = [config.DATA_DIR, pathlib.Path(config.get_subagent_projects_root()),
         pathlib.Path(config.get_subagent_worktree_root()), pathlib.Path(config.get_deliverables_root()),
         config.APP_ROOT, pathlib.Path.home(), pathlib.Path(os.environ['PYTHONUSERBASE'])]
for root in roots:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'probe').write_text('test-only')
config.save_settings({'TOTAL_BUDGET': 0})
update_merge._log_supervisor({'type': 'isolation_probe'})
print(json.dumps([str(root) for root in roots]))
"""


def test_preflight_children_and_concurrent_runs_leave_owner_sentinels_untouched(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    sentinels = []
    for name in ("data", "projects", "worktrees", "Deliverables", "home", "app", "userbase"):
        path = owner / name / "sentinel"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"unchanged")
        sentinels.append(path)
    for key, name in (("HOME", "home"), ("OUROBOROS_APP_ROOT", "app"),
                      ("OUROBOROS_DATA_DIR", "data"), ("OUROBOROS_SUBAGENT_PROJECTS_ROOT", "projects"),
                      ("OUROBOROS_SUBAGENT_WORKTREE_ROOT", "worktrees"),
                      ("OUROBOROS_DELIVERABLES_ROOT", "Deliverables"), ("PYTHONUSERBASE", "userbase")):
        monkeypatch.setenv(key, str(owner / name))
    monkeypatch.setenv("OUROBOROS_MANAGED_BY_LAUNCHER", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://invalid.example")
    before = {path.relative_to(owner): path.read_bytes() for path in sentinels}

    def run(number):
        root = tmp_path / f"run-{number}"
        env = _preflight_env(root, REPO)
        assert not {"OPENAI_API_KEY", "OPENAI_COMPATIBLE_BASE_URL", "OUROBOROS_MANAGED_BY_LAUNCHER"} & env.keys()
        result = subprocess.run([sys.executable, "-c", _WRITE_ROOTS], cwd=REPO, env=env,
                                text=True, capture_output=True, timeout=120)
        assert result.returncode == 0, result.stderr
        paths = json.loads(result.stdout.splitlines()[-1])
        assert all(Path(path).is_relative_to(root) for path in paths)
        assert (root / "data" / "settings.json").is_file()
        assert (root / "data" / "logs" / "supervisor.jsonl").is_file()
        return set(paths)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(run, range(2))
    assert first.isdisjoint(second)
    assert {path.relative_to(owner): path.read_bytes() for path in owner.rglob("*") if path.is_file()} == before


def test_scrubbed_child_reinjects_roots_but_preserves_explicit_synthetic_home(tmp_path):
    from tests.conftest import _isolated_child_env, _PYTEST_DEFAULTS

    env = _isolated_child_env({})
    for key in ("HOME", "OUROBOROS_APP_ROOT", "OUROBOROS_SUBAGENT_PROJECTS_ROOT",
                "OUROBOROS_SUBAGENT_WORKTREE_ROOT", "OUROBOROS_DELIVERABLES_ROOT",
                "PYTHONPYCACHEPREFIX", "PYTHONUSERBASE"):
        assert env[key] == _PYTEST_DEFAULTS[key]
    empty = _isolated_child_env({key: "" for key in _PYTEST_DEFAULTS})
    for key in ("OUROBOROS_DATA_DIR", "OUROBOROS_SETTINGS_PATH", "OUROBOROS_APP_ROOT",
                "OUROBOROS_SUBAGENT_PROJECTS_ROOT", "OUROBOROS_SUBAGENT_WORKTREE_ROOT",
                "OUROBOROS_DELIVERABLES_ROOT", "PYTHONUSERBASE"):
        assert empty[key] == _PYTEST_DEFAULTS[key]
    assert empty["PYTHONDONTWRITEBYTECODE"] == empty["PYTHONPYCACHEPREFIX"] == ""
    selected = tmp_path / "synthetic-home"
    env = _isolated_child_env({"HOME": str(selected), "USERPROFILE": str(selected),
                               "OUROBOROS_DELIVERABLES_ROOT": str(tmp_path / "explicit")})
    assert env["HOME"] == str(selected)
    assert "OUROBOROS_SUBAGENT_PROJECTS_ROOT" not in env
    assert "OUROBOROS_SUBAGENT_WORKTREE_ROOT" not in env
    assert env["OUROBOROS_DELIVERABLES_ROOT"] == str(tmp_path / "explicit")


def test_git_discovery_cannot_escape_to_an_ancestor_checkout(tmp_path, monkeypatch):
    from ouroboros.workspace_admission import validate_workspace_root, WorkspaceRootError

    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    subprocess.run(["git", "init", str(ancestor)], check=True, capture_output=True)
    root = ancestor / "test-boundary"
    env = isolated_environment(root, REPO)
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                            cwd=root / "tmp", env=env, capture_output=True)
    assert result.returncode != 0
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(root))
    assert validate_workspace_root(root / "tmp", system_repo_dir=REPO,
                                   drive_root=root / "data") == root / "tmp"
    (root / "tmp" / ".git").write_text("invalid git metadata", encoding="utf-8")
    with pytest.raises(WorkspaceRootError, match="could not be resolved"):
        validate_workspace_root(root / "tmp", system_repo_dir=REPO, drive_root=root / "data")
    (root / "tmp" / ".git").unlink()
    own = root / "tmp" / "own"
    subprocess.run(["git", "init", str(own)], env=env, check=True, capture_output=True)
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                            cwd=own, env=env, check=True, capture_output=True, text=True)
    assert Path(result.stdout.strip()).resolve() == own.resolve()


def test_boundary_is_established_before_runtime_imports_in_nested_collection(tmp_path):
    root = tmp_path / "boundary"
    env = isolated_environment(root, REPO)
    result = subprocess.run([sys.executable, "-c", "import tests.conftest\n" + _WRITE_ROOTS],
                            cwd=REPO, env=env, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr
    paths = json.loads(result.stdout.splitlines()[-1])
    assert all(Path(path).is_relative_to(root) for path in paths)


def test_safe_test_refuses_a_disposable_root_inside_the_checkout(tmp_path):
    """A nested root is not disposable: git walks up from it into THIS working tree."""
    launcher = [sys.executable, "-I", "-S", str(REPO / "scripts" / "safe_test.py")]
    probe = [str(sys.executable), "-c", "print('boundary ran')"]

    refused = subprocess.run(launcher + ["--temp-parent", str(REPO / "build"), "--"] + probe,
                             cwd=REPO, text=True, capture_output=True, timeout=120)
    assert refused.returncode == 2, refused.stdout
    assert "outside the repository working tree" in refused.stderr
    assert "boundary ran" not in refused.stdout

    accepted = subprocess.run(launcher + ["--temp-parent", str(tmp_path), "--"] + probe,
                              cwd=REPO, text=True, capture_output=True, timeout=120)
    assert accepted.returncode == 0, accepted.stderr
    assert "boundary ran" in accepted.stdout
