from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.browser_ci_scope import documentation_only
from tests import browser_lane

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


@pytest.mark.parametrize("paths,expected", [
    (["README.md", "docs/development/14-build-and-ci.md"], True),
    (["docs/new.rst"], True),
    ([".github/PULL_REQUEST_TEMPLATE.md"], True),
    (["docs/install/index.html"], False),
    (["site/src/index.md"], False),
    (["assets/logo.md"], False),
    (["README.md", "server.py"], False),
    (["requirements.txt"], False),
    (["Makefile"], False),
    (["Ouroboros.spec"], False),
    (["prompts/SYSTEM.md"], False),
    (["skills/new/SKILL.md"], False),
    (["tests/test_new.py"], False),
    ([".github/workflows/ci.yml"], False),
    (["unknown/new-code.ext"], False),
    ([], False),
])
def test_only_complete_documentation_changes_skip_browser(paths, expected):
    assert documentation_only(paths) is expected


def _workflow(name):
    import yaml

    loaded = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded, loaded.get("on", loaded.get(True))


def test_one_browser_lane_serves_pull_requests_manual_tags_and_ouroboros_pushes():
    shared, triggers = _workflow("ui-browser.yml")
    assert list(triggers) == ["workflow_call"], "the shared lane runs only when called"
    job = shared["jobs"]["ui-smoke"]
    assert "secrets." not in str(job)
    full = next(step for step in job["steps"] if "--require-ui-browser" in step.get("run", ""))
    assert "pytest tests/ -m ui_browser" in full["run"]
    assert "safe_test.py" in full["run"]
    setup = next(step for step in job["steps"] if step.get("id") == "setup_python")
    assert setup["with"]["install-project"] == "false"

    ci, ci_triggers = _workflow("ci.yml")
    caller = ci["jobs"]["ui-smoke"]
    assert caller["uses"] == "./.github/workflows/ui-browser.yml"
    assert "github.event_name == 'pull_request'" in caller["if"]
    assert "github.event_name == 'workflow_dispatch'" in caller["if"]
    assert "startsWith(github.ref, 'refs/tags/v')" in caller["if"]
    # A push reaches the lane through its own workflow, never through ci.yml's
    # path-filtered push trigger.
    assert "push" not in caller["if"] and "schedule" not in caller["if"]
    assert ci_triggers["schedule"] == [{"cron": "37 4 * * *"}, {"cron": "17 3 * * *"}]

    push, push_triggers = _workflow("ui-browser-push.yml")
    assert list(push_triggers) == ["push"]
    assert push_triggers["push"] == {"branches": ["ouroboros"]}
    assert push["jobs"]["ui-smoke"]["uses"] == caller["uses"]


def _item(name, marked=True):
    return SimpleNamespace(nodeid=name,
                           get_closest_marker=lambda marker: marked and marker == "ui_browser")


class _Config(SimpleNamespace):
    def getoption(self, _name):
        return True


def _config(**kwargs):
    plugins = {}
    manager = SimpleNamespace(
        get_plugin=plugins.get,
        register=lambda plugin, name: plugins.__setitem__(name, plugin),
    )
    return _Config(args=["tests/"], pluginmanager=manager,
                   option=SimpleNamespace(collectonly=False), **kwargs)


@pytest.mark.parametrize("partial", [False, True])
def test_collection_guard_accepts_full_lane_and_refuses_deselection(partial):
    config = _config()
    browser_lane.pytest_configure(config)
    items = [_item("first"), _item("second")]
    hook = browser_lane.pytest_collection_modifyitems(config, items)
    next(hook)
    if partial:
        items.pop()
        with pytest.raises(pytest.UsageError, match="UI_BROWSER_INCOMPLETE"):
            next(hook)
    else:
        with pytest.raises(StopIteration):
            next(hook)
        assert config._required_ui_nodes == {"first", "second"}
        assert browser_lane._reconciliation(config).required == {"first", "second"}


def test_collection_guard_refuses_an_empty_lane():
    hook = browser_lane.pytest_collection_modifyitems(_config(), [])
    next(hook)
    with pytest.raises(pytest.UsageError, match="UI_BROWSER_INCOMPLETE"):
        next(hook)


def test_browser_skip_cannot_become_a_successful_required_run():
    item = SimpleNamespace(config=_config(), nodeid="tests/test_x.py::missing_engine")
    report = SimpleNamespace(skipped=True, outcome="skipped",
                             longrepr=("tests/test_x.py", 10, "Skipped: no browser executable"))
    hook = browser_lane.pytest_runtest_makereport(item, None)
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: report))
    assert report.outcome == "failed"
    assert "UI_BROWSER_SKIPPED" in report.longrepr


def test_registered_platform_skip_survives_with_its_recorded_reason():
    nodeid = "tests/test_settings_restart_browser.py::test_pending_survives_reconnect"
    item = SimpleNamespace(config=_config(), nodeid=nodeid)
    report = SimpleNamespace(
        skipped=True, outcome="skipped", when="setup", nodeid=nodeid,
        longrepr=("tests/test_settings_restart_browser.py", 17,
                  "Skipped: POSIX test launcher; production browser behavior is shared"))
    hook = browser_lane.pytest_runtest_makereport(item, None)
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: report))
    assert report.outcome == "skipped"
    assert "POSIX test launcher" in report.ui_browser_platform_skip

    reconciliation = browser_lane.LaneReconciliation({nodeid})
    reconciliation.pytest_runtest_logreport(report)
    assert reconciliation.missing == [], "a permitted skip is still an executed node"
    assert reconciliation.platform_skips[nodeid].endswith("shared")


def test_collected_nodes_that_never_ran_fail_the_lane():
    reconciliation = browser_lane.LaneReconciliation({"a::one", "b::two"})
    reconciliation.pytest_runtest_logreport(
        SimpleNamespace(when="setup", outcome="passed", nodeid="a::one", skipped=False))
    assert reconciliation.missing == ["a::one", "b::two"], "setup alone is not a run"
    reconciliation.pytest_runtest_logreport(
        SimpleNamespace(when="call", outcome="passed", nodeid="a::one", skipped=False))
    assert reconciliation.missing == ["b::two"]

    session = SimpleNamespace(config=_config(), exitstatus=0)
    session.config.pluginmanager.register(reconciliation, "ui_browser_lane_reconciliation")
    browser_lane.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1, "an unexecuted collected node must not exit green"


def test_xdist_controller_receives_collection_without_collecting_itself():
    config = _config()
    browser_lane.pytest_configure(config)
    tracker = browser_lane._reconciliation(config)
    tracker.pytest_xdist_node_collection_finished(None, ["tests/a.py::one"])
    session = SimpleNamespace(config=config, exitstatus=0)
    browser_lane.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1
    tracker.pytest_runtest_logreport(SimpleNamespace(
        when="call", outcome="passed", nodeid="tests/a.py::one"))
    session.exitstatus = 0
    browser_lane.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0


def test_required_engine_is_launched_and_missing_engine_fails(monkeypatch):
    from contextlib import nullcontext
    import playwright.sync_api

    monkeypatch.setenv("OUROBOROS_RUN_UI_SMOKE", "1")
    monkeypatch.setenv("OUROBOROS_EXPECT_BROWSER_ENGINES", "chromium,webkit")
    launches = []

    def launch(**kwargs):
        launches.append(kwargs)
        return SimpleNamespace(close=lambda: None)

    engine = SimpleNamespace(launch=launch)
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: nullcontext(
        SimpleNamespace(chromium=engine, webkit=engine)))
    session = SimpleNamespace(config=_config())
    browser_lane.pytest_collection_finish(session)
    assert launches == [{"headless": True}] * 2
    engine.launch = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("missing executable"))
    with pytest.raises(pytest.UsageError, match="UI_BROWSER_UNAVAILABLE"):
        browser_lane.pytest_collection_finish(session)
