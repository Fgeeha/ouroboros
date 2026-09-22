"""Owner-approved room provenance; all history and registry data are synthetic."""
from __future__ import annotations

import json

import pytest

from ouroboros import consolidator as c, projects_registry, room_consolidation as rc
from ouroboros.context import build_recent_sections
from ouroboros.dialogue_provenance import RoomLabelResolver, source_continuation_note
from ouroboros.memory import Memory
from tests.test_consolidator_context_fit import _LLM, fit  # shared isolated Light route fixture


def _write_chat(root, rows):
    path = root / "logs" / "chat.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return path


def _registry(root, projects):
    path = root / "state" / "projects.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")
    return path


def _recent(memory, chat_id):
    sections = build_recent_sections(memory, None, thread_chat_id=chat_id)
    return next(s for s in sections if s.startswith("## Recent chat\n"))


def test_room_resolution_is_current_read_only_and_not_lineage(tmp_path, monkeypatch):
    project = projects_registry.create_project(tmp_path, "alpha", name="Original")
    projects_registry.update_project(tmp_path, "alpha", name="Renamed")
    path = tmp_path / "state" / "projects.json"
    before = path.read_bytes()
    read = projects_registry.list_reserved_projects
    calls = []
    monkeypatch.setattr(projects_registry, "list_reserved_projects", lambda root: (calls.append(root), read(root))[1])
    resolver = RoomLabelResolver(tmp_path)
    for _ in range(10):
        assert resolver.label({"chat_id": 1, "project_id": "alpha"}) == "Main"
        assert resolver.label({"chat_id": project["chat_id"], "project_id": "wrong-lineage"}) == (
            f"Project Renamed [chat_id={project['chat_id']}]"
        )
        assert resolver.label({"project_id": "alpha"}) == "Unresolved room [chat_id=missing]"
    assert calls == [tmp_path]
    assert path.read_bytes() == before


@pytest.mark.parametrize("lifecycle", ["active", "deleting", "tombstoned"])
def test_reserved_names_and_removed_or_ambiguous_rooms(tmp_path, lifecycle):
    row = {"id": "alpha", "chat_id": 1500, "name": "Alpha ] team", "lifecycle": lifecycle}
    path = _registry(tmp_path, [row])
    assert RoomLabelResolver(tmp_path).label({"chat_id": 1500}) == "Project Alpha ] team [chat_id=1500]"
    _registry(tmp_path, [{**row, "name": ""}])
    assert RoomLabelResolver(tmp_path).label({"chat_id": 1500}) == "Project name unavailable [chat_id=1500]"
    _registry(tmp_path, [row, {**row, "id": "other", "name": "Other"}])
    resolver = RoomLabelResolver(tmp_path)
    assert resolver.label({"chat_id": 1500}) == "Ambiguous room [chat_id=1500]"
    assert 1500 in resolver.project_chat_ids  # Label uncertainty must not widen focused visibility.
    _registry(tmp_path, [])
    assert RoomLabelResolver(tmp_path).label({"chat_id": 1500}) == "Unknown room [chat_id=1500]"
    path.unlink()
    assert RoomLabelResolver(tmp_path).label({"chat_id": 1500}) == "Unknown room [chat_id=1500]"
    assert not path.exists()


@pytest.mark.parametrize("entry,label", [
    ({}, "Unresolved room [chat_id=missing]"),
    ({"chat_id": None}, "Unresolved room [chat_id=missing]"),
    ({"chat_id": "oops"}, "Unresolved room [chat_id=oops]"),
    ({"chat_id": True}, "Unresolved room [chat_id=True]"),
    ({"chat_id": 1.2}, "Unresolved room [chat_id=1.2]"),
    ({"chat_id": "1"}, "Main"),
    ({"chat_id": 0}, "Hidden [chat_id=0]"),
    ({"chat_id": 987654}, "Unknown room [chat_id=987654]"),
])
def test_unknown_address_never_defaults_to_main(entry, label):
    assert RoomLabelResolver(projects=[]).label(entry) == label


def test_actual_main_context_opts_in_once_and_keeps_existing_visibility(tmp_path, monkeypatch):
    _registry(tmp_path, [{"id": "alpha", "chat_id": 1500, "name": "Alpha"}])
    rows = [
        {"chat_id": 1, "direction": "in", "text": "MAIN", "project_id": "alpha"},
        {"chat_id": 1500, "direction": "out", "text": "PROJECT"},
        {"chat_id": 987654, "direction": "system", "text": "UNKNOWN"},
        {"direction": "in", "text": "MISSING"},
        {"chat_id": 0, "direction": "system", "text": "HIDDEN"},
        {"chat_id": -10, "direction": "in", "text": "A2A EXCLUDED"},
    ]
    _write_chat(tmp_path, rows)
    memory = Memory(tmp_path)
    expected_rows, _ = memory.read_unconsolidated_chat({}, 1000)
    read = projects_registry.list_reserved_projects
    calls = []
    monkeypatch.setattr(projects_registry, "list_reserved_projects", lambda root: (calls.append(root), read(root))[1])
    recent = _recent(memory, 1)
    assert calls == [tmp_path]
    assert recent == "## Recent chat\n\n" + memory.summarize_chat(
        expected_rows, include_room_labels=True, room_resolver=RoomLabelResolver(projects=read(tmp_path)),
    )
    for marker in ("[room=Main]", "[room=Project Alpha [chat_id=1500]]",
                   "[room=Unknown room [chat_id=987654]]", "[room=Unresolved room [chat_id=missing]]"):
        assert marker in recent
    assert "A2A EXCLUDED" not in recent
    assert recent.index("MAIN") < recent.index("PROJECT") < recent.index("UNKNOWN") < recent.index("MISSING")


@pytest.mark.parametrize("ambiguous", [False, True])
def test_focused_project_and_explicit_history_remain_byte_identical(tmp_path, monkeypatch, ambiguous):
    monkeypatch.setattr("ouroboros.memory._chat_history_snapshot_id", lambda *_: "fixture")
    projects = [{"id": "alpha", "chat_id": 1500, "name": "Alpha"}]
    if ambiguous:
        projects.append({"id": "beta", "chat_id": 1500, "name": "Beta"})
    _registry(tmp_path, projects)
    base = {"ts": "2026-01-01T00:01:00Z", "direction": "in", "sender_label": "Alex"}
    _write_chat(tmp_path, [
        {**base, "chat_id": 1, "text": "main"},
        {**base, "chat_id": 1500, "text": "project\nsecond line", "transport": {"provider": "mail"}},
        {**base, "chat_id": 1501, "text": "sibling"},
        {**base, "chat_id": -10, "text": "a2a"},
    ])
    memory = Memory(tmp_path)
    assert _recent(memory, 1500).encode() == (
        "## Recent chat\n\n← 00:01 [Alex [provider=mail]] project\nsecond line"
    ).encode()
    expected_history = (
        "Showing 3 of 3 messages; 0 older remain. Continue with offset=3, snapshot=fixture."
        " Pagination used a live offset; repeating an offset without the returned snapshot"
        " is shiftable if history changes.\n\n"
        "← [2026-01-01T00:01] [Alex] main\n"
        "← [2026-01-01T00:01] [Alex [provider=mail]] project\nsecond line\n"
        "← [2026-01-01T00:01] [Alex] sibling"
    ).encode()
    for chat_id in (1, 1500):
        assert memory.chat_history(chat_id=chat_id).encode() == expected_history


@pytest.mark.parametrize("direction", ["in", "incoming", "out", "outgoing", "system"])
def test_block_format_retains_author_direction_transport_and_body(direction):
    row = {"ts": "2026-01-01T00:00:00Z", "chat_id": 1500, "direction": direction,
           "sender_label": "Alex", "text": "line one\r\n\r\nЖ🙂 line two\n",
           "transport": {"provider": "mail", "account_id": "acct", "conversation_id": "conv",
                         "thread_id": "thread", "delivery": {"state": "accepted"}}}
    resolver = RoomLabelResolver(projects=[{"id": "alpha", "chat_id": 1500, "name": "Alpha"}])
    old = c._format_entries_for_block([row])
    new = c._format_entries_for_block([row], include_room_labels=True, room_resolver=resolver)
    assert new.replace("[room=Project Alpha [chat_id=1500]] ", "", 1).encode() == old.encode()
    assert new.endswith(row["text"])
    assert "provider=mail; account=acct; conversation=conv; thread=thread; delivery=accepted" in new
    assert ("Ouroboros" if direction in {"out", "outgoing", "system"} else "Alex") in new


@pytest.mark.parametrize("rooms", [(1, 1, 1, 1), (1, 1500, 987654, None)])
def test_actual_consolidation_labels_every_source_and_retains_token_ceiling(tmp_path, fit, monkeypatch, rooms):
    _registry(tmp_path, [{"id": "alpha", "chat_id": 1500, "name": "Alpha"}])
    rows = [{"ts": f"2026-01-01T00:{i:02d}:00Z", "chat_id": room, "direction": "in", "text": str(i)}
            for i, room in enumerate(rooms)]
    chat = _write_chat(tmp_path, [*rows, {"chat_id": -10, "text": "A2A EXCLUDED"}])
    monkeypatch.setattr(c, "BLOCK_SIZE", 2)
    read = projects_registry.list_reserved_projects
    calls = []
    monkeypatch.setattr(projects_registry, "list_reserved_projects", lambda root: (calls.append(root), read(root))[1])
    llm = _LLM()
    c.consolidate(chat, tmp_path / "memory/blocks.json", tmp_path / "memory/meta.json", llm)
    assert calls == [tmp_path]
    # Each room is drafted and then source-checked; two logical chunks are
    # processed, with one or two rooms per chunk depending on the fixture.
    assert len(llm.calls) == (4 if len(set(rooms)) == 1 else 8)
    resolver = RoomLabelResolver(projects=read(tmp_path))
    for call in llm.calls:
        assert call["max_tokens"] == 16384
        assert "A2A EXCLUDED" not in call["messages"][0]["content"]
        assert "[room=" in call["messages"][0]["content"]


def test_room_source_split_preserves_exact_bytes_and_boundaries():
    rows = [
        {"chat_id": 1500, "direction": "in", "text": "A body\n\n" * 80},
        {"chat_id": 1501, "direction": "out", "text": "B body\n\n" * 80},
    ]
    resolver = RoomLabelResolver(projects=[
        {"id": "alpha", "chat_id": 1500, "name": "Alpha"},
        {"id": "beta", "chat_id": 1501, "name": "Beta"},
    ])
    spans = []
    source = c._format_entries_for_block(rows, include_room_labels=True, room_resolver=resolver, source_spans=spans)
    left, right = rc.split_source_text(source, tuple(start for start, _, _ in spans))
    assert left + right == source
    assert right.startswith(spans[1][2])
    assert "[room=Fake]" not in source


def test_boundary_split_does_not_add_rooms_and_prompts_are_adaptive():
    spans = []
    source = c._format_entries_for_block([
        {"chat_id": 1, "text": "body\n\n" * 20},
        {"chat_id": 555, "text": "other\n\n" * 20},
    ], include_room_labels=True, source_spans=spans)
    left, right = rc.split_source_text(source, tuple(start for start, _, _ in spans))
    assert left + right == source and right.startswith(spans[1][2])
    assert source_continuation_note(spans, len(left), len(source)) == ""
    assert spans[0][2] in source_continuation_note(spans, 0, 2)
    prompt = rc.room_draft_prompt(source, room_label="test", block_range_text="range", message_count=2)
    assert "fixed total word range" not in prompt
    assert "First person as Ouroboros" in prompt and "source" in prompt


def test_era_prompt_preserves_rooms_with_original_token_ceiling(fit):
    llm = _LLM()
    era, _ = c._compress_blocks_to_era([
        {"range": "2026-01-01 00:00 - 00:01", "message_count": 1, "content": "Project A decision"},
        {"range": "2026-01-01 00:02 - 00:03", "message_count": 1, "content": "Project B approval"},
    ], llm, "")
    assert era and len(llm.calls) == 2  # legacy records share one unknown-provenance room
    call = llm.calls[0]
    prompt = call["messages"][0]["content"]
    assert "one room" in prompt and "other rooms are compressed separately" in prompt
    assert "open commitments" in prompt and "one first-person Ouroboros" in prompt
    assert call["max_tokens"] == 16384


def test_draft_nominations_are_released_only_with_their_corrected_part():
    """A draft whose correction failed never entered the block, so its
    nominations must not survive the split that replaces it (claim_2)."""
    spans = []
    rows = [{"ts": f"2026-01-01T00:0{i}:00Z", "direction": "in", "text": f"entry-{i} " + "Ж🙂x" * 40, "chat_id": 1}
            for i in range(2)]
    text = c._format_entries_for_block(rows, include_room_labels=True, source_spans=spans)
    spans = [(start, end, note) for start, end, note in spans]

    class _Knowledge:
        def bind_entries(self, entries):
            return list(entries or [])

    calls = []

    def call(prompt, label, *, fixed_prompt="", input_limit=None, call_type=""):
        calls.append((label, prompt))
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}
        if label == "Room summary":
            nomination = "" if len(calls) > 1 else '\nKNOWLEDGE_ENTRIES_JSON: [{"topic":"leak","scope":"global","content":"from a discarded draft"}]'
            return f"draft-{len(calls)}{nomination}", usage, _Knowledge()
        if len(calls) == 2:  # the FIRST correction (whole source) overflows -> the part is split
            return "", {**usage, "_consolidation_errors": [{
                "kind": "context_overflow", "preflight_only": True, "message": "too big",
                "fixed_tokens": 1, "fixed_bytes": 1}]}, _Knowledge()
        return f"corrected-{len(calls)}", usage, _Knowledge()

    draft_prompt = lambda part, note: rc.room_draft_prompt(  # noqa: E731
        part, room_label="Main", block_range_text="r", message_count=2, identity_text="", continuation_note=note)
    correct_prompt = lambda draft, part, note: rc.correction_prompt(  # noqa: E731
        draft, part, room_label="Main", scope="block r", identity_text="", continuation_note=note)
    content, usage = rc.summarize_source(call, text, spans, draft_prompt, correct_prompt)

    assert content and "corrected-" in content
    labels = [label for label, _ in calls]
    assert labels == ["Room summary", "Room correction"] + ["Room summary", "Room correction"] * 2
    assert "_knowledge_entries" not in usage, usage.get("_knowledge_entries")
