"""Exercise public CI evidence against real pytest processes and private JUnit."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ET

import pytest

pytestmark = pytest.mark.serial
REPO = Path(__file__).resolve().parents[1]
SECRET = "synthetic-ci-secret-47d5fb6c-never-sent"
ARTIFACT_URL = "https://github.com/example/project/actions/runs/123/artifacts/456"
RUN_FACTS = {"GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
             "GITHUB_RUN_ID": "123456789", "GITHUB_RUN_ATTEMPT": "2"}


def _producer_command(tmp_path, source, *, conftest="", broken_export=False, args=()):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_specimen.py").write_text(textwrap.dedent(source), encoding="utf-8")
    if conftest:
        (suite / "conftest.py").write_text(textwrap.dedent(conftest), encoding="utf-8")
    public, private = tmp_path / "public", tmp_path / "private" / "junit.xml"
    if broken_export:
        public.write_text("output destination is a file", encoding="utf-8")
    argv = ["-p", "tests.conftest", "-o", "addopts=", "--rootdir", str(suite),
            "--confcutdir", str(suite), "--junitxml", str(private),
            "--ci-evidence-dir", str(public), "-q", *args, str(suite)]
    # The entry runs after safe_test scrubs the real environment. This public
    # synthetic value exists before pytest_configure takes its redaction snapshot.
    entry = tmp_path / "run_specimen.py"
    entry.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        f"os.environ['OPENAI_API_KEY'] = {SECRET!r}\n"
        f"os.environ.update({RUN_FACTS!r})\n"
        f"with open({str(tmp_path / 'producer.pid')!r}, 'w', encoding='utf-8') as pid:\n"
        "    pid.write(str(os.getpid()))\n"
        "import pytest\n"
        f"raise SystemExit(pytest.main({argv!r}))\n", encoding="utf-8")
    command = [sys.executable, "-I", "-S", str(REPO / "scripts/safe_test.py"),
               "--temp-parent", str(tmp_path), "--", sys.executable, str(entry)]
    return command, public, private


def _producer(tmp_path, source, *, timeout=60, **options):
    command, public, private = _producer_command(tmp_path, source, **options)
    result = subprocess.run(command, cwd=REPO, capture_output=True, text=True,
                            encoding="utf-8", timeout=timeout)
    return result, public, private


def _summary(tmp_path, public, *, producer="success", artifact="success",
             artifact_url=ARTIFACT_URL, scope="full", label=None):
    summary = tmp_path / "summary.md"
    summary.unlink(missing_ok=True)  # The reporter appends; each call returns its own text.
    # -I -S cannot load pytest/site-packages or our package. The reporter is a
    # standalone stdlib program, just as the after-producer Actions step needs.
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(REPO / "tests/ci_evidence.py"), "summarize",
         "--evidence-dir", str(public), "--summary", str(summary),
         "--producer-outcome", producer, "--artifact-outcome", artifact,
         "--artifact-url", artifact_url, "--scope", scope,
         *(() if label is None else ("--label", label))],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return summary.read_text(encoding="utf-8"), result


def _annotate(tmp_path, *directories, limit=None):
    command = [sys.executable, "-I", "-S", str(REPO / "tests/ci_evidence.py"), "annotate"]
    for directory in directories:
        command += ["--evidence-dir", str(directory)]
    if limit is not None:
        command += ["--limit", str(limit)]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.split("\n")[:-1] if result.stdout else []


def _public_text(public):
    return "\n".join(path.read_text(encoding="utf-8")
                     for path in public.rglob("*") if path.is_file())


def _results(public):
    return json.loads((public / "results.json").read_text(encoding="utf-8"))


def _events(public):
    """Complete journal rows; a live writer may be between lines when this reads."""
    rows = []
    try:
        lines = (public / "events.jsonl").read_text(encoding="utf-8").split("\n")
    except OSError:
        return rows
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def _kill_when(tmp_path, source, ready, *, args=()):
    """Hard-kill the pytest process once its journal satisfies `ready`."""
    command, public, _ = _producer_command(tmp_path, source, args=args)
    log = tmp_path / "producer.log"
    # A file, not a pipe: orphaned xdist workers inherit it and must not block this test.
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(command, cwd=REPO, stdout=output, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 120
        while not ready(_events(public)):
            assert process.poll() is None, log.read_text(encoding="utf-8", errors="replace")
            assert time.monotonic() < deadline, log.read_text(encoding="utf-8", errors="replace")
            time.sleep(0.05)
        # No handler or finally block runs: SIGKILL on POSIX; Windows has no SIGKILL
        # and maps every other os.kill signal to TerminateProcess.
        os.kill(int((tmp_path / "producer.pid").read_text(encoding="utf-8")),
                getattr(signal, "SIGKILL", signal.SIGTERM))
        process.wait(timeout=60)
    finally:
        (tmp_path / "release").write_text("", encoding="utf-8")  # Orphaned workers stop waiting.
        if process.poll() is None:
            process.kill()
            process.wait(timeout=60)
    return public


def _blocking(tmp_path, tests):
    """A specimen whose block() holds a test in flight until _kill_when releases it."""
    return textwrap.dedent(f"""
        import time
        from pathlib import Path
        import pytest
        def block():
            deadline = time.monotonic() + 120
            while not Path({str(tmp_path / "release")!r}).exists() and time.monotonic() < deadline:
                time.sleep(0.05)
    """) + textwrap.dedent(tests)


def _started(rows):
    return [row["nodeid"] for row in rows if row.get("event") == "start"]


def _write_results(public, reports=()):
    public.mkdir(exist_ok=True)
    (public / "results.json").write_text(json.dumps({
        "format": 1, "session_exit_code": 0, "reports": list(reports),
        "collection_failures": [],
    }), encoding="utf-8")


def test_passing_process_exports_safe_results_and_redacts_parameter_identity(tmp_path):
    result, public, private = _producer(tmp_path, f"""
        import os
        import pytest
        @pytest.mark.parametrize("value", [1], ids=["ordinary-label-{SECRET}"])
        def test_pass(value, request):
            import tests.conftest as boundary
            assert boundary._PYTEST_DATA_DIR.is_dir()
            assert os.environ["OUROBOROS_PYTEST_ACTIVE"] == "1"
            assert request.config.pluginmanager.hasplugin("ci_safe_results")
            assert value == 1
    """)
    assert result.returncode == 0, result.stdout + result.stderr
    facts = _results(public)
    assert facts["session_exit_code"] == 0
    assert facts["github"] == {"sha": RUN_FACTS["GITHUB_SHA"],
        "run_id": RUN_FACTS["GITHUB_RUN_ID"], "run_attempt": RUN_FACTS["GITHUB_RUN_ATTEMPT"]}
    assert [row["outcome"] for row in facts["reports"] if row["phase"] == "call"] == ["passed"]
    assert "ordinary-label" in _public_text(public)
    assert SECRET in private.read_text(encoding="utf-8")
    assert SECRET not in _public_text(public)
    assert {path.name for path in public.iterdir()} == {"results.json", "events.jsonl"}
    summary, _ = _summary(tmp_path, public)
    assert "Producer step: **success**" in summary
    assert "passed=1" in summary and ARTIFACT_URL in summary
    assert all(value in summary for value in RUN_FACTS.values())
    assert "diagnostics_incomplete" not in summary


def test_http_chain_assertion_and_setup_bodies_remain_in_private_junit(tmp_path):
    result, public, private = _producer(tmp_path, f"""
        import httpx
        import pytest
        SECRET = {SECRET!r}
        def test_assertion():
            assert False, "assertion body " + SECRET
        def test_http_chain():
            try:
                raise httpx.HTTPStatusError("HTTP body " + SECRET,
                    request=httpx.Request("GET", "https://provider.invalid/"),
                    response=httpx.Response(503, text=SECRET))
            except httpx.HTTPStatusError as cause:
                raise RuntimeError("chained body " + SECRET) from cause
        @pytest.fixture
        def failed_setup():
            raise ValueError("setup body " + SECRET)
        def test_setup(failed_setup):
            pass
        def test_skip():
            pytest.skip("skip body " + SECRET)
    """)
    assert result.returncode == 1, result.stdout + result.stderr
    raw = private.read_text(encoding="utf-8")
    assert all(label in raw for label in ("HTTP body", "chained body", "assertion body", "setup body"))
    assert SECRET in raw
    exported = _public_text(public)
    assert SECRET not in exported
    assert not any(label in exported for label in ("HTTP body", "chained body", "assertion body", "setup body"))
    facts = _results(public)
    assert facts["session_exit_code"] == 1
    assert {row["error_type"] for row in facts["reports"] if row["outcome"] == "failed"} == {
        "AssertionError", "RuntimeError", "ValueError"}
    summary, _ = _summary(tmp_path, public, producer="failure")
    assert "Producer step: **failure**" in summary
    assert "failed=3" in summary and "skipped=1" in summary
    assert "setup" in summary and "RuntimeError" in summary
    assert SECRET not in summary


def test_collection_error_body_is_private_and_not_a_passing_result(tmp_path):
    result, public, private = _producer(tmp_path,
        f"raise RuntimeError('collection body {SECRET}')\n")
    assert result.returncode == 2, result.stdout + result.stderr
    assert SECRET in private.read_text(encoding="utf-8")
    assert SECRET not in _public_text(public)
    assert _results(public)["collection_failures"]
    summary, _ = _summary(tmp_path, public, producer="failure")
    assert "Producer step: **failure**" in summary
    assert "Collection failures: 1" in summary
    assert "passed=0" in summary


def test_passing_cases_do_not_overwrite_failed_session_exit(tmp_path):
    result, public, private = _producer(tmp_path, "def test_pass():\n    pass\n", conftest="""
        def pytest_sessionfinish(session, exitstatus):
            session.exitstatus = 1
    """)
    assert result.returncode == 1, result.stdout + result.stderr
    assert len(ET.parse(private).findall(".//testcase")) == 1
    assert not ET.parse(private).findall(".//failure")
    assert _results(public)["session_exit_code"] == 1
    summary, _ = _summary(tmp_path, public, producer="failure")
    assert "Producer step: **failure**" in summary and "passed=1" in summary
    assert "Observed pytest session exit: 1" in summary


@pytest.mark.parametrize("fails", [False, True])
def test_export_failure_never_changes_the_producer_exit(tmp_path, fails):
    source = "def test_producer():\n    " + (f"assert False, {SECRET!r}\n" if fails else "pass\n")
    result, public, private = _producer(tmp_path, source, broken_export=True)
    assert result.returncode == int(fails), result.stdout + result.stderr
    assert "CI_DIAGNOSTICS_INCOMPLETE" in result.stdout
    assert private.is_file() and not (public / "results.json").exists()
    summary, _ = _summary(tmp_path, public, producer="failure" if fails else "success",
                          artifact="failure", artifact_url="")
    assert f"Producer step: **{'failure' if fails else 'success'}**" in summary
    assert "diagnostics_incomplete" in summary and "Case outcomes: **unknown**" in summary
    assert SECRET not in summary


@pytest.mark.parametrize("payload", [None, "not-json", "[]", '{"reports":[{}]}',
    '{"reports":"invalid"}', '{"reports":[{"nodeid":"x","phase":"call","outcome":"unknown"}]}',
    '{"reports":[{"nodeid":"x","phase":"unknown","outcome":"passed"}]}'])
def test_missing_or_corrupt_results_are_incomplete_not_passed(tmp_path, payload):
    public = tmp_path / "public"
    public.mkdir()
    if payload is not None:
        (public / "results.json").write_text(payload, encoding="utf-8")
    summary, result = _summary(tmp_path, public, producer="failure")
    assert "diagnostics_incomplete" in summary
    assert "Producer step: **failure**" in summary
    assert "Case outcomes: **unknown**" in summary
    assert "::warning::" in result.stdout


@pytest.mark.parametrize("provider", [None, {"canary_id": "route-a", "outcome": "failed",
    "diagnostics_errors": ["physical request unavailable"]}, []])
def test_expected_provider_evidence_and_capture_gaps_are_visible(tmp_path, provider):
    public = tmp_path / "public"
    _write_results(public, [{"nodeid": "test_canary[route-a]", "phase": "call",
        "outcome": "failed", "error_type": "AssertionError", "canary_id": "route-a"}])
    if provider is not None:
        (public / "provider-route-a.json").write_text(json.dumps(provider), encoding="utf-8")
    summary, _ = _summary(tmp_path, public, producer="failure")
    assert "diagnostics_incomplete" in summary
    assert "Producer step: **failure**" in summary


def test_real_canary_parameter_identifies_missing_provider_facts(tmp_path):
    result, public, _ = _producer(tmp_path, """
        from types import SimpleNamespace
        import pytest
        @pytest.mark.parametrize("canary", [SimpleNamespace(canary_id="offline-route")])
        def test_canary(canary):
            assert canary.canary_id == "offline-route"
    """)
    assert result.returncode == 0, result.stdout + result.stderr
    call = [row for row in _results(public)["reports"] if row["phase"] == "call"]
    assert [row["canary_id"] for row in call] == ["offline-route"]
    summary, _ = _summary(tmp_path, public)
    assert "diagnostics_incomplete" in summary
    assert "provider result projections are missing" in summary
    assert "Producer step: **success**" in summary


@pytest.mark.parametrize("producer", ["success", "failure"])
@pytest.mark.parametrize("artifact", ["success", "failure"])
def test_partial_scope_and_artifact_outcome_stay_separate_from_producer(tmp_path, producer, artifact):
    public = tmp_path / "public"
    _write_results(public)
    summary, _ = _summary(tmp_path, public, producer=producer, artifact=artifact,
                          artifact_url=ARTIFACT_URL if artifact == "success" else "", scope="viewport")
    assert "PARTIAL DIAGNOSTIC" in summary and "Selection: **viewport**" in summary
    assert f"Producer step: **{producer}**" in summary
    assert ("diagnostics_incomplete" in summary) is (artifact == "failure")


@pytest.mark.parametrize("payload, reason", [
    ('{"diagnostics_incomplete":true}', "browser evidence contains recorded capture gaps"),
    ("[]", "a browser projection is unreadable"),
    ("not-json", "a browser projection is unreadable"),
])
def test_browser_capture_gap_is_diagnostics_incomplete(tmp_path, payload, reason):
    public = tmp_path / "public"
    _write_results(public)
    browser = public / "browser" / "viewport-webkit"
    browser.mkdir(parents=True)
    (browser / "evidence.json").write_text(payload, encoding="utf-8")
    summary, _ = _summary(tmp_path, public, producer="failure")
    assert "diagnostics_incomplete" in summary
    assert reason in summary


def test_successful_upload_without_a_url_is_incomplete(tmp_path):
    public = tmp_path / "public"
    _write_results(public)
    summary, _ = _summary(tmp_path, public, artifact_url="")
    assert "diagnostics_incomplete" in summary
    assert "Producer step: **success**" in summary


@pytest.mark.parametrize("body, args", [
    ("import os; os._exit(3)", ("-n", "2")),
    # The parallel CI pass: the thread timeout kills its worker and nothing restarts it.
    ("import time; time.sleep(60)", ("-n", "2", "--dist", "loadscope", "--max-worker-restart=0",
                                     "--timeout=2", "--timeout-method=thread")),
], ids=["exit", "ci-timeout"])
def test_dead_xdist_worker_is_a_named_crash_and_not_an_unknown_session(tmp_path, body, args):
    result, public, _ = _producer(tmp_path, f"""
        def test_dies():
            {body}
        class TestOtherScope:
            def test_survives(self, request):
                # Only the controller owns the journal file.
                assert hasattr(request.config, "workerinput")
                assert request.config.pluginmanager.get_plugin("ci_safe_results").journal is None
    """, args=args, timeout=120)
    assert result.returncode == 1, result.stdout + result.stderr
    survivor = "test_specimen.py::TestOtherScope::test_survives"
    final = {row["nodeid"]: (row["phase"], row["outcome"], row["error_type"])
             for row in _results(public)["reports"] if row["phase"] != "setup"}
    assert final["test_specimen.py::test_dies"] == ("crash", "failed", "WorkerCrash")
    assert final[survivor] == ("teardown", "passed", "")
    summary, _ = _summary(tmp_path, public, producer="failure")
    assert "| test_specimen.py::test_dies | crash | WorkerCrash |" in summary
    assert "passed=1, failed=1" in summary and "test_survives" not in summary
    assert "Case outcomes: **unknown**" not in summary and "diagnostics_incomplete" not in summary
    # The controller re-emits worker events; a complete session never reads them back.
    journal = _events(public)
    assert sorted(_started(journal)) == [survivor, "test_specimen.py::test_dies"]
    assert {"event": "finish", "nodeid": survivor} in journal
    assert {"event": "finish", "nodeid": "test_specimen.py::test_dies"} not in journal
    assert "in flight" not in summary
    assert _annotate(tmp_path, public) == [
        "::error title=Failed test::test_specimen.py::test_dies (crash, WorkerCrash)"]


@pytest.mark.parametrize("args, unreported", [(("-x",), 2), ((), 0)], ids=["stopped", "complete"])
def test_stopped_session_counts_the_collected_tests_it_never_ran(tmp_path, args, unreported):
    result, public, _ = _producer(tmp_path, """
        def test_first():
            assert False
        def test_second():
            pass
        def test_third():
            pass
    """, args=args)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _results(public)["tests_collected"] == 3
    summary, _ = _summary(tmp_path, public, producer="failure")
    line = f"**{unreported} collected test(s) produced no report**"
    assert (line in summary) is bool(unreported)
    assert ("produced no report" in summary) is bool(unreported)


def test_killed_session_names_the_test_in_flight_and_earlier_failures(tmp_path):
    public = _kill_when(tmp_path, _blocking(tmp_path, f"""
        def test_fails():
            assert False, {SECRET!r}
        def test_passes():
            pass
        @pytest.mark.parametrize("value", [1], ids=["blocked-{SECRET}"])
        def test_blocks(value):
            block()
    """), lambda rows: any("test_blocks" in node for node in _started(rows)))
    assert not (public / "results.json").exists()
    assert SECRET not in _public_text(public)
    blocked = "test_specimen.py::test_blocks[blocked-***]"
    assert _started(_events(public))[-1] == blocked
    summary, result = _summary(tmp_path, public, producer="failure")
    assert "Case outcomes: **unknown**. No passing result is inferred." in summary
    assert "ended before its final export" in summary and "diagnostics_incomplete" in summary
    assert "1 test(s) in flight and 1 failed report(s)" in summary
    assert f"| {blocked} |" in summary
    assert "| test_specimen.py::test_fails | call | AssertionError |" in summary
    assert "test_passes" not in summary and SECRET not in summary
    assert "::warning::" in result.stdout
    assert _annotate(tmp_path, public) == [
        "::error title=Failed test::test_specimen.py::test_fails (call, AssertionError)",
        f"::error title=Test in flight when the session was killed::{blocked}"]


def test_killed_xdist_session_names_every_test_in_flight(tmp_path):
    public = _kill_when(tmp_path, _blocking(tmp_path, """
        def test_blocks_a():
            block()
        def test_blocks_b():
            block()
    """), lambda rows: len(_started(rows)) == 2, args=("-n", "2"))
    assert not (public / "results.json").exists()
    blocked = {"test_specimen.py::test_blocks_a", "test_specimen.py::test_blocks_b"}
    summary, _ = _summary(tmp_path, public, producer="cancelled")
    assert "Case outcomes: **unknown**" in summary
    assert "2 test(s) in flight and 0 failed report(s)" in summary
    assert all(f"| {node} |" in summary for node in blocked)
    assert set(_annotate(tmp_path, public)) == {
        f"::error title=Test in flight when the session was killed::{node}" for node in blocked}


@pytest.mark.parametrize("finished", [False, True], ids=["interrupted", "complete"])
def test_exported_session_still_names_a_test_its_journal_left_in_flight(tmp_path, finished):
    # An interrupt (a console CTRL_C at a step ceiling) lets pytest write its final export.
    public, node = tmp_path / "public", "t.py::test_hangs"
    _write_results(public, [{"nodeid": node, "phase": "setup", "outcome": "passed"}])
    rows = [{"event": "start", "nodeid": node}] + ([{"event": "finish", "nodeid": node}] * finished)
    (public / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows),
                                         encoding="utf-8")
    summary, _ = _summary(tmp_path, public, producer="failure")
    assert "Case outcomes: **unknown**" not in summary and "not_run=1" in summary
    assert (f"| {node} |" in summary) is not finished
    assert ("in flight" in summary) is not finished
    assert _annotate(tmp_path, public) == (
        [] if finished else [f"::error title=Test in flight when the session was killed::{node}"])


def test_journal_holds_only_redacted_identity_enums_and_exception_types(tmp_path):
    result, public, _ = _producer(tmp_path, f"""
        import pytest
        @pytest.mark.parametrize("value", [1, 2], ids=["kept-label-{SECRET}", "other"])
        def test_param(value):
            assert value == 2, {SECRET!r}
    """)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "CI_DIAGNOSTICS_INCOMPLETE" not in result.stdout
    journal = (public / "events.jsonl").read_text(encoding="utf-8")
    assert SECRET not in journal and "kept-label-***" in journal
    rows = _events(public)
    assert [row["event"] for row in rows] == ["start", "report", "report", "report", "finish"] * 2
    assert {tuple(row) for row in rows} == {
        ("event", "nodeid"), ("event", "nodeid", "phase", "outcome", "error_type")}
    assert [(row["phase"], row["error_type"]) for row in rows
            if row.get("outcome") == "failed"] == [("call", "AssertionError")]


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("broken", ["directory", "open", "write"])
def test_journal_failure_is_one_line_and_never_changes_the_producer_exit(tmp_path, broken, fails):
    source = "def test_producer():\n    " + ("assert False\n" if fails else "pass\n")
    conftest = ""
    if broken == "open":
        (tmp_path / "public" / "events.jsonl").mkdir(parents=True)
    elif broken == "write":
        conftest = """
            def pytest_collection_finish(session):
                session.config.pluginmanager.get_plugin("ci_safe_results").journal.close()
        """
    result, public, _ = _producer(tmp_path, source, conftest=conftest,
                                  broken_export=broken == "directory")
    assert result.returncode == int(fails), result.stdout + result.stderr
    disclosed = [line for line in result.stdout.splitlines()
                 if line.startswith("CI_DIAGNOSTICS_INCOMPLETE: incremental journal disabled")]
    assert len(disclosed) == 1, result.stdout
    assert result.stdout.count("CI_DIAGNOSTICS_INCOMPLETE") == (2 if broken == "directory" else 1)
    if broken != "directory":  # The final export does not depend on the journal.
        assert _results(public)["session_exit_code"] == int(fails)
        assert not _events(public)


def test_torn_and_invalid_journal_rows_are_skipped(tmp_path):
    rows = [
        {"event": "start", "nodeid": "t.py::test_done"},
        {"event": "report", "nodeid": "t.py::test_done", "phase": "call", "outcome": "failed",
         "error_type": "KeyError"},
        {"event": "finish", "nodeid": "t.py::test_done"},
        {"event": "start", "nodeid": "t.py::test_crashed"},
        {"event": "report", "nodeid": "t.py::test_crashed", "phase": "crash", "outcome": "failed",
         "error_type": "WorkerCrash"},
        {"event": "start", "nodeid": "t.py::test_hangs"},
    ]
    clean = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
    noise = [b"not-json\n", b"[]\n", b"\xff\xfe torn bytes\n", b"\n",
             b'{"event":"report","nodeid":"t.py::test_bad_phase","phase":"unknown","outcome":"failed"}\n',
             b'{"event":"report","nodeid":7,"phase":"call","outcome":"failed"}\n',
             b'{"event":"start","nodeid":["t.py::test_bad_identity"]}\n']
    summaries = []
    for name, payload in (("clean", clean), ("noisy", b"".join(noise) + clean
                                                 + b'{"event":"start","nodeid":"t.py::test_torn')):
        public = tmp_path / name
        public.mkdir()
        (public / "events.jsonl").write_bytes(payload)
        summary, _ = _summary(tmp_path, public, producer="failure")
        summaries.append(summary)
        assert _annotate(tmp_path, public) == [
            "::error title=Failed test::t.py::test_done (call, KeyError)",
            "::error title=Failed test::t.py::test_crashed (crash, WorkerCrash)",
            "::error title=Test in flight when the session was killed::t.py::test_hangs"]
    assert summaries[0] == summaries[1]
    assert "| t.py::test_hangs |" in summaries[0] and "| t.py::test_crashed | crash | WorkerCrash |" in summaries[0]
    assert "1 test(s) in flight and 2 failed report(s)" in summaries[0]
    assert not any(name in summaries[1] for name in ("test_bad", "test_torn"))
    empty = tmp_path / "empty"
    empty.mkdir()
    silent, _ = _summary(tmp_path, empty, producer="failure")
    assert "Case outcomes: **unknown**" in silent and "final export" not in silent
    assert _annotate(tmp_path, empty, tmp_path / "absent") == []


def test_annotations_are_limited_escaped_and_quiet_for_green_results(tmp_path):
    from tests.ci_evidence import _escaped

    def failed(index):
        return {"nodeid": f"t.py::test_{index}", "phase": "call", "outcome": "failed",
                "error_type": "ValueError"}
    green, red, odd, killed = (tmp_path / name for name in ("green", "red", "odd", "killed"))
    _write_results(green, [{"nodeid": "t.py::test_ok", "phase": "call", "outcome": "passed"}])
    assert _annotate(tmp_path, green) == []
    _write_results(red, [failed(index) for index in range(12)])
    lines = _annotate(tmp_path, red)
    assert lines[:10] == [f"::error title=Failed test::t.py::test_{index} (call, ValueError)"
                          for index in range(10)]
    assert lines[10:] == ["::notice::2 more failed tests are listed in the job summary"]
    assert len(_annotate(tmp_path, red, limit=12)) == 12
    assert not any(line.startswith("::notice::") for line in _annotate(tmp_path, red, limit=12))
    # A nodeid cannot end its own command line, and a missing type leaves no empty field.
    _write_results(odd, [{"nodeid": "t.py::test_pct[100%\r\nnext]", "phase": "setup", "outcome": "failed"}])
    assert _annotate(tmp_path, odd) == ["::error title=Failed test::t.py::test_pct[100%25%0D%0Anext] (setup)"]
    assert _escaped("a:b,c%\n", property_value=True) == "a%3Ab%2Cc%25%0A"
    assert _escaped("a:b,c%\n") == "a:b,c%25%0A"
    # Across directories failed tests lead, then collection failures, then tests in flight.
    killed.mkdir()
    (killed / "events.jsonl").write_text(
        json.dumps({"event": "start", "nodeid": "k.py::test_hangs"}) + "\n"
        + json.dumps({"event": "report", "nodeid": "k.py::test_late", "phase": "teardown",
                      "outcome": "failed", "error_type": "OSError"}) + "\n", encoding="utf-8")
    (odd / "results.json").write_text(json.dumps({"reports": [failed(0)], "collection_failures": [
        {"nodeid": "t.py", "outcome": "failed"}]}), encoding="utf-8")
    assert _annotate(tmp_path, killed, odd, green) == [
        "::error title=Failed test::k.py::test_late (teardown, OSError)",
        "::error title=Failed test::t.py::test_0 (call, ValueError)",
        "::error title=Collection failed::t.py",
        "::error title=Test in flight when the session was killed::k.py::test_hangs"]
    assert _annotate(tmp_path, killed, odd, limit=2)[2:] == [
        "::notice::2 more failed tests are listed in the job summary"]
    # Advisory even when miswired, while the summary contract still rejects bad usage.
    program = [sys.executable, "-I", "-S", str(REPO / "tests/ci_evidence.py")]
    for arguments in (["annotate"], ["annotate", "--evidence-dir", str(red), "--limit", "ten"]):
        miswired = subprocess.run(program + arguments, capture_output=True, text=True,
                                  encoding="utf-8", timeout=30)
        assert miswired.returncode == 0 and miswired.stdout.startswith("::warning::")
    assert subprocess.run(program + ["summarize"], capture_output=True, timeout=30).returncode == 2


def test_label_extends_only_the_heading(tmp_path):
    public = tmp_path / "public"
    _write_results(public, [{"nodeid": "t.py::test_x", "phase": "call", "outcome": "failed",
                             "error_type": "ValueError"}])
    plain, _ = _summary(tmp_path, public, producer="failure")
    assert plain == (
        "## CI test evidence\n\n"
        "Producer step: **failure**. Selection: **full**.\n"
        "Testcase results and diagnostic availability are separate from that process outcome.\n\n"
        "Cases: passed=0, failed=1, skipped=0, not_run=0.\n"
        "Observed pytest session exit: 0.\n\n"
        "| Test | Phase | Error type |\n| --- | --- | --- |\n| t.py::test_x | call | ValueError |\n\n"
        f"[Download safe evidence]({ARTIFACT_URL})\n\n"
        "Raw JUnit, exception bodies, credentials and private runtime stores are not in this export.\n")
    labelled, _ = _summary(tmp_path, public, producer="failure", label="parallel pass")
    assert labelled == plain.replace("## CI test evidence\n", "## CI test evidence — parallel pass\n", 1)
    assert labelled != plain
    partial, _ = _summary(tmp_path, public, scope="viewport", label="serial | pass")
    assert partial.startswith("## CI test evidence — serial &#124; pass — PARTIAL DIAGNOSTIC\n")
