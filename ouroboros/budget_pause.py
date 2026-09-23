"""Exact mid-run budget pause and its owner-granted same-ID Resume (#1196).

A pooled task that meets ANY monetary rail after work has already been done
(global exhaustion, root fence, graceful in-task ceiling, last-fit wrap-up,
soft landing, a refused dispatch) no longer spends a wrap-up call and ends as
``failed/budget_exhausted``. It PAUSES instead:

1. ``pausing`` — a process-local dispatch fence closes for this task id:
   ``usage_accounting.reserve_attempt`` refuses every NEW physical send under
   the task's scope (the loop itself, tools, reviewers, verdict extraction),
   so nothing sent after this point can outrun the checkpoint. Already-sent
   local producers keep their durable identities; nothing is re-POSTed and
   nothing is extracted with a Light model on the way out.
2. External observation — every delegated run this task still holds is
   observed from the durable custody rows and, because pre-terminal
   subscription cost coverage is NOT provable, a stop is REQUESTED through the
   verified cancel seam (owner Q8). The typed outcome (requested / confirmed /
   failed / containment fault) is recorded per run; an unknown stop never
   licenses a second writer.
3. Checkpoint — the ONE loop serializer (``owner_wait.continuation_state``)
   captures the exact continuation plus a program counter: which tool calls of
   the last batch still have no result, so a resume executes only those and
   never replays a completed call.
4. Durable ``budget_pause`` row on the task result (state ``pausing``), THEN
   ``BudgetPauseRequested`` unwinds the loop nonterminally. The worker reports
   ``budget_pause`` with ``exact_continuation=True``; the supervisor moves the
   SAME task id back to PENDING under a ``_budget_pause`` marker (no worker,
   no slot), writes state ``paused``, and keeps it there across restarts
   without waking it (``restore_budget_pause_allowed``).

Resume is an explicit OWNER act (owner Q7/Q10): raising a budget wakes
nobody. ``queue_transitions.resume_budget_paused_task`` validates money,
Stop/cancel intent, the task deadline, the finite lifetime (with the paused
interval carried SEPARATELY — the original ``started_at`` is never moved and
the quota clock is never used as a pause clock) and the checkpoint source,
then mints ONE single-use ``grant``. The loop consumes the grant
(``resume_paused_loop``), rebinds the saved cognition, refreshes the
planning threshold within the money still authorized (Q10) and discloses
workspace drift and external custody before any new effect. Cancelled,
completed and otherwise-stopped members are never revived; a root's Resume
only makes its own budget-paused descendants ELIGIBLE — the model selects
each one explicitly through the same task control (Q9).

Direct chat actors are NOT silently excluded: they keep the existing terminal
rail, and the recorded ``resource_limit`` names why (``exact_pause_unavailable``).
This module adds no scheduler, ledger or recovery framework: it reuses the
queue's ``_budget_pause`` carrier, the actor source store, the task-result
authority and the delegated custody rows that already exist.
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
import uuid
from dataclasses import asdict
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Rails (which monetary stop produced the pause). GRACEFUL_RAILS are the
# planning stops an explicit Resume may refresh (Q10); the others are hard
# money and need an owner increase before any grant validates.
RAIL_GLOBAL_EXHAUSTED = "global_exhausted"
RAIL_DISPATCH_REFUSED = "dispatch_refused"
RAIL_GRACEFUL_CEILING = "graceful_ceiling"
RAIL_WRAPUP_LAST_FIT = "wrapup_last_fit"
RAIL_SOFT_LAND = "soft_land"
GRACEFUL_RAILS = frozenset({RAIL_GRACEFUL_CEILING, RAIL_WRAPUP_LAST_FIT, RAIL_SOFT_LAND})
RAILS = GRACEFUL_RAILS | {RAIL_GLOBAL_EXHAUSTED, RAIL_DISPATCH_REFUSED}

STATE_PAUSING = "pausing"
STATE_PAUSED = "paused"
STATE_RESUME_GRANTED = "resume_granted"
STATE_RESUMED = "resumed"
LIVE_PAUSE_STATES = frozenset({STATE_PAUSING, STATE_PAUSED, STATE_RESUME_GRANTED})

RESUME_POLICY = "owner_resume_same_id"
REASON_CODE = "budget_paused"

# External-run stop outcomes as recorded on the pause row (independent facts,
# never collapsed into one boolean).
EXTERNAL_RUNNING = "running"
EXTERNAL_STOP_REQUESTED = "stop_requested"
EXTERNAL_STOP_CONFIRMED = "stop_confirmed"
EXTERNAL_STOP_UNKNOWN = "stop_unknown"


class BudgetPauseRequested(Exception):
    """The pause record is durable; unwind the loop WITHOUT a terminal.

    Carries the durable pause row. The worker must not emit ``task_done`` or a
    Main final for this task; the supervisor owns the queue transition.
    """

    def __init__(self, pause: Dict[str, Any]) -> None:
        super().__init__(f"budget pause {pause.get('pause_id', '')} ({pause.get('rail', '')})")
        self.pause = dict(pause)


# --- process-local dispatch fence ------------------------------------------------

_FENCE_LOCK = threading.Lock()
_FENCED: set[str] = set()


def begin_dispatch_fence(task_id: str) -> None:
    """Close NEW physical sends for ``task_id`` in this process (idempotent)."""
    tid = str(task_id or "").strip()
    if not tid:
        return
    with _FENCE_LOCK:
        _FENCED.add(tid)


def end_dispatch_fence(task_id: str) -> None:
    with _FENCE_LOCK:
        _FENCED.discard(str(task_id or "").strip())


def dispatch_fenced(task_id: str) -> bool:
    """Whether a NEW send under ``task_id`` must be refused right now."""
    tid = str(task_id or "").strip()
    if not tid:
        return False
    with _FENCE_LOCK:
        return tid in _FENCED


# --- eligibility and program counter ---------------------------------------------

def pause_ineligibility(ctx: Any) -> str:
    """Empty when an exact pause is possible; otherwise the typed reason.

    The reason is recorded on the terminal ``resource_limit`` so a direct actor
    or a context without a queue continuation owner is excluded LOUDLY.
    """
    if bool(getattr(ctx, "is_direct_chat", False)):
        return "direct_actor"
    if not callable(getattr(ctx, "owner_wait_callback", None)):
        return "no_queue_continuation_owner"
    if not str(getattr(ctx, "task_id", "") or ""):
        return "no_task_id"
    if not (getattr(ctx, "budget_drive_root", None) or getattr(ctx, "drive_root", None)):
        return "no_durable_root"
    return ""


def pending_tool_call_ids(messages: List[Dict[str, Any]]) -> List[str]:
    """Tool calls of the LAST assistant batch that have no recorded result.

    A missing result row is NOT proof the call never ran (a timeout or an
    exception after the effect, a parallel batch cut mid-way). These ids are
    therefore recorded as EXECUTION-UNKNOWN: a resume restores cognition only,
    never re-executes them, and tells the model to verify from authoritative
    state before repeating any of them.
    """
    last_assistant = None
    for index in range(len(messages) - 1, -1, -1):
        row = messages[index]
        if isinstance(row, dict) and row.get("role") == "assistant":
            last_assistant = index
            break
    if last_assistant is None:
        return []
    calls = messages[last_assistant].get("tool_calls") or []
    wanted = [str(call.get("id") or "") for call in calls if isinstance(call, dict) and call.get("id")]
    if not wanted:
        return []
    answered = {
        str(row.get("tool_call_id") or "")
        for row in messages[last_assistant + 1:]
        if isinstance(row, dict) and row.get("role") == "tool"
    }
    return [call_id for call_id in wanted if call_id not in answered]


def resume_point(messages: List[Dict[str, Any]], round_idx: int) -> Dict[str, Any]:
    pending = pending_tool_call_ids(messages)
    return {
        "round_idx": int(round_idx),
        "phase": "partial_tool_batch_unknown" if pending else "boundary",
        "unanswered_tool_call_ids": pending,
        "unanswered_policy": "not_re_executed_execution_unknown",
    }


# --- external custody (owner Q8) ---------------------------------------------------

def _external_run_rows(ctx: Any) -> tuple[List[Any], str]:
    """``(unsettled runs held by this task, read_error)``: an unreadable custody
    store is a typed failure, never an empty (clean-looking) list."""
    try:
        from ouroboros import delegate_custody as custody

        root = custody.custody_root(ctx)
        mine = str(ctx.task_id)
        return [run for run in custody.replay(root).values()
                if str(getattr(run, "task_id", "") or "") == mine and not getattr(run, "settled", True)], ""
    except Exception as exc:
        log.warning("External custody rows unreadable at budget pause", exc_info=True)
        return [], f"{type(exc).__name__}: {str(exc)[:200]}"


def observe_external_runs(ctx: Any, *, request_stop: bool = True) -> Dict[str, Any]:
    """Observe every unsettled delegated run this task holds; request stops.

    Pre-terminal subscription cost coverage cannot be proved from the ledger
    (the reservation of one call never covers a whole session), so the owner's
    Q8 branch for UNPROVEN coverage applies to every open run: a stop is
    requested through the verified cancel seam and its typed outcome recorded.
    ``requested`` and ``unknown`` are NOT death: the run stays under this
    task's custody and no second writer may be started over it.
    """
    runs, read_error = _external_run_rows(ctx)
    if read_error:
        # Held as UNKNOWN on the pause row: the grant re-reads custody and
        # refuses while it stays unreadable (never "no runs").
        return {"runs": [], "observed_at": time.time(), "custody_read": "failed",
                "error": read_error, "coverage_basis": "custody_unreadable"}
    if not runs:
        return {"runs": [], "observed_at": time.time(), "custody_read": "ok", "coverage_basis": "no_open_runs"}
    rows: List[Dict[str, Any]] = []
    gateway = None
    if request_stop:
        try:
            from ouroboros.gateways.claudexor import ClaudexorGateway

            gateway = ClaudexorGateway()
            gateway.handshake()
        except Exception as exc:
            log.warning("Budget pause cannot reach the harness gateway to request stops: %s", exc)
            gateway = None
    try:
        from ouroboros import delegate_custody as custody

        root = custody.custody_root(ctx)
        for run in runs:
            row = {
                "run_id": str(getattr(run, "run_id", "") or ""),
                "route": str(getattr(run, "route", "") or ""),
                "cost_coverage": "unproven_preterminal",
                "stop_policy": "request_stop",
                "state": EXTERNAL_RUNNING,
                "stop_outcome": "",
                "detail": "",
            }
            if gateway is None:
                row.update(state=EXTERNAL_STOP_UNKNOWN, stop_outcome="not_issued_gateway_unavailable")
            else:
                try:
                    result = custody.cancel_and_verify(root, gateway, run, "budget_pause_uncovered_cost")
                    outcome = str(result.get("outcome") or "")
                    row.update(stop_outcome=outcome, detail=str(result.get("detail") or ""))
                    if outcome == custody.CANCEL_CONFIRMED:
                        row["state"] = EXTERNAL_STOP_CONFIRMED
                    elif outcome == getattr(custody, "CANCEL_REQUESTED", "requested"):
                        row["state"] = EXTERNAL_STOP_REQUESTED
                    else:
                        row["state"] = EXTERNAL_STOP_UNKNOWN
                except Exception as exc:
                    row.update(state=EXTERNAL_STOP_UNKNOWN, stop_outcome=f"error:{type(exc).__name__}",
                               detail=str(exc)[:300])
            rows.append(row)
    finally:
        if gateway is not None:
            try:
                gateway.close()
            except Exception:
                log.debug("Gateway close after pause stop requests failed", exc_info=True)
    return {"runs": rows, "observed_at": time.time(), "custody_read": "ok",
            "coverage_basis": "preterminal_subscription_coverage_unprovable"}


def drain_local_review_attempts(task_id: str, *, timeout_sec: float) -> Dict[str, Any]:
    """Bounded quiescence barrier over THIS task's in-flight review attempts.

    Observation only: the fence already refuses new sends; this waits for the
    requests that were ALREADY sent to settle through their own custody path
    (``review_custody`` records each actor and its response reference before
    signalling the attempt's event). An attempt still open at the bound is
    recorded by operation id as ``unsettled`` — its durable identity is what the
    late-result custody adopts when the provider answers after release; it is
    never marked settled, PASS, failed or refunded here.
    """
    settled: List[Dict[str, str]] = []
    unsettled: List[Dict[str, str]] = []
    try:
        from ouroboros import review_custody as rc

        with rc._ACTIVE_LOCK:
            mine = [entry for entry in rc._ACTIVE.values()
                    if str(task_id) in str(getattr(entry, "wave_key", "") or "").split("|")
                    or f"|{task_id}|" in f"|{getattr(entry, 'key', '')}|"]
    except Exception:
        log.debug("Review attempt registry unavailable at budget pause", exc_info=True)
        return {"drained": False, "registry": "unavailable", "settled": settled, "unsettled": unsettled}
    deadline = time.monotonic() + max(0.0, float(timeout_sec or 0.0))
    for entry in mine:
        remaining = max(0.0, deadline - time.monotonic())
        row = {"operation_id": str(getattr(entry, "operation_id", "") or ""),
               "key": str(getattr(entry, "key", "") or "")[:120]}
        if entry.event.wait(remaining):
            settled.append(row)
        else:
            unsettled.append(row)
    return {"drained": not unsettled, "registry": "ok", "timeout_sec": float(timeout_sec or 0.0),
            "settled": settled, "unsettled": unsettled}


def _drain_bound_sec() -> float:
    """The EXISTING review operation window — the bound those attempts already
    wait under; no number of this module's own. Unresolvable -> 0, and a zero
    bound cannot prove quiescence, so the caller refuses to pause."""
    from ouroboros.deadline_utils import review_operation_timeout_sec

    return max(0.0, float(review_operation_timeout_sec() or 0.0))


def local_producer_observation(ctx: Any) -> Dict[str, Any]:
    """Fence, then DRAIN: what already-sent local producers left behind.

    No new send, no re-POST, no Light extraction. The paid acceptance identity
    travels in the checkpoint's ``acceptance`` block, so a resume continues that
    panel instead of opening a duplicate one. ``drained`` is a GATE for the
    caller: an attempt still open at the bound means quiescence is unproven
    and the exact pause must not be taken (nothing is adopted after release).
    """
    try:
        bound = _drain_bound_sec()
    except Exception:
        bound = 0.0
    drain = (drain_local_review_attempts(str(ctx.task_id), timeout_sec=bound) if bound > 0
             else {"drained": False, "registry": "no_operation_bound", "settled": [], "unsettled": []})
    return {
        "dispatch_fence": "closed",
        "review_attempts": drain,
        "quiescent": bool(drain.get("drained")) and drain.get("registry") == "ok",
        "acceptance_pending": str(getattr(ctx, "_task_acceptance_pending", "") or ""),
        "acceptance_reviewed_subject": str(getattr(ctx, "_task_acceptance_reviewed_subject", "") or ""),
        "note": ("sends started before the fence settled through their own custody; an unsettled "
                 "attempt at the bound refuses the exact pause; nothing was re-sent, extracted, "
                 "settled or refunded during pausing"),
    }


# --- durable row --------------------------------------------------------------------

def set_budget_pause(root: Any, task_id: str, row: Dict[str, Any],
                     expected_pause_id: Optional[str] = None) -> Dict[str, Any]:
    """Update only the ``budget_pause`` projection of the task result row."""
    from ouroboros.task_results import (
        _TRULY_TERMINAL_STATUSES, require_writable_task_result_schema,
        stamp_task_result_schema, task_result_path,
    )
    from ouroboros.utils import update_json_locked

    def update(current: dict) -> dict:
        require_writable_task_result_schema(current)
        if current.get("status") in _TRULY_TERMINAL_STATUSES:
            raise ValueError("a terminal task cannot be budget-paused")
        old = current.get("budget_pause") or {}
        if expected_pause_id is not None and old.get("pause_id") != expected_pause_id:
            raise ValueError("budget pause identity changed")
        return stamp_task_result_schema({**current, "budget_pause": dict(row)})

    update_json_locked(task_result_path(root, task_id), update, strict_existing_dict=True)
    return dict(row)


def budget_pause_row(root: Any, task_id: str) -> Dict[str, Any]:
    from ouroboros.task_results import load_task_result

    row = load_task_result(pathlib.Path(root), str(task_id), strict=True) or {}
    pause = row.get("budget_pause")
    return dict(pause) if isinstance(pause, dict) else {}


def has_budget_pause_checkpoint(root: Any, task_id: str, task_attempt: int) -> bool:
    """A live pause record for THIS attempt: automatic crash retry must not replay it.

    Fail-closed: an UNREADABLE record cannot authorize an ordinary retry, and a
    ``pausing`` row without a source yet (death during the drain) still fences
    the retry — the work up to that point is not re-run.
    """
    try:
        pause = budget_pause_row(root, task_id)
    except Exception:
        return True
    return bool(pause and pause.get("state") in LIVE_PAUSE_STATES
                and int(pause.get("task_attempt") or 0) == int(task_attempt))


# --- the pause itself -----------------------------------------------------------------

def request_pause(limit_ctx: Any, *, rail: str, scope: str, reason_text: str,
                  root_task_id: str = "") -> None:
    """Enter the durable pause and unwind the loop; returns only when ineligible.

    Order is the invariant: fence -> external observation -> checkpoint bytes
    -> ``pausing`` row -> raise. A failure before the row leaves the task on
    its ordinary terminal rail (the caller falls through); nothing may report
    a pause that has no durable record.
    """
    tools = getattr(limit_ctx, "tools", None)
    ctx = getattr(tools, "_ctx", None)
    if ctx is None or rail not in RAILS:
        return None
    ineligible = pause_ineligibility(ctx)
    usage = limit_ctx.accumulated_usage
    if ineligible:
        usage["exact_pause_unavailable"] = ineligible
        return None
    from ouroboros.owner_wait import continuation_state, store_continuation_source

    task_id = str(ctx.task_id)
    begin_dispatch_fence(task_id)
    setattr(ctx, "_budget_pausing", True)
    pause_id = uuid.uuid4().hex
    root = pathlib.Path(ctx.budget_drive_root or ctx.drive_root)
    attempt = int(ctx.task_attempt or 1)
    # Durable "pausing" FIRST (before the drain and the external observation),
    # so a death during the drain meets the crash-retry fence, not a replay.
    try:
        set_budget_pause(root, task_id, {
            "pause_id": pause_id, "state": STATE_PAUSING, "reason": "budget", "rail": rail,
            "scope": str(scope or "global"), "task_attempt": attempt, "source_ref": None,
            "started_at": getattr(ctx, "task_started_at", None), "pausing_since": time.time(),
            "exact_continuation": True, "replay_safe": False, "auto_resume": False,
        })
    except Exception:
        log.error("Exact budget pause could not open a durable pausing row for %s", task_id, exc_info=True)
        end_dispatch_fence(task_id)
        setattr(ctx, "_budget_pausing", False)
        usage["exact_pause_unavailable"] = "pause_record_failed"
        return None
    try:
        local = local_producer_observation(ctx)
        if not local.get("quiescent"):
            raise _NotQuiescent(local)
        external = observe_external_runs(ctx)
        messages = limit_ctx.messages
        trace = limit_ctx.llm_trace if isinstance(limit_ctx.llm_trace, dict) else {}
        seen = set(limit_ctx.owner_msg_seen or ())
        point = resume_point(messages, limit_ctx.round_idx)
        # The rail already stamped its terminal projection on the live usage;
        # the continuation must not carry "failed/budget_exhausted" into the
        # resumed loop's eventual honest terminal.
        usage_for_state = {key: value for key, value in usage.items()
                           if key not in ("execution_status", "reason_code", "_best_effort_extracted")}
        state = {
            **continuation_state(ctx, messages, trace, usage_for_state, limit_ctx.round_idx,
                                 list(limit_ctx.tool_schemas or []), seen),
            "pause_id": pause_id, "reason": "budget", "rail": rail, "scope": scope,
            "resume_point": point, "external_runs": external, "local_producers": local,
        }
        source = store_continuation_source(ctx, state, "budget-pause-" + pause_id)
        cost_ceiling = getattr(ctx, "_cost_ceiling", None)
        physical_calls = None
        try:
            from ouroboros.usage_accounting import usage_breakdown

            budget_root = getattr(ctx, "budget_drive_root", None) or ctx.drive_root
            breakdown = usage_breakdown(pathlib.Path(budget_root), task_id=task_id)
            if not breakdown.get("integrity_degraded"):
                physical_calls = int(breakdown.get("physical_calls") or 0)
        except Exception:
            log.debug("Physical call count unavailable at budget pause", exc_info=True)
        row = {
            "pause_id": pause_id, "state": STATE_PAUSING, "reason": "budget",
            "rail": rail, "scope": str(scope or "global"),
            "root_task_id": str(root_task_id or getattr(ctx, "root_task_id", "") or ""),
            "reason_text": str(reason_text or ""),
            "task_attempt": int(ctx.task_attempt or 1),
            "source_ref": source,
            "execution_drive_root": str(ctx.drive_root),
            "started_at": getattr(ctx, "task_started_at", None),
            "paused_at": time.time(),
            "paused_duration_sec": float(getattr(ctx, "_budget_paused_sec", 0.0) or 0.0),
            "resume_point": point,
            "cost_ceiling": asdict(cost_ceiling) if cost_ceiling is not None else None,
            "external_runs": external, "local_producers": local,
            "physical_calls": physical_calls,
            "exact_continuation": True, "replay_safe": False, "auto_resume": False,
            "resume_policy": RESUME_POLICY,
            "model_wait_quota_clock": (state.get("model_wait") or {}).get("quota_clock", {}),
        }
        set_budget_pause(root, task_id, row, expected_pause_id=pause_id)
    except _NotQuiescent as unsettled:
        # Quiescence unproven within the EXISTING operation bound: no exact
        # pause (nothing may be adopted after release). The historical terminal
        # rail takes over; the pausing row is closed as abandoned.
        _abandon_pausing_row(root, task_id, pause_id, "local_producers_unsettled", unsettled.detail)
        end_dispatch_fence(task_id)
        setattr(ctx, "_budget_pausing", False)
        usage["exact_pause_unavailable"] = "local_producers_unsettled"
        usage["exact_pause_unsettled_attempts"] = (unsettled.detail.get("review_attempts") or {}).get("unsettled")
        return None
    except Exception:
        # No durable record: the task keeps its ordinary terminal rail. Reopen
        # the fence so the wrap-up call that rail spends is not refused.
        log.error("Exact budget pause could not be recorded for %s; falling back to the terminal rail",
                  task_id, exc_info=True)
        _abandon_pausing_row(root, task_id, pause_id, "pause_record_failed", {})
        end_dispatch_fence(task_id)
        setattr(ctx, "_budget_pausing", False)
        usage["exact_pause_unavailable"] = "pause_record_failed"
        return None
    usage["reason_code"] = REASON_CODE
    usage["execution_status"] = "paused"
    usage["budget_pause"] = {key: row[key] for key in ("pause_id", "rail", "scope", "paused_at", "resume_point")}
    raise BudgetPauseRequested(row)


class _NotQuiescent(Exception):
    def __init__(self, detail: Dict[str, Any]) -> None:
        super().__init__("local producers not quiescent")
        self.detail = dict(detail)


STATE_ABANDONED = "abandoned"


def _abandon_pausing_row(root: Any, task_id: str, pause_id: str, reason: str, detail: Dict[str, Any]) -> None:
    """Close an opened ``pausing`` row that will not become a pause (typed, never silent)."""
    try:
        current = budget_pause_row(root, task_id)
        if current.get("pause_id") == pause_id:
            set_budget_pause(root, task_id, {**current, "state": STATE_ABANDONED, "abandon_reason": reason,
                                             "abandon_detail": detail, "abandoned_at": time.time()},
                             expected_pause_id=pause_id)
    except Exception:
        log.warning("Abandoned pausing row for %s could not be closed", task_id, exc_info=True)


def pause_event(task: Dict[str, Any], pause: Dict[str, Any]) -> Dict[str, Any]:
    """The worker->supervisor ``budget_pause`` event for an exact continuation."""
    from ouroboros.utils import utc_now_iso

    task_id = str(task.get("id") or "")
    return {
        "type": "budget_pause",
        "task_id": task_id,
        "task_type": str(task.get("type") or "task"),
        "worker_id": task.get("worker_id"),
        "chat_id": task.get("chat_id"),
        "root_task_id": str(pause.get("root_task_id") or task.get("root_task_id") or task_id),
        "resource_limit": exact_pause_marker(pause, default_root=str(task.get("root_task_id") or task_id)),
        "ts": utc_now_iso(),
    }


STATUS_PAUSED_EXACT = "paused_exact_continuation"


def exact_pause_marker(row: Dict[str, Any], *, default_root: str = "") -> Dict[str, Any]:
    """The queue's ``_budget_pause`` marker projected from the DURABLE pause row.

    The row on the task result is the source of truth; the marker is its
    locator inside the queue (and the worker event's ``resource_limit``).
    """
    return {
        "status": STATUS_PAUSED_EXACT,
        "scope": str(row.get("scope") or "global"),
        "root_task_id": str(row.get("root_task_id") or default_root or ""),
        "physical_calls": row.get("physical_calls"),
        "replay_safe": False,
        "exact_continuation": True,
        "auto_resume": False,
        "resume_policy": RESUME_POLICY,
        "rail": str(row.get("rail") or ""),
        "paused_at": row.get("paused_at"),
        "checkpoint": {
            key: row.get(key) for key in (
                "pause_id", "task_attempt", "source_ref", "execution_drive_root",
                "started_at", "paused_at", "paused_duration_sec", "resume_point",
                "model_wait_quota_clock", "external_runs", "rail", "scope", "reason_text",
            )
        },
    }


# --- resume (loop side) -----------------------------------------------------------------

def load_budget_pause(ctx: Any, handoff: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Consume ONE granted resume: the row must carry this exact single-use grant.

    Refuses (raises) a missing or corrupt source, a spent or foreign grant and
    an attempt mismatch: a stale snapshot must never revive a continuation.
    """
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.task_results import _TRULY_TERMINAL_STATUSES, load_task_result

    handoff = handoff or getattr(ctx, "budget_pause_resume", None)
    if not handoff:
        return {}
    root = pathlib.Path(ctx.budget_drive_root or ctx.drive_root)
    row = load_task_result(root, ctx.task_id, strict=True) or {}
    current = row.get("budget_pause") or {}
    grant = current.get("grant") or {}
    if (row.get("status") in _TRULY_TERMINAL_STATUSES
            or current.get("state") != STATE_RESUME_GRANTED
            or current.get("pause_id") != handoff.get("pause_id")
            or not grant.get("grant_id")
            or grant.get("grant_id") != handoff.get("grant_id")
            or grant.get("consumed_at")):
        raise ValueError("budget pause continuation has no live single-use grant")
    state = json.loads(read_actor_source_bytes(root, ctx.task_id, current["source_ref"]))
    if (state.get("task_id") != ctx.task_id
            or state.get("pause_id") != current.get("pause_id")
            or int(state.get("task_attempt") or 0) != int(ctx.task_attempt or 1)):
        raise ValueError("budget pause continuation identity mismatch")
    return {**state, "_pause_row": dict(current)}


def _refresh_planning_threshold(ctx: Any, budget_remaining_usd: Optional[float]) -> Dict[str, Any]:
    """Owner Q10: after an explicit Resume of a GRACEFUL stop, the planning
    threshold moves forward within the money still authorized, so the task is
    not paused again on the very number that paused it. The hard tree cap and
    the global ledger fence are untouched; the planning margin is what the
    owner's explicit act spends. Returns the disclosure row."""
    from ouroboros import task_pacing
    from ouroboros.loop_budget import _loop_tree_accounting

    old = getattr(ctx, "_cost_ceiling", None)
    if not isinstance(old, task_pacing.CostCeiling):
        return {"refreshed": False, "reason": "no_ceiling"}
    tree = _loop_tree_accounting(refresh=True, max_age_sec=0.0)
    usage = getattr(ctx, "_accumulated_usage", None) or {}
    task_cost = usage.get("cost")
    deciding, basis = task_pacing.resolve_deciding_spend(
        tree_cost_usd=tree.get("accounted_usd") if isinstance(tree, dict) else None,
        task_cost_usd=float(task_cost) if task_cost is not None else None,
        root_cap_usd=old.root_cap_usd,
    )
    spent = float(deciding or 0.0)
    components: List[float] = []
    if old.root_cap_usd is not None:
        components.append(float(old.root_cap_usd) - spent)
    if budget_remaining_usd is not None and float(budget_remaining_usd) > 0:
        profile = task_pacing.resolve_budget_profile(ctx)
        pct = profile.get("cost_hard_stop_pct")
        pct = task_pacing._DEFAULT_COST_HARD_STOP_PCT if pct is None else max(0, min(100, int(pct)))
        if pct > 0:
            components.append(float(budget_remaining_usd) * pct / 100.0)
    room = min(components) if components else None
    if room is None or room <= 0:
        return {"refreshed": False, "reason": "no_authorized_room", "spent_usd": spent, "basis": basis}
    refreshed = task_pacing.CostCeiling(
        state=task_pacing.COST_CEILING_ACTIVE, ceiling_usd=spent + room,
        root_cap_usd=old.root_cap_usd, planning_margin_usd=old.planning_margin_usd,
        basis=f"owner_resume_refresh({old.basis or 'previous'})",
    )
    ctx._cost_ceiling = refreshed
    return {"refreshed": True, "previous_ceiling_usd": old.ceiling_usd,
            "ceiling_usd": refreshed.ceiling_usd, "spent_usd": spent, "basis": basis}


def resume_paused_loop(tools: Any, state: Dict[str, Any], messages: list, trace: dict,
                       usage: dict, seen: set, *, budget_remaining_usd: Optional[float]) -> tuple:
    """Restore the paused cognition, consume the grant, disclose drift/custody.

    Returns ``(model, effort, use_local, mode, round_idx, plan)``. Cognition
    only: a tool call of the interrupted batch that has no recorded result is
    closed with a host row stating its execution is UNKNOWN — nothing is
    re-executed by the host, and the model is told to verify before repeating.
    """
    from ouroboros.owner_wait import rebind_restored_route, restore_continuation_state

    ctx = tools._ctx
    restore_continuation_state(tools, state, messages, trace, usage, seen)
    row = state.get("_pause_row") or {}
    root = pathlib.Path(ctx.budget_drive_root or ctx.drive_root)
    grant = dict(row.get("grant") or {})
    grant["consumed_at"] = time.time()
    set_budget_pause(root, ctx.task_id, {**row, "state": STATE_RESUMED, "grant": grant,
                                         "resumed_at": grant["consumed_at"]},
                     expected_pause_id=str(row.get("pause_id") or ""))
    ctx._budget_paused_sec = float(grant.get("paused_duration_sec") or row.get("paused_duration_sec") or 0.0)
    ctx.budget_pause_resume = None
    end_dispatch_fence(str(ctx.task_id))
    setattr(ctx, "_budget_pausing", False)
    if state.get("cost_ceiling") is not None:
        from ouroboros.task_pacing import CostCeiling

        ctx._cost_ceiling = CostCeiling(**state["cost_ceiling"])
    refresh: Dict[str, Any] = {"refreshed": False, "reason": "hard_rail"}
    if str(row.get("rail") or "") in GRACEFUL_RAILS:
        refresh = _refresh_planning_threshold(ctx, budget_remaining_usd)
    usage["budget_pause_resume"] = {"pause_id": row.get("pause_id"), "grant_id": grant.get("grant_id"),
                                    "paused_duration_sec": ctx._budget_paused_sec, "threshold_refresh": refresh}
    plan, mode = rebind_restored_route(tools, state, messages)
    external = (state.get("external_runs") or {}).get("runs") or []
    external_lines = "".join(
        f"\n- run {run.get('run_id')}: {run.get('state')} (stop outcome: {run.get('stop_outcome') or 'n/a'})"
        for run in external if isinstance(run, dict)
    ) or "\n- none"
    pending = pending_tool_call_ids(messages)
    for call_id in pending:
        # Transcript validity for the provider AND the honest fact: unknown, not
        # "did not run" and not "ran". The host never re-executes it.
        messages.append({"role": "tool", "tool_call_id": call_id, "content": (
            "[HOST NOTICE] No result for this tool call was recorded before the budget pause. "
            "Its execution state is UNKNOWN: it may have run and produced effects, or not run at all. "
            "It was NOT re-executed. Verify from authoritative state (files, git, services, custody) "
            "before repeating it.")})
    messages.append({"role": "user", "content": (
        "[SYSTEM NOTICE]\nThis task continued from its budget pause after an explicit owner Resume "
        f"(paused {ctx._budget_paused_sec:.0f}s; rail: {row.get('rail')}; planning threshold "
        f"{'refreshed to $%.2f' % refresh['ceiling_usd'] if refresh.get('refreshed') else 'not refreshed: ' + str(refresh.get('reason'))}). "
        "Cumulative spend, rounds and elapsed execution time were NOT reset. Prior tool results remain "
        "recorded; do not repeat completed effects. The pause ended the previous browser process and "
        "task-local services; their recorded results remain evidence, not proof they are still running. "
        "The workspace may have drifted while paused: re-read any file before building on it. "
        f"Delegated runs held at the pause:{external_lines}\n"
        "An unknown or merely requested stop is NOT proof of termination: never start a second writer "
        "over such a run; inspect its custody first. "
        + (f"The last tool batch was interrupted: {len(pending)} call(s) have no recorded result and were "
           "NOT re-executed (see the host rows above); their execution state is unknown."
           if pending else ""))})
    return (ctx.active_model, ctx.active_effort, ctx.active_use_local,
            mode, int(state["round_idx"]), plan)


# --- restore-after-restart gate (supervisor side) -------------------------------------------

def restore_budget_pause_allowed(root: Any, task: Dict[str, Any]) -> bool:
    """A paused PENDING row survives a restart and any snapshot age WITHOUT waking:
    the row is a locator, the task-result authority and the readable source make
    it restorable. Panic / no-resume flags and a terminal row refuse."""
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.task_results import _TRULY_TERMINAL_STATUSES, load_task_result

    pause = task.get("_budget_pause") if isinstance(task, dict) else None
    if not isinstance(pause, dict) or not pause.get("exact_continuation"):
        return False
    checkpoint = pause.get("checkpoint") if isinstance(pause.get("checkpoint"), dict) else {}
    root = pathlib.Path(root)
    if any((root / "state" / name).exists() for name in ("panic_stop.flag",)):
        return False
    task_id = str(task.get("id") or "")
    try:
        row = load_task_result(root, task_id, strict=True) or {}
    except Exception:
        return False
    current = row.get("budget_pause") or {}
    if (row.get("status") in _TRULY_TERMINAL_STATUSES
            or current.get("state") not in LIVE_PAUSE_STATES
            or current.get("pause_id") != checkpoint.get("pause_id")
            or not current.get("source_ref")):
        return False
    try:
        read_actor_source_bytes(root, task_id, current["source_ref"])
    except Exception:
        return False
    return True
