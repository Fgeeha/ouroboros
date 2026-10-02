"""GitHub tools retain Project selection and explicit targets through every subcall.

The checks reader is driven through the registered tool over a fake ``gh``: its report
states GitHub's recorded facts and the sources it could not read, never a verdict.
"""

import json
import subprocess
from types import SimpleNamespace

import pytest

from ouroboros.tools import github, github_checks
from ouroboros.tools.registry import ToolContext


def _context(tmp_path, kind):
    system = tmp_path / "system"
    project = tmp_path / "project"
    system.mkdir(exist_ok=True)
    project.mkdir(exist_ok=True)
    ctx = ToolContext(repo_dir=system, drive_root=tmp_path / "data", task_id="task-fixture")
    if kind == "queued":
        ctx.workspace_root, ctx.workspace_mode, ctx.project_id = project, "external", "project-fixture"
    elif kind == "room":
        ctx.is_direct_chat, ctx.project_id = True, "project-fixture"
        ctx.task_metadata = {"_project_room_dir": str(project)}
    return ctx, project if kind != "system" else system


SHA, MOVED = "a" * 40, "b" * 40
BASE = "https://github.example/owner/selected"


def _run(run_id, name="CI", status="completed", conclusion="success", **extra):
    return {"databaseId": run_id, "workflowName": name, "event": "pull_request", "status": status,
            "conclusion": conclusion, "attempt": 1, "url": f"{BASE}/actions/runs/{run_id}", "headSha": SHA, **extra}


def _job(job_id, name, status="completed", conclusion="success", steps=(), run_id=11):
    return {"databaseId": job_id, "name": name, "status": status, "conclusion": conclusion,
            "url": f"{BASE}/actions/runs/{run_id}/job/{job_id}",
            "steps": [{"number": number, "name": step, "status": step_status, "conclusion": step_conclusion}
                      for number, step, step_status, step_conclusion in steps]}


@pytest.fixture
def gh_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(github, "github_token_from_env_or_settings", lambda: "fixture-token")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = ""
        if argv[1:3] in (["issue", "list"], ["pr", "list"]):
            output = "[]"
        elif argv[1:3] in (["issue", "view"], ["pr", "view"]):
            output = json.dumps({"number": 7, "title": "Fixture", "state": "OPEN", "author": {"login": "fixture"},
                                 "headRefOid": SHA, "url": f"{BASE}/pull/7"})
        elif argv[1:3] == ["run", "list"]:
            output = json.dumps([_run(11, conclusion="failure")])
        elif argv[1:3] == ["run", "view"]:
            output = json.dumps({"jobs": [_job(55, "test", conclusion="failure")]})
        elif argv[1] == "api":
            output = "[]"
        elif argv[1:3] == ["issue", "create"]:
            output = "https://github.com/owner/selected/issues/7"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


_CALLS = [
    ("list_github_issues", {}, 1), ("get_github_issue", {"number": 7}, 1),
    ("comment_on_issue", {"number": 7, "body": "text"}, 1),
    ("close_github_issue", {"number": 7, "comment": "closing"}, 2),
    ("create_github_issue", {"title": "Title", "body": "Body", "labels": "bug"}, 2),
    ("list_github_prs", {}, 1), ("get_github_pr", {"number": 7}, 3),
    ("comment_on_pr", {"number": 7, "body": "text"}, 1),
    ("get_github_checks", {"number": 7}, 4),
]


@pytest.mark.parametrize("kind", ["system", "queued", "room"])
@pytest.mark.parametrize("name,args,count", _CALLS)
@pytest.mark.parametrize("repo", ["", "github.example/owner/selected"])
def test_every_repository_tool_keeps_target_in_all_subcalls(tmp_path, gh_calls, monkeypatch, kind, name, args, count, repo):
    ctx, expected_cwd = _context(tmp_path, kind)
    monkeypatch.setenv("GH_REPO", "unrelated/wrong-repo")
    monkeypatch.setenv("GH_HOST", "configured.example")
    entry = next(item for item in github.get_tools() if item.name == name)

    result = entry.handler(ctx, **args, repo=repo)

    assert not result.startswith("⚠️"), result
    assert len(gh_calls) == count
    assert "repo" in entry.schema["parameters"]["properties"]
    assert sum(argv[1] == "api" for argv, _ in gh_calls) == (name == "get_github_checks")
    for argv, kwargs in gh_calls:
        if argv[1] == "api":  # The checks reader's one literal path: its target is the URL GitHub returned.
            assert argv == ["gh", "api", "repos/owner/selected/check-runs/55/annotations?per_page=100",
                            "--hostname", "github.example"]
            continue
        assert kwargs["cwd"] == str(expected_cwd)
        if repo:
            assert argv[-2:] == ["--repo", repo]
        else:
            assert "--repo" not in argv
        if kind != "system":
            assert "GH_REPO" not in kwargs["env"]
        assert kwargs["env"]["GH_HOST"] == "configured.example"
    if name == "get_github_pr" and (kind != "system" or repo):
        assert "fetch_pr_ref(" not in result
        assert "stage_pr_merge(" not in result


@pytest.mark.parametrize("failure", ["note", "missing-room", "fileless", "missing-workspace", "invalid-workspace-mode"])
def test_unusable_project_never_calls_gh_on_system_repo(tmp_path, gh_calls, failure):
    ctx, project = _context(tmp_path, "queued" if "workspace" in failure else "room")
    if failure == "note":
        ctx.task_metadata = {"_project_room_note": "registry unavailable"}
    elif failure == "fileless":
        ctx.task_metadata = {}
    elif failure == "invalid-workspace-mode":
        ctx.workspace_mode = ""
    else:
        project.rmdir()

    result = github._list_issues(ctx)

    assert "GH_TARGET_" in result
    assert gh_calls == []


def test_fileless_room_accepts_explicit_repo(tmp_path, gh_calls):
    ctx, _ = _context(tmp_path, "room")
    ctx.task_metadata = {}
    assert not github._get_issue(ctx, 7, repo="owner/selected").startswith("⚠️")
    assert gh_calls[0][0][-2:] == ["--repo", "owner/selected"]


def test_generic_hub_transport_keeps_explicit_api_contract(tmp_path, gh_calls, monkeypatch):
    ctx, _ = _context(tmp_path, "room")
    ctx.task_metadata = {"_project_room_note": "registry unavailable"}
    monkeypatch.setenv("GH_REPO", "configured/hub")
    github._gh_cmd(["api", "/repos/owner/hub/contents/catalog.json"], ctx)
    assert gh_calls[0][0] == ["gh", "api", "/repos/owner/hub/contents/catalog.json"]
    assert gh_calls[0][1]["cwd"] == str(ctx.repo_dir)
    assert gh_calls[0][1]["env"]["GH_REPO"] == "configured/hub"


@pytest.mark.parametrize("kind", ["system", "queued", "room", "fileless", "broken"])
@pytest.mark.parametrize("name,args,_count", _CALLS)
def test_public_null_target_never_selects_internal_hub_transport(tmp_path, gh_calls, monkeypatch, kind, name, args, _count):
    from ouroboros.tools.registry import ToolRegistry

    ctx, _ = _context(tmp_path, "room" if kind in {"fileless", "broken"} else kind)
    if kind == "fileless":
        ctx.task_metadata = {}
    elif kind == "broken":
        ctx.task_metadata = {"_project_room_note": "registry unavailable"}
    monkeypatch.setenv("GH_REPO", "unrelated/wrong-repo")
    monkeypatch.setenv("OUROBOROS_SAFETY_MODE", "off")
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    result = registry.execute_result(name, {**args, "repo": None})
    assert "GH_TARGET_INVALID" in result.text
    assert (result.status, result.code) == ("error", "TOOL_ARG_ERROR")
    assert not gh_calls


@pytest.mark.parametrize("name,args,_count", _CALLS)
def test_required_target_is_an_error_then_a_valid_call_has_no_stale_failure(tmp_path, gh_calls, monkeypatch, name, args, _count):
    from ouroboros.loop_tool_execution import _typed_execution_failure
    from ouroboros.tools.registry import ToolRegistry

    ctx, _ = _context(tmp_path, "room")
    ctx.task_metadata = {}
    monkeypatch.setenv("OUROBOROS_SAFETY_MODE", "off")
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    refused = registry.execute_result(name, args)
    assert "GH_TARGET_REQUIRED" in refused.text
    assert (refused.status, refused.code) == ("error", "TOOL_ARG_ERROR")
    assert _typed_execution_failure(True, refused)
    assert not gh_calls
    valid = registry.execute_result(name, {**args, "repo": "owner/selected"})
    assert valid.status == "ok" and not _typed_execution_failure(True, valid)
    assert len(gh_calls) == _count


@pytest.mark.parametrize("repo", [False, 0, [], {}, 1])
def test_invalid_public_target_values_cannot_be_generic_transport(tmp_path, gh_calls, repo):
    ctx, _ = _context(tmp_path, "queued")
    assert "GH_TARGET_INVALID" in github._get_issue(ctx, 7, repo=repo)
    assert not gh_calls


def test_room_registry_failure_is_visible(tmp_path, monkeypatch):
    from ouroboros import projects_registry
    from ouroboros.workspace_admission import room_chat_lens_dir

    def fail(*args, **kwargs):
        raise OSError("fixture registry failure")

    monkeypatch.setattr(projects_registry, "get_reserved_project", fail)
    directory, note = room_chat_lens_dir(tmp_path, "project-fixture")
    assert directory == ""
    assert "registry entry is unreadable" in note
    assert "OSError" in note


@pytest.mark.parametrize("token", ["GITHUB_TOKEN", "GH_TOKEN", "settings"])
def test_cli_discovery_accepts_the_execution_token_sources(tmp_path, monkeypatch, token):
    from ouroboros import config
    from ouroboros.tools.registry_guards import _builtin_tool_availability

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(config, "load_settings", lambda: {"GITHUB_TOKEN": "fixture"} if token == "settings" else {})
    if token != "settings":
        monkeypatch.setenv(token, "fixture")
    ctx, _ = _context(tmp_path, "queued")
    assert _builtin_tool_availability("get_github_issue", ctx)[0] is True


def test_cli_store_metadata_enables_only_cli_tools_without_probing(tmp_path, monkeypatch):
    from ouroboros.tools.registry_guards import _builtin_tool_availability

    monkeypatch.setattr(github, "github_token_from_env_or_settings", lambda: "")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    (tmp_path / "gh").mkdir()
    (tmp_path / "gh" / "hosts.yml").write_text("github.com:\n  user: fixture\n", encoding="utf-8")

    def no_probe(*args, **kwargs):
        raise AssertionError("discovery must not spawn an authentication probe")

    monkeypatch.setattr(subprocess, "run", no_probe)
    ctx, _ = _context(tmp_path, "queued")
    for name, _, _ in _CALLS:
        assert _builtin_tool_availability(name, ctx)[0] is True
    for name in ("submit_skill_to_hub", "generate_evolution_stats"):
        assert _builtin_tool_availability(name, ctx) == (False, "missing_credential", "GITHUB_TOKEN")


@pytest.mark.parametrize("bound,explicit", [(False, False), (False, True), (True, True)])
def test_presence_repository_selection_keeps_host_argument_authority(tmp_path, gh_calls, bound, explicit):
    from ouroboros.presence_authority import PresenceCapabilityCeiling, PresenceToolGrant, presence_ceiling_payload
    from ouroboros.presence_capabilities import PresenceArgumentBinding
    from ouroboros.tools.registry import ToolRegistry

    bindings = (PresenceArgumentBinding(("repo",), "static", static_value="owner/allowed"),) if bound else ()
    ceiling = PresenceCapabilityCeiling(
        skill_name="fixture", skill_content_hash="a" * 64, profile_fingerprint="b" * 64,
        state_fingerprint="c" * 64, selection_fingerprint="d" * 64, model_slot="main",
        inline_max_rounds=10, tool_grants=(PresenceToolGrant("get_github_issue", bindings),),
        resource_grants=(), digest="e" * 64,
    )
    ctx, _ = _context(tmp_path, "system")
    ctx.task_contract = {"capability_ceiling": presence_ceiling_payload(ceiling)}
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    args = {"number": 7, **({"repo": "owner/unselected"} if explicit else {})}

    result = registry.execute("get_github_issue", args)

    if explicit and not bound:
        assert "PRESENCE_ARGUMENT_BINDING_BLOCKED" in result
        assert not gh_calls
    else:
        assert "Issue #7" in result
        assert len(gh_calls) == 1
        if bound:
            assert gh_calls[0][0][-2:] == ["--repo", "owner/allowed"]
        else:
            assert "--repo" not in gh_calls[0][0]


def test_child_does_not_gain_github_access_from_explicit_repo(tmp_path, gh_calls):
    from ouroboros.tools.registry import ToolRegistry

    ctx, _ = _context(tmp_path, "queued")
    ctx.task_metadata = {"delegation_role": "subagent"}
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    result = registry.execute("get_github_issue", {"number": 7, "repo": "owner/unselected"})
    assert "BLOCKED" in result
    assert not gh_calls


def _answer(payload):
    return github.GhResult(True, json.dumps(payload), 0, None, "")


_FORBIDDEN = github.GhResult(False, "⚠️ GH_ERROR: gh: Resource not accessible by personal access token (HTTP 403)",
                             1, 403, "exit")


class FakeChecks:
    """GitHub's answers about one commit; every gh call is recorded with its transport and timeout."""

    def __init__(self, polls=((),), jobs=None, annotations=None, rollup=(), heads=(SHA,), failing=None, fork=False):
        self.polls, self.heads = [list(poll) for poll in polls], list(heads)
        self.jobs, self.annotations, self.rollup = jobs or {}, annotations or {}, list(rollup)
        self.failing, self.fork, self.calls = failing or {}, fork, []

    def __call__(self, args, ctx, timeout=30, input_data=None, *, repo=github._GENERIC_TRANSPORT):
        self.calls.append((list(args), repo, timeout))
        source = {"pr": "rollup" if "statusCheckRollup" in args[-1] else "pr", "run": "runs" if args[1] == "list" else "jobs",
                  "api": "annotations"}[args[0]]
        failure = self.failing.get(source)
        if isinstance(failure, list):  # One entry per call of that source; None lets the call through.
            failure = failure.pop(0) if failure else None
        if failure:
            return failure
        if args[0] == "pr":
            head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
            pr = {"headRefOid": head, "url": f"{BASE}/pull/7", "isCrossRepository": self.fork}
            return _answer({**pr, "statusCheckRollup": self.rollup} if source == "rollup" else pr)
        if source == "runs":
            return _answer(self.polls.pop(0) if len(self.polls) > 1 else self.polls[0])
        if source == "jobs":
            return _answer({"jobs": self.jobs.get(int(args[2]), [])})
        return _answer(self.annotations.get(int(args[1].split("/")[4]), []))

    def sent(self, kind):
        return [args for args, _repo, _timeout in self.calls if args[0] == kind]


@pytest.fixture
def checks(tmp_path, monkeypatch):
    """Call the registered ``get_github_checks`` over a fake gh, a fixed observation time and a fake clock."""
    from ouroboros.tools.registry import ToolRegistry

    clock = SimpleNamespace(now=0.0, slept=[], request_cost=0.0)
    monkeypatch.setattr(github, "github_token_from_env_or_settings", lambda: "fixture-token")
    monkeypatch.setattr(github_checks, "utc_now_iso", lambda: "2026-10-02T12:00:00+00:00")
    monkeypatch.setattr(github_checks, "time", SimpleNamespace(
        monotonic=lambda: clock.now,
        sleep=lambda seconds: (clock.slept.append(seconds), setattr(clock, "now", clock.now + seconds))))
    ctx, _ = _context(tmp_path, "queued")
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)

    def call(fake, **args):
        def transport(*a, **kw):
            clock.now += clock.request_cost
            return fake(*a, **kw)

        monkeypatch.setattr(github_checks, "_gh_run", transport)
        result = registry.execute_result("get_github_checks", args)
        assert not any(word in result.text.lower() for word in ("passed", "failing")), result.text
        return result

    call.clock = clock
    return call


_RUNS = f"{BASE}/actions/runs"
_HEADER = [f"GitHub checks for commit {SHA}", "Repository: github.example/owner/selected"]
_PR_LINE = f"Pull request: #7 {BASE}/pull/7"
_SHA_TAIL = ["", "Other checks: not read — a commit SHA target reads GitHub Actions workflow runs only; "
             "third-party checks and commit statuses are read for a pull request number."]
_ACTIONS_ROLLUP = [{"__typename": "CheckRun", "name": "quick-test", "workflowName": "CI", "status": "COMPLETED",
                    "conclusion": "SUCCESS", "detailsUrl": f"{_RUNS}/11/job/54"}]
_RUNNING = {"status": "in_progress", "conclusion": ""}


def _failed_world(**kw):
    """One failed workflow run (a failed job with a failed step and published annotations) beside a skipped one."""
    return FakeChecks(
        polls=[[_run(12, "Mirror", conclusion="skipped", event="pull_request_target"), _run(11, conclusion="failure")]],
        jobs={11: [_job(54, "quick-test"),
                   _job(55, "full-test (windows-latest)", conclusion="failure", steps=[
                       (9, "Install", "completed", "success"), (10, "Run tests (parallel)", "completed", "failure"),
                       (11, "Run tests (serial)", "completed", "success")]),
                   _job(56, "build", conclusion="skipped")],
              12: [_job(60, "redirect", conclusion="skipped", run_id=12)]},
        annotations={55: [
            {"annotation_level": "warning", "title": "", "message": "Node.js 20 is deprecated."},
            {"annotation_level": "failure", "title": "Failed test", "message": "tests/test_x.py::test_y (call, AssertionError)"},
            {"annotation_level": "failure", "title": "", "message": "Process completed with exit code 1."}]}, **kw)


_FAILED_RUN = [
    "", "Runs not completed with success (2):",
    f"- CI (pull_request) run 11 attempt 1: failure {_RUNS}/11",
    "    log of the failed steps: gh run view 11 --log-failed --repo github.example/owner/selected",
    "    jobs: 3 — success 1, failure 1, skipped 1",
    f"    - job full-test (windows-latest) [55]: failure {_RUNS}/11/job/55",
    "        step 10 Run tests (parallel): failure"]
_SKIPPED_RUN = [f"- Mirror (pull_request_target) run 12 attempt 1: skipped {_RUNS}/12", "    jobs: 1 — skipped 1"]


def test_checks_report_states_run_facts_and_no_verdict(checks):
    # A run GitHub calls success can hold skipped jobs: the rollup's job counts state them at no extra call.
    rollup = _ACTIONS_ROLLUP + [{**_ACTIONS_ROLLUP[0], "name": name, "conclusion": "SKIPPED"}
                                for name in ("full-test (ubuntu-latest)", "full-test (windows-latest)", "build")]
    fake = FakeChecks(polls=[[_run(11), _run(12, "UI browser")]], rollup=rollup, fork=True)
    result = checks(fake, number=7)
    assert (result.status, result.code) == ("ok", "OK")
    assert result.text.splitlines() == [
        *_HEADER, _PR_LINE + " (head in a fork); head as read by this call",
        "Observed: 2026-10-02T12:00:00+00:00",
        "Sources read: workflow runs; jobs (runs: 0); annotations (jobs: 0); pull request check rollup",
        "Sources unavailable: none",
        "Workflow runs: 2 — success 2",
        "Pull request rollup, GitHub Actions jobs: 4 — success 1, skipped 3",
        "", "Runs completed with success (2):",
        f"- CI (pull_request) run 11 attempt 1: success {_RUNS}/11",
        f"- UI browser (pull_request) run 12 attempt 1: success {_RUNS}/12",
        "", "Other checks: none — every entry of the pull request rollup (4) is a job of a GitHub Actions workflow."]
    # One pull request read fixes the head and carries the rollup; a run completed with success costs no jobs read.
    assert [args[:2] for args, _repo, _timeout in fake.calls] == [["pr", "view"], ["run", "list"]]
    assert fake.calls[1][0][2:4] == ["--commit", SHA] and all(repo == "" for _args, repo, _timeout in fake.calls)


def test_checks_report_names_the_failed_job_its_step_and_annotations(checks):
    fake = _failed_world()
    result = checks(fake, sha=SHA.upper(), repo="github.example/owner/selected")
    assert (result.status, result.code) == ("ok", "OK")
    assert result.text.splitlines() == [
        *_HEADER, "Observed: 2026-10-02T12:00:00+00:00",
        "Sources read: workflow runs; jobs (runs: 2); annotations (jobs: 1)",
        "Sources unavailable: none",
        "Workflow runs: 2 — failure 1, skipped 1",  # Skipped is its own state, never added to success.
        *_FAILED_RUN,
        "        annotation failure [Failed test]: tests/test_x.py::test_y (call, AssertionError)",
        "        annotation failure: Process completed with exit code 1.",
        "        annotations: 2 at failure level, 2 shown; 3 of all levels read",
        *_SKIPPED_RUN, *_SHA_TAIL]
    # Every repository command carries the explicit target; the annotation read is the one literal
    # API path, sent on the generic transport with the host GitHub returned.
    assert [(args[0], repo) for args, repo, _timeout in fake.calls] == [
        ("run", "github.example/owner/selected"), ("run", "github.example/owner/selected"),
        ("api", github._GENERIC_TRANSPORT), ("run", "github.example/owner/selected")]
    assert fake.sent("api") == [["api", "repos/owner/selected/check-runs/55/annotations?per_page=100",
                                 "--hostname", "github.example"]]


def test_checks_report_keeps_unfinished_and_unregistered_apart_from_results(checks):
    running = FakeChecks(polls=[[_run(11, **_RUNNING)]], jobs={11: [
        _job(54, "quick-test"),
        _job(55, "full-test (macos-latest)", steps=[(10, "Run tests", "in_progress", ""), (11, "Serial", "pending", "")], **_RUNNING),
        _job(56, "ui-smoke", status="queued", conclusion="")]})
    assert checks(running, sha=SHA).text.splitlines()[2:] == [
        "Observed: 2026-10-02T12:00:00+00:00",
        "Sources read: workflow runs; jobs (runs: 1); annotations (jobs: 0)",
        "Sources unavailable: none",
        "Workflow runs: 1 — in_progress 1",
        "", "Runs not completed with success (1):",
        f"- CI (pull_request) run 11 attempt 1: in_progress {_RUNS}/11",
        "    jobs: 3 — success 1, queued 1, in_progress 1",
        f"    - job full-test (macos-latest) [55]: in_progress {_RUNS}/11/job/55",
        "        step 10 Run tests: in_progress",
        f"    - job ui-smoke [56]: queued {_RUNS}/11/job/56",
        *_SHA_TAIL]
    assert running.sent("api") == []  # An unfinished job has no annotations to read yet.

    nothing = checks(FakeChecks(), sha=SHA)
    assert nothing.status == "ok" and nothing.text.splitlines() == [
        f"GitHub checks for commit {SHA}", "Repository: resolved by the GitHub CLI from the Project directory",
        "Observed: 2026-10-02T12:00:00+00:00",
        "Sources read: workflow runs; jobs (runs: 0); annotations (jobs: 0)",
        "Sources unavailable: none",
        "Workflow runs: 0 — no workflow run is registered for this commit — this is not a test result",
        "A commit SHA the repository does not hold and a commit with no run yet read the same; the repository is the one "
        "the GitHub CLI resolves from the Project directory (pass repo='[HOST/]OWNER/REPO' to name it).",
        *_SHA_TAIL]
    named = checks(FakeChecks(), sha=SHA, repo="owner/selected").text.splitlines()
    assert named[1] == "Repository: owner/selected"
    assert named[6] == "A commit SHA the repository does not hold and a commit with no run yet read the same."
    # A pull request head is a commit GitHub holds, and a commit with a run is known: neither carries the line.
    assert "read the same" not in checks(FakeChecks(rollup=_ACTIONS_ROLLUP), number=7).text
    assert "read the same" not in checks(FakeChecks(polls=[[_run(11)]]), sha=SHA).text

    # A run that failed before it created a job is still a run with its own state.
    jobless = checks(FakeChecks(polls=[[_run(11, conclusion="startup_failure")]]), sha=SHA).text.splitlines()
    assert jobless[5:] == [
        "Workflow runs: 1 — startup_failure 1",
        "", "Runs not completed with success (1):",
        f"- CI (pull_request) run 11 attempt 1: startup_failure {_RUNS}/11",
        "    log of the failed steps: gh run view 11 --log-failed --repo github.example/owner/selected",
        "    jobs: 0 — GitHub lists no job for this run",
        *_SHA_TAIL]


def test_checks_report_keeps_cancelled_and_skipped_under_their_own_names(checks):
    fake = FakeChecks(polls=[[_run(11, conclusion="cancelled"), _run(12, "Mirror", conclusion="skipped")]], jobs={
        11: [_job(54, "quick-test", conclusion="cancelled", steps=[(3, "Run", "completed", "cancelled")]),
             _job(55, "build", conclusion="skipped")],
        12: [_job(60, "redirect", conclusion="skipped", run_id=12)]})
    lines = checks(fake, sha=SHA).text.splitlines()
    assert lines[5:] == [
        "Workflow runs: 2 — cancelled 1, skipped 1",
        "", "Runs not completed with success (2):",
        f"- CI (pull_request) run 11 attempt 1: cancelled {_RUNS}/11",
        "    jobs: 2 — cancelled 1, skipped 1",
        f"    - job quick-test [54]: cancelled {_RUNS}/11/job/54",
        f"- Mirror (pull_request) run 12 attempt 1: skipped {_RUNS}/12",
        "    jobs: 1 — skipped 1",
        *_SHA_TAIL]
    assert "Runs completed with success" not in "\n".join(lines) and fake.sent("api") == []


def test_checks_report_lists_third_party_checks_and_statuses_of_a_pull_request(checks):
    rollup = _ACTIONS_ROLLUP + [
        {"__typename": "CheckRun", "name": "Vercel Agent Review", "workflowName": "", "status": "COMPLETED",
         "conclusion": "NEUTRAL", "detailsUrl": "https://vercel.example/github"},
        {"__typename": "StatusContext", "context": "deploy/netlify", "state": "PENDING", "targetUrl": "https://netlify.example/7"}]
    lines = checks(FakeChecks(polls=[[_run(11)]], rollup=rollup), number=7).text.splitlines()
    assert lines[-4:] == [
        "", "Other checks, outside GitHub Actions (2) — neutral 1, pending 1:",
        "- check run Vercel Agent Review: neutral https://vercel.example/github",
        "- commit status deploy/netlify: pending https://netlify.example/7"]
    assert "Workflow runs: 1 — success 1" in lines  # Other checks never enter the workflow-run counts.
    # A bare commit names the gap instead of reporting an empty list.
    assert checks(FakeChecks(polls=[[_run(11)]], rollup=rollup), sha=SHA).text.splitlines()[-2:] == _SHA_TAIL


def test_an_unavailable_source_is_named_while_the_others_are_reported(checks):
    refused = "HTTP 403: gh: Resource not accessible by personal access token (HTTP 403)"
    fake = FakeChecks(polls=[[_run(11)]], rollup=_ACTIONS_ROLLUP, failing={"rollup": _FORBIDDEN},
                      jobs={11: [_job(54, "quick-test"), _job(56, "build", conclusion="skipped")]})
    result = checks(fake, number=7)
    lines = result.text.splitlines()
    assert result.status == "ok" and f"Sources unavailable: pull request check rollup ({refused})" in lines
    # Without the rollup's job counts the run GitHub calls success is read for its own.
    assert "Sources read: workflow runs; jobs (runs: 1); annotations (jobs: 0)" in lines
    assert lines[-6:] == ["", "Runs completed with success (1):", f"- CI (pull_request) run 11 attempt 1: success {_RUNS}/11",
                          "    jobs: 2 — success 1, skipped 1",
                          "", "Other checks: not read — the pull request check rollup is unavailable."]
    # The head is read again without the refused field: the rollup is the only loss.
    assert [args[-1] for args in fake.sent("pr")] == ["headRefOid,url,isCrossRepository,statusCheckRollup",
                                                       "headRefOid,url,isCrossRepository"]

    result = checks(_failed_world(failing={"annotations": _FORBIDDEN}), sha=SHA)
    assert result.status == "ok" and result.text.splitlines()[3:] == [
        "Sources read: workflow runs; jobs (runs: 2); annotations (jobs: 0)",
        f"Sources unavailable: annotations (jobs: 1; {refused})",
        "Workflow runs: 2 — failure 1, skipped 1",
        *_FAILED_RUN, f"        annotations: unavailable ({refused})", *_SKIPPED_RUN, *_SHA_TAIL]

    slow = github.GhResult(False, "⚠️ GH_TIMEOUT: exceeded 30s.", None, None, "timeout")
    result = checks(_failed_world(failing={"jobs": slow}), sha=SHA)
    assert result.status == "ok" and result.text.splitlines()[3:] == [
        "Sources read: workflow runs; jobs (runs: 0); annotations (jobs: 0)",
        "Sources unavailable: jobs (runs: 2; timeout)",
        "Workflow runs: 2 — failure 1, skipped 1",
        "", "Runs not completed with success (2):",
        f"- CI (pull_request) run 11 attempt 1: failure {_RUNS}/11",
        "    log of the failed steps: gh run view 11 --log-failed --repo github.example/owner/selected",
        "    jobs: unavailable (timeout)",
        _SKIPPED_RUN[0], "    jobs: unavailable (timeout)", *_SHA_TAIL]


def test_only_a_failing_runs_source_is_a_typed_error(checks):
    gone = github.GhResult(False, "⚠️ GH_ERROR: HTTP 404: Not Found (https://api.github.example/repos/owner/selected/actions/runs)",
                           1, 404, "exit")
    result = checks(FakeChecks(polls=[[_run(11)]], failing={"runs": gone}), sha=SHA)
    assert result.status == "error" and result.text == gone.text
    result = checks(FakeChecks(polls=[[_run(11)]], failing={"runs": github.GhResult(True, "not json", 0, None, "")}), sha=SHA)
    assert (result.status, result.code) == ("error", "TOOL_ERROR") and "workflow runs JSON" in result.text
    assert "operation_outcome" not in result.meta  # After a gh launch a refusal attests nothing about effects.
    # A pull request whose head cannot be read leaves no commit to report on.
    fake = FakeChecks(polls=[[_run(11)]], failing={"rollup": gone, "pr": gone})
    result = checks(fake, number=7)
    assert result.status == "error" and result.text == gone.text and fake.sent("run") == []
    # The same refusal on a secondary source is a named gap, not an error.
    assert checks(_failed_world(failing={"jobs": gone}), sha=SHA).status == "ok"


def test_wait_ends_when_every_run_completes_or_at_its_cap(checks):
    polls = [[_run(11, status="queued", conclusion="")], [_run(11, **_RUNNING)], [_run(11)]]
    fake = FakeChecks(polls=polls, rollup=_ACTIONS_ROLLUP)
    waited = checks(fake, number=7, wait_seconds=120).text.splitlines()
    assert checks.clock.slept == [15, 15] and len(fake.sent("run")) == 3
    assert waited[2:4] == [_PR_LINE + "; head unchanged after the wait",
                           "Observed: 2026-10-02T12:00:00+00:00; waited 30s of 120s for the runs to complete"]
    # A waited call reports what a later call without a wait reports.
    checks.clock.slept.clear()
    later = checks(FakeChecks(polls=polls[-1:], rollup=_ACTIONS_ROLLUP), number=7).text.splitlines()
    assert checks.clock.slept == [] and waited[4:] == later[4:]

    # The requested wait is capped; an unfinished run is reported as unfinished when the time is up.
    checks.clock.now, fake = 0.0, FakeChecks(polls=[[_run(11, **_RUNNING)]], jobs={11: [_job(55, "full-test", **_RUNNING)]})
    lines = checks(fake, sha=SHA, wait_seconds=999).text.splitlines()
    assert sum(checks.clock.slept) == 240 and max(checks.clock.slept) == 15
    assert "Observed: 2026-10-02T12:00:00+00:00; waited 240s of 240s for the runs to complete" in lines
    assert "Workflow runs: 1 — in_progress 1" in lines and f"    - job full-test [55]: in_progress {_RUNS}/11/job/55" in lines

    # A commit with no registered run keeps the wait: a run that registers late is still waited for.
    checks.clock.now, checks.clock.slept[:] = 0.0, []
    lines = checks(FakeChecks(polls=[[], [_run(11, **_RUNNING)], [_run(11)]]), sha=SHA, wait_seconds=60).text.splitlines()
    assert checks.clock.slept == [15, 15] and "Workflow runs: 1 — success 1" in lines

    # The last sleep is cut to what is left of the wait, and slow requests never report more waiting than the wait.
    checks.clock.now, checks.clock.slept[:] = 0.0, []
    assert "; waited 20s of 20s for" in checks(FakeChecks(polls=[[_run(11, **_RUNNING)]]), sha=SHA, wait_seconds=20).text
    assert checks.clock.slept == [15, 5]
    checks.clock.now, checks.clock.slept[:], checks.clock.request_cost = 0.0, [], 4.0
    text = checks(FakeChecks(polls=[[_run(11, **_RUNNING)]]), sha=SHA, wait_seconds=20).text
    assert "; waited 20s of 20s for" in text and checks.clock.slept == [15] and checks.clock.now > 20

    # A run-list read that fails during the wait ends the call with that error.
    checks.clock.now, checks.clock.request_cost = 0.0, 0.0
    slow = github.GhResult(False, "⚠️ GH_TIMEOUT: exceeded 30s.", None, None, "timeout")
    fake = FakeChecks(polls=[[_run(11, **_RUNNING)]], failing={"runs": [None, slow]})
    result = checks(fake, sha=SHA, wait_seconds=60)
    assert result.status == "timeout" and result.text == slow.text and len(fake.sent("run")) == 2


def test_one_deadline_bounds_every_request(checks):
    fake = _failed_world()
    checks.clock.request_cost = 40.0  # Each request spends 40 s of the 90 s a call without a wait has.
    lines = checks(fake, sha=SHA).text.splitlines()
    # The runs and the first jobs read get the per-request ceiling, the annotation read gets what is left,
    # and the jobs read of the second run is never sent.
    assert [(args[0], timeout) for args, _repo, timeout in fake.calls] == [("run", 30), ("run", 30), ("api", 10)]
    assert "Sources unavailable: jobs (runs: 1; deadline)" in lines and lines[-4:-2] == [
        _SKIPPED_RUN[0], "    jobs: unavailable (deadline)"]
    assert lines[:2] == _HEADER and "Workflow runs: 2 — failure 1, skipped 1" in lines
    # With time left every request is sent under the per-request ceiling.
    checks.clock.now, checks.clock.request_cost, fake = 0.0, 0.0, _failed_world()
    assert "deadline" not in checks(fake, sha=SHA).text
    assert [timeout for _args, _repo, timeout in fake.calls] == [30, 30, 30, 30] and len(fake.sent("api")) == 1


def test_a_head_that_moves_during_the_wait_is_named(checks):
    fake = FakeChecks(polls=[[_run(11, **_RUNNING)], [_run(11, conclusion="cancelled")]], heads=[SHA, MOVED],
                      rollup=_ACTIONS_ROLLUP, jobs={11: [_job(55, "full-test", conclusion="cancelled")]})
    lines = checks(fake, number=7, wait_seconds=60).text.splitlines()
    assert lines[:3] == [*_HEADER, f"{_PR_LINE}; head moved to {MOVED} during the wait; this report is for {SHA}"]
    assert "Sources unavailable: pull request check rollup (GitHub's rollup describes the moved head)" in lines
    assert lines[-1] == "Other checks: not read — the pull request check rollup is unavailable."
    assert all(args[3] == SHA for args in fake.sent("run") if args[1] == "list")  # The SHA is fixed at the start.
    # A head that stays put keeps its rollup.
    checks.clock.now = 0.0
    steady = FakeChecks(polls=[[_run(11, **_RUNNING)], [_run(11)]], rollup=_ACTIONS_ROLLUP)
    lines = checks(steady, number=7, wait_seconds=60).text.splitlines()
    assert lines[2] == _PR_LINE + "; head unchanged after the wait" and lines[-1].startswith("Other checks: none")
    # A head that cannot be read after the wait is named as not read, never as unchanged.
    checks.clock.now = 0.0
    refused = "HTTP 403: gh: Resource not accessible by personal access token (HTTP 403)"
    blind = FakeChecks(polls=[[_run(11, **_RUNNING)], [_run(11)]], rollup=_ACTIONS_ROLLUP,
                       failing={"rollup": _FORBIDDEN, "pr": [None, _FORBIDDEN]})
    lines = checks(blind, number=7, wait_seconds=60).text.splitlines()
    assert lines[2] == _PR_LINE + "; head after the wait not read" and "unchanged" not in "\n".join(lines)
    assert (f"Sources unavailable: pull request check rollup ({refused}); "
            f"pull request head after the wait ({refused})") in lines
    assert lines[-1] == "Other checks: not read — the pull request check rollup is unavailable."


@pytest.mark.parametrize("args", [{"sha": "main"}, {"sha": SHA[:12]}, {"sha": SHA, "number": 7}, {}, {"number": -1}])
def test_checks_target_is_one_pull_request_or_one_full_sha(checks, args):
    fake = FakeChecks(polls=[[_run(11)]])
    result = checks(fake, **args)
    assert (result.status, result.code) == ("error", "TOOL_ARG_ERROR") and fake.calls == []
    assert result.meta.get("operation_outcome") == "completed_no_effect"  # Refused before any gh launch.
    assert ("git rev-parse" in result.text) == ("sha" in args and "number" not in args)
    assert checks(fake, sha=SHA).status == "ok" and checks(fake, number=7).status == "ok"


def test_bounds_keep_the_header_and_every_run_id(checks):
    from ouroboros.tool_capabilities import tool_result_limit

    steps = [(1, "Run tests " + "y" * 80, "completed", "failure")]
    fake = FakeChecks(
        polls=[[_run(100 + index, f"Workflow {index}", conclusion="failure") for index in range(40)]],
        jobs={100 + index: [_job(1000 * (index + 1) + job, f"job {job} " + "x" * 60, conclusion="failure",
                                 run_id=100 + index, steps=steps) for job in range(300)] for index in range(40)})
    text = checks(fake, sha=SHA).text
    lines = text.splitlines()
    assert len(text) <= tool_result_limit("get_github_checks")
    assert lines[:2] == _HEADER and "Workflow runs: 40 — failure 40" in lines and lines[-2:] == _SHA_TAIL
    assert all(str(100 + index) in text for index in range(40))
    assert "- 20 more runs — failure 20; ids: " + ", ".join(str(120 + index) for index in range(20)) in lines
    assert ("Sources read: workflow runs; jobs (runs: 8; jobs not read for 32 runs: one call reads the jobs of 8 runs); "
            "annotations (jobs: 10)") in lines
    assert "    290 more jobs not shown: failure 290" in lines
    assert "    jobs: not read (one call expands 8 runs)" in lines
    assert any(line.endswith("more detail lines of this run are not shown: the result bound is reached") for line in lines)
    assert len(fake.sent("run")) == 1 + 8 and len(fake.sent("api")) == 10
    assert "        annotations: not read (one call reads the annotations of 10 jobs)" in lines
    # A small report carries none of the bound notices.
    small = checks(_failed_world(), sha=SHA).text
    assert not any(notice in small for notice in ("more runs", "more jobs", "not shown", "jobs: not read", "jobs not read",
                                                  "annotations: not read", "may hold more"))


def test_a_run_completed_with_success_states_its_job_counts(checks):
    """GitHub calls a run success while its jobs are skipped or one of them failed: the counts say so."""
    jobs = [_job(54, "quick-test"), _job(55, "full-test (ubuntu-latest)", conclusion="skipped"),
            _job(56, "build", conclusion="skipped"),
            _job(57, "lint", conclusion="failure", steps=[(2, "Run", "completed", "failure")])]
    fake = FakeChecks(polls=[[_run(11), _run(12, "Docs")]], jobs={11: jobs, 12: [_job(60, "build", run_id=12)]})
    assert checks(fake, sha=SHA).text.splitlines()[3:] == [
        "Sources read: workflow runs; jobs (runs: 2); annotations (jobs: 0)",
        "Sources unavailable: none",
        "Workflow runs: 2 — success 2",
        "", "Runs completed with success (2):",
        f"- CI (pull_request) run 11 attempt 1: success {_RUNS}/11",
        "    log of the failed steps: gh run view 11 --log-failed --repo github.example/owner/selected",
        "    jobs: 4 — success 1, failure 1, skipped 2",
        f"- Docs (pull_request) run 12 attempt 1: success {_RUNS}/12",
        "    jobs: 1 — success 1",
        *_SHA_TAIL]
    assert fake.sent("api") == []  # Only the counts line: no job list and no annotation read.

    # One call reads the jobs of eight runs; what is left is stated in the header and on each run.
    ten = {20 + index: [_job(1, "test", run_id=20 + index)] for index in range(10)}
    fake = FakeChecks(polls=[[_run(run_id) for run_id in ten]], jobs=ten)
    lines = checks(fake, sha=SHA).text.splitlines()
    assert ("Sources read: workflow runs; jobs (runs: 8; jobs not read for 2 runs: one call reads the jobs of 8 runs); "
            "annotations (jobs: 0)") in lines
    assert lines.count("    jobs: 1 — success 1") == 8 and lines.count("    jobs: not read (one call expands 8 runs)") == 2
    assert len(fake.sent("run")) == 1 + 8
    eight = checks(FakeChecks(polls=[[_run(run_id) for run_id in list(ten)[:8]]], jobs=ten), sha=SHA).text
    assert "jobs not read" not in eight and "jobs: not read" not in eight and eight.count("    jobs: 1 — success 1") == 8


def test_rollup_facts_reach_the_header_and_a_job_no_listed_run_explains_is_shown(checks):
    failed_job = {"__typename": "CheckRun", "name": "full-test", "workflowName": "CI", "status": "COMPLETED",
                  "conclusion": "FAILURE", "detailsUrl": f"{_RUNS}/99/job/5"}
    vercel = {"__typename": "CheckRun", "name": "Vercel", "workflowName": "", "status": "COMPLETED",
              "conclusion": "FAILURE", "detailsUrl": "https://vercel.example/github"}
    stray = "in a run absent from the run list or listed as success"
    lines = checks(FakeChecks(polls=[[_run(11)]], rollup=_ACTIONS_ROLLUP + [failed_job, vercel]), number=7).text.splitlines()
    # The header carries the failure of a third-party check and of a job whose run is not listed.
    assert lines[6:lines.index("")] == [
        "Workflow runs: 1 — success 1",
        f"Pull request rollup, GitHub Actions jobs: 2 — success 1, failure 1; 1 of them {stray}",
        "Other checks, outside GitHub Actions: 1 — failure 1"]
    assert lines[-4:] == [
        f"GitHub Actions jobs of the rollup {stray} (1) — failure 1:",
        f"- job CI / full-test: failure {_RUNS}/99/job/5",
        "Other checks, outside GitHub Actions (1) — failure 1:",
        "- check run Vercel: failure https://vercel.example/github"]
    # A failed job of a run GitHub lists as success is shown the same way ...
    inside = {**failed_job, "detailsUrl": f"{_RUNS}/11/job/5"}
    lines = checks(FakeChecks(polls=[[_run(11)]], rollup=[inside]), number=7).text.splitlines()
    assert lines[-3:] == [f"GitHub Actions jobs of the rollup {stray} (1) — failure 1:",
                          f"- job CI / full-test: failure {_RUNS}/11/job/5",
                          "Other checks: none — every entry of the pull request rollup (1) is a job of a GitHub Actions workflow."]
    # ... while a run listed as not success accounts for its job, and no other check adds no header line.
    text = checks(FakeChecks(polls=[[_run(11, conclusion="failure")]], rollup=[inside]), number=7).text
    assert "Pull request rollup, GitHub Actions jobs: 1 — failure 1\n" in text
    assert stray not in text and "Other checks, outside GitHub Actions" not in text


def test_failure_class_runs_are_listed_and_expanded_first(checks):
    order = [_run(1, conclusion="skipped"), _run(2, **_RUNNING), _run(3, conclusion="cancelled"), _run(4),
             _run(5, conclusion="timed_out"), _run(6, conclusion="failure")]
    spines = [line for line in checks(FakeChecks(polls=[order]), sha=SHA).text.splitlines() if line.startswith("- CI ")]
    assert [line.split()[4] for line in spines] == ["6", "5", "3", "2", "1", "4"]

    # Twenty cancelled runs that GitHub lists first leave the five failed ones their spine, log hint and jobs.
    runs = [_run(100 + index, conclusion="cancelled") for index in range(20)] + [
        _run(200 + index, conclusion="failure") for index in range(5)]
    held = [_job(1, "build", conclusion="failure", run_id=100, steps=[(4, "Compile", "completed", "failure")]),
            _job(2, "test", conclusion="cancelled", run_id=100)]
    fake = FakeChecks(polls=[runs], jobs={100: held, 101: [_job(3, "test", conclusion="cancelled", run_id=101)]})
    lines = checks(fake, sha=SHA).text.splitlines()
    spines = [line for line in lines if line.startswith("- CI ")]
    assert [line.split()[4] for line in spines[:6]] == ["200", "201", "202", "203", "204", "100"]
    assert [args[2] for args in fake.sent("run")[1:]] == ["200", "201", "202", "203", "204", "100", "101", "102"]
    assert "- 5 more runs — cancelled 5; ids: 115, 116, 117, 118, 119" in lines
    hint = "    log of the failed steps: gh run view {} --log-failed --repo github.example/owner/selected"
    assert all(lines[lines.index(spine) + 1] == hint.format(spine.split()[4]) for spine in spines[:5])
    # A cancelled run that holds a failed job carries the log command; a cancelled run without one does not.
    at = lines.index(f"- CI (pull_request) run 100 attempt 1: cancelled {_RUNS}/100")
    assert lines[at + 1:at + 5] == [hint.format(100), "    jobs: 2 — failure 1, cancelled 1",
                                    f"    - job build [1]: failure {_RUNS}/100/job/1", "        step 4 Compile: failure"]
    at = lines.index(f"- CI (pull_request) run 101 attempt 1: cancelled {_RUNS}/101")
    assert lines[at + 1] == "    jobs: 1 — cancelled 1" and hint.format(101) not in lines


def test_the_limits_of_the_run_list_are_stated(checks):
    note = "The run list is read up to 100 runs; GitHub may hold more for this commit."
    other_head = _run(999, conclusion="failure", headSha=MOVED)
    fake = FakeChecks(polls=[[_run(run_id) for run_id in range(1, 100)] + [other_head]], rollup=_ACTIONS_ROLLUP)
    text = checks(fake, number=7).text
    # A run of another head is left out of the report, and it still counts as a row GitHub returned.
    assert "Workflow runs: 99 — success 99" in text.splitlines() and f"{_RUNS}/999" not in text and "failure" not in text
    assert note in text.splitlines()[:9] and fake.sent("run")[0][4:6] == ["--limit", "100"]
    text = checks(FakeChecks(polls=[[_run(run_id) for run_id in range(1, 100)]], rollup=_ACTIONS_ROLLUP), number=7).text
    assert "Workflow runs: 99 — success 99" in text and "may hold more" not in text

    # A failed job GitHub returns without a URL leaves no literal annotation path to read.
    fake = FakeChecks(polls=[[_run(11, conclusion="failure")]], jobs={11: [{**_job(55, "test", conclusion="failure"), "url": ""}]})
    lines = checks(fake, sha=SHA).text.splitlines()
    assert lines[lines.index("    - job test [55]: failure") + 1] == "        annotations: not read (GitHub returned no job URL)"
    assert fake.sent("api") == [] and "no job URL" not in checks(_failed_world(), sha=SHA).text


def test_checks_reader_is_registered_read_only():
    from ouroboros import tool_capabilities
    from ouroboros.consciousness_authority import OBSERVE_DISABLED
    from ouroboros.safety import POLICY_SKIP, TOOL_POLICY
    from ouroboros.tools.registry_guards import _GITHUB_TOKEN_TOOLS

    entry = next(item for item in github.get_tools() if item.name == "get_github_checks")
    assert TOOL_POLICY["get_github_checks"] == POLICY_SKIP and "get_github_checks" in _GITHUB_TOKEN_TOOLS
    assert entry.schema["parameters"]["required"] == [] and not entry.mutates_worktree
    assert set(entry.schema["parameters"]["properties"]) == {"number", "sha", "wait_seconds", "repo"}
    assert github_checks._CHECKS_WAIT_CAP_SEC + github_checks._CHECKS_READ_BUDGET_SEC < entry.timeout_sec
    assert "ends the call with that error" in entry.schema["parameters"]["properties"]["wait_seconds"]["description"]
    for listing in (OBSERVE_DISABLED, tool_capabilities.OBSERVE_WORLD_MUTATION_TOOLS, tool_capabilities.READ_ONLY_PARALLEL_TOOLS,
                    tool_capabilities.LOCAL_READONLY_SUBAGENT_TOOL_NAMES, tool_capabilities.ACTING_SUBAGENT_TOOL_NAMES):
        assert "get_github_checks" not in listing
    assert "comment_on_pr" in OBSERVE_DISABLED  # The same lists do carry the family's write verbs.
    # The reader's module is a helper of the family: the family module registers the tool, in frozen builds too.
    from ouroboros.tools.registry import ToolRegistry

    assert not hasattr(github_checks, "get_tools") and hasattr(github, "get_tools")
    assert "github" in ToolRegistry._FROZEN_TOOL_MODULES and "github_checks" not in ToolRegistry._FROZEN_TOOL_MODULES
