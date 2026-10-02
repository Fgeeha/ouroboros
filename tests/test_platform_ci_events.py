"""Ordinary desktop PR coverage at the existing CI matrix/event seam."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))


def _value(expression, *, event, ref, base="", schedule="", cancelled=False, attempt=1):
    """Evaluate the workflow's small expression vocabulary against real event shapes."""
    expression = expression.strip()
    if expression.startswith("$" + "{{"):
        expression = expression[3:-2]
    expression = " ".join(expression.split()).replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", "not ", expression)
    github = SimpleNamespace(
        event_name=event, ref=ref, base_ref=base, run_id=77, run_attempt=attempt,
        event=SimpleNamespace(
            schedule=schedule, inputs=SimpleNamespace(e2e_live="false"), before="previous-tip",
            pull_request=SimpleNamespace(number=42, base=SimpleNamespace(sha="pr-base")),
        ),
    )
    return eval(expression, {"__builtins__": {}}, {
        "github": github, "fromJSON": json.loads,
        "startsWith": lambda value, prefix: value.startswith(prefix),
        "always": lambda: True, "cancelled": lambda: cancelled,
        "format": lambda template, *values: template.format(*values),
    })


@pytest.mark.parametrize("event,ref,base,quick,platforms", [
    ("pull_request", "refs/pull/42/merge", "ouroboros", True, ["windows-latest", "macos-latest"]),
    ("pull_request", "refs/pull/42/merge", "main", False, []),
    # A landed commit gets the same two desktop systems a pull request gets; Ubuntu is quick-test.
    ("push", "refs/heads/ouroboros", "", True, ["windows-latest", "macos-latest"]),
    ("push", "refs/heads/main", "", False, []),
    ("push", "refs/heads/ouroboros-stable", "", False, ["ubuntu-latest", "windows-latest", "macos-latest"]),
    ("push", "refs/tags/v7.0.0", "", False, ["ubuntu-latest", "windows-latest", "macos-latest"]),
    ("workflow_dispatch", "refs/heads/candidate", "", True, ["ubuntu-latest", "windows-latest", "macos-latest"]),
    ("schedule", "refs/heads/main", "", False, []),
])
def test_ordinary_matrix_covers_prs_and_landed_pushes_and_keeps_other_events(event, ref, base, quick, platforms):
    facts = {"event": event, "ref": ref, "base": base}
    assert bool(_value(WORKFLOW["jobs"]["quick-test"]["if"], **facts)) is quick
    job = WORKFLOW["jobs"]["full-test"]
    admitted = _value(job["if"], **facts)
    actual = _value(job["strategy"]["matrix"]["os"], **facts) if admitted else []
    assert actual == platforms
    assert job["strategy"]["fail-fast"] is False
    assert job["runs-on"] == "$" + "{{ matrix.os }}"


def test_desktop_pr_matrix_keeps_merge_checkout_and_pr_base_evidence_secret_free():
    triggers = WORKFLOW.get("on") or WORKFLOW[True]  # PyYAML's YAML 1.1 spelling of "on".
    assert triggers["pull_request"]["branches"] == ["ouroboros"]
    assert "pull_request_target" not in triggers
    assert WORKFLOW["permissions"] == {"contents": "read"}
    job = WORKFLOW["jobs"]["full-test"]
    assert "secrets." not in json.dumps(job)
    assert not job.get("permissions")
    checkout = job["steps"][0]
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"] == {"fetch-depth": 0}  # Default PR checkout tests the merge ref.
    base = next(step["env"]["OURO_SIZE_RATCHET_BASE_REF"] for step in job["steps"]
                if "OURO_SIZE_RATCHET_BASE_REF" in step.get("env", {}))
    assert _value(base, event="pull_request", ref="refs/pull/42/merge", base="ouroboros") == "pr-base"
    assert _value(base, event="push", ref="refs/heads/ouroboros-stable") == "previous-tip"


@pytest.mark.parametrize("cron", ["37 4 * * *", "17 3 * * *"])
def test_scheduled_main_runs_do_not_enter_the_ordinary_matrix(cron):
    for name in ("quick-test", "full-test"):
        assert not _value(
            WORKFLOW["jobs"][name]["if"], event="schedule", ref="refs/heads/main", schedule=cron,
        )


@pytest.mark.parametrize("name", [
    "integration-test", "skill-smoke", "system-e2e-mock", "e2e-live",
    "marker-guards", "docker-ui-smoke", "docker-portable-test",
    "release-preflight", "build", "release", "vendor-package-smoke",
])
def test_desktop_pr_coverage_does_not_admit_provider_or_release_jobs(name):
    job = WORKFLOW["jobs"][name]
    assert not _value(job["if"], event="pull_request", ref="refs/pull/42/merge", base="ouroboros")


@pytest.mark.parametrize("event,ref,schedule,called", [
    ("push", "refs/heads/main", "", False),
    ("push", "refs/heads/ouroboros", "", False),
    ("push", "refs/heads/ouroboros-stable", "", False),
    ("push", "refs/tags/v7.0.0", "", True),
    ("workflow_dispatch", "refs/heads/candidate", "", True),
    ("pull_request", "refs/pull/42/merge", "", False),
    ("schedule", "refs/heads/main", "37 4 * * *", False),
    ("schedule", "refs/heads/main", "17 3 * * *", False),
])
def test_provider_canaries_join_this_workflow_only_for_manual_runs_and_tags(event, ref, schedule, called):
    """Branch pushes reach the canaries through provider-canary-push.yml, so a
    provider outage on a landed commit leaves this workflow's result to the code."""
    job = WORKFLOW["jobs"]["integration-test"]
    assert job["uses"] == "./.github/workflows/provider-canary.yml"
    assert bool(_value(job["if"], event=event, ref=ref, base="ouroboros", schedule=schedule)) is called


@pytest.mark.parametrize(("event", "cancelled", "expected"), [
    ("pull_request", False, True), ("pull_request", True, False),
    ("schedule", False, False), ("schedule", True, False),
])
def test_status_checks_and_inequality_keep_independent_meanings(event, cancelled, expected):
    assert _value(
        "${{ always() && !cancelled() && github.event_name != 'schedule' }}",
        event=event, ref="refs/heads/candidate", cancelled=cancelled,
    ) is expected


@pytest.mark.parametrize("event,attempt,group,cancels", [
    ("pull_request", 1, "ci-pr-42", True),
    # GitHub keeps one pending run per group: a re-run of an old head must not share the new head's.
    ("pull_request", 2, "ci-run-77-2", False),
    ("push", 1, "ci-run-77-1", False),
    ("schedule", 1, "ci-run-77-1", False),
    ("workflow_dispatch", 1, "ci-run-77-1", False),
])
def test_only_a_new_pull_request_head_cancels_a_run(event, attempt, group, cancels):
    concurrency = WORKFLOW["concurrency"]
    facts = {"event": event, "ref": "refs/heads/candidate", "attempt": attempt}
    assert _value(concurrency["group"], **facts) == group
    assert bool(_value(concurrency["cancel-in-progress"], **facts)) is cancels
