"""Notes: the mind's own words, shown to the owner at the time it chose.

``schedule_followup(notify=true)`` stores a ``kind: "notify"`` row in the ONE
schedule table (``queue_schedules.py``) holding the text the mind wrote and when
it wrote it. When the row is due, the scheduler tick consumes that occurrence in
the table — a one-shot is spent, a cron row moves to its next future point, so
any downtime gap collapses into one note — and, after the table lock is released,
this module puts the exact text in the owner's chat as one System row
(``system_type="reminder"``) signed with when it was written and when it was due.
No model call and no task: the next turn reads the row like any System row.

Consumed before shown: a failed table write shows nothing and the row stays due;
a show whose outcome is unknown is recorded on the row and never retried, so a
note can be lost once but is never shown twice.
"""

from __future__ import annotations

import datetime
import logging
import pathlib
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

NOTE_KIND = "notify"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def is_note(record: Dict[str, Any]) -> bool:
    return str(record.get("kind") or "") == NOTE_KIND


def owner_chat_id(drive_root: Any) -> int:
    """The owner's primary chat (bound by the first owner message), else Main."""
    from ouroboros.contracts.chat_id_policy import WEB_UI_CHAT_ID
    from supervisor.state import control_in_copy

    known, value = control_in_copy(pathlib.Path(drive_root) / "state" / "state.json", "owner_chat_id")
    try:
        chat_id = int(value) if known and value not in (None, "") else 0
    except (TypeError, ValueError):
        chat_id = 0
    return chat_id if chat_id > 0 else WEB_UI_CHAT_ID


def consume(record: Dict[str, Any], due_at: str, now: datetime.datetime) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Under the table lock: retire this occurrence; return ``(note, changed)``.

    The note is ``None`` when the row cannot be shown or cannot advance; its
    ``last_error`` says why, and an unchanged error does not rewrite the table.
    """
    from supervisor.schedule_time import next_cron_time, record_last_error

    notification = record.get("notification") if isinstance(record.get("notification"), dict) else {}
    text = str(notification.get("text") or "").strip()
    if not text:
        return None, record_last_error(record, "note has no text")
    trigger = record.get("trigger") if isinstance(record.get("trigger"), dict) else {}
    if str(trigger.get("type") or "cron") == "once":
        record.update(enabled=False, completed_at=now.isoformat(), next_run_at="")
    else:
        try:  # the successor first: a row that cannot advance would show again every pass
            successor = next_cron_time(str(trigger.get("expr") or record.get("cron") or ""), now)
        except Exception as exc:
            return None, record_last_error(record, f"{type(exc).__name__}: {exc}")
        record["next_run_at"] = successor.isoformat()
    record.update(last_run_at=now.isoformat(), last_error="")
    # The signature names who wrote the words: the mind's own follow-up is Ouroboros;
    # a row from any other source keeps that source rather than borrowing the voice.
    source = str(record.get("source") or "")
    return {"id": str(record.get("id") or ""), "text": text, "set_at": str(notification.get("set_at") or ""),
            "author": "Ouroboros" if source == "task_followup" else (source or "unknown source"),
            "scheduled_for": str(due_at or ""), "timezone": str(record.get("timezone") or "")}, True


def _clock(raw: str, tz: datetime.tzinfo) -> str:
    from supervisor.schedule_time import parse_schedule_time

    moment = parse_schedule_time(raw, tz)
    return f"{_MONTHS[moment.month - 1]} {moment.day} {moment:%H:%M}" if moment else ""


def note_text(note: Dict[str, Any], delivered_at: str) -> str:
    """The row: a host-composed signature line, then the mind's words verbatim.

    Times are the schedule's zone (the server's when none is stored), named once
    as a UTC offset; ``delivered`` appears whenever it reads differently from the
    due time, so a note held back by downtime shows both times.
    """
    from supervisor.schedule_time import parse_schedule_time, timezone_for_schedule

    tz = timezone_for_schedule({"timezone": note.get("timezone")})
    due = _clock(note.get("scheduled_for", ""), tz)
    delivered = _clock(delivered_at, tz)
    parts = ["Reminder", str(note.get("author") or "Ouroboros")]
    if written := _clock(note.get("set_at", ""), tz):
        parts.append(f"written {written}")
    parts.append(f"for {due}")
    if delivered and delivered != due:
        parts.append(f"delivered {delivered}")
    offset = (parse_schedule_time(delivered_at, tz) or datetime.datetime.now(tz)).utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    zone = "UTC" if not minutes else f"UTC{'+' if minutes > 0 else '-'}{abs(minutes) // 60}" + (
        f":{abs(minutes) % 60:02d}" if minutes % 60 else "")
    return f"{' · '.join(parts)} ({zone})\n{note['text']}"


def deliver(notes: List[Dict[str, Any]], drive_root: Any) -> None:
    """After the table lock: show each consumed note once; an unknown outcome is recorded, never retried."""
    if not notes:
        return
    from ouroboros.utils import utc_now_iso
    from supervisor.message_bus import send_with_budget

    chat_id, unconfirmed = owner_chat_id(drive_root), []
    for note in notes:
        delivered_at = utc_now_iso()
        try:
            send_with_budget(
                chat_id, note_text(note, delivered_at), role="system", system_type="reminder",
                require_write=True, ensure_record_boundary=True,
                progress_meta={"source": note["author"], "set_at": note["set_at"],
                               "scheduled_for": note["scheduled_for"], "delivered_at": delivered_at},
            )
        except Exception:
            log.warning("Note %s was consumed but its delivery is not confirmed; not retried", note["id"], exc_info=True)
            unconfirmed.append(note["id"])
    if unconfirmed:
        _record_unconfirmed(set(unconfirmed), drive_root)


def _record_unconfirmed(ids: set, drive_root: Any) -> None:
    from supervisor.queue_schedules import _write_scheduled_tasks, load_schedule_store, schedule_transaction
    from supervisor.schedule_time import record_last_error

    try:
        with schedule_transaction(drive_root):
            data = load_schedule_store(drive_root)
            changed = False
            for row in data.get("tasks") or []:
                if str(row.get("id") or "") in ids:
                    changed = record_last_error(row, "delivery not confirmed") or changed
            if changed:
                _write_scheduled_tasks(data, drive_root)
    except Exception:
        log.warning("Unconfirmed note delivery %s could not be recorded on its row", sorted(ids), exc_info=True)
