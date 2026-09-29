"""A Project room is read where the reader can see the newest message (DESIGN "Project unread dot").

Real server, real retained chat history, real history endpoint and read cursor:
a late answer keeps the time it was written, so it sorts into the middle of the
room while it is the message that arrived last. The composer is drawn over the
bottom of the feed; the answer beneath it intersects the feed's viewport but is
not visible, so the room stays unread until the answer clears the composer. A
newest reply in its ordinary place is read the same way: the owner's own later
messages and a task card below it are not conversation messages, and being at
the bottom past them is not reading it. An ordinary room is read on landing; a
question delivered live is read once the next read names it and its card is on
screen; a late answer written before the whole recent window is read only after
`Load older` shows it. Clients share one read cursor: a room read on one clears
the dot on another at its next state refresh.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data as direct_server_with_data
from tests.ui_chat_viewport_smoke import _CAPTURE_TEST_SOCKET, _SETTLE_RESTORE_FRAMES, _emit_ws_frame

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
PANEL = "#project-panel .chat-messages"
LATE = "Late final answer"
NEWEST = "Newest reply in its place"
QUESTION = "Merge the release branch now?"
# Place the message's top `offset` px below the composer's top edge, and report
# the geometry the reader actually has.
_PLACE = """(messages, [text, offset]) => {
    const node = [...messages.querySelectorAll('[data-history-id]')].find(n => n.textContent.includes(text));
    const composer = messages.parentElement.querySelector('.chat-input-area');
    messages.scrollTop += node.getBoundingClientRect().top - (composer.getBoundingClientRect().top + offset);
    messages.dispatchEvent(new Event('scroll'));
    const box = (el) => { const r = el.getBoundingClientRect(); return {top: r.top, bottom: r.bottom}; };
    return {node: box(node), feed: box(messages), composer: box(composer), id: node.dataset.historyId};
}"""
_AT_BOTTOM = """(messages) => {
    messages.scrollTop = messages.scrollHeight;
    messages.dispatchEvent(new Event('scroll'));
    return messages.scrollHeight - messages.scrollTop - messages.clientHeight;
}"""
_BOX_OF = """(messages, text) => {
    const node = [...messages.querySelectorAll('[data-history-id]')].find(n => n.textContent.includes(text));
    const r = node.getBoundingClientRect(), feed = messages.getBoundingClientRect();
    return {top: r.top, bottom: r.bottom, feedTop: feed.top};
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_a_late_answer_under_the_composer_is_not_read_until_it_clears_it(direct_server_with_data, engine):
    from playwright.sync_api import sync_playwright

    from ouroboros.projects_registry import create_project, increment_project_visible_revision

    data = direct_server_with_data["data_dir"]
    project = create_project(data, "late-room", name="Late room")
    chat_id = project["chat_id"]
    rows = [{"ts": f"2026-09-28T10:{minute:02d}:00Z", "direction": "out", "chat_id": chat_id,
             "text": f"Reply {minute}\nline two\nline three"} for minute in range(30)]
    # Arrived last, written at 10:05:30: it sorts between Reply 5 and Reply 6.
    rows.append({"ts": "2026-09-28T10:05:30Z", "direction": "out", "chat_id": chat_id,
                 "text": f"{LATE}\nwith its details"})
    (data / "logs").mkdir(parents=True, exist_ok=True)
    (data / "logs" / "chat.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    revision = increment_project_visible_revision(data, chat_id=chat_id)["visible_revision"]
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", data.parent))
    evidence.mkdir(parents=True, exist_ok=True)
    acks = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1187, "height": 734})
            page.on("request", lambda request: acks.append(json.loads(request.post_data or "{}"))
                    if request.method == "POST" and request.url.endswith("/api/ui/preferences") else None)
            page.add_init_script(f"({_CAPTURE_TEST_SOCKET})()")
            page.goto(direct_server_with_data["url"], wait_until="domcontentloaded")
            page.wait_for_function("() => window.__testSockets?.some(s => s.readyState === 1)")
            row = page.locator('.nav-project-row[data-project-id="late-room"]')
            row.locator(".nav-unread-dot").wait_for(state="attached")
            row.evaluate("el => el.click()")
            messages = page.locator(PANEL)
            messages.locator(".chat-bubble").filter(has_text=LATE).wait_for(state="attached")
            page.evaluate(_SETTLE_RESTORE_FRAMES)
            seen = lambda: [ack for ack in acks if "late-room" in (ack.get("project_seen_revision") or {})]  # noqa: E731
            assert seen() == [], "landing at the bottom is not reading an answer placed above it"

            under = messages.evaluate(_PLACE, [LATE, 12])
            page.evaluate(_SETTLE_RESTORE_FRAMES)
            page.screenshot(path=str(evidence / f"read-band-{engine}-under-composer.png"))
            assert under["node"]["top"] < under["feed"]["bottom"], under  # it intersects the feed's viewport
            assert under["node"]["top"] >= under["composer"]["top"], under  # ...but only beneath the composer
            page.wait_for_timeout(300)
            assert seen() == [], ("an answer beneath the composer is not read", under)
            assert row.locator(".nav-unread-dot").count() == 1

            clear = messages.evaluate(_PLACE, [LATE, -160])
            page.evaluate(_SETTLE_RESTORE_FRAMES)
            assert clear["node"]["bottom"] <= clear["composer"]["top"], clear
            for _ in range(50):
                if seen():
                    break
                page.wait_for_timeout(100)
            page.screenshot(path=str(evidence / f"read-band-{engine}-clear.png"))
            assert seen() == [{"project_seen_revision": {"late-room": revision}}], clear
            row.locator(".nav-unread-dot").wait_for(state="detached")
            (evidence / f"read-band-{engine}.json").write_text(
                json.dumps({"under": under, "clear": clear, "acks": acks}, indent=2), encoding="utf-8")
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_an_in_place_newest_reply_below_the_fold_is_not_read_at_the_bottom(direct_server_with_data, engine):
    from playwright.sync_api import sync_playwright

    from ouroboros.projects_registry import create_project, increment_project_visible_revision

    data = direct_server_with_data["data_dir"]
    project = create_project(data, "tail-room", name="Tail room")
    chat_id = project["chat_id"]
    at = lambda minute: f"2026-09-28T10:{minute:02d}:00Z"  # noqa: E731
    rows = [{"ts": at(minute), "direction": "out", "chat_id": chat_id,
             "text": f"Reply {minute}\nline two\nline three"} for minute in range(20)]
    # The newest reply arrived last and sorts last among the conversation messages.
    rows.append({"ts": at(30), "direction": "out", "chat_id": chat_id, "text": f"{NEWEST}\nwith its details"})
    # After it: the owner's own follow-ups and a row the host placed in a task card.
    rows += [{"ts": at(31 + index), "direction": "in", "chat_id": chat_id,
              "text": "\n".join(f"Owner follow-up {index}, line {line}" for line in range(8))} for index in range(6)]
    rows.append({"ts": at(41), "direction": "system", "chat_id": chat_id, "task_id": "tail-task",
                 "type": "custody_notice", "card_row": "timeline", "card_row_id": "final:tail-task:custody",
                 "text": "Custody settled"})
    (data / "logs").mkdir(parents=True, exist_ok=True)
    (data / "logs" / "chat.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (data / "logs" / "progress.jsonl").write_text(json.dumps(
        {"ts": at(40), "chat_id": chat_id, "task_id": "tail-task", "content": "Working on the follow-up"}) + "\n",
        encoding="utf-8")
    revision = increment_project_visible_revision(data, chat_id=chat_id)["visible_revision"]
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", data.parent))
    evidence.mkdir(parents=True, exist_ok=True)
    acks = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1187, "height": 734})
            page.on("request", lambda request: acks.append(json.loads(request.post_data or "{}"))
                    if request.method == "POST" and request.url.endswith("/api/ui/preferences") else None)
            page.add_init_script(f"({_CAPTURE_TEST_SOCKET})()")
            page.goto(direct_server_with_data["url"], wait_until="domcontentloaded")
            page.wait_for_function("() => window.__testSockets?.some(s => s.readyState === 1)")
            row = page.locator('.nav-project-row[data-project-id="tail-room"]')
            row.locator(".nav-unread-dot").wait_for(state="attached")
            row.evaluate("el => el.click()")
            messages = page.locator(PANEL)
            messages.locator(".chat-bubble").filter(has_text="Owner follow-up 5").wait_for(state="attached")
            page.evaluate(_SETTLE_RESTORE_FRAMES)
            seen = lambda: [ack for ack in acks if "tail-room" in (ack.get("project_seen_revision") or {})]  # noqa: E731

            gap = messages.evaluate(_AT_BOTTOM)
            page.evaluate(_SETTLE_RESTORE_FRAMES)
            page.wait_for_timeout(300)
            bottom = messages.evaluate(_BOX_OF, NEWEST)
            page.screenshot(path=str(evidence / f"read-band-{engine}-tail-bottom.png"))
            assert gap <= 1 and bottom["bottom"] <= bottom["feedTop"], ("at the bottom, the reply is above the fold",
                                                                        gap, bottom)
            assert seen() == [], "the bottom of the conversation is not the newest reply"
            assert row.locator(".nav-unread-dot").count() == 1

            clear = messages.evaluate(_PLACE, [NEWEST, -160])
            page.evaluate(_SETTLE_RESTORE_FRAMES)
            assert clear["node"]["bottom"] <= clear["composer"]["top"], clear
            for _ in range(50):
                if seen():
                    break
                page.wait_for_timeout(100)
            page.screenshot(path=str(evidence / f"read-band-{engine}-tail-clear.png"))
            assert seen() == [{"project_seen_revision": {"tail-room": revision}}], clear
            row.locator(".nav-unread-dot").wait_for(state="detached")
        finally:
            browser.close()


def _seed_room(data, project_id, name, rows):
    from ouroboros.projects_registry import create_project, increment_project_visible_revision

    chat_id = create_project(data, project_id, name=name)["chat_id"]
    chat_log = data / "logs" / "chat.jsonl"
    chat_log.parent.mkdir(parents=True, exist_ok=True)
    chat_log.write_text("".join(json.dumps({**row, "chat_id": chat_id}) + "\n" for row in rows), encoding="utf-8")
    return chat_id, chat_log, increment_project_visible_revision(data, chat_id=chat_id)["visible_revision"]


def _client(browser, url, project_id, acks):
    """One client showing the room's row unread; ``acks`` collects its read receipts."""
    page = browser.new_page(viewport={"width": 1187, "height": 734})
    page.on("request", lambda request: acks.append(json.loads(request.post_data or "{}"))
            if request.method == "POST" and request.url.endswith("/api/ui/preferences") else None)
    page.add_init_script(f"({_CAPTURE_TEST_SOCKET})()")
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_function("() => window.__testSockets?.some(s => s.readyState === 1)")
    row = page.locator(f'.nav-project-row[data-project-id="{project_id}"]')
    row.locator(".nav-unread-dot").wait_for(state="attached")
    return page, row


def _open_room(pw, engine, url, project_id, acks):
    browser = getattr(pw, engine).launch()
    page, row = _client(browser, url, project_id, acks)
    row.evaluate("el => el.click()")
    return browser, page, row


def _wait_for(page, check, attempts=100):
    for _ in range(attempts):
        if check():
            return True
        page.wait_for_timeout(100)
    return check()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_a_question_delivered_live_is_read_where_its_card_is_shown(direct_server_with_data, engine):
    """The live card is the node the next read names: it gains that row, so the reader at it has read."""
    from playwright.sync_api import sync_playwright

    from ouroboros.projects_registry import increment_project_visible_revision

    data = direct_server_with_data["data_dir"]
    chat_id, chat_log, first = _seed_room(data, "ask-room", "Ask room", [
        {"ts": f"2026-09-28T10:{minute:02d}:00Z", "direction": "out", "text": f"Reply {minute}"} for minute in range(4)])
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", data.parent))
    evidence.mkdir(parents=True, exist_ok=True)
    acks = []
    seen = lambda: [ack["project_seen_revision"]["ask-room"] for ack in acks  # noqa: E731
                    if "ask-room" in (ack.get("project_seen_revision") or {})]
    with sync_playwright() as pw:
        browser, page, row = _open_room(pw, engine, direct_server_with_data["url"], "ask-room", acks)
        try:
            messages = page.locator(PANEL)
            messages.locator(".chat-bubble").filter(has_text="Reply 3").wait_for(state="attached")
            page.evaluate(_SETTLE_RESTORE_FRAMES)
            assert _wait_for(page, lambda: seen() == [first]), ("an ordinary room is read on landing", acks)
            row.locator(".nav-unread-dot").wait_for(state="detached")

            # The bridge broadcasts the question, then persists it and advances the revision.
            ts = "2026-09-28T10:30:00Z"
            quiz = {"quiz_id": "q-live", "wait_for_answer": False, "options": [{"label": "Yes"}, {"label": "No"}],
                    "stake": "", "assumption": "", "state": "open"}
            _emit_ws_frame(page, {"type": "quiz", "role": "assistant", "question": QUESTION, "ts": ts,
                                  "chat_id": chat_id, "task_id": "ask-task", **quiz})
            card = messages.locator(".chat-quiz-card").filter(has_text=QUESTION)
            card.wait_for(state="visible")
            assert card.evaluate("n => n.closest('[data-history-id]')") is None, "live, the card names no row yet"
            with chat_log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"ts": ts, "direction": "out", "chat_id": chat_id, "text": QUESTION,
                                         "type": "quiz", "task_id": "ask-task", "quiz": quiz}) + "\n")
            second = increment_project_visible_revision(data, chat_id=chat_id)["visible_revision"]
            _emit_ws_frame(page, {"type": "projects_changed"})
            read = _wait_for(page, lambda: seen() == [first, second])
            page.evaluate(_SETTLE_RESTORE_FRAMES)
            page.screenshot(path=str(evidence / f"read-band-{engine}-live-question.png"))
            stamped = card.evaluate("n => n.closest('[data-history-id]')?.dataset.historyId")
            geometry = card.evaluate("""n => {
                const box = (el) => { const r = el.getBoundingClientRect(); return {top: r.top, bottom: r.bottom}; };
                const feed = n.closest('.chat-messages');
                return {card: box(n), feed: box(feed), composer: box(feed.parentElement.querySelector('.chat-input-area'))};
            }""")
            (evidence / f"read-band-{engine}-live-question.json").write_text(
                json.dumps({"acks": acks, "stamped": stamped, "box": geometry}, indent=2), encoding="utf-8")
            assert geometry["card"]["top"] < geometry["composer"]["top"], ("the card is on screen", geometry)
            assert read, ("the question on screen is read", acks, stamped, geometry)
            assert str(stamped).startswith("chat:"), stamped
            row.locator(".nav-unread-dot").wait_for(state="detached")
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_a_late_answer_below_the_recent_window_is_read_once_an_older_page_shows_it(direct_server_with_data, engine):
    """Written before every reply of the 150-row recent window but arriving last, the answer is
    delivered only by the first older page, and read only there, on screen."""
    from playwright.sync_api import sync_playwright

    data = direct_server_with_data["data_dir"]
    rows = [{"ts": f"2026-09-28T{10 + minute // 60:02d}:{minute % 60:02d}:00Z", "direction": "out",
             "text": f"Reply {minute}"} for minute in range(160)]
    rows.append({"ts": "2026-09-28T09:00:00Z", "direction": "out", "text": f"{LATE}\nwith its details"})
    _chat_id, _chat_log, revision = _seed_room(data, "deep-room", "Deep room", rows)
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", data.parent))
    evidence.mkdir(parents=True, exist_ok=True)
    acks = []
    seen = lambda: [ack["project_seen_revision"]["deep-room"] for ack in acks  # noqa: E731
                    if "deep-room" in (ack.get("project_seen_revision") or {})]
    with sync_playwright() as pw:
        browser, page, row = _open_room(pw, engine, direct_server_with_data["url"], "deep-room", acks)
        try:
            messages = page.locator(PANEL)
            messages.locator(".chat-bubble").filter(has_text="Reply 159").wait_for(state="attached")
            page.evaluate(_SETTLE_RESTORE_FRAMES)
            page.wait_for_timeout(300)
            page.screenshot(path=str(evidence / f"read-band-{engine}-deep-bottom.png"))
            assert messages.locator(".chat-bubble").filter(has_text=LATE).count() == 0, "below the recent window"
            assert seen() == [], "the bottom of the recent window is not the answer that arrived last"
            assert row.locator(".nav-unread-dot").count() == 1

            page.locator(f"{PANEL} .chat-load-older button").evaluate("node => node.click()")
            messages.locator(".chat-bubble").filter(has_text=LATE).wait_for(state="attached")
            page.evaluate(_SETTLE_RESTORE_FRAMES)
            clear = messages.evaluate(_PLACE, [LATE, -160])
            page.evaluate(_SETTLE_RESTORE_FRAMES)
            assert clear["node"]["top"] >= clear["feed"]["top"] and clear["node"]["bottom"] <= clear["composer"]["top"], clear
            read = _wait_for(page, lambda: seen() == [revision])
            page.screenshot(path=str(evidence / f"read-band-{engine}-deep-older-page.png"))
            (evidence / f"read-band-{engine}-deep.json").write_text(
                json.dumps({"clear": clear, "acks": acks}, indent=2), encoding="utf-8")
            assert read, ("the answer on screen after the older page is read", acks, clear)
            row.locator(".nav-unread-dot").wait_for(state="detached")
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_a_room_read_on_one_client_clears_the_dot_on_another_at_its_next_state_refresh(direct_server_with_data, engine):
    """Two clients share one forward-only read cursor: the reader's receipt clears the other
    client's dot at its next state refresh, and the other client posts no receipt of its own."""
    from playwright.sync_api import sync_playwright

    data = direct_server_with_data["data_dir"]
    _chat_id, _chat_log, revision = _seed_room(data, "shared-room", "Shared room", [
        {"ts": f"2026-09-28T10:{minute:02d}:00Z", "direction": "out", "text": f"Reply {minute}"} for minute in range(4)])
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", data.parent))
    evidence.mkdir(parents=True, exist_ok=True)
    reader_acks, other_acks = [], []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        try:
            other, other_row = _client(browser, direct_server_with_data["url"], "shared-room", other_acks)
            reader, row = _client(browser, direct_server_with_data["url"], "shared-room", reader_acks)
            row.evaluate("el => el.click()")
            reader.locator(PANEL).locator(".chat-bubble").filter(has_text="Reply 3").wait_for(state="attached")
            reader.evaluate(_SETTLE_RESTORE_FRAMES)
            assert _wait_for(reader, lambda: [ack.get("project_seen_revision") for ack in reader_acks]
                             == [{"shared-room": revision}]), reader_acks
            row.locator(".nav-unread-dot").wait_for(state="detached")
            assert other_row.locator(".nav-unread-dot").count() == 1, "the other client has not refreshed yet"
            _emit_ws_frame(other, {"type": "projects_changed"})
            other_row.locator(".nav-unread-dot").wait_for(state="detached")
            other.screenshot(path=str(evidence / f"read-band-{engine}-other-client.png"))
            assert [ack for ack in other_acks if ack.get("project_seen_revision")] == [], \
                "the other client never read the room and acknowledges nothing"
        finally:
            browser.close()
