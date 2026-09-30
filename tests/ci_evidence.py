"""Opt-in, safe CI result facts; raw JUnit is deliberately never read or uploaded.

Registered after tests/conftest.py establishes isolation. The summary entrypoint
uses only stdlib and trusts the supplied Actions producer outcome, not case totals.
"""
from __future__ import annotations

import argparse
from collections import Counter
import html
import json
import math
import os
from pathlib import Path
import platform
import tempfile


def output_dir(config) -> Path | None:
    value = getattr(getattr(config, "option", None), "ci_evidence_dir", None)
    return Path(value).resolve() if value else None


def write_json(path: Path, value) -> None:
    """Write an already approved public projection, never a private source blob."""
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cell(value) -> str:
    return html.escape(str(value), quote=True).replace("|", "&#124;").replace("\n", "<br>")


def _case_outcomes(reports: list[dict]) -> dict[str, str]:
    cases = {}
    for row in reports:
        node = row["nodeid"]
        status = row["outcome"]
        if status == "failed":
            cases[node] = "failed"
        elif cases.get(node) != "failed":
            if status == "skipped":
                cases[node] = "skipped"
            elif row["phase"] == "call":
                if cases.get(node) != "skipped":
                    cases[node] = "passed"
            else:
                cases.setdefault(node, "not_run")
    return cases


def render_summary(root: Path, *, producer_outcome: str, artifact_outcome: str,
                   artifact_url: str = "", scope: str = "full") -> str:
    """Reporter failure cannot replace producer truth; missing reports stay unknown."""
    available = False
    error = ""
    try:
        data = json.loads((root / "results.json").read_text(encoding="utf-8"))
        reports = data.get("reports") if isinstance(data, dict) else None
        available = isinstance(reports, list) and all(
            isinstance(row, dict) and all(isinstance(row.get(key), str)
                                         for key in ("nodeid", "phase", "outcome"))
            and row["phase"] in {"setup", "call", "teardown"}
            and row["outcome"] in {"passed", "failed", "skipped"}
            for row in reports)
        if not available:
            error = "invalid result projection"
            data = {}
    except (OSError, ValueError):
        data, error = {}, "result projection unavailable"
    lines = [
        "## CI test evidence" + (" — PARTIAL DIAGNOSTIC" if scope != "full" else ""),
        "",
        f"Producer step: **{_cell(producer_outcome)}**. Selection: **{_cell(scope)}**.",
        "Testcase results and diagnostic availability are separate from that process outcome.",
        "",
    ]
    if available:
        identity = data.get("github", {})
        if identity:
            lines.append(f"Commit: `{_cell(identity.get('sha', 'unknown'))}`; "
                         f"run {_cell(identity.get('run_id', 'unknown'))}, "
                         f"attempt {_cell(identity.get('run_attempt', 'unknown'))}.")
        counts = Counter(_case_outcomes(data["reports"]).values())
        lines.append("Cases: " + ", ".join(f"{name}={counts[name]}" for name in
                                         ("passed", "failed", "skipped", "not_run")) + ".")
        lines.append(f"Observed pytest session exit: {_cell(data.get('session_exit_code', 'unknown'))}.")
        failed = [row for row in data["reports"] if row["outcome"] == "failed"]
        if failed:
            lines.extend(["", "| Test | Phase | Error type |", "| --- | --- | --- |"])
            lines.extend(f"| {_cell(row['nodeid'])} | {_cell(row['phase'])} | "
                         f"{_cell(row.get('error_type') or 'see test step')} |" for row in failed)
        if data.get("collection_failures"):
            lines.append(f"Collection failures: {len(data['collection_failures'])}.")
    else:
        lines.append("Case outcomes: **unknown**. No passing result is inferred.")
    providers = []
    for path in sorted(root.glob("provider-*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(row, dict):
                raise ValueError("invalid provider projection")
            providers.append(row)
        except (OSError, ValueError):
            error = "a provider projection is unreadable"
    expected_providers = {row["canary_id"] for row in data.get("reports", [])
                          if row.get("canary_id")}
    recorded_providers = {row.get("canary_id") for row in providers}
    if expected_providers - recorded_providers:
        error = "provider result projections are missing"
    if any(row.get("diagnostics_errors") for row in providers):
        error = "provider evidence contains recorded capture gaps"
    for path in sorted(root.glob("browser/*/evidence.json")):
        try:
            browser = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(browser, dict):
                raise ValueError("invalid browser projection")
            if browser.get("diagnostics_incomplete"):
                error = "browser evidence contains recorded capture gaps"
        except (OSError, ValueError):
            error = "a browser projection is unreadable"
    if providers:
        lines.extend(["", "| Provider check | Outcome | Recorded cause |",
                      "| --- | --- | --- |"])
        for row in providers:
            lines.append(f"| {_cell(row.get('canary_id', row.get('nodeid', 'unknown')))} | "
                         f"{_cell(row.get('outcome', 'unknown'))} | "
                         f"{_cell(row.get('violation') or row.get('classification') or 'none recorded')} |")
    if artifact_url.startswith("https://"):
        lines.extend(["", f"[Download safe evidence]({artifact_url})"])
    incomplete = error or artifact_outcome != "success" or not artifact_url
    if incomplete:
        lines.extend(["", "**diagnostics_incomplete**: " +
                      _cell(error or f"artifact outcome={artifact_outcome}; artifact URL available={bool(artifact_url)}") +
                      ". The original producer outcome above is unchanged."])
    lines.extend(["", "Raw JUnit, exception bodies, credentials and private runtime stores are not in this export."])
    return "\n".join(lines) + "\n"


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["summarize"])
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--producer-outcome", required=True)
    parser.add_argument("--artifact-outcome", required=True)
    parser.add_argument("--artifact-url", default="")
    parser.add_argument("--scope", default="full")
    args = parser.parse_args(argv)
    text = render_summary(args.evidence_dir, producer_outcome=args.producer_outcome,
                          artifact_outcome=args.artifact_outcome, artifact_url=args.artifact_url,
                          scope=args.scope)
    with args.summary.open("a", encoding="utf-8") as handle:
        handle.write(text)
    if "diagnostics_incomplete" in text:
        print("::warning::CI diagnostics are incomplete; original test outcome is unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

# pytest is intentionally below the stdlib-only CLI entrypoint and loaded only
# through conftest's post-isolation plugin registration.
import pytest


def pytest_addoption(parser):
    parser.addoption("--ci-evidence-dir", default=None,
                     help="Dedicated public projection directory; raw JUnit must be elsewhere.")


def pytest_configure(config):
    if output_dir(config) is not None:
        config.pluginmanager.register(_Results(config), "ci_safe_results")


class _Results:
    def __init__(self, config):
        from ouroboros.secret_masking import MASKED_SECRET_SETTING_KEYS

        self.config = config
        self.reports = []
        self.collection_failures = []
        self.secrets = tuple(os.environ[key] for key in MASKED_SECRET_SETTING_KEYS if os.environ.get(key))

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        # The exception TYPE is useful; repr/str/longrepr can echo credentials.
        report.ci_error_type = type(call.excinfo.value).__name__ if call.excinfo else ""
        canary = getattr(getattr(item, "callspec", None), "params", {}).get("canary")
        report.ci_canary_id = getattr(canary, "canary_id", "")

    def pytest_runtest_logreport(self, report):
        duration = float(getattr(report, "duration", 0.0))
        self.reports.append({
            "nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome,
            "duration_seconds": duration if math.isfinite(duration) else None,
            "error_type": getattr(report, "ci_error_type", ""),
            "canary_id": getattr(report, "ci_canary_id", ""),
        })

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_failures.append({"nodeid": report.nodeid, "outcome": "failed"})

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
        yield  # Read the final session status after ordinary guards have run.
        if hasattr(self.config, "workerinput"):
            return  # xdist's controller receives the worker reports.
        try:
            from ouroboros.observability import redact_projection
            from ouroboros.secret_masking import redact_known_values

            facts = {
                "format": 1, "session_exit_code": int(session.exitstatus),
                "tests_collected": session.testscollected, "reports": self.reports,
                "collection_failures": self.collection_failures,
                "environment": {"python": platform.python_version(), "platform": platform.system(),
                                "pytest": pytest.__version__},
                "raw_exception_text": "omitted",
                "raw_junit": "private_not_copied",
                "github": {name: os.environ.get("GITHUB_" + name.upper(), "")
                           for name in ("sha", "run_id", "run_attempt")},
            }
            safe = redact_projection(redact_known_values(facts, self.secrets)).value
            write_json(output_dir(self.config) / "results.json", safe)
        except Exception as error:
            # Diagnostics alone never alter session.exitstatus or expose a body.
            print(f"CI_DIAGNOSTICS_INCOMPLETE: safe result export failed ({type(error).__name__})")
