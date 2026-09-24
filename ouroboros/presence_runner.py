"""Fresh-agent execution for bounded, host-admitted presence turns."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ouroboros.artifacts import stage_task_attachments
from ouroboros.contracts.task_contract import attach_task_contract
from ouroboros.presence_admission import PresenceAdmission
from ouroboros.presence_authority import presence_ceiling_payload
from ouroboros.task_results import (
    STATUS_COMPLETED,
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
    is_reconciled_presence_placeholder,
    load_task_result,
    reopen_reconciled_presence_placeholder,
)
from ouroboros.utils import append_jsonl, atomic_write_json, iter_jsonl_objects, read_json_dict, utc_now_iso


class PresenceTurnError(ValueError):
    def __init__(
        self,
        code: str,
        field: str,
        *,
        attachment_manifest: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.code = str(code or "presence_turn_failed")
        self.field = str(field or "presence_turn")
        self.attachment_manifest = [
            dict(row) for row in attachment_manifest if isinstance(row, Mapping)
        ]
        super().__init__(f"{self.code}: {self.field}")


@dataclass(frozen=True)
class PresenceTurnEvent:
    source_event_id: str
    provider: str
    account_id: str
    conversation_id: str
    thread_id: str
    conversation_key: str
    actor: Mapping[str, Any]
    conversation: Mapping[str, Any]
    message: Mapping[str, Any]
    text: str
    delivery_reporting_version: int = 0


@dataclass(frozen=True)
class PresenceTurnResult:
    outcome: str
    text: str
    task_id: str
    work_ref: str = ""
    delivery_reporting_version: int = 0


def _presence_delivery(outcome: str, text: str, terminal_origin: str, *, legacy: bool = False) -> tuple[str, str]:
    """Project speech from producer facts, never from its wording or task status."""
    from ouroboros.task_finalization import HOST_AUTHORED_TERMINAL_ORIGINS, TERMINAL_ORIGIN_MODEL_FINAL

    if outcome not in {"message", "silent", "tool_delivered", "deferred"}:
        outcome = "message"
    if outcome not in {"message", "deferred"}:
        return outcome, ""
    # Unknown origin remains explicit stored-row compatibility, not evidence
    # authorizing new speech. Even an old frozen body cannot override known host provenance.
    authored = terminal_origin == TERMINAL_ORIGIN_MODEL_FINAL
    if not authored and not (legacy and terminal_origin not in HOST_AUTHORED_TERMINAL_ORIGINS):
        return ("deferred" if outcome == "deferred" else "silent"), ""
    return outcome, text


def presence_result_from_stored(stored: Mapping[str, Any], task_id: str) -> PresenceTurnResult:
    """One replay projection for cached turns and completed delegated work."""
    from ouroboros.task_finalization import TERMINAL_ORIGIN_MODEL_FINAL, provider_terminal_body, terminal_notice_text

    metadata = stored.get("metadata") if isinstance(stored.get("metadata"), dict) else {}
    text = metadata["presence_result_text"] if "presence_result_text" in metadata else stored.get("result")
    origin = str(stored.get("terminal_origin") or "")
    if origin == TERMINAL_ORIGIN_MODEL_FINAL and text and "result" in stored:
        raw, notice = str(stored["result"] or ""), terminal_notice_text(stored)
        # Undo only the recorded host composition. Older explicit reply bodies
        # could differ from the raw final; neither they nor deliberate emptiness are guessed away.
        if notice and text == provider_terminal_body(raw, notice):
            text = raw
    # Unknown-origin compatibility covers completed rows only: a failed row may
    # carry host text (an orphan reconcile or exception notice), never a reply.
    outcome, text = _presence_delivery(
        str(metadata.get("presence_outcome") or "message"), str(text or ""), origin,
        legacy=str(stored.get("status") or "") == "completed",
    )
    return PresenceTurnResult(
        outcome=outcome, text=text, task_id=task_id,
        work_ref=str(metadata.get("presence_work_ref") or ""),
        delivery_reporting_version=int((metadata.get("presence") or {}).get("delivery_reporting_version") == 1),
    )


def build_presence_result_event(task: dict[str, Any], text: str, ctx: Any, *, terminal_origin: str = "",
                                retain_scheduled_handoff: bool = False) -> dict[str, Any]:
    """Freeze typed delivery metadata before the ordinary durable result write."""
    from ouroboros.task_finalization import HOST_AUTHORED_TERMINAL_ORIGINS

    completion = getattr(ctx, "_presence_completion", None)
    completion = completion if (
        isinstance(completion, dict) and getattr(ctx, "_presence_completion_accepted", False)
    ) else {}
    outcome = str(completion.get("outcome") or "message").strip()
    handoff = getattr(ctx, "_swarm_handoff_attempt", None)
    handoff = handoff if isinstance(handoff, dict) else {}
    work_ref = (
        str(handoff.get("task_id") or "")
        if str(handoff.get("status") or "") == "scheduled"
        else ""
    )
    if outcome == "deferred" and not work_ref:
        outcome = "message"
    if work_ref and (retain_scheduled_handoff or terminal_origin in HOST_AUTHORED_TERMINAL_ORIGINS):
        # A failed/forced or host-replaced final still owes an admitted child's result.
        # Transports poll only deferred outcomes, even when no reply was authored.
        outcome = "deferred"
    outcome, result_text = _presence_delivery(outcome, str(text or ""), terminal_origin)
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    metadata["presence_outcome"] = outcome
    metadata["presence_result_text"] = result_text
    if work_ref:
        metadata["presence_work_ref"] = work_ref
    task["metadata"] = metadata
    return {
        "type": "presence_result",
        "task_id": str(task.get("id") or ""),
        "outcome": outcome,
        "text": result_text,
        # The accepted presence_finish message; a tool_delivered note is context, never speech.
        "message": result_text or (str(completion.get("message") or "") if outcome == "tool_delivered" else ""),
        "work_ref": work_ref,
        "ts": utc_now_iso(),
    }


class PresenceTurnGate:
    """Cross-process cap plus one active turn for each conversation."""

    def __init__(self, max_active: int = 2, *, state_root: Path | None = None) -> None:
        self._max_active = max(1, int(max_active))
        self._slots = threading.BoundedSemaphore(max(1, int(max_active)))
        self._guard = threading.Lock()
        self._conversations: dict[str, threading.Lock] = {}
        self._state_root = Path(state_root).resolve(strict=False) if state_root is not None else None
        self._claimed_slots: set[int] = set()

    @contextmanager
    def _file_lock(self, path: Path):
        from ouroboros.platform_layer import file_lock_exclusive, file_unlock

        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            file_lock_exclusive(fd)
            yield
        finally:
            try:
                file_unlock(fd)
            finally:
                os.close(fd)

    @contextmanager
    def _file_slot(self):
        from ouroboros.platform_layer import file_lock_exclusive_nb, file_unlock

        if self._state_root is None:
            with self._slots:
                yield
            return
        slot_root = self._state_root / "presence_turn_gate"
        slot_root.mkdir(parents=True, exist_ok=True)
        while True:
            for index in range(self._max_active):
                with self._guard:
                    if index in self._claimed_slots:
                        continue
                    self._claimed_slots.add(index)
                path = slot_root / f"slot-{index}.lock"
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    file_lock_exclusive_nb(fd)
                except OSError:
                    os.close(fd)
                    with self._guard:
                        self._claimed_slots.discard(index)
                    continue
                try:
                    yield
                finally:
                    try:
                        file_unlock(fd)
                    finally:
                        os.close(fd)
                        with self._guard:
                            self._claimed_slots.discard(index)
                return
            time.sleep(0.05)

    def run(self, conversation_key: str, callback: Callable[[], PresenceTurnResult]) -> PresenceTurnResult:
        key = str(conversation_key or "").strip()
        if not key:
            raise PresenceTurnError("presence_conversation_key_required", "conversation_key")
        with self._guard:
            conversation_lock = self._conversations.setdefault(key, threading.Lock())
        with conversation_lock:
            if self._state_root is None:
                with self._file_slot():
                    return callback()
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            conversation_path = self._state_root / "presence_turn_gate" / f"conversation-{digest}.lock"
            with self._file_lock(conversation_path):
                with self._file_slot():
                    return callback()


_GATES_LOCK = threading.Lock()
_GATES: dict[tuple[str, int], PresenceTurnGate] = {}
_LIVE_LOCK = threading.Lock()
_LIVE_PRESENCE_TASKS: set[str] = set()


def presence_turn_is_live(task_id: str) -> bool:
    """Process-local liveness for the orphan reconciler; never an owner-addressable actor."""
    with _LIVE_LOCK:
        return str(task_id or "") in _LIVE_PRESENCE_TASKS


def _configured_gate(drive_root: Path | None = None) -> PresenceTurnGate:
    from ouroboros.config import SETTINGS_DEFAULTS, _bounded_positive_int_setting

    limit = _bounded_positive_int_setting(
        "OUROBOROS_PRESENCE_MAX_ACTIVE",
        default=int(SETTINGS_DEFAULTS["OUROBOROS_PRESENCE_MAX_ACTIVE"]),
        hard_max=20,
    )
    state_root = Path(drive_root).resolve(strict=False) / "state" if drive_root is not None else None
    key = (str(state_root or ""), limit)
    with _GATES_LOCK:
        return _GATES.setdefault(key, PresenceTurnGate(limit, state_root=state_root))


def _stable_numeric_id(prefix: str, value: str) -> int:
    digest = hashlib.sha256(f"{prefix}\0{value}".encode("utf-8")).digest()
    return (1 << 40) + (int.from_bytes(digest[:6], "big") & ((1 << 40) - 1))


def _task_id(admission: PresenceAdmission, event: PresenceTurnEvent) -> str:
    digest = hashlib.sha256(
        f"{admission.binding_id}\0{event.source_event_id}".encode("utf-8")
    ).hexdigest()
    return f"presence-{digest[:24]}"


def _cached_result(drive_root: Path, task_id: str) -> PresenceTurnResult | None:
    stored = load_task_result(drive_root, task_id) or {}
    if str(stored.get("status") or "") not in {"completed", "failed"} or is_reconciled_presence_placeholder(stored):
        return None  # a host-lost turn is not a result: the transport's retry runs it again
    return presence_result_from_stored(stored, task_id)


def _live_task_rows(drive_root: Path, task_id: str) -> list[dict[str, Any]]:
    """This task's rows in the live chat generation, in order; an attempt starts with its inbound row."""
    return [row for row in iter_jsonl_objects(Path(drive_root) / "logs" / "chat.jsonl") if row.get("task_id") == task_id]


def _confirmed_sends(rows: Sequence[Mapping[str, Any]]) -> list[str] | None:
    """One text per confirmed v1 receipt among *rows*; None once the attempt's rows left the live generation.

    An attempt's rows follow its inbound row, so that row in the live file means every receipt
    is there too. Without it a rotated archive may hold receipts: the count is unknown, not zero.
    """
    if not any(row.get("direction") == "in" for row in rows):
        return None
    parts: dict[tuple[str, str], str] = {}
    for row in rows:
        delivery = _receipt(row)
        if delivery.get("state") in {"delivered", "accepted"}:
            parts.setdefault((str(delivery.get("delivery_id")), str(delivery.get("part_id"))), str(row.get("text") or ""))
    return list(parts.values())


def _receipt(row: Mapping[str, Any]) -> Mapping[str, Any]:
    transport = row.get("transport") if isinstance(row.get("transport"), Mapping) else {}
    delivery = transport.get("delivery") if isinstance(transport.get("delivery"), Mapping) else {}
    return delivery if row.get("type") == "presence_delivery" else {}


def _uncertain_parts(rows: Sequence[Mapping[str, Any]]) -> int:
    """Parts the provider never confirmed nor refused (a timed-out send may have landed)."""
    keys = {(str(r.get("delivery_id")), str(r.get("part_id"))) for r in map(_receipt, rows) if r.get("state") == "uncertain"}
    return len(keys - {(str(r.get("delivery_id")), str(r.get("part_id")))
                       for r in map(_receipt, rows) if r.get("state") in {"delivered", "accepted"}})


def _previous_turn_path(drive_root: Path, conversation_key: str) -> Path:
    digest = hashlib.sha256(conversation_key.encode("utf-8")).hexdigest()
    return Path(drive_root) / "state" / "presence_turn_gate" / f"last-{digest}.json"


def _read_previous_turn(drive_root: Path, conversation_key: str) -> dict[str, Any] | None:
    """Last executed turn of this exact conversation; a rebuildable projection."""
    row = read_json_dict(_previous_turn_path(drive_root, conversation_key)) or {}
    return row if row.get("conversation_key") == conversation_key else None


def _write_previous_turn(drive_root: Path, conversation_key: str, task_id: str, *, outcome: str, message: str,
                         sends: Sequence[str], work_ref: str, finished_at: str, delivery: str) -> None:
    atomic_write_json(_previous_turn_path(drive_root, conversation_key), {
        "conversation_key": conversation_key, "task_id": task_id, "outcome": outcome, "message": message,
        "transport_sends": [text for text in sends if text], "work_ref": work_ref,
        "finished_at": finished_at, "delivery": delivery,
    })


def _pointer_behind(drive_root: Path, conversation_key: str, task_id: str) -> bool:
    """A completed turn whose pointer never landed (lost between its terminal write and the pointer write)."""
    stored = load_task_result(drive_root, task_id) or {}
    if str(stored.get("status") or "") != STATUS_COMPLETED:
        return False
    pointer = _read_previous_turn(drive_root, conversation_key)
    return pointer is None or (
        pointer.get("task_id") != task_id and str(pointer.get("finished_at") or "") <= str(stored.get("ts") or ""))


def _repair_previous_turn(drive_root: Path, conversation_key: str, task_id: str) -> None:
    """Rebuild the pointer from the durable row; the caller holds the conversation lock."""
    if not _pointer_behind(drive_root, conversation_key, task_id):
        return
    stored = load_task_result(drive_root, task_id) or {}
    replay = presence_result_from_stored(stored, task_id)
    sends = _confirmed_sends(_live_task_rows(drive_root, task_id)) if replay.delivery_reporting_version else []
    _write_previous_turn(drive_root, conversation_key, task_id, outcome=replay.outcome, message=replay.text,
                         sends=sends or [], work_ref=replay.work_ref, finished_at=str(stored.get("ts") or ""),
                         delivery="confirmed" if sends else "unknown")


def _log_dialogue(
    drive_root: Path,
    *,
    direction: str,
    chat_id: int,
    user_id: int,
    text: str,
    event: PresenceTurnEvent,
    task: Mapping[str, Any],
    task_id: str,
) -> None:
    from ouroboros.dialogue_provenance import presence_provenance_from_task

    state = read_json_dict(drive_root / "state" / "state.json") or {}
    append_jsonl(
        drive_root / "logs" / "chat.jsonl",
        {
            "ts": utc_now_iso(),
            "session_id": state.get("session_id"),
            "direction": direction,
            "chat_id": chat_id,
            "user_id": user_id,
            "text": text,
            "format": "markdown" if direction == "out" else "",
            "source": f"presence:{event.provider}",
            "sender_label": str(event.actor.get("display_name") or event.actor.get("username") or ""),
            "sender_session_id": str(event.actor.get("platform_actor_id") or event.actor.get("id") or ""),
            "client_message_id": event.source_event_id,
            "transport": {
                "provider": event.provider,
                "account_id": event.account_id,
                "conversation_id": event.conversation_id,
                "thread_id": event.thread_id,
                "conversation_key": event.conversation_key,
                "actor": dict(event.actor),
                "conversation": dict(event.conversation),
                "message": dict(event.message),
                **({"delivery": {"state": "authored"}} if direction == "out" else {}),
            },
            "presence_provenance": presence_provenance_from_task(task),
            "task_id": task_id,
        },
    )


def _build_task(
    admission: PresenceAdmission,
    event: PresenceTurnEvent,
    *,
    drive_root: Path,
    staged_files: Sequence[Path],
    lost_attempt: bool = False,
    prior_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    from ouroboros.config import runtime_setting

    task_id = _task_id(admission, event)
    chat_id = _stable_numeric_id("presence-conversation", event.conversation_key)
    actor_id = _stable_numeric_id(
        "presence-actor",
        f"{event.provider}:{event.account_id}:{event.actor.get('platform_actor_id') or event.actor.get('id') or ''}",
    )
    presence_context = {
        "binding_id": admission.binding_id,
        "transport_skill": admission.transport_skill,
        "behavior_skill": admission.behavior_skill,
        "profile_fingerprint": admission.profile_fingerprint,
        "instructions": admission.instructions,
        "context_topics": list(admission.context_topics),
        "delivery_reporting_version": event.delivery_reporting_version,
        "event": {
            "source_event_id": event.source_event_id,
            "provider": event.provider,
            "account_id": event.account_id,
            "conversation_id": event.conversation_id,
            "thread_id": event.thread_id,
            "conversation_key": event.conversation_key,
            "actor": dict(event.actor),
            "conversation": dict(event.conversation),
            "message": dict(event.message),
            "origin": admission.origin.__dict__,
            "destination": admission.destination.__dict__,
        },
    }
    previous_turn = _read_previous_turn(drive_root, event.conversation_key)
    if previous_turn:
        presence_context["previous_turn"] = previous_turn
    if lost_attempt:
        # Unknown (None) when the transport reports no receipts or the attempt's rows left the live generation.
        sent = _confirmed_sends(prior_rows) if event.delivery_reporting_version else None
        presence_context["previous_attempt"] = {
            "delivered_count": None if sent is None else len(sent),
            "delivered": None if sent is None else [text for text in sent if text],
            "uncertain_count": 0 if sent is None else _uncertain_parts(prior_rows),
        }
    metadata: dict[str, Any] = {
        "source": "presence",
        "client_message_id": event.source_event_id,
        "inline_max_rounds": admission.inline_max_rounds,
        "presence": presence_context,
    }
    if admission.model_slot == "light":
        from ouroboros.config import get_light_model

        metadata["model"] = get_light_model()
        metadata["use_local_model"] = runtime_setting("USE_LOCAL_LIGHT", "").lower() in {"true", "1"}
    task: dict[str, Any] = {
        "id": task_id,
        "type": "presence",
        "chat_id": chat_id,
        "actor_id": str(event.actor.get("platform_actor_id") or event.actor.get("id") or actor_id),
        "text": str(event.text or "").strip(),
        "_is_direct_chat": True,
        "_presence_turn": True,
        "context_requires_development": False,
        "metadata": metadata,
        "task_contract": {"capability_ceiling": presence_ceiling_payload(admission.capability_ceiling)},
    }
    if admission.workspace_root:
        task.update(
            workspace_root=admission.workspace_root,
            workspace_mode="external",
            memory_mode="shared",
        )
    manifest = stage_task_attachments(
        drive_root,
        task_id,
        [{"path": str(path), "label": path.name} for path in staged_files],
    )
    # Partial staging is the default for initial-task ingress (В25c, capinv-447):
    # good attachments stage, rejected ones ride along as disclosed manifest
    # rows — mirrors the gateway task API default. A FULLY-rejected set stays
    # atomic: the turn would run with none of its declared material.
    if manifest:
        from ouroboros.artifacts import (
            attachment_manifest_all_rejected,
            remove_staged_attachments,
        )

        if attachment_manifest_all_rejected(manifest):
            remove_staged_attachments(manifest)
            raise PresenceTurnError(
                "presence_attachment_admission_rejected",
                "staged_files",
                attachment_manifest=manifest,
            )
    if manifest:
        from ouroboros.gateway.tasks import _render_attachment_lines

        # The manifest is task authority, not merely presentation prose.  Keep
        # every staged/rejected declaration on the canonical carrier before the
        # task contract is normalized so a later promotion or child can inherit
        # and materialize the exact inputs.
        from ouroboros.artifacts import attachment_manifest_projection
        authority = attachment_manifest_projection(drive_root, task_id, manifest)
        task.update(authority)
        task["attachments"] = authority["attachment_manifest"]
        task["attachment_images"] = [
            dict(item) for item in manifest
            if str(item.get("status") or "staged") == "staged" and item.get("is_image")
        ]
        rendered = _render_attachment_lines(authority)
        if rendered:
            task["text"] = f"{task['text']}\n\n[ATTACHMENTS]\n{rendered}\n[END_ATTACHMENTS]".strip()
    if not task["text"]:
        task["text"] = "(attachments received)" if manifest else "(empty presence event)"
    return attach_task_contract(task)


def run_presence_turn(
    *,
    admission: PresenceAdmission,
    event: PresenceTurnEvent,
    repo_dir: Path,
    drive_root: Path,
    staged_files: Sequence[Path] = (),
    event_queue: Any = None,
    agent_factory: Callable[..., Any] | None = None,
    gate: PresenceTurnGate | None = None,
) -> PresenceTurnResult:
    """Run one bounded turn; adapters retain durable provider custody."""

    task_id = _task_id(admission, event)
    cached = _cached_result(Path(drive_root), task_id)
    if cached is not None and not _pointer_behind(Path(drive_root), event.conversation_key, task_id):
        return cached

    def execute() -> PresenceTurnResult:
        second_cached = _cached_result(Path(drive_root), task_id)
        if second_cached is not None:
            # A turn lost between its terminal write and its pointer write replays from the durable
            # row; its pointer is rebuilt here, under the conversation lock, so no newer turn is undone.
            _repair_previous_turn(Path(drive_root), event.conversation_key, task_id)
            return second_cached
        with _LIVE_LOCK:
            _LIVE_PRESENCE_TASKS.add(task_id)
        try:
            return _execute_live()
        finally:
            with _LIVE_LOCK:
                _LIVE_PRESENCE_TASKS.discard(task_id)

    def _execute_live() -> PresenceTurnResult:
        # Both locks are held: no other execution of this conversation runs, so a running or
        # interrupted row of this task (not yet reconciled) belongs to a lost attempt too.
        stored = load_task_result(Path(drive_root), task_id) or {}
        lost_attempt = is_reconciled_presence_placeholder(stored) or str(stored.get("status") or "") in {
            STATUS_RUNNING, STATUS_INTERRUPTED}
        prior_rows = _live_task_rows(Path(drive_root), task_id) if lost_attempt else []
        task = _build_task(
            admission,
            event,
            drive_root=Path(drive_root),
            staged_files=tuple(Path(item) for item in staged_files),
            lost_attempt=lost_attempt,
            prior_rows=prior_rows,
        )
        chat_id = int(task["chat_id"])
        actor_id = _stable_numeric_id("presence-actor-log", str(task.get("actor_id") or ""))
        if not any(row.get("direction") == "in" for row in prior_rows):  # the lost attempt already logged it
            _log_dialogue(
                Path(drive_root),
                direction="in",
                chat_id=chat_id,
                user_id=actor_id,
                text=event.text or task["text"],
                event=event,
                task=task,
                task_id=task_id,
            )
        if agent_factory is None:
            from ouroboros.agent import make_agent

            factory = make_agent
        else:
            factory = agent_factory
        agent = factory(
            repo_dir=str(repo_dir),
            drive_root=str(drive_root),
            event_queue=event_queue,
        )
        # The host mark moves aside only now: a rejected build or a failed factory leaves it intact.
        reopen_reconciled_presence_placeholder(Path(drive_root), task_id)
        events = agent.handle_task(task)
        row = next((item for item in events if item.get("type") == "presence_result"), None)
        if not isinstance(row, dict):
            raise PresenceTurnError("presence_result_missing", "presence_result")
        result = PresenceTurnResult(
            outcome=str(row.get("outcome") or "message"),
            text=str(row.get("text") or ""),
            task_id=task_id,
            work_ref=str(row.get("work_ref") or ""),
            delivery_reporting_version=event.delivery_reporting_version,
        )
        if result.outcome in {"message", "deferred"} and result.text and not result.delivery_reporting_version:
            _log_dialogue(
                Path(drive_root),
                direction="out",
                chat_id=chat_id,
                user_id=0,
                text=result.text,
                event=event,
                task=task,
                task_id=task_id,
            )
        # Still under the conversation lock; cached replays return before execute() and
        # never write, so an older replay cannot overwrite the newest executed turn.
        sends = _confirmed_sends(_live_task_rows(Path(drive_root), task_id)) if event.delivery_reporting_version else []
        delivery = "unknown"  # also when a mid-turn rotation hid this turn's receipts (sends is None)
        if sends is not None and event.delivery_reporting_version and (sends or result.text):
            delivery = "confirmed" if sends else "authored"  # the v1 reply's receipt arrives after this return
        _write_previous_turn(Path(drive_root), event.conversation_key, task_id, outcome=result.outcome,
                             message=str(row.get("message") or result.text), sends=sends or [],
                             work_ref=result.work_ref, finished_at=utc_now_iso(), delivery=delivery)
        return result

    return (gate or _configured_gate(Path(drive_root))).run(event.conversation_key, execute)


__all__ = [
    "PresenceTurnError",
    "PresenceTurnEvent",
    "PresenceTurnGate",
    "PresenceTurnResult",
    "build_presence_result_event",
    "presence_turn_is_live",
    "run_presence_turn",
]
