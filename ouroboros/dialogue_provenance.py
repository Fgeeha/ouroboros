"""Neutral rendering of exact actor and conversation facts in dialogue memory."""

from __future__ import annotations

import json
from typing import Any, Mapping

from ouroboros.contracts.chat_id_policy import HIDDEN_CHAT_ID, WEB_UI_CHAT_ID


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_presence_task(task: Mapping[str, Any]) -> bool:
    metadata = _mapping(task.get("metadata"))
    return bool(
        task.get("_presence_turn")
        or task.get("_presence_origin")
        or isinstance(metadata.get("presence"), Mapping)
    )


def presence_provenance_from_task(task: Mapping[str, Any]) -> dict[str, str]:
    """Return the stable, non-secret presence facts carried by one task.

    The host-authored event owns transport identity while the immutable
    capability ceiling owns the reviewed state/selection fingerprints.  Keep
    this projection small so dialogue and reflection records share one exact
    provenance shape without copying prompt text or arbitrary actor metadata.
    """

    metadata = _mapping(task.get("metadata"))
    presence = _mapping(metadata.get("presence"))
    if not presence:
        return {}
    event = _mapping(presence.get("event"))
    actor = _mapping(event.get("actor"))
    contract = _mapping(task.get("task_contract"))
    ceiling = _mapping(contract.get("capability_ceiling"))
    return {
        "binding_id": _text(presence.get("binding_id")),
        "transport_skill": _text(presence.get("transport_skill")),
        "behavior_skill": _text(presence.get("behavior_skill")),
        "profile_fingerprint": _text(ceiling.get("profile_fingerprint") or presence.get("profile_fingerprint")),
        "state_fingerprint": _text(ceiling.get("state_fingerprint")),
        "selection_fingerprint": _text(ceiling.get("selection_fingerprint")),
        "source_event_id": _text(event.get("source_event_id")),
        "conversation_key": _text(event.get("conversation_key")),
        "provider": _text(event.get("provider")),
        "account_id": _text(event.get("account_id")),
        "conversation_id": _text(event.get("conversation_id")),
        "thread_id": _text(event.get("thread_id")),
        "actor_id": _text(actor.get("platform_actor_id") or actor.get("id")),
    }


def presence_provenance_fields(task: Mapping[str, Any]) -> dict[str, Any]:
    value = presence_provenance_from_task(task)
    return {"presence_provenance": value} if value else {}


def dialogue_speaker(entry: Mapping[str, Any]) -> str:
    transport = entry.get("transport") if isinstance(entry.get("transport"), Mapping) else {}
    actor = transport.get("actor") if isinstance(transport.get("actor"), Mapping) else {}
    return str(
        entry.get("sender_label")
        or entry.get("username")
        or entry.get("author")
        or actor.get("display_name")
        or actor.get("username")
        or actor.get("platform_actor_id")
        or actor.get("id")
        or "User"
    )


def dialogue_provenance(entry: Mapping[str, Any]) -> str:
    transport = entry.get("transport") if isinstance(entry.get("transport"), Mapping) else {}
    facts = []
    for label, key in (
        ("provider", "provider"),
        ("account", "account_id"),
        ("conversation", "conversation_id"),
        ("thread", "thread_id"),
    ):
        value = str(transport.get(key) or "").strip()
        if value:
            facts.append(f"{label}={value}")
    source = str(entry.get("source") or "").strip()
    if source and not facts:
        facts.append(f"source={source}")
    delivery = _mapping(transport.get("delivery"))
    state = _text(delivery.get("state"))
    if state:
        label = {"authored": "authored (delivery unconfirmed)",
                 "accepted": "accepted (provider acceptance only)"}.get(state, state)
        facts.append(f"delivery={label}")
    return "; ".join(facts)


def dialogue_author(entry: Mapping[str, Any]) -> str:
    speaker = dialogue_speaker(entry)
    provenance = dialogue_provenance(entry)
    return f"{speaker} [{provenance}]" if provenance else speaker


def dialogue_text(entry: Mapping[str, Any]) -> str:
    """Keep observed delivery metadata distinct from the quoted message body."""
    text = str(entry.get("text", ""))
    transport = _mapping(entry.get("transport"))
    message = _mapping(transport.get("message"))
    if entry.get("type") == "presence_delivery" and message:
        text += "\n[Delivery details: " + json.dumps(dict(message), ensure_ascii=False, sort_keys=True) + "]"
    return text


class RoomLabelResolver:
    """Resolve source-room labels from one immutable registry snapshot.

    ``chat_id`` is the room authority.  Lineage fields such as ``project_id``
    are deliberately ignored here: a row can retain its original room while
    its work is later bound to a Project.  The snapshot is read once by the
    caller for a render/consolidation window, so formatting a line never scans
    the registry or writes resolver state.
    """

    def __init__(self, drive_root: Any = None, *, projects: Any = None) -> None:
        self._by_chat: dict[int, str] = {}
        self._ambiguous: set[int] = set()
        if projects is None and drive_root is not None:
            try:
                from ouroboros.projects_registry import list_reserved_projects

                projects = list_reserved_projects(drive_root)
            except Exception:
                projects = []
        for project in projects or []:
            if not isinstance(project, Mapping):
                continue
            try:
                raw_chat_id = project.get("chat_id")
                if isinstance(raw_chat_id, (bool, float)):
                    continue
                chat_id = int(raw_chat_id)
            except (TypeError, ValueError):
                continue
            if chat_id in {HIDDEN_CHAT_ID, WEB_UI_CHAT_ID}:
                continue
            if chat_id in self._by_chat:
                self._ambiguous.add(chat_id)
            else:
                self._by_chat[chat_id] = " ".join(str(project.get("name") or "").split())
        for chat_id in self._ambiguous:
            self._by_chat.pop(chat_id, None)

    @property
    def project_chat_ids(self) -> frozenset[int]:
        # Membership controls the existing focused view, independently of
        # whether a display name can be resolved without ambiguity.
        return frozenset(self._by_chat) | self._ambiguous

    @staticmethod
    def _chat_id(entry: Mapping[str, Any]) -> tuple[int | None, str]:
        """``(integral chat id, "")`` or ``(None, unresolved spelling)``; never a guess."""
        if "chat_id" not in entry or entry.get("chat_id") is None:
            return None, "missing"
        raw_chat_id = entry.get("chat_id")
        if isinstance(raw_chat_id, (bool, float)):
            return None, str(raw_chat_id)
        try:
            return int(raw_chat_id), ""
        except (TypeError, ValueError):
            return None, str(raw_chat_id)

    def room_id(self, entry: Mapping[str, Any]) -> str:
        """Stable host-set grouping key: the chat id itself, or the unresolved spelling.

        Consolidation partitions and era compression regroup by this key, so a
        renamed project keeps one room while a missing or malformed id can never
        merge into Main or into another room.
        """
        chat_id, unresolved = self._chat_id(entry)
        return str(chat_id) if chat_id is not None else f"unresolved:{unresolved}"

    def label(self, entry: Mapping[str, Any]) -> str:
        """Return an honest display label; no missing value defaults to Main."""
        chat_id, unresolved = self._chat_id(entry)
        if chat_id is None:
            return f"Unresolved room [chat_id={unresolved}]"
        if chat_id == WEB_UI_CHAT_ID:
            return "Main"
        if chat_id == HIDDEN_CHAT_ID:
            return "Hidden [chat_id=0]"
        if chat_id in self._ambiguous:
            return f"Ambiguous room [chat_id={chat_id}]"
        name = self._by_chat.get(chat_id)
        if name is not None:
            if name:
                return f"Project {name} [chat_id={chat_id}]"
            return f"Project name unavailable [chat_id={chat_id}]"
        return f"Unknown room [chat_id={chat_id}]"


def source_continuation_note(spans: list[tuple[int, int, str]], offset: int, part_end: int) -> str:
    """Carry only the continued message's header, never parse quoted body text.

    Spans are ephemeral character offsets recorded by the formatter, not a
    persistent ledger. Original source slices stay byte-exact and disjoint.
    """
    for index, (start, end, header) in enumerate(spans, 1):
        if start <= offset < end and (offset > start or part_end < start + len(header)):
            return ("## Source continuation\n"
                    f"This part continues source message {index}. Attribution: {header}\n"
                    "The header is context, not another message. Summarize only the supplied "
                    "source portion; do not infer or repeat unsupplied body text.\n")
    return ""


__all__ = [
    "dialogue_author",
    "dialogue_provenance",
    "dialogue_speaker",
    "dialogue_text",
    "RoomLabelResolver",
    "source_continuation_note",
    "is_presence_task",
    "presence_provenance_fields",
    "presence_provenance_from_task",
]
