"""The durable ``tools.jsonl`` call record: one host invocation, up to three rows (#1316).

The loop freezes ONE ``invocation_id`` per tool call at wrapper entry, BEFORE the
call is handed to an executor, together with the task attempt, execution, round,
LLM call and provider ``tool_call_id`` it answers (providers reuse those ids, so
the provider id alone is not an identity). Every row of that call carries the
same frozen facts:

- ``tool_call_started`` — the host began processing the call (an executor wait
  included). It is NOT evidence that the handler ran or that any effect happened.
- ``tool_call`` — the settlement: the handler's result (or a host refusal after
  the start), with the host-measured ``elapsed_ms`` of the whole call, distinct
  from a process's own ``duration_ms``.
- ``tool_call_timeout`` — the caller's wait ended; the worker may still settle
  later. Timeout and settlement are independent facts and may land in either order.

Readers count invocations, never rows. A row without ``invocation_id`` is legacy
and stands alone (never joined by guesswork); a start with no later row is
``unknown`` — never running, never failed. Appends are ordinary appends with no
power-loss promise; each target's outcome is returned so a missed start can be
disclosed on the later rows. Nothing here vetoes execution.
"""

from __future__ import annotations

import pathlib
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

from ouroboros.utils import append_jsonl

CALL_STARTED = "tool_call_started"
CALL_SETTLED = "tool_call"
CALL_WAIT_ENDED = "tool_call_timeout"
_FROZEN_KEYS = ("invocation_id", "tool_call_id", "task_attempt", "execution_id", "round_id", "llm_call_id")


def new_invocation(tool_call_id: Any, correlation: Dict[str, Any], task_attempt: Any) -> Dict[str, Any]:
    """The immutable identity of one call, captured before submission."""
    frozen = {
        "invocation_id": uuid.uuid4().hex,
        "tool_call_id": str(tool_call_id or ""),
        "task_attempt": task_attempt,
        "execution_id": correlation.get("execution_id"),
        "round_id": correlation.get("round_id"),
        "llm_call_id": correlation.get("llm_call_id"),
    }
    return {**{key: value for key, value in frozen.items() if value not in (None, "")},
            "_started_mono": time.monotonic()}


def invocation_fields(invocation: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The frozen facts a row carries (never the private monotonic stamp)."""
    return {key: invocation[key] for key in _FROZEN_KEYS if invocation and key in invocation}


def elapsed_ms(invocation: Optional[Dict[str, Any]]) -> Optional[int]:
    started = (invocation or {}).get("_started_mono")
    return None if started is None else int((time.monotonic() - started) * 1000)


def append_call_row(meta: Dict[str, Any], drive_logs: pathlib.Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Append one row to the task's log and its canonical copy; return each outcome.

    ``meta`` is the task metadata (lineage, ``budget_drive_root``). The canonical
    copy is ALWAYS attempted, even when the task-log append failed, and the result
    names ``{"task_log": ok, "canonical": ok | None}`` (None: no separate copy)."""
    from ouroboros.task_results import resolve_task_lineage

    if task_id := str(payload.get("task_id") or "").strip():
        # ONE lineage resolver (a direct root is its own root), so every task row
        # carries root_task_id/delegation_role for the task log stream readers.
        lineage = resolve_task_lineage(task_id, metadata=meta)
        payload["root_task_id"] = lineage["root_task_id"]
        if role := lineage["delegation_role"] or ("root" if lineage["is_root_task"] else ""):
            payload["delegation_role"] = role
    for key in ("parent_task_id", "task_depth"):
        if meta.get(key) not in (None, ""):
            payload[key] = meta.get(key)
    local = pathlib.Path(drive_logs) / "tools.jsonl"
    outcome: Dict[str, Any] = {"task_log": _append(local, payload), "canonical": None}
    root = str(meta.get("budget_drive_root") or "").strip()
    if root:
        candidate = pathlib.Path(root).resolve(strict=False) / "logs" / "tools.jsonl"
        if candidate != local.resolve(strict=False):
            outcome["canonical"] = _append(candidate, payload)
    return outcome


def _append(path: pathlib.Path, payload: Dict[str, Any]) -> bool:
    try:
        return bool(append_jsonl(path, payload))
    except Exception:
        return False


def append_failed(outcome: Optional[Dict[str, Any]]) -> bool:
    return bool(outcome) and (outcome.get("task_log") is False or outcome.get("canonical") is False)


def counts_as_call(row: Dict[str, Any]) -> bool:
    """A row that opens one logical call: a start, or a legacy row without identity."""
    return row.get("type") == CALL_STARTED or not row.get("invocation_id")


def logical_calls(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group rows into calls in first-seen order.

    Each call names its ``started``/``settled``/``wait_ended`` rows and ``state``:
    ``settled`` (a result exists, in either order with a timeout), ``wait_ended``
    (the caller stopped waiting, no result recorded), ``unknown`` (only a start),
    ``legacy`` (a pre-identity row) or ``orphan`` (a result whose start row lies
    outside the rows given)."""
    calls: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    slot = {CALL_STARTED: "started", CALL_SETTLED: "settled", CALL_WAIT_ENDED: "wait_ended"}
    for row in rows:
        if not isinstance(row, dict):
            continue
        invocation_id = str(row.get("invocation_id") or "")
        if not invocation_id:
            calls.append({"state": "legacy", "tool": row.get("tool"), "args": row.get("args"), "settled": row})
            continue
        call = by_id.get(invocation_id)
        if call is None:
            call = by_id[invocation_id] = {"invocation_id": invocation_id, "tool": row.get("tool"),
                                           "args": row.get("args")}
            calls.append(call)
        call.setdefault(slot.get(str(row.get("type") or ""), "settled"), row)
    for call in calls:
        if "state" not in call:
            call["state"] = ("settled" if "settled" in call and "started" in call
                             else "orphan" if "settled" in call
                             else "wait_ended" if "wait_ended" in call else "unknown")
    return calls
