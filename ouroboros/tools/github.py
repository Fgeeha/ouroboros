"""GitHub tools: issues, pull requests, comments, checks."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional

from ouroboros.secret_masking import redact_known_values
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.tool_result import ToolResult, _publish_tool_result
from ouroboros.tool_capabilities import tool_result_limit
from ouroboros.utils import truncate_within_limit, utc_now_iso
from ouroboros.utils import truncate_review_artifact as _truncate_with_notice

log = logging.getLogger(__name__)
_GENERIC_TRANSPORT = object()


# gh's own HTTP status shapes (see ``_gh_run``); the first match in stderr order wins.
_GH_STATUS_RE = re.compile(
    r"\(HTTP (\d{3})\)[ \t\r]*$"
    r"|^(?:[a-z][a-z ]*: )*HTTP (\d{3})(?::| \(|[ \t\r]*$)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class GhResult:
    ok: bool
    text: str
    exit_code: int | None
    http_status: int | None
    # "target" is a local refusal and "deadline" a request the checks reader never sent;
    # neither is a subprocess exit or exception.
    failure: str


def _refuse(ctx: ToolContext, text: str, code: str = "TOOL_ARG_ERROR") -> str:
    """Publish a refusal this module AUTHORS as a typed result; text unchanged.

    The registry types a string result by its first-line typed marker (the
    warning sign plus an UPPER_SNAKE code), so prose such as ``⚠️ issue number must be positive`` was recorded ``status=ok``
    although the producer already knew it had refused. Both codes carry
    ``status="error"``."""
    return _publish_tool_result(ctx, ToolResult(status="error", code=code, text=text))


def github_token_from_env_or_settings() -> str:
    from ouroboros.config import load_settings
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        try:
            token = load_settings().get("GITHUB_TOKEN", "")
        except Exception:
            token = ""
    return str(token or "").strip()


def _gh_env(ctx: ToolContext) -> dict:
    env = os.environ.copy()
    token = github_token_from_env_or_settings()
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    return env


def github_cli_configured() -> bool:
    """Local credential configuration, not a live authentication assertion."""
    if github_token_from_env_or_settings():
        return True
    config_dir = os.environ.get("GH_CONFIG_DIR", "")
    if not config_dir:
        base = os.environ.get("XDG_CONFIG_HOME", "")
        config_dir = str(pathlib.Path(base) / "gh") if base else ""
    if not config_dir:
        from ouroboros.platform_layer import IS_WINDOWS

        app_data = os.environ.get("APPDATA", "") if IS_WINDOWS else ""
        config_dir = str(pathlib.Path(app_data) / "GitHub CLI") if app_data else str(pathlib.Path.home() / ".config" / "gh")
    try:
        import yaml

        hosts = yaml.safe_load((pathlib.Path(config_dir) / "hosts.yml").read_text(encoding="utf-8"))
        return isinstance(hosts, dict) and any(
            isinstance(host, dict) and bool(host.get("user") or host.get("users") or host.get("oauth_token"))
            for host in hosts.values()
        )
    except (OSError, ValueError, yaml.YAMLError):
        return False


def _gh_run(args: List[str], ctx: ToolContext, timeout: int = 30, input_data: Optional[str] = None,
            *, repo: object = _GENERIC_TRANSPORT) -> GhResult:
    # Only omitted internal API/Hub calls keep the generic transport contract.
    # Public repository tools always pass repo, including '' for Project focus.
    # The target refusals below publish a typed argument error into the calling
    # tool's sidecar; the publication transport omits `repo`, so it can never
    # reach them and its own final result is never shadowed from here.
    if repo is not _GENERIC_TRANSPORT and not isinstance(repo, str):
        return GhResult(False, _publish_tool_result(ctx, ToolResult(
            status="error", code="TOOL_ARG_ERROR",
            text="⚠️ GH_TARGET_INVALID: repo must be a string; omit it to use the selected Project.",
        )), None, None, "target")
    try:
        cwd, env = pathlib.Path(ctx.repo_dir), _gh_env(ctx)
        cmd = ["gh", *args]
        if repo is not _GENERIC_TRANSPORT:
            from ouroboros.tool_access import build_resolved_resource_binding

            metadata = getattr(ctx, "task_metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            workspace = getattr(ctx, "workspace_root", None)
            room_dir = str(metadata.get("_project_room_dir") or "")
            project = str(getattr(ctx, "project_id", "") or "")
            if not repo:
                note = str(metadata.get("_project_room_note") or "")
                selected = workspace or room_dir
                if note or (selected and not pathlib.Path(selected).is_dir()):
                    return GhResult(False,
                        f"⚠️ GH_TARGET_UNAVAILABLE: {note or 'The selected Project directory is unavailable.'}",
                        None, None, "target")
                if project and not selected:
                    return GhResult(False, _publish_tool_result(ctx, ToolResult(
                        status="error", code="TOOL_ARG_ERROR",
                        text="⚠️ GH_TARGET_REQUIRED: this Project has no repository directory; pass repo='[HOST/]OWNER/REPO'.",
                    )), None, None, "target")
            binding = build_resolved_resource_binding(ctx, operation="shell", process_cwd="")
            cwd = binding.target_path
            if workspace and cwd != pathlib.Path(workspace).resolve(strict=False):
                return GhResult(False,
                    "⚠️ GH_TARGET_UNAVAILABLE: the task's Project binding could not be resolved.",
                    None, None, "target")
            if workspace or room_dir or project:
                env.pop("GH_REPO", None)  # Ambient defaults cannot replace the selected Project.
            if repo:
                cmd.extend(["--repo", repo])
        res = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_data,
            env=env,
        )
        if res.returncode != 0:
            # Redact the WHOLE stderr first (a cut could split a token), read gh's own
            # status marker before any bounding, then keep a bounded head. gh writes the
            # status in three deterministic shapes and nowhere else: ``gh: <msg> (HTTP NNN)``
            # at the end of a line (``gh api`` with a message), ``gh: HTTP NNN`` (``gh api``
            # without one) and ``HTTP NNN: <msg> (<url>)`` — optionally wrapped as
            # ``failed to fork: HTTP NNN: …`` — from every other command. A marker quoted
            # mid-sentence is prose, not a status.
            err = redact_known_values(res.stderr or "", [github_token_from_env_or_settings()])
            status = _GH_STATUS_RE.search(err)
            head = " | ".join([line.strip() for line in err.splitlines() if line.strip()][:3])
            head = truncate_within_limit(head, 600)
            # gh's canMerge refusal precedes its mutation (pkg/cmd/pr/merge).
            # HTTP status alone proves nothing about which CLI step failed.
            pre_effect = (args[:2] == ["pr", "merge"] and not res.stdout.strip() and re.fullmatch(
                r"X Pull request [\w.-]+/[\w.-]+#\d+ is not mergeable: "
                r"(?:the base branch policy prohibits the merge|the head branch is not up to date with the base branch)\.\n"
                r"To have the pull request merged after all the requirements have been met, add the `--auto` flag\.\n"
                r"To use administrator privileges to immediately merge the pull request, add the `--admin` flag\.",
                err.strip()) is not None)
            return GhResult(False, "⚠️ GH_ERROR: " + head, res.returncode,
                            int(status.group(1) or status.group(2)) if status else None,
                            "pre_effect" if pre_effect else "exit")
        return GhResult(True, res.stdout.strip(), res.returncode, None, "")
    except FileNotFoundError as e:
        missing = str(getattr(e, "filename", "") or "")
        if not missing or pathlib.Path(missing).name == "gh":
            return GhResult(False,
                "⚠️ GH_ERROR: `gh` CLI not found. Install GitHub CLI and ensure it is on PATH (https://cli.github.com/)",
                None, None, "cli_missing")
        detail = truncate_within_limit(redact_known_values(str(e), [github_token_from_env_or_settings()]), 600)
        return GhResult(False, f"⚠️ GH_ERROR: {detail}", None, None, "exception")
    except subprocess.TimeoutExpired:
        return GhResult(False, f"⚠️ GH_TIMEOUT: exceeded {timeout}s.", None, None, "timeout")
    except Exception as e:
        detail = truncate_within_limit(redact_known_values(str(e), [github_token_from_env_or_settings()]), 600)
        return GhResult(False, f"⚠️ GH_ERROR: {detail}", None, None, "exception")


def _gh_cmd(args: List[str], ctx: ToolContext, timeout: int = 30, input_data: Optional[str] = None,
            *, repo: object = _GENERIC_TRANSPORT) -> str:
    return _gh_run(args, ctx, timeout=timeout, input_data=input_data, repo=repo).text


def _list_issues(ctx: ToolContext, state: str = "open", labels: str = "", limit: int = 20, repo: str = "") -> str:
    args = [
        "issue", "list",
        "--state", state,
        "--limit", str(min(limit, 50)),
        "--json", "number,title,body,labels,createdAt,author,assignees,state",
    ]
    if labels:
        args.extend(["--label", labels])

    raw = _gh_cmd(args, ctx, repo=repo)
    if raw.startswith("⚠️"):
        return raw

    try:
        issues = json.loads(raw)
    except json.JSONDecodeError:
        return _refuse(ctx, f"⚠️ TOOL_ERROR: failed to parse issues JSON: {raw[:500]}", "TOOL_ERROR")

    if not issues:
        return f"No {state} issues found."

    lines = [f"**{len(issues)} {state} issue(s):**\n"]
    for issue in issues:
        labels_str = ", ".join(l.get("name", "") for l in issue.get("labels", []))
        author = issue.get("author", {}).get("login", "unknown")
        lines.append(
            f"- **#{issue['number']}** {issue['title']}"
            f" (by @{author}{', labels: ' + labels_str if labels_str else ''})"
        )
        body = (issue.get("body") or "").strip()
        if body:
            preview = body[:200] + ("..." if len(body) > 200 else "")
            lines.append(f"  > {preview}")

    return "\n".join(lines)


def _get_issue(ctx: ToolContext, number: int, repo: str = "") -> str:
    if number <= 0:
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: issue number must be positive")

    args = [
        "issue", "view", str(number),
        "--json", "number,title,body,labels,createdAt,author,assignees,state,comments",
    ]

    raw = _gh_cmd(args, ctx, repo=repo)
    if raw.startswith("⚠️"):
        return raw

    try:
        issue = json.loads(raw)
    except json.JSONDecodeError:
        return _refuse(ctx, f"⚠️ TOOL_ERROR: failed to parse issue JSON: {raw[:500]}", "TOOL_ERROR")

    labels_str = ", ".join(l.get("name", "") for l in issue.get("labels", []))
    author = issue.get("author", {}).get("login", "unknown")

    lines = [
        f"## Issue #{issue['number']}: {issue['title']}",
        f"**State:** {issue['state']}  |  **Author:** @{author}",
    ]
    if labels_str:
        lines.append(f"**Labels:** {labels_str}")

    body = (issue.get("body") or "").strip()
    if body:
        lines.append(f"\n**Body:**\n{_truncate_with_notice(body, 3000)}")

    comments = issue.get("comments", [])
    if comments:
        shown_comments = comments[:10]
        lines.append(f"\n**Comments (showing {len(shown_comments)} of {len(comments)}):**")
        for c in shown_comments:
            c_author = c.get("author", {}).get("login", "unknown")
            c_body = _truncate_with_notice((c.get("body") or "").strip(), 500)
            lines.append(f"\n@{c_author}:\n{c_body}")

    return "\n".join(lines)


def _comment_on_issue(ctx: ToolContext, number: int, body: str, repo: str = "") -> str:
    if number <= 0:
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: issue number must be positive")

    if not body or not body.strip():
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: comment body cannot be empty.")

    args = ["issue", "comment", str(number), "--body-file", "-"]
    raw = _gh_cmd(args, ctx, input_data=body, repo=repo)
    if raw.startswith("⚠️"):
        return raw
    return f"✅ Comment added to issue #{number}."


def _close_issue(ctx: ToolContext, number: int, comment: str = "", repo: str = "") -> str:
    if number <= 0:
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: issue number must be positive")

    if comment and comment.strip():
        result = _comment_on_issue(ctx, number, comment, repo=repo)
        if result.startswith("⚠️"):
            return result

    args = ["issue", "close", str(number)]
    raw = _gh_cmd(args, ctx, repo=repo)
    if raw.startswith("⚠️"):
        return raw
    return f"✅ Issue #{number} closed."

def _list_prs(ctx: ToolContext, state: str = "open", limit: int = 20, repo: str = "") -> str:
    args = [
        "pr", "list",
        "--state", state,
        "--limit", str(min(limit, 50)),
        "--json", "number,title,author,headRefName,baseRefName,createdAt,isDraft,reviewDecision,commits",
    ]
    raw = _gh_cmd(args, ctx, repo=repo)
    if raw.startswith("⚠️"):
        return raw

    try:
        prs = json.loads(raw)
    except json.JSONDecodeError:
        return _refuse(ctx, f"⚠️ TOOL_ERROR: failed to parse PRs JSON: {raw[:500]}", "TOOL_ERROR")

    if not prs:
        return f"No {state} pull requests found."

    lines = [f"**{len(prs)} {state} PR(s):**\n"]
    for pr in prs:
        author = pr.get("author", {}).get("login", "unknown")
        head = pr.get("headRefName", "?")
        base = pr.get("baseRefName", "?")
        draft = " [DRAFT]" if pr.get("isDraft") else ""
        review = pr.get("reviewDecision") or ""
        review_str = f" [{review}]" if review else ""
        n_commits = len(pr.get("commits", []))
        lines.append(
            f"- **PR #{pr['number']}**{draft}{review_str} {pr['title']}"
            f" (by @{author}, {head}→{base}, {n_commits} commits, created {pr['createdAt'][:10]})"
        )

    return "\n".join(lines)


def _get_pr(ctx: ToolContext, number: int, repo: str = "") -> str:
    if number <= 0:
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: PR number must be positive.")

    meta_args = [
        "pr", "view", str(number),
        "--json", "number,title,body,author,headRefName,baseRefName,headRepository,"
                  "createdAt,updatedAt,state,isDraft,reviewDecision,mergeable,"
                  "additions,deletions,changedFiles,commits,reviews,comments",
    ]
    raw = _gh_cmd(meta_args, ctx, timeout=30, repo=repo)
    if raw.startswith("⚠️"):
        return raw

    try:
        pr = json.loads(raw)
    except json.JSONDecodeError:
        return _refuse(ctx, f"⚠️ TOOL_ERROR: failed to parse PR JSON: {raw[:500]}", "TOOL_ERROR")

    author = pr.get("author", {}).get("login", "unknown")
    head_repo = (pr.get("headRepository") or {}).get("nameWithOwner", "?")

    lines = [
        f"## PR #{pr['number']}: {pr['title']}",
        f"**State:** {pr['state']}  |  **Author:** @{author}",
        f"**Branch:** {head_repo}@{pr.get('headRefName','?')} → {pr.get('baseRefName','?')}",
        f"**Changes:** +{pr.get('additions',0)} / -{pr.get('deletions',0)}"
        f" across {pr.get('changedFiles',0)} file(s)",
        f"**Mergeable:** {pr.get('mergeable', 'unknown')}",
    ]
    if pr.get("isDraft"):
        lines.append("**⚠️ Draft PR**")
    if pr.get("reviewDecision"):
        lines.append(f"**Review decision:** {pr['reviewDecision']}")

    body = (pr.get("body") or "").strip()
    if body:
        lines.append(f"\n**Description:**\n{_truncate_with_notice(body, 2000)}")

    commits = pr.get("commits", [])
    if commits:
        lines.append(
            f"\n**Commits ({len(commits)}) — original author preserved on cherry-pick:**"
        )
        shas_for_pick = []
        for c in commits:
            node = c.get("commit", c)
            sha = c.get("oid", "?")[:12]
            full_sha = c.get("oid", "?")
            msg = (node.get("messageHeadline") or node.get("message") or "?")[:70]
            authored_by = node.get("authors", {})
            if isinstance(authored_by, dict):
                authored_by = authored_by.get("nodes", [])
            if authored_by:
                a = authored_by[0]
                author_str = f"{a.get('name','?')} <{a.get('email','?')}>"
            else:
                author_str = "unknown"
            lines.append(f"  {sha} | {author_str} | {msg}")
            shas_for_pick.append(full_sha)
        lines.append(f"\nCommit SHAs:\n  {shas_for_pick}")

    diff_names_raw = _gh_cmd(["pr", "diff", str(number), "--name-only"], ctx, timeout=30, repo=repo)
    if not diff_names_raw.startswith("⚠️") and diff_names_raw.strip():
        file_list = diff_names_raw.strip().splitlines()
        lines.append(f"\n**Changed files ({len(file_list)}):**")
        for f in file_list[:50]:
            lines.append(f"  {f}")
        if len(file_list) > 50:
            lines.append(f"  ... and {len(file_list) - 50} more")

    diff_raw = _gh_cmd(["pr", "diff", str(number)], ctx, timeout=60, repo=repo)
    if not diff_raw.startswith("⚠️") and diff_raw.strip():
        lines.append("\n**Diff (truncated to 8000 chars):**\n```diff")
        lines.append(_truncate_with_notice(diff_raw, 8000))
        lines.append("```")

    reviews = pr.get("reviews", [])
    comments = pr.get("comments", [])
    if reviews or comments:
        lines.append(f"\n**Reviews ({len(reviews)}) + PR comments ({len(comments)}):**")
        for rv in reviews[:5]:
            rv_author = (rv.get("author") or {}).get("login", "?")
            rv_state = rv.get("state", "?")
            rv_body = _truncate_with_notice((rv.get("body") or "").strip(), 300)
            lines.append(f"  [{rv_state}] @{rv_author}: {rv_body}")
        for cm in comments[:5]:
            cm_author = (cm.get("author") or {}).get("login", "?")
            cm_body = _truncate_with_notice((cm.get("body") or "").strip(), 300)
            lines.append(f"  @{cm_author}: {cm_body}")

    if not (repo or getattr(ctx, "workspace_root", None) or getattr(ctx, "project_id", "")):
        lines.append(
            f"\n**Integration steps:**\n"
            f"  1. fetch_pr_ref(pr_number={number})\n"
            f"  2. create_integration_branch(pr_number={number})\n"
            f"  3. cherry_pick_pr_commits(shas=[...])  # SHAs above; use override_author only for placeholder identities\n"
            f"  4. stage_adaptations()                 # optional; do NOT commit_reviewed on the integration branch\n"
            f"  5. stage_pr_merge(branch='integrate/pr-{number}') → preflight_review → commit_reviewed\n"
            f"  6. comment_on_pr(number={number}, body='Integrated as ...')"
        )

    return "\n".join(lines)


def _comment_on_pr(ctx: ToolContext, number: int, body: str, repo: str = "") -> str:
    if number <= 0:
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: PR number must be positive.")
    if not (body or "").strip():
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: comment body cannot be empty.")

    args = ["pr", "comment", str(number), "--body-file", "-"]
    raw = _gh_cmd(args, ctx, input_data=body, repo=repo)
    if raw.startswith("⚠️"):
        return raw
    return f"✅ Comment added to PR #{number}."


def _pr_merge(ctx: ToolContext, number: int, expected_head_sha: str, method: str,
              review_task_ids: Optional[List[str]] = None, reviewed_head_sha: str = "",
              reviewed_base_sha: str = "", review_scope: str = "full", review_verdict: str = "",
              repo: str = "") -> str:
    """Thin transport binding; the receipt contract lives in ``merge_receipts``."""
    from ouroboros.merge_receipts import REVIEW_SCOPES, _VERDICT_RE, _sha, run_pr_merge
    from ouroboros.tool_access import canonical_data_root

    if review_scope not in REVIEW_SCOPES or (review_verdict and not _VERDICT_RE.fullmatch(review_verdict)):
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: review_scope is full|delta; review_verdict is a short word such as PASS.")
    declared = ({"reviewed_head_sha": _sha(reviewed_head_sha), "reviewed_base_sha": _sha(reviewed_base_sha),
                 "scope": review_scope, "verdict": review_verdict}
                if (reviewed_head_sha or review_verdict or review_task_ids) else None)
    receipt = run_pr_merge(
        ctx, lambda args, **kw: _gh_run(args, ctx, repo=repo, **kw), lambda args, **kw: _gh_run(args, ctx, **kw),
        drive_root=canonical_data_root(ctx), task_id=str(ctx.task_id or ""), number=int(number or 0),
        expected_head_sha=expected_head_sha, method=method,
        review={"declared": declared, "task_ids": list(review_task_ids or [])})
    if receipt.get("refused"):
        code = "TOOL_ARG_ERROR" if receipt["refused"] == "arguments" else "TOOL_ERROR"
        return _refuse(ctx, f"⚠️ PR_MERGE_REFUSED: {receipt['refused']} — {receipt.get('detail', '')}", code)
    from ouroboros.merge_receipts import card_row_text

    status = (receipt.get("outcome") or {}).get("status", "unknown")
    lines = [card_row_text(receipt), f"receipt_id={receipt['receipt_id']} (task result: merge_receipts)"]
    if receipt.get("readback_only"):
        lines.append("An earlier request for this PR had no confirmed outcome, so this call only read GitHub back "
                     "and sent no new merge request. Unknown and queued requests remain observation-only.")
    if receipt.get("republished"):
        lines.append("Receipt publication retried; the merge was not repeated.")
    publication = receipt.get("publication") or {}
    if receipt.get("receipt_write_gap"):
        lines.append("⚠️ Merge receipt persistence is unknown after the external effect: "
                     + receipt["receipt_write_gap"])
    if status in ("merged", "queued") and (publication.get("body") or {}).get("status") != "published":
        lines.append("⚠️ The PR-body receipt block was not confirmed; call pr_merge again to retry publication only.")
    card = publication.get("card") or {}
    if status in ("merged", "queued") and card.get("status") not in ("owed", "delivered"):
        lines.append("⚠️ The task-card receipt is not confirmed: " + str(card.get("reason") or "publication unknown")
                     + "; call pr_merge again for observation/publication only.")
    text = "\n".join(lines)
    if status in ("merged", "queued"):
        return text
    return _refuse(ctx, f"⚠️ PR_MERGE_{status.upper()}: " + text, "TOOL_ERROR")


def _create_issue(ctx: ToolContext, title: str, body: str = "", labels: str = "", repo: str = "") -> str:
    if not title or not title.strip():
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: issue title cannot be empty.")

    args = ["issue", "create", f"--title={title}"]
    if body:
        args.append("--body-file=-")
        raw = _gh_cmd(args, ctx, input_data=body, repo=repo)
    else:
        raw = _gh_cmd(args, ctx, repo=repo)

    if labels:
        if not raw.startswith("⚠️"):
            import re
            match = re.search(r'/issues/(\d+)', raw)
            if match:
                issue_num = int(match.group(1))
                label_args = ["issue", "edit", str(issue_num), f"--add-label={labels}"]
                _gh_cmd(label_args, ctx, repo=repo)

    if raw.startswith("⚠️"):
        return raw
    return f"✅ Issue created: {raw}"


# get_github_checks. GitHub's states in report order; the quiet ones carry no failed step.
_CHECK_STATES = ("success", "failure", "cancelled", "timed_out", "startup_failure", "action_required", "stale",
                 "skipped", "neutral", "queued", "in_progress", "waiting", "requested", "pending")
_CHECKS_QUIET = ("success", "skipped", "neutral")
# The wait cap plus the read budget stays under the ToolEntry default of 360 s.
_CHECKS_WAIT_CAP_SEC, _CHECKS_READ_BUDGET_SEC, _CHECKS_POLL_SEC = 240, 90, 15
_CHECKS_RUN_LIMIT, _CHECKS_RUN_LINES, _CHECKS_EXPANDED_RUNS = 100, 20, 8
_CHECKS_JOB_LINES, _CHECKS_ANNOTATED_JOBS, _CHECKS_OTHER_LINES = 10, 10, 12
_GH_REPO_URL_RE = re.compile(r"^https://([^/]+)/([^/]+)/([^/]+)/")
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


def _checks_job_lines(jobs: List[dict], annotations) -> List[str]:
    """Detail lines of one run: job counts, then its failed, unfinished and cancelled jobs."""
    if not jobs:
        return ["    jobs: 0 — GitHub lists no job for this run"]
    lines = [f"    jobs: {len(jobs)} — {_state_counts(jobs)}"]
    loud = sorted((job for job in jobs if _check_state(job) not in _CHECKS_QUIET),
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


def _other_check_lines(others: Optional[List[dict]], number: int, rollup_size: int) -> List[str]:
    """Checks GitHub Actions does not own: third-party check runs and commit statuses of a pull request."""
    if others is None:
        return ["Other checks: not read — " + (
            "the pull request check rollup is unavailable." if number else
            "a commit SHA target reads GitHub Actions workflow runs only; "
            "third-party checks and commit statuses are read for a pull request number.")]
    if not others:
        return [f"Other checks: none — every entry of the pull request rollup ({rollup_size}) is a job of a GitHub Actions workflow."]
    lines = [f"Other checks, outside GitHub Actions ({len(others)}) — {_state_counts(others)}:"]
    for check in others[:_CHECKS_OTHER_LINES]:
        kind = "commit status" if check.get("__typename") == "StatusContext" else "check run"
        lines.append(f"- {kind} {_one_line(check.get('context') or check.get('name'), 80)}: {_check_state(check)} "
                     f"{_one_line(check.get('targetUrl') or check.get('detailsUrl'), 200)}".rstrip())
    if len(others) > _CHECKS_OTHER_LINES:
        lines.append(f"- {len(others) - _CHECKS_OTHER_LINES} more other checks: {_state_counts(others[_CHECKS_OTHER_LINES:])}")
    return lines


def _render_checks(lines: List[str], runs: List[dict], details: dict, slug: str, tail: List[str]) -> str:
    """Header, then one spine line per run with its id, then other checks; these always appear.

    The detail lines of expanded runs are admitted while the result bound has room."""
    blocks: List[tuple] = []
    spare, done = _CHECKS_RUN_LINES, [run for run in runs if _check_state(run) == "success"]
    for title, group in (("Runs not completed with success", [run for run in runs if _check_state(run) != "success"]),
                         ("Runs completed with success", done)):
        if not group:
            continue
        blocks.append((f"\n{title} ({len(group)}):", []))
        for run in group[:spare]:
            spine = (f"- {_one_line(run.get('workflowName'), 80)} ({run.get('event')}) run {run.get('databaseId')} "
                     f"attempt {run.get('attempt')}: {_check_state(run)} {run.get('url') or ''}").rstrip()
            if run.get("status") == "completed" and _check_state(run) not in (*_CHECKS_QUIET, "cancelled"):
                spine += (f"\n    log of the failed steps: gh run view {run.get('databaseId')} --log-failed"
                          + (f" --repo {slug}" if slug else ""))
            if group is not done and run.get("databaseId") not in details:
                spine += f"\n    jobs: not read (one call expands {_CHECKS_EXPANDED_RUNS} runs)"
            blocks.append((spine, details.get(run.get("databaseId"), [])))
        if len(group) > spare:
            blocks.append((f"- {len(group) - spare} more runs, ids: "
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


def _get_checks(ctx: ToolContext, number: int = 0, sha: str = "", wait_seconds: int = 0, repo: str = "") -> str:
    """Report what GitHub records about one commit's checks: facts and unavailable sources, never a verdict."""
    number, sha = int(number or 0), str(sha or "").strip().lower()
    if number < 0 or (number > 0) == bool(sha):
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: pass exactly one target: number (a pull request) or sha (a full 40-hex commit SHA).")
    if sha and not _FULL_SHA_RE.fullmatch(sha):
        return _refuse(ctx, "⚠️ TOOL_ARG_ERROR: sha must be a full 40-hex commit SHA; pass a pull request number, "
                            "or resolve a branch or tag with `git rev-parse <ref>` first.")
    started = time.monotonic()
    wait = max(0, min(int(wait_seconds or 0), _CHECKS_WAIT_CAP_SEC))
    deadline = started + wait + _CHECKS_READ_BUDGET_SEC  # The one bound of every request and every sleep.
    unavailable: List[str] = []
    job_failures: List[str] = []
    annotation_failures: List[str] = []
    annotation_reads: List[str] = []

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
                    "databaseId,workflowName,event,status,conclusion,attempt,url,headSha,createdAt"])
        if not res.ok:
            return res.text
        runs = _gh_json(res, list)
        if runs is None:
            return _refuse(ctx, f"⚠️ TOOL_ERROR: failed to parse workflow runs JSON: {res.text[:500]}", "TOOL_ERROR")
        runs = sorted((run for run in runs if isinstance(run, dict) and str(run.get("headSha") or sha).lower() == sha),
                      key=lambda run: (_check_state(run) == "success", _check_state(run) in _CHECKS_QUIET))
        left = started + wait - time.monotonic()
        if left <= 0 or (runs and all(run.get("status") == "completed" for run in runs)):
            break
        time.sleep(min(_CHECKS_POLL_SEC, left))
    waited = int(time.monotonic() - started)
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

    open_runs = [run for run in runs if _check_state(run) != "success"]
    details: dict = {}
    for run in open_runs[:_CHECKS_EXPANDED_RUNS]:
        res = call(["run", "view", str(run.get("databaseId")), "--json", "jobs"])
        data = _gh_json(res, dict)
        if data is None:
            job_failures.append(_gh_failure(res))
            details[run.get("databaseId")] = [f"    jobs: unavailable ({job_failures[-1]})"]
        else:
            details[run.get("databaseId")] = _checks_job_lines(
                [job for job in data.get("jobs") or [] if isinstance(job, dict)], annotations)
    if job_failures:
        unavailable.append(f"jobs (runs: {len(job_failures)}; {job_failures[0]})")
    if annotation_failures:
        unavailable.append(f"annotations (jobs: {len(annotation_failures)}; {annotation_failures[0]})")
    read = ["workflow runs", f"jobs (runs: {min(len(open_runs), _CHECKS_EXPANDED_RUNS) - len(job_failures)})",
            f"annotations (jobs: {len(annotation_reads) - len(annotation_failures)})"]
    others = None
    if "statusCheckRollup" in pr:
        read.append("pull request check rollup")
        rollup = [check for check in pr["statusCheckRollup"] or [] if isinstance(check, dict)]
        others = [check for check in rollup if not (check.get("__typename") == "CheckRun" and check.get("workflowName"))]

    where = _GH_REPO_URL_RE.match(str((runs[0].get("url") if runs else "") or pr.get("url") or ""))
    repository = "/".join(where.groups()) if where else (repo or "resolved by the GitHub CLI from the Project directory")
    slug = ("/".join(where.groups()[1:]) if where.group(1) == "github.com" else repository) if where else repo
    lines = [f"GitHub checks for commit {sha}", f"Repository: {repository}"]
    if number:
        lines.append(f"Pull request: #{number} {pr.get('url') or ''}{' (head in a fork)' if pr.get('isCrossRepository') else ''}; {head_note}")
    lines += [f"Observed: {utc_now_iso()}" + (f"; waited {waited}s of {wait}s for the runs to complete" if wait else ""),
              "Sources read: " + "; ".join(read), "Sources unavailable: " + ("; ".join(unavailable) or "none"),
              f"Workflow runs: {len(runs)} — {_state_counts(runs)}" if runs else
              "Workflow runs: 0 — no workflow run is registered for this commit — this is not a test result"]
    if len(runs) >= _CHECKS_RUN_LIMIT:
        lines.append(f"The run list is read up to {_CHECKS_RUN_LIMIT} runs; GitHub may hold more for this commit.")
    return _render_checks(lines, runs, details, slug, _other_check_lines(others, number, len(pr.get("statusCheckRollup") or [])))

def get_tools() -> List[ToolEntry]:
    tools = [
        ToolEntry("list_github_prs", {
            "name": "list_github_prs",
            "description": (
                "List GitHub pull requests for the current repository. "
                "Shows PR number, title, author, branch, commit count, and state. "
                "Use before get_github_pr to identify which PR to inspect."
            ),
            "parameters": {"type": "object", "properties": {
                "state": {"type": "string", "default": "open",
                          "enum": ["open", "closed", "merged", "all"],
                          "description": "Filter by PR state"},
                "limit": {"type": "integer", "default": 20,
                          "description": "Max PRs to return (max 50)"},
            }, "required": []},
        }, _list_prs),

        ToolEntry("get_github_pr", {
            "name": "get_github_pr",
            "description": (
                "Get full details of a GitHub PR: metadata, description, commit list "
                "with original author names/emails, changed files list, diff/patch "
                "(truncated to 8000 chars), review comments, and mergeable state. "
                "Includes exact commit SHAs for the selected repository."
            ),
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "PR number"},
            }, "required": ["number"]},
        }, _get_pr),

        ToolEntry("get_github_checks", {
            "name": "get_github_checks",
            "description": (
                "Read what GitHub records about the checks of one commit: every workflow run with its state, "
                "the failed and unfinished jobs and steps, failure annotations (test names when the workflow "
                "publishes them) and, for a pull request, third-party checks and commit statuses. Read-only: "
                "pushes and dispatches nothing. Reports facts and names each source it could not read; it gives "
                "no verdict, and a workflow that did not start has no record to report."
            ),
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "default": 0,
                           "description": "Pull request number; its head commit is read. Pass exactly one of number / sha."},
                "sha": {"type": "string", "default": "",
                        "description": "Full 40-hex commit SHA (resolve a branch or tag with `git rev-parse <ref>`)."},
                "wait_seconds": {"type": "integer", "default": 0,
                                 "description": "Poll until a workflow run is registered and every registered run is completed, "
                                                "or this many seconds pass (max 240); the report states what is unfinished."},
            }, "required": []},
        }, _get_checks),

        ToolEntry("comment_on_pr", {
            "name": "comment_on_pr",
            "description": (
                "Add a comment to a GitHub pull request. "
                "Use to acknowledge receipt, report integration status, request changes, "
                "or leave an audit trail after integration."
            ),
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "PR number"},
                "body": {"type": "string", "description": "Comment text (markdown)"},
            }, "required": ["number", "body"]},
        }, _comment_on_pr),

        ToolEntry("pr_merge", {
            "name": "pr_merge",
            "description": (
                "Merge a GitHub pull request so a receipt exists: states the exact head you expect "
                "and the method (never auto-merge or admin), records what review you declare beside "
                "what the host observes, reads GitHub back, and writes the receipt to this task's "
                "record, its card and the PR body. A missing review is recorded loudly, never a lock. "
                "An unknown or queued merge stays observation/publication-only on repeat calls; no resend. "
                "Distinct from stage_pr_merge, which stages a local merge for a reviewed commit."
            ),
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "PR number"},
                "expected_head_sha": {"type": "string", "description": "The PR head you intend to merge; GitHub refuses if it moved"},
                "method": {"type": "string", "enum": ["merge", "squash", "rebase"]},
                "review_task_ids": {"type": "array", "items": {"type": "string"}, "default": [],
                                    "description": "Task ids of the reviews you rely on; the host records what it can observe of each"},
                "reviewed_head_sha": {"type": "string", "default": "", "description": "The head those reviews covered (declared)"},
                "reviewed_base_sha": {"type": "string", "default": "", "description": "The base those reviews covered (declared)"},
                "review_scope": {"type": "string", "enum": ["full", "delta"], "default": "full",
                                 "description": "delta = only the change since an earlier review; never counted as whole-PR coverage"},
                "review_verdict": {"type": "string", "default": "", "description": "The declared verdict word, e.g. PASS"},
            }, "required": ["number", "expected_head_sha", "method"]},
        }, _pr_merge),

        ToolEntry("list_github_issues", {
            "name": "list_github_issues",
            "description": "List GitHub issues. Use to check for new tasks, bug reports, or feature requests from the user or contributors.",
            "parameters": {"type": "object", "properties": {
                "state": {"type": "string", "default": "open", "enum": ["open", "closed", "all"], "description": "Filter by state"},
                "labels": {"type": "string", "default": "", "description": "Filter by label (comma-separated)"},
                "limit": {"type": "integer", "default": 20, "description": "Max issues to return (max 50)"},
            }, "required": []},
        }, _list_issues),

        ToolEntry("get_github_issue", {
            "name": "get_github_issue",
            "description": "Get full details of a GitHub issue including body and comments.",
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "Issue number"},
            }, "required": ["number"]},
        }, _get_issue),

        ToolEntry("comment_on_issue", {
            "name": "comment_on_issue",
            "description": "Add a comment to a GitHub issue. Use to respond to issues, share progress, or ask clarifying questions.",
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "Issue number"},
                "body": {"type": "string", "description": "Comment text (markdown)"},
            }, "required": ["number", "body"]},
        }, _comment_on_issue),

        ToolEntry("close_github_issue", {
            "name": "close_github_issue",
            "description": "Close a GitHub issue with optional closing comment.",
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "Issue number"},
                "comment": {"type": "string", "default": "", "description": "Optional closing comment"},
            }, "required": ["number"]},
        }, _close_issue),

        ToolEntry("create_github_issue", {
            "name": "create_github_issue",
            "description": "Create a new GitHub issue. Use for tracking tasks, documenting bugs, or planning features.",
            "parameters": {"type": "object", "properties": {
                "title": {"type": "string", "description": "Issue title"},
                "body": {"type": "string", "default": "", "description": "Issue body (markdown)"},
                "labels": {"type": "string", "default": "", "description": "Labels (comma-separated)"},
            }, "required": ["title"]},
        }, _create_issue),
    ]
    for entry in tools:
        entry.schema["parameters"]["properties"]["repo"] = {
            "type": "string", "default": "",
            "description": "Explicit [HOST/]OWNER/REPO. Omit for the active Project repository; required for a Project without a repository folder. An omitted HOST follows GitHub CLI host configuration.",
        }
    return tools
