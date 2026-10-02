"""Public CI exports preserve producer exits, secret boundaries and full UI proof."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def _workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def _steps(name, job):
    return {step["id"]: step for step in _workflow(name)["jobs"][job]["steps"] if "id" in step}


def test_provider_diagnostics_stay_outside_test_and_release_authority():
    workflow = _workflow("ci.yml")
    job = workflow["jobs"]["integration-test"]
    steps = _steps("ci.yml", "integration-test")
    producer = steps["provider_tests"]
    assert "continue-on-error" not in job and "continue-on-error" not in producer
    assert "pytest tests/test_provider_integration.py -m integration -q -rs --tb=short" in producer["run"]
    assert "--ci-evidence-dir=" in producer["run"] and "--junitxml=" in producer["run"]
    assert "ci-private/provider/results.xml" in producer["run"]
    assert "ci-evidence/provider" in producer["run"]
    assert "secrets." in str(producer["env"])
    for step in job["steps"]:
        if step.get("id") != "provider_tests":
            assert "secrets." not in str(step)
    assert "env" not in job
    upload, summary = steps["provider_evidence"], steps["provider_summary"]
    assert upload["uses"] == UPLOAD_ACTION
    assert upload["with"]["path"] == "${{ runner.temp }}/ci-evidence/provider/"
    assert upload["with"]["retention-days"] == 30
    assert "github.sha" in upload["with"]["name"] and "github.run_attempt" in upload["with"]["name"]
    assert summary["env"]["PRODUCER_OUTCOME"] == "${{ steps.provider_tests.outcome }}"
    assert summary["env"]["ARTIFACT_URL"] == "${{ steps.provider_evidence.outputs.artifact-url }}"
    assert "python -m tests.ci_evidence summarize" in summary["run"]
    assert list(steps).index("provider_evidence") < list(steps).index("provider_summary")
    assert workflow["jobs"]["release-preflight"]["needs"] == ["full-test", "integration-test", "system-e2e-mock"]
    release = workflow["jobs"]["release"]
    assert "needs.release-preflight.result == 'success'" in release["if"]


def test_only_informational_steps_tolerate_errors_and_report_missing_uploads():
    for name, jobname in (("ci.yml", "integration-test"), ("ui-browser.yml", "ui-smoke")):
        job = _workflow(name)["jobs"][jobname]
        for step in job["steps"]:
            if step.get("continue-on-error"):
                assert "!cancelled()" in step["if"]
                assert (step.get("uses") == UPLOAD_ACTION or
                        "tests.ci_evidence summarize" in step.get("run", "") or
                        "diagnostics_incomplete" in step.get("run", ""))
                assert "-m pytest" not in step.get("run", "")
        warning = next(step for step in job["steps"] if "Disclose incomplete" in step.get("name", ""))
        assert "outputs.artifact-url == ''" in warning["if"]
        assert "summary.outcome != 'success'" in warning["if"]
        assert "::warning::" in warning["run"] and "$GITHUB_STEP_SUMMARY" in warning["run"]


def test_manual_ui_selection_is_fixed_partial_and_never_a_paid_or_full_check():
    caller = _workflow("ui-browser-push.yml")
    triggers = caller.get("on", caller.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["ouroboros"]}
    selection = triggers["workflow_dispatch"]["inputs"]["diagnostic"]
    assert selection["type"] == "choice" and selection["default"] == "full"
    assert selection["options"] == ["full", "viewport", "inflight"]
    assert "PARTIAL DIAGNOSTIC" in caller["run-name"]
    assert list(caller["jobs"]) == ["ui-smoke"] and "secrets" not in str(caller)
    assert caller["jobs"]["ui-smoke"]["with"]["diagnostic"] == "${{ inputs.diagnostic || 'full' }}"
    assert "PARTIAL DIAGNOSTIC" in caller["jobs"]["ui-smoke"]["name"]
    shared = _workflow("ui-browser.yml")
    assert list(shared["jobs"]) == ["ui-smoke"]
    assert "PARTIAL DIAGNOSTIC" in shared["jobs"]["ui-smoke"]["name"]
    steps = _steps("ui-browser.yml", "ui-smoke")
    full, partial, tools = [steps[key] for key in ("ui_tests", "ui_diagnostic", "browser_tools")]
    assert "inputs.diagnostic == 'full'" in full["if"]
    assert "inputs.diagnostic == 'full'" in tools["if"]
    assert "inputs.diagnostic != 'full'" in partial["if"]
    assert "--require-ui-browser" in full["run"] and "pytest tests/ -m ui_browser" in full["run"]
    assert "--require-ui-browser" not in partial["run"]
    assert '${{ inputs.diagnostic }}' not in partial["run"]
    assert partial["env"]["DIAGNOSTIC"] == "${{ inputs.diagnostic }}"
    assert "--scope \"$DIAGNOSTIC\"" in steps["ui_summary"]["run"]


def test_ui_producers_have_distinct_reports_and_only_the_public_parent_is_uploaded():
    steps = _steps("ui-browser.yml", "ui-smoke")
    upload = steps["ui_evidence"]
    assert upload["uses"] == UPLOAD_ACTION
    assert upload["with"]["path"] == "${{ runner.temp }}/ci-evidence/ui/"
    assert "github.run_attempt" in upload["with"]["name"] and "github.sha" in upload["with"]["name"]
    for key, directory in (("ui_tests", "host"), ("ui_diagnostic", "host"), ("browser_tools", "tools")):
        run = steps[key]["run"]
        assert f"ci-private/ui/{directory}.xml" in run
        assert f"ci-evidence/ui/{directory}" in run
        assert "continue-on-error" not in steps[key]
    assert "steps.ui_tests.outcome" in steps["ui_summary"]["env"]["PRODUCER_OUTCOME"]
    assert "steps.ui_diagnostic.outcome" in steps["ui_summary"]["env"]["PRODUCER_OUTCOME"]
    assert steps["tools_summary"]["env"]["PRODUCER_OUTCOME"] == "${{ steps.browser_tools.outcome }}"
    assert "ci-evidence/ui/host" in steps["ui_summary"]["run"]
    assert "ci-evidence/ui/tools" in steps["tools_summary"]["run"]


def _run_shell(step, tmp_path, *, diagnostic="viewport", result=0):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("workflow shell unavailable on this host")
    args = tmp_path / "args"
    command = step["run"].replace("${{ runner.temp }}", str(tmp_path))
    stub = 'python() { printf "%s\\n" "$@" > "$ARGV_FILE"; return "$PRODUCER_EXIT"; }\n'
    env = {**os.environ, "ARGV_FILE": str(args), "PRODUCER_EXIT": str(result), "DIAGNOSTIC": diagnostic}
    completed = subprocess.run([bash, "-e", "-o", "pipefail", "-c", stub + command],
                               env=env, capture_output=True, text=True, encoding="utf-8", timeout=10)
    return completed, args.read_text(encoding="utf-8").splitlines() if args.exists() else []


@pytest.mark.parametrize("result", [0, 17])
@pytest.mark.parametrize("workflow,job,step", [
    ("ci.yml", "integration-test", "provider_tests"),
    ("ui-browser.yml", "ui-smoke", "ui_tests"),
    ("ui-browser.yml", "ui-smoke", "ui_diagnostic"),
    ("ui-browser.yml", "ui-smoke", "browser_tools"),
    *[("ci.yml", job, f"tests_{label}") for job in ("quick-test", "full-test")
      for label in ("parallel", "serial", "size")],
])
def test_actual_producer_shell_preserves_success_and_failure_exit(tmp_path, workflow, job, step, result):
    completed, args = _run_shell(_steps(workflow, job)[step], tmp_path, result=result)
    assert completed.returncode == result, completed.stderr
    assert args.count("pytest") == 1 and "summarize" not in args


@pytest.mark.parametrize("selection,target", [
    ("viewport", "tests/test_ui_smoke_playwright.py::test_ui_smoke_live_card_mutations_preserve_viewport"),
    ("inflight", "tests/test_ui_smoke_inflight_indicator.py::test_ui_smoke_chat_inflight_indicator_lifecycle"),
])
def test_partial_selection_passes_exact_target_as_argv(tmp_path, selection, target):
    completed, args = _run_shell(_steps("ui-browser.yml", "ui-smoke")["ui_diagnostic"],
                                 tmp_path, diagnostic=selection)
    assert completed.returncode == 0 and target in args
    assert "--require-ui-browser" not in args and "tests/test_browser_tools_smoke.py" not in args
    assert "-k" not in args and "tests/" not in args


def test_unknown_or_shell_like_selection_cannot_execute_or_silently_run_full(tmp_path):
    marker = tmp_path / "injected"
    completed, args = _run_shell(_steps("ui-browser.yml", "ui-smoke")["ui_diagnostic"], tmp_path,
                                 diagnostic=f"viewport; touch {marker}")
    assert completed.returncode == 2 and not args and not marker.exists()


def test_every_added_outcome_reference_names_an_already_declared_step():
    pattern = re.compile(r"steps\.([A-Za-z_][A-Za-z0-9_]*)\.")
    for workflow, job in (("ci.yml", "integration-test"), ("ui-browser.yml", "ui-smoke")):
        known = set()
        for step in _workflow(workflow)["jobs"][job]["steps"]:
            references = pattern.findall(str(step))
            assert set(references) <= known, (workflow, step.get("name"), references, known)
            if step.get("id"):
                known.add(step["id"])


EVIDENCE_ACTION = "./.github/actions/test-evidence"
PASSES = ("parallel", "serial", "size")


def _evidence_action():
    return yaml.safe_load((ROOT / EVIDENCE_ACTION / "action.yml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("jobname,artifact", [
    ("quick-test", "quick-test"), ("full-test", "full-test-${{ matrix.os }}"),
])
def test_ordinary_jobs_name_their_failures_without_owning_the_result(jobname, artifact):
    job = _workflow("ci.yml")["jobs"][jobname]
    steps = _steps("ci.yml", jobname)
    for label in PASSES:
        producer = steps[f"tests_{label}"]
        assert "continue-on-error" not in producer
        assert f'--ci-evidence-dir="${{{{ runner.temp }}}}/ci-evidence/{label}"' in producer["run"]
        # A hang fails the step, so the evidence step still runs; a job limit would cancel it.
        assert isinstance(producer["timeout-minutes"], int)
    assert "continue-on-error" not in job and "timeout-minutes" not in job
    evidence = next(step for step in job["steps"] if step.get("uses") == EVIDENCE_ACTION)
    assert evidence["continue-on-error"] is True and "!cancelled()" in evidence["if"]
    assert evidence["with"] == {"name": artifact, "passes": " ".join(
        f"{label}=${{{{ steps.tests_{label}.outcome }}}}" for label in PASSES)}
    assert job["steps"].index(evidence) > max(job["steps"].index(steps[f"tests_{label}"])
                                              for label in PASSES)


def test_evidence_action_steps_are_independent_diagnostics():
    upload, report = _evidence_action()["runs"]["steps"]
    for step in (upload, report):  # Neither waits for the other's success nor fails the caller.
        assert step["if"] == "${{ !cancelled() }}" and step["continue-on-error"] is True
    assert upload["uses"] == UPLOAD_ACTION
    assert upload["with"]["path"] == "${{ runner.temp }}/ci-evidence/"
    for fact in ("inputs.name", "github.run_id", "github.run_attempt"):
        assert fact in upload["with"]["name"]
    assert report["shell"] == "bash"  # One shell on all three runner systems.
    assert report["env"]["ARTIFACT_OUTCOME"] == "${{ steps.upload.outcome }}"
    assert report["env"]["ARTIFACT_URL"] == "${{ steps.upload.outputs.artifact-url }}"
    assert "python -I -S tests/ci_evidence.py summarize" in report["run"]
    assert "python -I -S tests/ci_evidence.py annotate" in report["run"]
    assert "pytest" not in report["run"] and "${{" not in report["run"]


def _publish(tmp_path, passes):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("workflow shell unavailable on this host")
    summary = tmp_path / "summary.md"
    env = {**os.environ, "PASSES": passes, "ARTIFACT_OUTCOME": "success",
           "ARTIFACT_URL": "https://github.com/example/project/actions/runs/1/artifacts/2",
           "EVIDENCE_ROOT": str(tmp_path / "ci-evidence"), "GITHUB_STEP_SUMMARY": str(summary),
           "REAL_PYTHON": sys.executable}
    script = 'python() { "$REAL_PYTHON" "$@"; }\n' + _evidence_action()["runs"]["steps"][1]["run"]
    completed = subprocess.run([bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                               cwd=ROOT, env=env, capture_output=True, text=True,
                               encoding="utf-8", timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    errors = [line for line in completed.stdout.splitlines() if line.startswith("::error")]
    return errors, summary.read_text(encoding="utf-8") if summary.exists() else ""


def _final_projection(directory, outcome, error_type=""):
    directory.mkdir(parents=True)
    (directory / "results.json").write_text(json.dumps({
        "format": 1, "session_exit_code": int(outcome == "failed"), "tests_collected": 1,
        "collection_failures": [], "github": {},
        "reports": [{"nodeid": "t.py::test_a", "phase": "call", "outcome": outcome,
                     "error_type": error_type}]}), encoding="utf-8")


def test_published_evidence_names_failed_killed_and_silent_passes(tmp_path):
    root = tmp_path / "ci-evidence"
    _final_projection(root / "parallel", "failed", "KeyError")
    (root / "serial").mkdir()
    (root / "serial" / "events.jsonl").write_text(
        json.dumps({"event": "start", "nodeid": "t.py::test_b"}) + "\n", encoding="utf-8")
    errors, summary = _publish(tmp_path, "parallel=failure serial=failure size=failure idle=skipped")
    assert errors == [
        "::error title=No test evidence::The size pass failed before any test reported; "
        "read that step's log.",
        "::error title=Failed test::t.py::test_a (call, KeyError)",
        "::error title=Test in flight when the session was killed::t.py::test_b",
    ]
    for label in PASSES:
        assert f"## CI test evidence — {label} pass" in summary
    assert "idle pass" not in summary  # A skipped producer ran nothing and reports nothing.
    assert "| t.py::test_a | call | KeyError |" in summary
    assert "| t.py::test_b |" in summary


def test_published_evidence_is_quiet_for_green_passes(tmp_path):
    _final_projection(tmp_path / "ci-evidence" / "parallel", "passed")
    errors, summary = _publish(tmp_path, "parallel=success serial=skipped size=skipped")
    assert errors == []
    assert "## CI test evidence — parallel pass" in summary and "passed=1, failed=0" in summary
    assert "serial pass" not in summary and "diagnostics_incomplete" not in summary
