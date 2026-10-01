"""Exercise public CI evidence against real pytest processes and private JUnit."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET

import pytest

pytestmark = pytest.mark.serial
REPO = Path(__file__).resolve().parents[1]
SECRET = "synthetic-ci-secret-47d5fb6c-never-sent"
ARTIFACT_URL = "https://github.com/example/project/actions/runs/123/artifacts/456"
RUN_FACTS = {"GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
             "GITHUB_RUN_ID": "123456789", "GITHUB_RUN_ATTEMPT": "2"}


def _producer(tmp_path, source, *, conftest="", broken_export=False):
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
            "--ci-evidence-dir", str(public), "-q", str(suite)]
    # The entry runs after safe_test scrubs the real environment. This public
    # synthetic value exists before pytest_configure takes its redaction snapshot.
    entry = tmp_path / "run_specimen.py"
    entry.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        f"os.environ['OPENAI_API_KEY'] = {SECRET!r}\n"
        f"os.environ.update({RUN_FACTS!r})\n"
        "import pytest\n"
        f"raise SystemExit(pytest.main({argv!r}))\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(REPO / "scripts/safe_test.py"),
         "--temp-parent", str(tmp_path), "--", sys.executable, str(entry)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    return result, public, private


def _summary(tmp_path, public, *, producer="success", artifact="success",
             artifact_url=ARTIFACT_URL, scope="full"):
    summary = tmp_path / "summary.md"
    # -I -S cannot load pytest/site-packages or our package. The reporter is a
    # standalone stdlib program, just as the after-producer Actions step needs.
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(REPO / "tests/ci_evidence.py"), "summarize",
         "--evidence-dir", str(public), "--summary", str(summary),
         "--producer-outcome", producer, "--artifact-outcome", artifact,
         "--artifact-url", artifact_url, "--scope", scope],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return summary.read_text(encoding="utf-8"), result


def _public_text(public):
    return "\n".join(path.read_text(encoding="utf-8")
                     for path in public.rglob("*") if path.is_file())


def _results(public):
    return json.loads((public / "results.json").read_text(encoding="utf-8"))


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
    assert {path.name for path in public.iterdir()} == {"results.json"}
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
