"""The owner's Pause of a whole tree, supervisor side (owner Batch4 5A).

``request_owner_pause`` is the one accept step behind ``POST
/api/tasks/{id}/pause``. Its order is the protocol (``ouroboros/owner_pause.py``):

1. Admission under the queue lock: a LIVE ROOT (pooled RUNNING or PENDING, or
   the in-process direct turn) with no pending Stop. A child is refused — the
   Pause is the tree's, never one member's.
2. The durable fence on the root's task result (atomic replace). Nothing is
   acknowledged before it lands; a failed write changes nothing else.
3. Under the queue lock again, the root's existing admission latch
   (``BUDGET_ROOT_FENCES`` typed ``cause=owner_pause``): no new descendant is
   admitted or assigned, including one a delegate event is draining right now,
   and a Resume grant minted but not yet dispatched returns to its pause. The
   census of RUNNING members is taken inside that same lock, so no member can
   be assigned between the census and the latch. The snapshot is persisted.
4. One ``owner_pause`` mailbox control per live member: a WAKE signal for a
   warm wait; every member's own launch gate and safe boundary read the fence.

The tree's truthful state is kept on the same fence: ``requested`` while any
member still runs or any parked member's sent work is unsettled, ``paused``
once none does (``refresh_owner_pause_tree``, called at every owner-reason
park, after a member's terminal and from the assignment tick's observe-only
re-check ``settle_requested_owner_pauses``). Only an explicit owner Resume of the root
reopens it. Budget-pause policy is untouched: nothing here requests a stop.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)


def _locate_root_locked(q: Any, task_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(task_row, lane)`` for a live task (queue lock held); lane ``running``/``pending``/``direct``."""
    meta = q.RUNNING.get(task_id) if isinstance(q.RUNNING, dict) else None
    if isinstance(meta, dict) and isinstance(meta.get("task"), dict):
        return dict(meta["task"]), "running"
    row = next((dict(item) for item in q.PENDING
                if isinstance(item, dict) and str(item.get("id") or "") == task_id), None)
    if row is not None:
        return row, "pending"
    from supervisor.workers import direct_chat_turn

    turn = direct_chat_turn(task_id)
    return (dict(turn), "direct") if isinstance(turn, dict) else (None, "")


def _running_members_locked(q: Any, root_task_id: str) -> List[Tuple[str, Dict[str, Any]]]:
    members = []
    for task_id, meta in q.RUNNING.items():
        task = meta.get("task") if isinstance(meta, dict) and isinstance(meta.get("task"), dict) else {}
        if root_task_id in (str(task.get("root_task_id") or ""), str(task_id)):
            members.append((str(task_id), dict(task)))
    return members


def request_owner_pause(task_id: str, *, request_id: str) -> Dict[str, Any]:
    """Accept the owner's Pause of ``task_id``'s tree (see module docstring)."""
    from ouroboros.owner_pause import OwnerPauseRefused, install_fence
    from ouroboros.task_results import resolve_task_lineage

    from supervisor import queue as q
    task_id = str(task_id or "").strip()
    with q._queue_lock:
        task, lane = _locate_root_locked(q, task_id)
        if task is None:
            return {"ok": False, "error": "task_not_live"}
        lineage = resolve_task_lineage(
            task_id, metadata=task.get("metadata"), root_task_id=task.get("root_task_id"),
            parent_task_id=task.get("parent_task_id"), delegation_role=task.get("delegation_role"),
            original_task_id=task.get("original_task_id"), timeout_retry_from=task.get("timeout_retry_from"))
        if not lineage["is_root_task"]:
            return {"ok": False, "error": "not_a_root_task", "root_task_id": str(task.get("root_task_id") or "")}
        from ouroboros.cancel_intents import cancel_pending

        try:
            if cancel_pending(q.DRIVE_ROOT, task_id, strict=True):
                return {"ok": False, "error": "cancel_pending"}
        except Exception:
            return {"ok": False, "error": "cancellation_authority_unavailable"}
        root_drive = pathlib.Path(task.get("budget_drive_root") or q.DRIVE_ROOT)
        if lane != "direct":
            try:
                q.ensure_control_task_result(task_id)
            except Exception:
                log.warning("Owner pause lifecycle authority unavailable for %s", task_id, exc_info=True)
                return {"ok": False, "error": "pause_record_unwritable"}
    try:
        fence, created = install_fence(root_drive, task_id, request_id=request_id)
    except OwnerPauseRefused as exc:
        return {"ok": False, "error": str(exc) or "pause_refused"}
    except Exception as exc:
        log.warning("Owner pause fence for %s was not written", task_id, exc_info=True)
        return {"ok": False, "error": "pause_record_unwritable", "detail": str(exc)[:200]}
    from supervisor.events_budget import _set_root_budget_pause_locked

    with q._queue_lock:
        latch = _set_root_budget_pause_locked(task_id, {
            "fence_id": str(fence["fence_id"]), "cause": "owner_pause",
            "paused_at": str(fence.get("requested_at") or utc_now_iso())})
        members = _running_members_locked(q, task_id)
        persisted = q.persist_queue_snapshot(reason="owner_pause_requested")
    if not persisted:
        # The durable fence already holds every launch; the queue latch lives
        # in memory until the next persisted snapshot. Disclosed, not hidden.
        log.error("Owner pause of %s: queue latch installed but its snapshot was not persisted", task_id)
    woken = _wake_members(q, task_id, fence, members, direct=(lane == "direct"))
    event = {"ts": utc_now_iso(), "type": "owner_pause_requested", "task_id": task_id,
             "root_task_id": task_id, "fence_id": fence["fence_id"], "duplicate": not created,
             "queue_latch_fence_id": latch.get("fence_id"), "members_woken": woken,
             "owner_visible": True, "toast_once": f"{task_id}:owner-pause:{fence['fence_id']}"}
    append_jsonl(q.DRIVE_ROOT / "logs" / "events.jsonl", event)
    state = refresh_owner_pause_tree(task_id)
    return {"ok": True, "task_id": task_id, "root_task_id": task_id, "fence_id": fence["fence_id"],
            "duplicate": not created, "state": state or fence.get("state"), "members": woken,
            "snapshot_persisted": bool(persisted)}


def _wake_members(q: Any, root_task_id: str, fence: Dict[str, Any], members: list, *, direct: bool) -> List[str]:
    """One wake control per live member; the fence, not this row, is the authority."""
    from ouroboros.owner_mailbox import KIND_OWNER_PAUSE, write_owner_message

    targets = [(member_id, row) for member_id, row in members]
    if direct:
        targets.append((root_task_id, {"id": root_task_id}))
    woken: List[str] = []
    for member_id, row in targets:
        try:
            drive = q._task_drive_for_task(row, member_id)
            if write_owner_message(drive, "owner_pause", member_id,
                                   msg_id=f"owner_pause:{fence['fence_id']}", kind=KIND_OWNER_PAUSE):
                woken.append(member_id)
        except Exception:
            # A member that misses its wake still meets the fence at its next
            # launch handoff or boundary; only a warm wait waits longer.
            log.warning("Owner pause wake for %s was not written", member_id, exc_info=True)
    return woken


def refresh_owner_pause_tree(root_task_id: str) -> str:
    """Turn the root's fence ``paused`` once the whole tree is saved and settled.

    Settled means: no member of the tree is RUNNING or live as a direct turn,
    and every parked owner-reason member's durable row is not waiting on sent
    work. Returns the fence state after the check (``""`` when no fence).
    """
    from ouroboros.budget_pause import budget_pause_row
    from ouroboros.owner_pause import (
        FENCE_PAUSED, FENCE_REQUESTED, SETTLEMENT_EXTERNAL_RUNNING, read_fence, set_fence_state,
    )
    from supervisor.workers import direct_chat_turn

    from supervisor import queue as q
    root_task_id = str(root_task_id or "").strip()
    with q._queue_lock:
        root_row = next((item for item in q.PENDING if str(item.get("id") or "") == root_task_id), None)
        root_meta = q.RUNNING.get(root_task_id)
        root_task = (root_meta.get("task") if isinstance(root_meta, dict) else None) or root_row or {}
        root_drive = pathlib.Path((root_task or {}).get("budget_drive_root") or q.DRIVE_ROOT)
        running = _running_members_locked(q, root_task_id)
        parked = [dict(item) for item in q.PENDING if isinstance(item, dict)
                  and root_task_id in (str(item.get("root_task_id") or ""), str(item.get("id") or ""))
                  and isinstance(item.get("_budget_pause"), dict)]
    try:
        fence = read_fence(root_drive, root_task_id)
    except Exception:
        log.debug("Owner pause fence unreadable for %s", root_task_id, exc_info=True)
        return ""
    if not fence or str(fence.get("state") or "") != FENCE_REQUESTED:
        return str(fence.get("state") or "")
    if running or direct_chat_turn(root_task_id) is not None:
        return FENCE_REQUESTED
    for item in parked:
        try:
            row = budget_pause_row(pathlib.Path(item.get("budget_drive_root") or q.DRIVE_ROOT),
                                   str(item.get("id") or ""))
        except Exception:
            return FENCE_REQUESTED
        if str(row.get("settlement") or "") == SETTLEMENT_EXTERNAL_RUNNING:
            return FENCE_REQUESTED
    from supervisor.continuation_admission import conflicting_writers

    if conflicting_writers(q, root_task_id, drive_root=root_drive):
        return FENCE_REQUESTED
    try:
        set_fence_state(root_drive, root_task_id, fence_id=str(fence.get("fence_id") or ""),
                        state=FENCE_PAUSED, parked_members=[str(item.get("id") or "") for item in parked])
    except Exception:
        log.warning("Owner pause of %s is settled but its paused state was not recorded", root_task_id,
                    exc_info=True)
        return FENCE_REQUESTED
    append_jsonl(q.DRIVE_ROOT / "logs" / "events.jsonl",
                 {"ts": utc_now_iso(), "type": "owner_pause_settled", "task_id": root_task_id,
                  "root_task_id": root_task_id, "fence_id": fence.get("fence_id"),
                  "parked_members": [str(item.get("id") or "") for item in parked],
                  "owner_visible": True, "toast_once": f"{root_task_id}:owner-paused:{fence.get('fence_id')}"})
    return FENCE_PAUSED


_SETTLE_RECHECK_SEC = 5.0
_LAST_SETTLE_CHECK: Dict[str, float] = {}


def _record_settled_member(q: Any, item: Dict[str, Any]) -> None:
    """A parked member whose sent work was running at its park: a fresh,
    observe-only custody read records it settled once nothing is open."""
    from ouroboros.budget_pause import budget_pause_row, observe_task_runs, set_budget_pause
    from ouroboros.owner_pause import SETTLEMENT_EXTERNAL_RUNNING, SETTLEMENT_SETTLED

    task_id = str(item.get("id") or "")
    result_root = pathlib.Path(item.get("budget_drive_root") or q.DRIVE_ROOT)
    row = budget_pause_row(result_root, task_id)
    if str(row.get("settlement") or "") != SETTLEMENT_EXTERNAL_RUNNING:
        return
    observed = observe_task_runs(result_root, task_id, reason="owner_pause_settlement_check", request_stop=False)
    if observed.get("custody_read") != "ok" or observed.get("runs"):
        return
    set_budget_pause(result_root, task_id, {**row, "settlement": SETTLEMENT_SETTLED,
                                            "settlement_observed_at": utc_now_iso()},
                     expected_pause_id=str(row.get("pause_id") or ""))


def settle_requested_owner_pauses(q: Any = None, *, now: Optional[float] = None) -> List[str]:
    """The assignment tick's re-check of owner Pauses still ``requested``.

    A tree stays ``pausing`` while a parked member's sent work runs; nothing
    else would notice that work ending while every member is parked. This
    pass (throttled per root, no second scheduler) records a member whose
    custody is now clean and lets ``refresh_owner_pause_tree`` decide. It
    never requests a stop and never grants anything. Returns the roots that
    turned ``paused``.
    """
    import time

    from ouroboros.owner_pause import FENCE_PAUSED

    if q is None:
        from supervisor import queue as q
    now = time.monotonic() if now is None else now
    with q._queue_lock:
        roots = [str(root_id) for root_id, fence in q.BUDGET_ROOT_FENCES.items()
                 if isinstance(fence, dict) and fence.get("cause") == "owner_pause"]
    for stale in set(_LAST_SETTLE_CHECK) - set(roots):
        _LAST_SETTLE_CHECK.pop(stale, None)
    settled: List[str] = []
    for root_id in roots:
        if now - _LAST_SETTLE_CHECK.get(root_id, float("-inf")) < _SETTLE_RECHECK_SEC:
            continue
        _LAST_SETTLE_CHECK[root_id] = now
        with q._queue_lock:
            parked = [dict(item) for item in q.PENDING if isinstance(item, dict)
                      and root_id in (str(item.get("root_task_id") or ""), str(item.get("id") or ""))
                      and isinstance(item.get("_budget_pause"), dict)]
        try:
            for item in parked:
                _record_settled_member(q, item)
            if refresh_owner_pause_tree(root_id) == FENCE_PAUSED:
                settled.append(root_id)
        except Exception:
            log.warning("Owner pause settlement re-check of %s failed; it stays pausing", root_id, exc_info=True)
    return settled
