"""Reader of one commit's GitHub checks for ``get_github_checks``: facts and unavailable sources, never a verdict.

Not a tool module: ``github.py`` registers the tool and owns the transport this reader calls.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from typing import List

from ouroboros.tool_capabilities import tool_result_limit
from ouroboros.tools.github import GhResult, _gh_run, _refuse
from ouroboros.tools.registry import ToolContext
from ouroboros.utils import utc_now_iso

# GitHub's states in report order; the quiet ones carry no failed step.
_CHECK_STATES = ("success", "failure", "cancelled", "timed_out", "startup_failure", "action_required", "stale",
                 "skipped", "neutral", "queued", "in_progress", "waiting", "requested", "pending")
_CHECKS_QUIET = ("success", "skipped", "neutral")
# Runs are listed and expanded in this order: failure-class states, unfinished ones, the rest, success last.
_CHECKS_RANK = {state: rank for rank, state in enumerate(
    ("failure", "timed_out", "startup_failure", "action_required", "cancelled", *_CHECK_STATES[9:]))}
# The wait cap plus the read budget stays under the ToolEntry default of 360 s.
_CHECKS_WAIT_CAP_SEC, _CHECKS_READ_BUDGET_SEC, _CHECKS_POLL_SEC = 240, 90, 15
_CHECKS_RUN_LIMIT, _CHECKS_RUN_LINES, _CHECKS_EXPANDED_RUNS = 100, 20, 8
_CHECKS_JOB_LINES, _CHECKS_ANNOTATED_JOBS, _CHECKS_OTHER_LINES = 10, 10, 12
_GH_REPO_URL_RE = re.compile(r"^https://([^/]+)/([^/]+)/([^/]+)/")
_GH_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _check_state(item: dict) -> str:
    """GitHub's own word for a run, job, step or check: the conclusion once completed, else the status."""
    status = str(item.get("status") or item.get("state") or "").lower()
    if status != "completed":
        return status or "unknown"
    return str(item.get("conclusion") or "").lower() or "completed"


def _state_counts(items: List[dict]) -> str:
    counts = Counter(_check_state(item) for item in items)
    order = [state for state in _CHECK_STATES if state in counts] + sorted(set(counts) - set(_CHECK_STATES))
    return ", ".join(f"{state} {counts[state]}" for state in order)


def _one_line(value: object, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _gh_json(res: GhResult, kind: type):
    """The parsed answer of a successful gh call when it has the expected container type, else None."""
    try:
        data = json.loads(res.text) if res.ok else None
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, kind) else None


def _gh_failure(res: GhResult) -> str:
    """Error class of an unavailable source: gh's HTTP status when it printed one, else how the call ended."""
    kind = f"HTTP {res.http_status}" if res.http_status else res.failure or "unparseable answer"
    return f"{kind}: {_one_line(res.text.partition(': ')[2], 160)}" if res.failure == "exit" else kind


def _checks_job_lines(jobs: List[dict], annotations, counts_only: bool) -> List[str]:
    """Detail lines of one run: job counts, then its failed, unfinished and cancelled jobs."""
    if not jobs:
        return ["    jobs: 0 — GitHub lists no job for this run"]
    lines = [f"    jobs: {len(jobs)} — {_state_counts(jobs)}"]
    loud = [] if counts_only else sorted(
        (job for job in jobs if _check_state(job) not in _CHECKS_QUIET),
        key=lambda job: (_check_state(job) == "cancelled", job.get("status") != "completed"))
    for job in loud[:_CHECKS_JOB_LINES]:
        state = _check_state(job)
        lines.append(f"    - job {_one_line(job.get('name'))} [{job.get('databaseId')}]: {state} {job.get('url') or ''}".rstrip())
        steps = [step for step in job.get("steps") or [] if isinstance(step, dict) and state != "cancelled"
                 and _check_state(step) not in (*_CHECKS_QUIET, "pending", "queued", "unknown")]
        lines += [f"        step {step.get('number')} {_one_line(step.get('name'))}: {_check_state(step)}" for step in steps[:3]]
        if len(steps) > 3:
            lines.append(f"        {len(steps) - 3} more steps of this job: {_state_counts(steps[3:])}")
        if job.get("status") == "completed" and state != "cancelled":
            lines += ["        " + line for line in annotations(job)]
    if len(loud) > _CHECKS_JOB_LINES:
        lines.append(f"    {len(loud) - _CHECKS_JOB_LINES} more jobs not shown: {_state_counts(loud[_CHECKS_JOB_LINES:])}")
    return lines


def _rollup_lines(title: str, checks: List[dict]) -> List[str]:
    """A bounded list of pull request rollup entries under their state counts; nothing for an empty list."""
    lines = [f"{title} ({len(checks)}) — {_state_counts(checks)}:"] if checks else []
    for check in checks[:_CHECKS_OTHER_LINES]:
        kind = "commit status" if check.get("__typename") == "StatusContext" else "job" if check.get("workflowName") else "check run"
        name = check.get("context") or " / ".join(filter(None, (check.get("workflowName"), check.get("name"))))
        lines.append(f"- {kind} {_one_line(name, 80)}: {_check_state(check)} "
                     f"{_one_line(check.get('targetUrl') or check.get('detailsUrl'), 200)}".rstrip())
    if len(checks) > _CHECKS_OTHER_LINES:
        lines.append(f"- {len(checks) - _CHECKS_OTHER_LINES} more of these: {_state_counts(checks[_CHECKS_OTHER_LINES:])}")
    return lines


def _rollup_report(checks: object, runs: List[dict]) -> tuple:
    """Header facts and closing lists of a pull request's check rollup: its Actions jobs and the other checks."""
    rollup = [check for check in checks or [] if isinstance(check, dict)]
    actions = [check for check in rollup if check.get("__typename") == "CheckRun" and check.get("workflowName")]
    others = [check for check in rollup if not (check.get("__typename") == "CheckRun" and check.get("workflowName"))]
    listed = {str(run.get("databaseId")) for run in runs if _check_state(run) != "success"}
    # A job in a failure or unfinished state that no run listed as not success holds is shown by itself.
    stray = [job for job in actions if _check_state(job) not in _CHECKS_QUIET
             and "".join(_GH_RUN_ID_RE.findall(str(job.get("detailsUrl") or ""))[:1]) not in listed]
    where = "in a run absent from the run list or listed as success"
    facts = [f"Pull request rollup, GitHub Actions jobs: {len(actions)} — {_state_counts(actions) or 'no entry'}"
             + (f"; {len(stray)} of them {where}" if stray else "")]
    if others:
        facts.append(f"Other checks, outside GitHub Actions: {len(others)} — {_state_counts(others)}")
    return facts, _rollup_lines(f"GitHub Actions jobs of the rollup {where}", stray) + (
        _rollup_lines("Other checks, outside GitHub Actions", others) or
        [f"Other checks: none — every entry of the pull request rollup ({len(rollup)}) is a job of a GitHub Actions workflow."])


def _render_checks(lines: List[str], runs: List[dict], details: dict, log_hint: dict, tail: List[str]) -> str:
    """Header, then one spine line per run with its id, then the rollup lists; these always appear.

    The detail lines of expanded runs are admitted while the result bound has room."""
    blocks: List[tuple] = []
    spare, done = _CHECKS_RUN_LINES, [run for run in runs if _check_state(run) == "success"]
    for title, group in (("Runs not completed with success", [run for run in runs if _check_state(run) != "success"]),
                         ("Runs completed with success", done)):
        if not group:
            continue
        blocks.append((f"\n{title} ({len(group)}):", []))
        for run in group[:spare]:
            detail = details.get(run.get("databaseId"), [])
            spine = (f"- {_one_line(run.get('workflowName'), 80)} ({run.get('event')}) run {run.get('databaseId')} "
                     f"attempt {run.get('attempt')}: {_check_state(run)} {run.get('url') or ''}").rstrip()
            spine += log_hint.get(run.get("databaseId"), "")
            if detail is None:
                spine += f"\n    jobs: not read (one call expands {_CHECKS_EXPANDED_RUNS} runs)"
            blocks.append((spine, detail or []))
        if len(group) > spare:
            blocks.append((f"- {len(group) - spare} more runs — {_state_counts(group[spare:])}; ids: "
                           + ", ".join(str(run.get("databaseId")) for run in group[spare:]), []))
        spare = max(0, spare - len(group))
    cut = "    {} more detail lines of this run are not shown: the result bound is reached"
    room = (tool_result_limit("get_github_checks") - 200 - sum(len(line) + 1 for line in lines + tail)
            - sum(len(spine) + 1 + (len(cut) + 3 if detail else 0) for spine, detail in blocks))
    for spine, detail in blocks:
        lines.append(spine)
        for index, line in enumerate(detail):
            if len(line) + 1 > room:
                lines.append(cut.format(len(detail) - index))
                break
            room -= len(line) + 1
            lines.append(line)
    return "\n".join(lines + [""] + tail)


def get_checks(ctx: ToolContext, number: int = 0, sha: str = "", wait_seconds: int = 0, repo: str = "") -> str:
    """Report what GitHub records about one commit's checks: facts and unavailable sources, never a verdict."""
    number, sha = int(number or 0), str(sha or "").strip().lower()
    if number < 0 or (number > 0) == bool(sha):
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: pass exactly one target: number (a pull request) or sha "
                            "(a full 40-hex commit SHA).", no_effect=True)
    if sha and not _FULL_SHA_RE.fullmatch(sha):
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: sha must be a full 40-hex commit SHA; pass a pull request number, "
                            "or resolve a branch or tag with `git rev-parse <ref>` first.", no_effect=True)
    started = time.monotonic()
    wait = max(0, min(int(wait_seconds or 0), _CHECKS_WAIT_CAP_SEC))
    deadline = started + wait + _CHECKS_READ_BUDGET_SEC  # The one bound of every request and every sleep.
    unavailable, job_failures, annotation_failures, annotation_reads = [], [], [], []  # Lists of str.

    def call(args: List[str], bound: bool = True) -> GhResult:
        left = int(deadline - time.monotonic())
        if left < 1:
            return GhResult(False, "⚠️ GH_TIMEOUT: the deadline of this call was reached before the request.", None, None, "deadline")
        return _gh_run(args, ctx, timeout=min(30, left), **({"repo": repo} if bound else {}))

    def read_pr(rollup: bool) -> GhResult:
        fields = "headRefOid,url,isCrossRepository"
        res = call(["pr", "view", str(number), "--json", fields + ",statusCheckRollup"]) if rollup else None
        if res is None or res.failure in ("exit", "timeout"):  # GitHub can refuse the rollup alone.
            if res is not None:
                unavailable.append(f"pull request check rollup ({_gh_failure(res)})")
            res = call(["pr", "view", str(number), "--json", fields])
        return res

    def annotations(job: dict) -> List[str]:
        # The one literal API path: host, owner and repository come from the URL GitHub returned.
        where, job_id = _GH_REPO_URL_RE.match(str(job.get("url") or "")), str(job.get("databaseId"))
        if not where or not job_id.isdigit():
            return ["annotations: not read (GitHub returned no job URL)"]
        if len(annotation_reads) >= _CHECKS_ANNOTATED_JOBS:
            return [f"annotations: not read (one call reads the annotations of {_CHECKS_ANNOTATED_JOBS} jobs)"]
        annotation_reads.append(job_id)
        host, owner, name = where.groups()
        res = call(["api", f"repos/{owner}/{name}/check-runs/{job_id}/annotations?per_page=100", "--hostname", host], bound=False)
        rows = _gh_json(res, list)
        if rows is None:
            annotation_failures.append(_gh_failure(res))
            return [f"annotations: unavailable ({annotation_failures[-1]})"]
        failures = [row for row in rows if isinstance(row, dict) and row.get("annotation_level") == "failure"]
        return [f"annotation failure{' [' + _one_line(row.get('title'), 80) + ']' if row.get('title') else ''}: "
                f"{_one_line(row.get('message'), 300)}" for row in failures[:5]] + [
            f"annotations: {len(failures)} at failure level, {min(len(failures), 5)} shown; {len(rows)} of all levels read"]

    pr: dict = {}
    if number:
        res = read_pr(rollup=not wait)
        if not res.ok:
            return res.text
        pr = _gh_json(res, dict) or {}
        sha = str(pr.get("headRefOid") or "").lower()
        if not _FULL_SHA_RE.fullmatch(sha):
            return _refuse(ctx, f"⚠️ TOOL_ERROR: GitHub returned no head commit for pull request #{number}.", "TOOL_ERROR")
    while True:  # Completion is judged on the runs of the fixed SHA, not on the jobs listed so far.
        res = call(["run", "list", "--commit", sha, "--limit", str(_CHECKS_RUN_LIMIT), "--json",
                    "databaseId,workflowName,event,status,conclusion,attempt,url,headSha"])
        if not res.ok:
            return res.text
        rows = _gh_json(res, list)
        if rows is None:
            return _refuse(ctx, f"⚠️ TOOL_ERROR: failed to parse workflow runs JSON: {res.text[:500]}", "TOOL_ERROR")
        runs = sorted((run for run in rows if isinstance(run, dict) and str(run.get("headSha") or sha).lower() == sha),
                      key=lambda run: _CHECKS_RANK.get(_check_state(run), len(_CHECKS_RANK) + (_check_state(run) == "success")))
        left = started + wait - time.monotonic()
        if left <= 0 or (runs and all(run.get("status") == "completed" for run in runs)):
            break
        time.sleep(min(_CHECKS_POLL_SEC, left))
    waited = min(int(time.monotonic() - started), wait)  # The time of the last request is not waiting.
    head_note = "head as read by this call"
    if number and wait:
        res = read_pr(rollup=True)
        latest = _gh_json(res, dict) or {}
        head_now = str(latest.get("headRefOid") or "").lower()
        if not head_now:
            unavailable.append(f"pull request head after the wait ({_gh_failure(res)})")
            head_note = "head after the wait not read"
        elif head_now == sha:
            head_note = "head unchanged after the wait"
            pr.update({key: latest[key] for key in ("statusCheckRollup",) if key in latest})
        else:
            head_note = f"head moved to {head_now} during the wait; this report is for {sha}"
            if "statusCheckRollup" in latest:
                unavailable.append("pull request check rollup (GitHub's rollup describes the moved head)")

    where = _GH_REPO_URL_RE.match(str((runs[0].get("url") if runs else "") or pr.get("url") or ""))
    repository = "/".join(where.groups()) if where else (repo or "resolved by the GitHub CLI from the Project directory")
    slug = ("/".join(where.groups()[1:]) if where.group(1) == "github.com" else repository) if where else repo
    # Without the rollup's job counts a run GitHub calls success is read for its own job counts.
    wanted = [run for run in runs if _check_state(run) != "success" or "statusCheckRollup" not in pr]
    details: dict = {run.get("databaseId"): None for run in wanted[_CHECKS_EXPANDED_RUNS:]}  # None: jobs not read.
    log_failed = {run.get("databaseId") for run in runs
                  if run.get("status") == "completed" and _check_state(run) not in (*_CHECKS_QUIET, "cancelled")}
    for run in wanted[:_CHECKS_EXPANDED_RUNS]:
        res = call(["run", "view", str(run.get("databaseId")), "--json", "jobs"])
        data = _gh_json(res, dict)
        if data is None:
            job_failures.append(_gh_failure(res))
            details[run.get("databaseId")] = [f"    jobs: unavailable ({job_failures[-1]})"]
            continue
        jobs = [job for job in data.get("jobs") or [] if isinstance(job, dict)]
        details[run.get("databaseId")] = _checks_job_lines(jobs, annotations, _check_state(run) == "success")
        if run.get("status") == "completed" and any(_check_state(job) in ("failure", "timed_out") for job in jobs):
            log_failed.add(run.get("databaseId"))  # A cancelled run can hold a failed job and its log.
    if job_failures:
        unavailable.append(f"jobs (runs: {len(job_failures)}; {job_failures[0]})")
    if annotation_failures:
        unavailable.append(f"annotations (jobs: {len(annotation_failures)}; {annotation_failures[0]})")
    over = len(wanted) - _CHECKS_EXPANDED_RUNS
    read = ["workflow runs", f"jobs (runs: {min(len(wanted), _CHECKS_EXPANDED_RUNS) - len(job_failures)}" + (
                f"; jobs not read for {over} runs: one call reads the jobs of {_CHECKS_EXPANDED_RUNS} runs" if over > 0 else "") + ")",
            f"annotations (jobs: {len(annotation_reads) - len(annotation_failures)})"]
    lines = [f"GitHub checks for commit {sha}", f"Repository: {repository}"]
    if number:
        lines.append(f"Pull request: #{number} {pr.get('url') or ''}{' (head in a fork)' if pr.get('isCrossRepository') else ''}; {head_note}")
    facts = [f"Workflow runs: {len(runs)} — {_state_counts(runs)}" if runs else
             "Workflow runs: 0 — no workflow run is registered for this commit — this is not a test result"]
    if not runs and not number:
        facts.append("A commit SHA the repository does not hold and a commit with no run yet read the same" + (
            "." if repo else "; the repository is the one the GitHub CLI resolves from the Project directory "
                             "(pass repo='[HOST/]OWNER/REPO' to name it)."))
    if len(rows) >= _CHECKS_RUN_LIMIT:
        facts.append(f"The run list is read up to {_CHECKS_RUN_LIMIT} runs; GitHub may hold more for this commit.")
    tail = ["Other checks: not read — " + (
        "the pull request check rollup is unavailable." if number else
        "a commit SHA target reads GitHub Actions workflow runs only; "
        "third-party checks and commit statuses are read for a pull request number.")]
    if "statusCheckRollup" in pr:
        read.append("pull request check rollup")
        rollup_facts, tail = _rollup_report(pr["statusCheckRollup"], runs)
        facts += rollup_facts
    lines += [f"Observed: {utc_now_iso()}" + (f"; waited {waited}s of {wait}s for the runs to complete" if wait else ""),
              "Sources read: " + "; ".join(read), "Sources unavailable: " + ("; ".join(unavailable) or "none"), *facts]
    hint = "\n    log of the failed steps: gh run view {} --log-failed" + (f" --repo {slug}" if slug else "")
    return _render_checks(lines, runs, details, {run_id: hint.format(run_id) for run_id in log_failed}, tail)
