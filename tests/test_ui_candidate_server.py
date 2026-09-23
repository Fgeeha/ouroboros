"""Exercise actual server/bootstrap custody and byte identity without a browser install."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
import urllib.request

import pytest

from tests import test_ui_smoke_playwright as ui
from tests.candidate_checkout import SENTINEL_ROUTE

pytestmark = [pytest.mark.serial, pytest.mark.ui_browser]


def _served(running, route: str) -> bytes:
    with urllib.request.urlopen(running["url"] + route, timeout=5) as response:  # noqa: S310
        return response.read()


@pytest.mark.parametrize("launcher", ["direct", "settings"])
def test_real_server_uses_disposable_candidate_and_keeps_identity_through_restart(tmp_path, monkeypatch, launcher):
    monkeypatch.setenv("OUROBOROS_RUN_UI_SMOKE", "1")
    fixture = ui.direct_server_with_data.__wrapped__(tmp_path)
    try:
        if launcher == "settings":
            from tests.test_settings_restart_browser import settings_server

            request = SimpleNamespace(getfixturevalue=lambda name: next(fixture))
            running = settings_server.__wrapped__(request, tmp_path, monkeypatch)
        else:
            running = next(fixture)
        assert running["repo_dir"] != Path(ui.REPO_ROOT)
        assert running["repo_dir"].is_relative_to(tmp_path)
        assert len(running["candidate_identity"]) == 64
        settings = (running["data_dir"] / "settings.json").read_text()
        assert "ui-smoke-key" in settings
        # The source VERSION never carries the suffix, so a server reporting it
        # imported THIS checkout's ouroboros package, not the source or HEAD.
        source_version = (Path(ui.REPO_ROOT) / "VERSION").read_text(encoding="utf-8").strip()
        assert running["candidate_version"] != source_version
        assert running["candidate_version"].startswith(source_version + "+candidate.")
        assert _served(running, SENTINEL_ROUTE) == running["candidate_sentinel"]
        assert json.loads(_served(running, "/api/health"))["runtime_version"] == running["candidate_version"]
        running["restart_server"]()
        # The restarted incarnation re-proves both origins (start_server asserts
        # them too; this is the same claim read from outside the fixture).
        assert _served(running, SENTINEL_ROUTE) == running["candidate_sentinel"]
        assert json.loads(_served(running, "/api/health"))["runtime_version"] == running["candidate_version"]
        assert os.environ["OUROBOROS_DATA_DIR"] != str(running["data_dir"])
    finally:
        fixture.close()
    assert not (tmp_path / "repo").exists()


def test_concurrent_servers_keep_distinct_roots_and_owner_sentinels(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    for key, name in (("HOME", "home"), ("OUROBOROS_DATA_DIR", "data"),
                      ("OUROBOROS_APP_ROOT", "app"), ("OUROBOROS_REPO_DIR", "repo"),
                      ("OUROBOROS_SUBAGENT_PROJECTS_ROOT", "projects"),
                      ("OUROBOROS_SUBAGENT_WORKTREE_ROOT", "worktrees"),
                      ("OUROBOROS_DELIVERABLES_ROOT", "Deliverables")):
        path = owner / name
        path.mkdir(parents=True)
        (path / "sentinel").write_bytes(b"owner state")
        monkeypatch.setenv(key, str(path))
    before = {path.relative_to(owner): path.read_bytes() for path in owner.rglob("sentinel")}
    monkeypatch.setenv("OUROBOROS_RUN_UI_SMOKE", "1")
    barrier = Barrier(2)

    def run(number):
        root = tmp_path / f"run-{number}"
        fixture = ui.direct_server_with_data.__wrapped__(root)
        try:
            running = next(fixture)
            assert running["data_dir"].is_relative_to(root)
            assert running["repo_dir"].is_relative_to(root)
            barrier.wait(timeout=90)  # Both owned servers must be alive together.
            assert _served(running, SENTINEL_ROUTE) == running["candidate_sentinel"]
            return running["url"], running["candidate_identity"], running["candidate_sentinel"]
        finally:
            fixture.close()
            assert not (root / "repo").exists()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(run, range(2))
    assert first[0] != second[0]
    assert first[1] == second[1], "the same source selection is one candidate identity"
    assert first[2] != second[2], "each live server proved its OWN checkout, not a shared one"
    assert {path.relative_to(owner): path.read_bytes()
            for path in owner.rglob("*") if path.is_file()} == before
