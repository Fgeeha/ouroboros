"""The owner's Pause of a whole task tree: its durable fence and launch gate.

Owner Batch4 (5A/7A): Pause fences NEW effects of every member of one root's
tree, lets operations already handed to an executor finish (no cancellation — unlike the budget
pause, which requests stops of delegated runs), then saves each member as the
exact same-ID pause the budget rail already writes (``budget_pause``, reason
``owner``). Nothing here is a scheduler, a ledger or a second pause store:

- The FENCE is one projection on the ROOT's task result (``owner_pause``),
  written by the supervisor's accept step through the task result's own
  locked read-modify-write (atomic replace), and released by the root's
  explicit Resume. Every member reads the same file: the queue's admission
  fence keeps new descendants from being admitted or assigned, and this
  durable projection is what a member that is ALREADY running consults at
  each launch handoff.
- Preparation and durable claims precede confirmed executor submission,
  serialized with Pause by a short per-root lock. No lock spans body/network
  completion. Claims and started operations are custody, not wire receipts;
  a crash never proves success. Nested/queued transports take their final gate
  when ready, and returned invocations cannot acquire a late background start.
- A member's SAFE BOUNDARY (the loop's round drain, a pre-dispatch model wait,
  a warm owner wait woken by the ``owner_pause`` mailbox control) reads the
  same fence and enters the exact pause without buying a model round.

The fence being closed is NOT the tree being Paused: the root's ``state``
turns ``paused`` only when no member of the tree still runs and every parked
member's sent work (delegated runs included) has settled; until then the
truthful state is ``requested`` (the tree is pausing).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import os
import pathlib
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)

_TOOL_OPERATION = ContextVar("owner_pause_tool_operation", default=None)
_HANDED_TOOL = ContextVar("owner_pause_handed_tool", default=None)
_HANDED_MODEL = ContextVar("owner_pause_handed_model", default="")

RAIL_OWNER_PAUSE = "owner_pause"
REASON_OWNER = "owner"

FENCE_REQUESTED = "requested"
FENCE_PAUSED = "paused"
FENCE_RELEASED = "released"
CLOSED_FENCE_STATES = frozenset({FENCE_REQUESTED, FENCE_PAUSED})

# The saved pause of a member whose sent delegated work had not settled yet: a
# checkpoint exists, but the member is not cleanly Paused (never a false Paused).
SETTLEMENT_EXTERNAL_RUNNING = "external_writers_running"
SETTLEMENT_SETTLED = "settled"

NOT_STARTED_TEXT = ("⚠️ OWNER_PAUSE_NOT_STARTED: NOT STARTED — the owner paused this task tree before this "
                    "operation was launched. Nothing ran; it may be issued again after Resume.")


class OwnerPauseRefused(Exception):
    """Pause authority refused fence installation or a new launch."""


# --- the durable fence --------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[Tuple[int, int, int], Dict[str, Any]]] = {}


def _result_path(root_drive: Any, root_task_id: str) -> pathlib.Path:
    from ouroboros.task_results import task_result_path

    return task_result_path(pathlib.Path(root_drive), str(root_task_id), create=False)


def read_fence(root_drive: Any, root_task_id: str) -> Dict[str, Any]:
    """The root's current ``owner_pause`` projection (``{}`` when none).

    Cached on the file's identity (inode, size, mtime): writers replace the
    file atomically, so an unchanged identity is an unchanged projection and
    the launch gate costs one ``stat`` on the hot path. An unreadable root
    result raises: an unknown fence is never read as an open one.
    """
    root_task_id = str(root_task_id or "").strip()
    if not root_task_id or root_drive in (None, ""):
        return {}
    path = _result_path(root_drive, root_task_id)
    key = str(path)
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        quarantine = path.parent / "quarantine"
        with _CACHE_LOCK:
            prior = _CACHE.get(key)
        if (prior or (quarantine / path.name).exists()
                or any(quarantine.glob(f"{root_task_id}.*.json"))):
            raise ValueError("owner_pause_authority_missing")
        return {}
    stamp = (int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return dict(cached[1])
    from ouroboros.task_results import load_task_result

    row = load_task_result(pathlib.Path(root_drive), root_task_id, strict=True) or {}
    fence = row.get("owner_pause") or {}
    if not isinstance(fence, dict) or (fence and fence.get("state") not in
                                       {FENCE_REQUESTED, FENCE_PAUSED, FENCE_RELEASED}):
        raise ValueError("owner_pause_authority_unreadable")
    with _CACHE_LOCK:
        _CACHE[key] = (stamp, dict(fence))
    return dict(fence)


def fence_closed(fence: Dict[str, Any]) -> bool:
    return str((fence or {}).get("state") or "") in CLOSED_FENCE_STATES


def install_fence(root_drive: Any, root_task_id: str, *, request_id: str) -> Tuple[Dict[str, Any], bool]:
    """Close the root's fence durably; ``(fence, created)``.

    Idempotent by ``request_id`` (a retried press returns the same fence) and
    by state (a second press while a fence is closed returns that fence). A
    terminal root refuses: a finished tree has nothing to pause. Raises
    ``OwnerPauseRefused`` without any write on refusal; any write failure
    propagates, so the caller never acknowledges an undurable Pause.
    """
    from ouroboros.task_results import (
        _TRULY_TERMINAL_STATUSES, require_writable_task_result_schema,
        stamp_task_result_schema, task_result_path,
    )
    from ouroboros.utils import update_json_locked, utc_now_iso

    outcome: Dict[str, Any] = {}

    def update(current: dict) -> Optional[dict]:
        if not current:
            raise OwnerPauseRefused("root_result_missing")
        require_writable_task_result_schema(current)
        if current.get("status") in _TRULY_TERMINAL_STATUSES:
            raise OwnerPauseRefused("task_terminal")
        old = current.get("owner_pause") if isinstance(current.get("owner_pause"), dict) else {}
        if fence_closed(old):
            outcome.update(fence=dict(old), created=False)
            return None
        fence = {
            "fence_id": uuid.uuid4().hex, "request_id": str(request_id or ""),
            "state": FENCE_REQUESTED, "requested_by": "owner", "requested_at": utc_now_iso(),
            "requested_at_ts": time.time(), "root_task_id": str(root_task_id),
            "generation": int(old.get("generation") or 0) + 1,
        }
        outcome.update(fence=fence, created=True)
        return stamp_task_result_schema({**current, "owner_pause": fence})

    with launch_lock(root_drive, root_task_id):
        update_json_locked(task_result_path(pathlib.Path(root_drive), str(root_task_id)), update,
                           strict_existing_dict=True)
    return dict(outcome["fence"]), bool(outcome["created"])


def set_fence_state(root_drive: Any, root_task_id: str, *, fence_id: str, state: str,
                    **fields: Any) -> Dict[str, Any]:
    """Compare-and-set the fence's state on its own ``fence_id`` (never another Pause's)."""
    from ouroboros.task_results import (
        require_writable_task_result_schema, stamp_task_result_schema, task_result_path,
    )
    from ouroboros.utils import update_json_locked, utc_now_iso

    written: Dict[str, Any] = {}

    def update(current: dict) -> Optional[dict]:
        require_writable_task_result_schema(current)
        old = current.get("owner_pause") if isinstance(current.get("owner_pause"), dict) else {}
        if str(old.get("fence_id") or "") != str(fence_id or ""):
            raise ValueError("owner pause fence identity changed")
        if str(old.get("state") or "") == state and not fields:
            written.update(old)
            return None
        fence = {**old, **fields, "state": state, f"{state}_at": utc_now_iso()}
        written.update(fence)
        return stamp_task_result_schema({**current, "owner_pause": fence})

    update_json_locked(task_result_path(pathlib.Path(root_drive), str(root_task_id)), update,
                       strict_existing_dict=True)
    return dict(written)


def release_fence(root_drive: Any, root_task_id: str, *, reason: str) -> Dict[str, Any]:
    """Reopen the root's fence for an explicit Resume (a no-op when already open).

    Called where the resumed ROOT actually starts: the worker consuming its
    exact grant, or the queue selecting a never-started root. A grant revoked
    before it ran (a restart, a Stop) therefore leaves the tree fenced.
    """
    fence = read_fence(root_drive, root_task_id)
    if not fence_closed(fence):
        return fence
    return set_fence_state(root_drive, root_task_id, fence_id=str(fence.get("fence_id") or ""),
                           state=FENCE_RELEASED, release_reason=str(reason or "owner_resume"))


# --- member side: the gate every launch family shares ----------------------------------

def admit_delegated_start(drive_root: Any, payload: Dict[str, Any]) -> bool:
    """Bind billing and register START_REQUESTED before releasing launch admission.

    This is custody, not an observation of transport bytes. A crash after the
    claim must be reconciled using its existing idempotency key.
    """
    from types import SimpleNamespace
    from ouroboros.delegate_custody import emit, START_REQUESTED
    from ouroboros.usage_admission import task_billing_fields

    source = SimpleNamespace(drive_root=drive_root, task_id=str(payload.get("task_id") or ""),
                             root_task_id=str(payload.get("root_task_id") or payload.get("task_id") or ""))
    binding = task_billing_fields({"id": source.task_id}, source.root_task_id, None, drive_root)
    payload["billing_group"] = {k: v for k, v in binding.items() if k.startswith("billing_group_")}
    with launch_admission(source):
        return emit(drive_root, START_REQUESTED, payload)


def _member_coordinates(source: Any) -> Tuple[str, str, str]:
    """``(root_drive, root_task_id, task_id)`` of a tool context or usage scope."""
    if getattr(source, "non_task_operation", False) is True:
        return "", "", ""
    meta = getattr(source, "task_metadata", None)
    meta = meta if isinstance(meta, dict) else {}
    task_id = str(getattr(source, "task_id", "") or "")
    root_task_id = str(meta.get("root_task_id") or getattr(source, "root_task_id", "") or task_id)
    root_drive = (meta.get("budget_drive_root") or getattr(source, "budget_drive_root", None)
                  or getattr(source, "drive_root", None) or "")
    return str(root_drive or ""), root_task_id, task_id


def member_fence(source: Any) -> Dict[str, Any]:
    """Read the shared fence strictly: unreadable authority refuses new effects."""
    root_drive, root_task_id, _task_id = _member_coordinates(source)
    if not root_drive or not root_task_id:
        return {}
    try:
        from ouroboros.model_wait import current_model_wait
        owner = current_model_wait()
        required = getattr(source, "task_lifecycle_bound", False) or (
            owner is not None and owner.task_id == _task_id)
        if required and not _result_path(root_drive, root_task_id).is_file():
            raise ValueError("owner_pause_authority_missing")
        fence = read_fence(root_drive, root_task_id)
    except Exception:
        log.warning("Owner pause authority unreadable for %s", root_task_id, exc_info=True)
        return {"state": "unknown", "reason": "owner_pause_authority_unreadable"}
    return fence if fence_closed(fence) else {}


def scope_fence() -> Dict[str, Any]:
    """The closed fence over the model send bound to this execution context."""
    try:
        from ouroboros.usage_accounting import current_usage_scope

        scope = current_usage_scope()
    except Exception:
        return {}
    if scope is None or not str(getattr(scope, "task_id", "") or ""):
        return {}
    return member_fence(scope)


@contextmanager
def launch_lock(root_drive: Any, root_task_id: str):
    """Serialize only local authority/registration with Pause; never transport."""
    from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock

    if not root_drive or not root_task_id:
        yield
        return
    path = _result_path(root_drive, root_task_id).with_suffix(".launch.lock")
    fd = acquire_exclusive_file_lock(path, owner_aware_stale=True)
    if fd is None:
        raise OwnerPauseRefused("owner_launch_authority_unavailable")
    try:
        yield
    finally:
        release_exclusive_file_lock(path, fd)


@contextmanager
def launch_admission(source: Any, *, root_resume: Optional[Dict[str, Any]] = None):
    """Register under the fence, or hand the root its exact explicit Resume.

    A resumed worker must start to consume its grant and reopen the fence.
    This exception authorizes that handoff only, never its tools or sends.
    """
    root_drive, root_id, task_id = _member_coordinates(source)
    from ouroboros.model_wait import current_model_wait
    owner = current_model_wait()
    if owner is not None and owner.task_id == task_id and owner.closed:
        raise OwnerPauseRefused("operation_already_returned")
    with launch_lock(root_drive, root_id):
        fence = member_fence(source)
        if fence:
            from ouroboros.budget_pause import budget_pause_row, STATE_RESUME_GRANTED

            allowed = False
            if task_id == root_id and root_resume and fence_closed(fence):
                row = budget_pause_row(pathlib.Path(root_drive), task_id)
                grant = row.get("grant") or {}
                allowed = bool(row.get("state") == STATE_RESUME_GRANTED
                    and (grant.get("owner_pause_fence_id") or row.get("owner_fence_id")) == fence.get("fence_id")
                    and row.get("pause_id") == root_resume.get("pause_id")
                    and grant.get("grant_id") == root_resume.get("grant_id")
                    and grant.get("authority") == "explicit_resume"
                    and not grant.get("revoked_at") and not grant.get("consumed_at"))
            if not allowed:
                raise OwnerPauseRefused(str(fence.get("reason") or "owner_pause"))
        from ouroboros.budget_pause import budget_pause_row, STATE_PAUSING, STATE_PAUSED, STATE_RESUME_GRANTED

        if root_drive and task_id:
            for member_id in {root_id, task_id}:
                try:
                    row = budget_pause_row(pathlib.Path(root_drive), member_id)
                except Exception as exc:
                    raise OwnerPauseRefused("model_sleep_authority_unreadable") from exc
                if row.get("reason") != "sleep" or row.get("state") not in {
                        STATE_PAUSING, STATE_PAUSED, STATE_RESUME_GRANTED}:
                    continue
                grant = row.get("grant") or {}
                resume_start = bool(root_resume and member_id == task_id
                    and row.get("state") == STATE_RESUME_GRANTED
                    and row.get("pause_id") == root_resume.get("pause_id")
                    and grant.get("grant_id") == root_resume.get("grant_id")
                    and not grant.get("revoked_at") and not grant.get("consumed_at"))
                if not resume_start:
                    raise OwnerPauseRefused("model_sleep")
        yield


def tree_member_results(root_drive: Any, root_task_id: str) -> Dict[str, Dict[str, Any]]:
    """Select complete retained membership, then strictly read its authority.

    Reuse the stat-invalidated lineage memo: unchanged foreign bodies are not
    parsed on every Pause tick. Unreadable or inadmissible facts refuse the
    census. Rows without a top-level root still need their legacy metadata read.
    The memo selects files only; it never supplies live status or custody.
    """
    from ouroboros.gateway.task_list_scan import raw_result_facts
    from ouroboros.task_results import load_task_result, task_results_dir

    facts, malformed = raw_result_facts(task_results_dir(root_drive, create=False))
    if malformed:
        raise ValueError("tree_membership_unreadable")
    members = {}
    for name, fact in facts.items():
        task_id = pathlib.Path(name).stem
        if fact.get("schema_refusal") or fact.get("task_id") != task_id:
            raise ValueError("tree_membership_unreadable")
        if fact.get("root_task_id") and fact["root_task_id"] != root_task_id:
            continue
        row = load_task_result(root_drive, task_id, strict=True)
        if row is None:
            raise ValueError("tree_member_disappeared")
        if str(row.get("root_task_id") or (row.get("metadata") or {}).get("root_task_id")
               or task_id) == root_task_id:
            members[task_id] = row
    return members


@contextmanager
def tool_handoff(source: Any, name: str):
    """Retain custody until the registry supplies positive operation settlement.

    A returned error, opaque remote receipt or escaping exception is not that
    evidence. Unknown claims survive terminality and never authorize replay.
    """
    from ouroboros.task_results import stamp_task_result_schema
    from ouroboros.utils import update_json_locked, utc_now_iso

    outcome: Dict[str, Any] = {"not_started": True}
    root_drive, root_id, task_id = _member_coordinates(source)
    if not root_drive or not task_id:
        yield outcome
        return
    path = _result_path(root_drive, task_id)
    op_id = uuid.uuid4().hex
    outcome["operation_id"] = op_id
    token = _TOOL_OPERATION.set((source, name, outcome))
    def claim(current):
        if not current.get("status") or current.get("task_id") != path.stem:
            raise OwnerPauseRefused("owner_pause_authority_unreadable")
        outstanding = dict(current.get("launch_handoffs") or {})
        outstanding[op_id] = {"tool": name, "task_id": task_id, "root_task_id": root_id,
                              "state": "claimed", "claimed_at": utc_now_iso()}
        return stamp_task_result_schema({**current, "launch_handoffs": outstanding})
    claimed = False
    try:
        # Stateful tools were submitted to their sticky executor by the loop.
        # This ticket belongs only to that invocation, not its nested launches.
        handed = _HANDED_TOOL.get()
        with _CACHE_LOCK:
            outcome["handed"] = bool(handed and handed["invocation"] == (id(source), name) and not handed["claimed"])
            if outcome["handed"]:
                handed["claimed"] = True  # Shared by copied contexts: exactly one consumer.
        from ouroboros.model_wait import current_model_wait
        owner = current_model_wait()
        if owner is not None and owner.task_id == task_id and owner.closed:
            raise OwnerPauseRefused("operation_already_returned")
        with (launch_lock(root_drive, root_id) if outcome["handed"] else launch_admission(source)):
            # A standalone tool context is not a lifecycle producer. Never
            # manufacture a status-less result that the next strict read must
            # refuse. A not-yet-published member uses its existing root owner.
            if not path.exists():
                path = _result_path(root_drive, root_id)
            if path.exists():
                update_json_locked(path, claim, strict_existing_dict=True)
                claimed = True
            elif getattr(source, "task_lifecycle_bound", None) is not False:
                raise OwnerPauseRefused("owner_pause_authority_unreadable")
        yield outcome
    finally:
        _TOOL_OPERATION.reset(token)
        def settle(current):
            if not current.get("status") or current.get("task_id") != path.stem:
                raise OwnerPauseRefused("owner_pause_authority_unreadable")
            outstanding = dict(current.get("launch_handoffs") or {})
            outstanding.pop(op_id, None)
            return stamp_task_result_schema({**current, "launch_handoffs": outstanding})
        # Close local admission even if durable cleanup cannot acquire its lock.
        # Failure retains custody; it must not replace an executed result with
        # a false NOT STARTED answer. Settlement still serializes with starts.
        outcome["closed"] = True
        try:
            with launch_lock(root_drive, root_id):
                if claimed and (outcome.get("settled") is True or outcome.get("not_started") is True):
                    update_json_locked(path, settle, strict_existing_dict=True)
        except Exception:
            log.warning("Tool %s custody could not settle (%s)", name, op_id, exc_info=True)


def current_tool_operation(source: Any, name: str) -> str:
    """Exact synchronous invocation identity, never an exemption by tool name."""
    active = _TOOL_OPERATION.get()
    return str(active[2].get("operation_id") or "") if active and active[0] is source and active[1] == name else ""


@contextmanager
def operation_start(source: Any = None):
    """Final local operation-start point after preparation, serialized with Pause.

    Hold only across the local submission (for example Popen), never its wait.
    Preparation must finish first. A nested start refused here proves no effect
    for that submission, not for an earlier start in the same invocation.
    """
    active = _TOOL_OPERATION.get()
    source = source if source is not None else (active[0] if active else None)
    with launch_admission(source):
        if active and active[0] is source:
            if active[2].get("closed"):
                raise OwnerPauseRefused("operation_already_returned")
            active[2]["not_started"] = False
        yield


def submit_tool(source: Any, name: str, submit: Any, function: Any, *args: Any):
    """Hand one invocation to its existing sticky executor under Pause exclusion."""
    with launch_admission(source):
        context = copy_context()
        context.run(_HANDED_TOOL.set, {"invocation": (id(source), name), "claimed": False})
        return submit(context.run, function, *args)


def run_operation(source: Any, function: Any, *args: Any, **kwargs: Any):
    """Submit an opaque synchronous call; join outside the short launch lock.

    The worker owns only this call. Its copied context does not authorize any
    nested process, model or delegated submission after a subsequent Pause.
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="operation") as executor:
        with operation_start(source):
            context = copy_context()
            future = executor.submit(context.run, function, *args, **kwargs)
        return future.result()


def run_tool_handler(source: Any, function: Any, *args: Any, **kwargs: Any):
    active = _TOOL_OPERATION.get()
    if active and active[0] is source and active[2].get("handed"):
        if active[2].get("closed"):
            raise OwnerPauseRefused("operation_already_returned")
        active[2]["not_started"] = False
        return function(*args, **kwargs)  # Keep browser/greenlet thread affinity.
    from ouroboros.tools.process_facts import process_facts_handoff
    with process_facts_handoff(function) as invoke:
        return run_operation(source, invoke, *args, **kwargs)


def submit_async_operation(source: Any, function: Any, *args: Any, **kwargs: Any):
    """Submit to the current event loop without executing the body under a lock."""
    import asyncio

    async def invoke():
        return await function(*args, **kwargs)

    with operation_start(source):
        return asyncio.create_task(invoke())


def model_handed_off(attempt_id: str = "") -> bool:
    """Only this physical attempt may finish despite a later owner Pause."""
    if not attempt_id:
        from ouroboros.usage_accounting import last_physical_attempt_capture
        attempt_id = getattr(last_physical_attempt_capture(), "attempt_id", "")
    return bool(attempt_id and _HANDED_MODEL.get() == attempt_id)


def submit_model(reservation: Any, submit: Any, function: Any):
    """Transfer the exact sender to an executor, mutually exclusive with Pause."""
    from ouroboros.llm_attempt import _PhysicalSendNotStarted, require_physical_dispatch_window

    try:
        with launch_admission(reservation.scope):
            require_physical_dispatch_window()
            context = copy_context()
            context.run(_HANDED_MODEL.set, reservation.attempt_id)
            return submit(context.run, function)
    except OwnerPauseRefused as exc:
        raise _PhysicalSendNotStarted(str(exc)) from exc
