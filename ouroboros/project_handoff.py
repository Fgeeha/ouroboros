"""Main handoff receipts over existing bindings and the terminal-delivery outbox.

A receipt describes a transfer, never liveness. Its identity is an ingress
message plus destination, or the exact task when no message identity exists.
The outbox owns persistence/replay and duplicate suppression; no second store.
"""
from __future__ import annotations

import hashlib
import json
import logging

log = logging.getLogger(__name__)


def handoff_identity(project_id: str, task_id: str, source_ref: object) -> str:
    """Retries sharing a captured origin share a receipt; missing origins never merge."""
    from ouroboros.projects_registry import origin_key

    origin = origin_key(source_ref)
    identity = ["origin", *origin, project_id] if origin else ["task", task_id, project_id]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return f"project-handoff:{digest}"


def enqueue_project_handoff(drive_root, task_id: str, *, source_ref=None, origin_chat=None) -> bool:
    """Publish only a proven binding, retaining a recoverable failure after commit.

    Callers must not undo or misreport a successful bind when this delivery fails.
    Repeating the same conversion retries the same outbox identity. An explicit
    origin_chat is the caller's captured ingress address for a legacy ref-less
    conversion, not a default inferred from an absent source.
    """
    from ouroboros.projects_registry import project_binding_for_task, task_presentation_snapshot
    from supervisor.terminal_delivery import enqueue_terminal_delivery

    try:
        binding = project_binding_for_task(drive_root, task_id) or {}
        pid = str(binding.get("project_id") or "")
        if not pid:
            return False
        source = source_ref if source_ref is not None else binding.get("source_ref")
        chat = source.get("chat_id") if isinstance(source, dict) else origin_chat
        if chat != 1:
            return False
        snapshot = task_presentation_snapshot(drive_root, task_id, project_id=pid)
        if not snapshot["project_routable"]:
            return False
        return bool(enqueue_terminal_delivery(drive_root, {
            "type": "send_message", "chat_id": 1, "task_id": task_id,
            "role": "system", "system_type": "project_handoff",
            "text": snapshot["target_label"],
            "delivery_id": handoff_identity(pid, task_id, source),
            "progress_meta": {
                "project_id": pid, "project_name": snapshot["project_name"],
                "handoff_id": handoff_identity(pid, task_id, source),
                "target_label": snapshot["target_label"],
            },
        }))
    except Exception:
        log.warning("Project binding committed but handoff delivery unavailable for %s", task_id, exc_info=True)
        return False
