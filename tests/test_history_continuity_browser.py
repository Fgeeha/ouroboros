"""#1347: real retained bytes → gateway → Chat, with only transport faulted."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.test_chat_history_paging_browser import (
    _open, _open_project, _step, _idle, _write, _screenshot, _FRAMES,
)
from tests.ui_chat_viewport_smoke import _SETTLE_RESTORE_FRAMES
from tests.test_chat_history_recovery_browser import _click_project
from tests.test_ui_smoke_playwright import direct_server_with_data as direct_server_with_data

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]

_FAULT = """() => {
    const original = window.fetch.bind(window);
    window.__continuityFault = null;
    window.__continuityScroll = [];
    const scroll = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
    Object.defineProperty(Element.prototype, 'scrollTop', {...scroll, set(value) {
        if (this.classList?.contains('chat-messages')) {
            window.__continuityScroll.push({mode: window.__continuityFault, id: this.id,
                before: scroll.get.call(this), value, stack: new Error().stack});
            if (window.__continuityScroll.length > 100) window.__continuityScroll.shift();
        }
        scroll.set.call(this, value);
    }});
    window.fetch = async (input, init) => {
        const url = new URL(typeof input === 'string' ? input : input.url, location.href);
        if (window.__holdQuestion && url.pathname === '/api/tasks/continuity-question') {
            await new Promise(resolve => { window.__releaseQuestion = resolve; });
        }
        const fault = window.__continuityFault;
        if (fault && url.pathname === '/api/chat/history' && Number(url.searchParams.get('chat_id')) === fault.chatId) {
            const kind = url.searchParams.has('cursor') ? 'saved' : 'recent';
            if (fault.fail === kind) throw new TypeError('controlled history read failure');
            if (fault.delay && (!fault.delayKind || fault.delayKind === kind))
                await new Promise(resolve => setTimeout(resolve, fault.delay));
        }
        return original(input, init);
    };
}"""


@pytest.mark.parametrize("browser_engine", ["chromium", "webkit"])
def test_deep_reopen_delay_retry_and_live_arrival(direct_server_with_data, browser_engine, tmp_path):
    from ouroboros.projects_registry import create_project
    from playwright.sync_api import sync_playwright

    root = direct_server_with_data["data_dir"]
    project = create_project(root, "continuity-room", name="History continuity")
    other = create_project(root, "other-room", name="Other room")
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _write(root / "logs" / "chat.jsonl", [{
        "direction": "in", "chat_id": project["chat_id"],
        "ts": (start + timedelta(minutes=index)).isoformat(),
        "client_message_id": f"continuity-{index}",
        "text": f"Saved message {index:04d}. Reading the original discussion across archive pages.",
    } for index in range(1200)])
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 850})
            page.add_init_script(f"({_FAULT})()")
            _open(page, direct_server_with_data["url"])
            feed = _open_project(page, project)
            for _ in range(3):
                _step(page, feed, automatic=True)
            target = page.locator(f'{feed} [data-client-message-id="continuity-650"]')
            target.wait_for(state="attached")
            target.evaluate("""node => {
                const feed = node.closest('.chat-messages');
                feed.dispatchEvent(new WheelEvent('wheel', {deltaY: -1}));
                feed.scrollTop += node.getBoundingClientRect().top - feed.getBoundingClientRect().top - 80;
            }""")
            page.evaluate(_FRAMES)
            offset = target.evaluate("node => node.getBoundingClientRect().top - node.closest('.chat-messages').getBoundingClientRect().top")
            _screenshot(page, tmp_path, f"continuity-before-{browser_engine}")
            for mode in ("fast", "delayed", "recent", "saved"):
                page.locator('#project-panel-close').click()
                page.evaluate("fault => { window.__continuityFault = fault; }", {
                    "chatId": project["chat_id"], "delay": 800 if mode == "delayed" else 0,
                    "fail": mode if mode in {"recent", "saved"} else None,
                })
                _click_project(page, project)
                if mode in {"recent", "saved"}:
                    retry = page.locator(f'{feed} .chat-load-older button')
                    page.wait_for_function("feed => document.querySelector(`${feed} .chat-load-older button`)?.textContent === 'Retry loading messages'", arg=feed)
                    assert 'could not be loaded' in page.locator(feed).locator('..').locator('.chat-load-older-note').inner_text()
                    retry.evaluate("node => node.addEventListener('click', () => { window.__continuityFault = null; }, {once:true, capture:true})")
                    retry.click()
                _idle(page, feed)
                target.wait_for(state="attached")
                page.evaluate(_FRAMES)
                after = target.evaluate("node => node.getBoundingClientRect().top - node.closest('.chat-messages').getBoundingClientRect().top")
                if abs(after - offset) > 8:
                    (tmp_path / f"continuity-failed-{mode}-{browser_engine}.json").write_text(json.dumps(
                        page.evaluate("() => ({scroll:window.__continuityScroll, reads:window.__historyReads})"), indent=2))
                    _screenshot(page, tmp_path, f"continuity-failed-{mode}-{browser_engine}")
                assert abs(after - offset) <= 8, (mode, offset, after)
                assert 'Shown messages may have gaps' in page.locator(feed).locator('..').inner_text()
                _screenshot(page, tmp_path, f"continuity-{mode}-{browser_engine}")

            # Switching while both old reads are delayed cannot overwrite the
            # original destination with empty geometry or paint into the new room.
            page.locator('#project-panel-close').click()
            page.evaluate("fault => { window.__continuityFault = fault; }", {
                "chatId": project["chat_id"], "delay": 800})
            _click_project(page, project)
            page.locator(feed).wait_for(state="visible")
            page.locator('#project-panel-close').click()
            other_feed = _open_project(page, other)
            assert not page.locator(other_feed).get_by_text('Saved message 0650.', exact=False).count()
            page.locator('#project-panel-close').click()
            _click_project(page, project)
            _idle(page, feed)
            target.wait_for(state="attached")
            page.evaluate(_FRAMES)
            assert abs(target.evaluate("node => node.getBoundingClientRect().top - node.closest('.chat-messages').getBoundingClientRect().top") - offset) <= 8
            page.evaluate("() => { window.__continuityFault = null; }")
            _screenshot(page, tmp_path, f"continuity-room-switch-{browser_engine}")

            # A live answer stays below the reading island, without moving it.
            from tests.ui_chat_viewport_smoke import _emit_ws_frame
            _emit_ws_frame(page, {"type": "chat", "chat_id": project["chat_id"], "role": "assistant",
                                  "content": "LIVE_REPLY_AT_PRESENT", "ts": "2026-09-27T22:00:00Z"})
            page.evaluate(_FRAMES)
            assert page.locator(feed).get_by_text('LIVE_REPLY_AT_PRESENT', exact=True).count() == 1
            after = target.evaluate("node => node.getBoundingClientRect().top - node.closest('.chat-messages').getBoundingClientRect().top")
            assert abs(after - offset) <= 8
            reads = page.evaluate("() => window.__historyReads.length")
            resize_anchor = page.locator(feed).evaluate("""root => {
                const top = root.getBoundingClientRect().top;
                const node = [...root.querySelectorAll('.chat-bubble[data-history-id]')].find(n => n.getBoundingClientRect().bottom > top);
                return {id: node.dataset.historyId, offset: node.getBoundingClientRect().top - top};
            }""")
            page.set_viewport_size({"width": 760, "height": 850})
            page.evaluate(_FRAMES)
            assert page.evaluate("() => window.__historyReads.length") == reads, "layout starts no archive read"
            narrow = page.locator(f'{feed} [data-history-id="{resize_anchor["id"]}"]').evaluate(
                "node => node.getBoundingClientRect().top - node.closest('.chat-messages').getBoundingClientRect().top")
            assert abs(narrow - resize_anchor["offset"]) <= 8, (resize_anchor, narrow)
            _screenshot(page, tmp_path, f"continuity-live-narrow-{browser_engine}")
            (tmp_path / f"continuity-reads-{browser_engine}.json").write_text(json.dumps(
                page.evaluate("() => window.__historyReads"), indent=2), encoding="utf-8")
        finally:
            browser.close()


@pytest.mark.parametrize("browser_engine", ["chromium", "webkit"])
def test_terminal_publication_timing_and_answer_copy(direct_server_with_data, browser_engine, tmp_path, monkeypatch):
    from tests.test_terminal_occurrence import published_terminal_fixture
    from playwright.sync_api import sync_playwright

    root = direct_server_with_data['data_dir']
    direct_server_with_data['stop_server']()
    published_terminal_fixture(root, monkeypatch)
    direct_server_with_data['start_server']()
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1280, 'height':850}, timezone_id='UTC', locale='en-US')
            _open(page, direct_server_with_data['url'])
            def check():
                rows = page.locator('#chat-messages .project-answer')
                assert rows.count() == 3
                assert rows.locator('.message').all_inner_texts() == ['NEW_TASK_ANSWER', 'OLD_TASK_ANSWER', 'LEGACY_TASK_ANSWER']
                notes = rows.locator('.msg-provenance').all_inner_texts()
                assert 'Task ended' in notes[0] and 'Sep 25, 2026' in notes[0]
                assert 'Sep 24, 2026' in notes[1] and 'Notification added Sep 26, 2026' in notes[1]
                assert notes[2].startswith('Task end time not recorded')
                assert 'Notification added Sep 26, 2026' in notes[2]
                assert rows.nth(1).evaluate("node => !node.querySelector('.message').contains(node.querySelector('.msg-provenance'))")
                return notes
            before = check()
            _screenshot(page, tmp_path, f'terminal-times-{browser_engine}')
            page.reload(wait_until='domcontentloaded')
            _idle(page, '#chat-messages')
            assert check() == before
            page.set_viewport_size({'width':390, 'height':844})
            if page.locator('#nav-drawer-backdrop').is_visible():
                page.locator('#nav-drawer-backdrop').click(position={'x':380, 'y':400})
            page.evaluate(_FRAMES)
            assert check() == before
            _screenshot(page, tmp_path, f'terminal-times-narrow-{browser_engine}')
        finally:
            browser.close()


@pytest.mark.parametrize("browser_engine", ["chromium", "webkit"])
def test_latest_and_question_supersede_pending_restoration(direct_server_with_data, browser_engine, tmp_path):
    from ouroboros.projects_registry import create_project, bind_task_to_project
    from ouroboros.task_results import write_task_result
    from playwright.sync_api import sync_playwright

    root = direct_server_with_data['data_dir']
    project = create_project(root, 'navigation-room', name='Addressed history')
    _write(root / 'logs/chat.jsonl', [{
        'direction': 'in', 'chat_id': project['chat_id'],
        'ts': '2026-09-03T10:00:00Z' if index >= 1180 else '2026-09-01T10:00:00Z',
        'client_message_id': f'navigation-{index}', 'text': f'NAVIGATION_MESSAGE_{index:04d}',
    } for index in range(1200)])
    bind_task_to_project(root, 'continuity-question', project['id'], project['chat_id'], origin={'absent':'system'})
    write_task_result(root, 'continuity-question', 'completed', project_id=project['id'],
        chat_id=project['chat_id'], owner_quiz={'addressed': {
            'quiz_id':'addressed', 'state':'expired_terminal', 'question':'ADDRESSED_QUESTION',
            'options':['Yes', 'No'], 'option_details':['Proceed', 'Wait'], 'asked_at':'2026-09-02T10:00:00Z',
        }})
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1280, 'height':850})
            page.add_init_script(f'({_FAULT})()')
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            for _ in range(3):
                _step(page, feed, automatic=True)
            target = page.locator(f'{feed} [data-client-message-id="navigation-650"]')
            target.wait_for(state='attached')
            target.evaluate("""node => {
                const feed = node.closest('.chat-messages');
                feed.dispatchEvent(new WheelEvent('wheel', {deltaY:-1}));
                feed.scrollTop += node.getBoundingClientRect().top - feed.getBoundingClientRect().top - 80;
            }""")
            page.evaluate(_FRAMES)
            page.locator('#project-panel-close').click()
            page.evaluate("fault => { window.__continuityFault = fault; }", {
                'chatId':project['chat_id'], 'delay':1500, 'delayKind':'saved'})
            _click_project(page, project)
            # A real latest-button click while the saved-page read is still in flight.
            button = page.locator('#project-panel-body .chat-scroll-bottom-btn')
            button.click()
            _idle(page, feed)
            page.evaluate(_FRAMES)
            assert page.locator(feed).evaluate('n => n.scrollHeight - n.scrollTop - n.clientHeight') <= 8
            _screenshot(page, tmp_path, f'latest-during-load-{browser_engine}')

            # Begin a targeted navigation through the application's public event.
            # The detail response is held; a newer latest click must win even
            # when the real canonical question later arrives.
            for _ in range(3):
                _step(page, feed, automatic=True)
            target.wait_for(state='attached')
            target.evaluate("""node => {
                const feed = node.closest('.chat-messages');
                feed.dispatchEvent(new WheelEvent('wheel', {deltaY:-1}));
                feed.scrollTop += node.getBoundingClientRect().top - feed.getBoundingClientRect().top - 80;
            }""")
            page.evaluate(_FRAMES)
            page.locator('#project-panel-close').click()
            page.evaluate("""project => {
                window.__holdQuestion = true;
                window.dispatchEvent(new CustomEvent('ouro:open-project', {
                    detail:{project, task_id:'continuity-question', quiz_id:'addressed'}}));
            }""", project)
            page.wait_for_function('() => Boolean(window.__releaseQuestion)')
            _idle(page, feed)
            assert abs(target.evaluate('n => n.getBoundingClientRect().top - n.closest(".chat-messages").getBoundingClientRect().top') - 80) > 8
            # Scroll up by a user gesture so the visible button can be activated.
            page.locator(feed).evaluate("n => { n.dispatchEvent(new WheelEvent('wheel', {deltaY:-1})); n.scrollTop -= 350; }")
            page.evaluate(_FRAMES)
            button.click()
            page.evaluate('() => { window.__holdQuestion = false; window.__releaseQuestion(); }')
            _idle(page, feed)
            page.wait_for_timeout(100)
            page.evaluate(_FRAMES)
            assert page.locator(f'{feed} [data-quiz-id="addressed"]').count() == 0
            assert page.locator(feed).evaluate('n => n.scrollHeight - n.scrollTop - n.clientHeight') <= 8

            # A subsequent addressed read lands and remains the reading target
            # after reflow, even though the preceding intent followed latest.
            page.evaluate("""project => window.dispatchEvent(new CustomEvent('ouro:open-project', {
                detail:{project, task_id:'continuity-question', quiz_id:'addressed'}}))""", project)
            quiz = page.locator(f'{feed} [data-quiz-id="addressed"]')
            quiz.wait_for(state='visible')
            _idle(page, feed)
            page.wait_for_function("selector => document.querySelector(selector)?.contains(document.activeElement)",
                                   arg=f'{feed} [data-quiz-id="addressed"]')
            anchor = page.locator(feed).evaluate("""root => {
                const top = root.getBoundingClientRect().top;
                const node = [...root.querySelectorAll('.chat-bubble[data-history-id]')].find(n => n.getBoundingClientRect().bottom > top);
                return {id:node.dataset.historyId, offset:node.getBoundingClientRect().top - top};
            }""")
            page.set_viewport_size({'width':900, 'height':850})
            page.evaluate(_FRAMES)
            page.wait_for_timeout(120)  # allow ResizeObserver → RAF → header-reserve layout to settle
            after = page.locator(f'{feed} [data-history-id="{anchor["id"]}"]').evaluate(
                'n => n.getBoundingClientRect().top - n.closest(".chat-messages").getBoundingClientRect().top')
            if abs(after - anchor['offset']) > 8:
                (tmp_path / f'question-reflow-{browser_engine}.json').write_text(json.dumps(page.locator(feed).evaluate("""root => ({
                    scrollTop:root.scrollTop, rect:root.getBoundingClientRect().toJSON(),
                    visible:[...root.children].filter(n => n.getBoundingClientRect().bottom > root.getBoundingClientRect().top
                        && n.getBoundingClientRect().top < root.getBoundingClientRect().bottom).map(n => ({
                        class:n.className, id:n.dataset.historyId, task:n.dataset.taskId,
                        top:n.getBoundingClientRect().top, bottom:n.getBoundingClientRect().bottom})),
                    scroll:window.__continuityScroll,
                })"""), indent=2))
                _screenshot(page, tmp_path, f'question-reflow-failed-{browser_engine}')
            assert abs(after - anchor['offset']) <= 8
            assert quiz.evaluate('n => n.getBoundingClientRect().bottom > n.closest(".chat-messages").getBoundingClientRect().top')
            _screenshot(page, tmp_path, f'question-after-load-{browser_engine}')
        finally:
            browser.close()


_OFFSET = "n => n.getBoundingClientRect().top - n.closest('.chat-messages').getBoundingClientRect().top"
_FIRST_VISIBLE = """root => {
    const top = root.getBoundingClientRect().top;
    const node = [...root.querySelectorAll('.chat-bubble[data-history-id]')].find(n => n.getBoundingClientRect().bottom > top + 1);
    return node && {id:node.dataset.historyId, offset:node.getBoundingClientRect().top - top, scrollTop:root.scrollTop};
}"""


def _deep_gesture_history(root):
    """A Review card, a photo and the reading target live on the third saved page."""
    from ouroboros.artifacts import store_task_artifact_bytes
    from ouroboros.projects_registry import create_project
    from tests.test_chat_history_paging_browser import _result

    project = create_project(root, 'gesture-room', name='Gesture continuity')
    cid, start = project['chat_id'], datetime(2026, 9, 1, tzinfo=timezone.utc)
    at = lambda index, seconds=0: (start + timedelta(minutes=index, seconds=seconds)).isoformat()
    image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWQAAAABJRU5ErkJggg==')
    image_name = f'chat-media-{hashlib.sha256(image).hexdigest()}.png'
    store_task_artifact_bytes(root, 'gesture-media', image_name, image)
    rows = [{'direction': 'in', 'chat_id': cid, 'ts': at(index), 'client_message_id': f'gesture-{index}',
             'text': f'Gesture message {index:04d}. Reading the original discussion across archive pages.'}
            for index in range(900)]
    rows[425] = {'direction': 'out', 'chat_id': cid, 'ts': at(425), 'type': 'photo', 'task_id': 'gesture-media',
                 'text': 'Held photo', 'caption': 'Held photo', 'mime': 'image/png',
                 'download_url': f'/api/tasks/gesture-media/artifacts/{image_name}'}
    _write(root / 'logs' / 'chat.jsonl', rows)
    review = {'panels': [{'panel_id': 'gesture-panel', 'surface': 'task_acceptance', 'aggregate_signal': 'PASS',
                          'transport_status': 'success', 'parse_status': 'valid',
                          'reason': 'DEEP_REVIEW_DETAIL ' + 'Reviewed evidence stays readable. ' * 12, 'actors': []}]}
    _write(root / 'logs' / 'progress.jsonl', [
        *[{'ts': at(420, index), 'chat_id': cid, 'task_id': 'deep-review',
           'content': f'Deep review narration {index}. ' + 'Inspecting retained history. ' * 6} for index in range(3)],
        *[{'ts': at(700 + index // 2, index % 2), 'chat_id': cid, 'task_id': 'later-work',
           'content': f'Later work {index:03d}'} for index in range(200)],
    ])
    for task_id, extra in [('deep-review', {'review_projection': review, 'suggested_name': 'Deep review card'}),
                           ('later-work', {}), ('gesture-media', {})]:
        _result(root, task_id, chat_id=cid, project_id=project['id'], **extra)
    return project, image_name


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
def test_deep_restoration_waits_for_data_and_actual_gestures_win(direct_server_with_data, browser_engine, tmp_path):
    """Held saved-page reads outlast any frame budget; real wheel/keyboard input owns the place."""
    from playwright.sync_api import sync_playwright

    root = direct_server_with_data['data_dir']
    project, image_name = _deep_gesture_history(root)
    evidence, failures = {}, []

    def check(label, ok, facts):
        evidence[label] = {'ok': bool(ok), **facts}
        if not ok:
            failures.append(label)
            _screenshot(page, tmp_path, f'gesture-failed-{label}-{browser_engine}')

    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            held_images = []
            photo_state = {'hold': True}

            def photo(route):
                if photo_state['hold']:
                    held_images.append(route)
                else:
                    route.continue_()
            page.route(f'**/api/tasks/gesture-media/artifacts/{image_name}', photo)
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            target = page.locator(f'{feed} [data-client-message-id="gesture-435"]')
            for _ in range(8):
                if target.count():
                    break
                _step(page, feed, automatic=True)
            target.wait_for(state='attached')

            def place(node, offset=80):
                node.evaluate("""(node, offset) => {
                    const feed = node.closest('.chat-messages');
                    feed.dispatchEvent(new WheelEvent('wheel', {deltaY:-1}));
                    feed.scrollTop += node.getBoundingClientRect().top - feed.getBoundingClientRect().top - offset;
                }""", offset)
                page.evaluate(_FRAMES)
                return node.evaluate(_OFFSET)

            def reopen(fault=''):
                page.locator('#project-panel-close').click()
                page.evaluate("fault => { window.__heldHistory = null; window.__historyFault = fault; }", fault)
                _click_project(page, project)
                if fault == 'hold':
                    page.wait_for_function('() => Boolean(window.__heldHistory)')
                    page.evaluate(_FRAMES)

            def release():
                page.evaluate('() => window.__releaseHistory()')
                _idle(page, feed)
                page.evaluate(_FRAMES)
                page.wait_for_timeout(150)

            # Control: a fast reopen keeps the deep passage.
            before = place(target)
            reopen()
            _idle(page, feed)
            target.wait_for(state='attached')
            page.evaluate(_SETTLE_RESTORE_FRAMES)
            after = target.evaluate(_OFFSET)
            check('fast-reopen', abs(after - before) <= 8, {'before': before, 'after': after})

            # The saved page is held far past any frame budget, then the photo
            # response arrives after restoration and fires a real load event.
            before = place(target)
            reopen('hold')
            page.wait_for_timeout(700)
            release()
            restored = target.evaluate(_OFFSET) if target.count() else None
            check('held-reopen', restored is not None and abs(restored - before) <= 8, {'before': before, 'after': restored})
            photo_node = page.locator(f'{feed} img.chat-photo')
            box = photo_node.evaluate('n => n.getBoundingClientRect().height') if photo_node.count() else None
            photo_state['hold'] = False
            for route in held_images:
                route.continue_()
            held_images.clear()
            if photo_node.count():
                page.wait_for_function('s => document.querySelector(s)?.complete && document.querySelector(s).naturalWidth > 0',
                                       arg=f'{feed} img.chat-photo')
            page.evaluate(_FRAMES)
            loaded = target.evaluate(_OFFSET) if target.count() else None
            check('late-photo', loaded is not None and abs(loaded - before) <= 8, {
                'before': before, 'after': loaded, 'box_before': box,
                'box_after': photo_node.evaluate('n => n.getBoundingClientRect().height') if photo_node.count() else None})
            _screenshot(page, tmp_path, f'gesture-held-photo-{browser_engine}')
            photo_state['hold'] = True

            # An expanded Review attempt deep in the saved page reopens with its
            # disclosure and nested anchor, even when the page read is held.
            card = page.locator(f'{feed} .chat-live-card[data-task-id="deep-review"]')
            if card.get_attribute('data-expanded') != '1':
                card.locator(':scope > [data-live-summary-button]').click()
            card.locator('[data-review-section-toggle]').click()
            card.locator('[data-review-group-toggle]').click()
            card.locator('[data-review-attempt-toggle]').first.click()
            detail = card.locator('[data-review-attempt-detail]').first
            detail.wait_for(state='visible')
            before = place(detail, 120)
            _screenshot(page, tmp_path, f'gesture-review-before-{browser_engine}')
            reopen('hold')
            page.wait_for_timeout(700)
            release()
            state = page.evaluate("""feed => {
                const card = document.querySelector(`${feed} .chat-live-card[data-task-id="deep-review"]`);
                const detail = card?.querySelector('[data-review-attempt-detail]');
                const visible = node => Boolean(node && node.getClientRects().length && node.getBoundingClientRect().height);
                return {card: card?.dataset.expanded, detail: visible(detail) ? detail.textContent.includes('DEEP_REVIEW_DETAIL') : false,
                    offset: visible(detail) ? detail.getBoundingClientRect().top - detail.closest('.chat-messages').getBoundingClientRect().top : null,
                    approximate: document.querySelector(feed).parentElement.innerText.includes('could not be restored exactly')};
            }""", feed)
            check('review-reopen', state['card'] == '1' and state['detail'] and state['offset'] is not None
                  and abs(state['offset'] - before) <= 8 and not state['approximate'], {'before': before, **state})
            _screenshot(page, tmp_path, f'gesture-review-after-{browser_engine}')

            # A wheel over the still-loading room ends the old restoration; the
            # held page then paints without pulling the reader back to it.
            before = place(target)
            reopen('hold')
            box = page.locator(feed).bounding_box()
            page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
            page.mouse.wheel(0, 240)
            page.wait_for_timeout(200)
            release()
            landed = page.locator(feed).evaluate(_FIRST_VISIBLE)
            page.wait_for_timeout(300)
            page.evaluate(_FRAMES)
            later = page.locator(feed).evaluate(_FIRST_VISIBLE)
            old = target.evaluate(_OFFSET) if target.count() else None
            check('wheel-while-loading', landed and later and landed['id'] == later['id']
                  and abs(landed['offset'] - later['offset']) <= 8 and (old is None or abs(old - before) > 8),
                  {'saved': before, 'landed': landed, 'later': later, 'saved_after': old})
            _screenshot(page, tmp_path, f'gesture-wheel-while-loading-{browser_engine}')

            # After a failed saved-page read the recent tail is readable. Actual
            # wheel or keyboard reading there wins over the pending bookmark, so
            # the later Retry paints the saved page without a snap-back.
            for label in ('wheel', 'keyboard', 'focused-control'):
                before = place(target)
                reopen('fail')
                retry = page.locator(f'{feed} .chat-load-older button')
                page.wait_for_function("s => document.querySelector(s)?.textContent === 'Retry loading messages'",
                                       arg=f'{feed} .chat-load-older button')
                _idle(page, feed)
                start = page.locator(feed).evaluate(_FIRST_VISIBLE)
                down = page.locator(feed).evaluate('n => n.scrollTop < (n.scrollHeight - n.clientHeight) / 2')
                if label == 'wheel':
                    box = page.locator(feed).bounding_box()
                    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                    page.mouse.wheel(0, 240 if down else -240)
                elif label == 'keyboard':
                    # Click the text being read (focus stays on the document), then page with keys.
                    page.locator(f'{feed} [data-history-id="{start["id"]}"] .message').click()
                    for key in ('ArrowDown', 'ArrowDown', 'PageDown') if down else ('ArrowUp', 'ArrowUp', 'PageUp'):
                        page.keyboard.press(key)
                else:
                    retry.focus()  # a keyboard user's focus on the feed's own Retry control
                    for key in ('PageDown', 'ArrowDown') if down else ('PageUp', 'ArrowUp'):
                        page.keyboard.press(key)
                page.wait_for_timeout(200)
                page.evaluate(_FRAMES)
                moved = page.locator(feed).evaluate(_FIRST_VISIBLE)
                active = page.evaluate('() => document.activeElement?.tagName')
                if label == 'focused-control':
                    page.keyboard.press('Enter')
                else:
                    retry.evaluate('node => node.click()')  # activation only; the navigation above was real input
                _idle(page, feed)
                page.evaluate(_FRAMES)
                page.wait_for_timeout(150)
                kept = page.locator(f'{feed} [data-history-id="{moved["id"]}"]')
                settled = kept.evaluate(_OFFSET) if kept.count() else None
                old = target.evaluate(_OFFSET) if target.count() else None
                check(f'{label}-before-retry', abs(moved['scrollTop'] - start['scrollTop']) > 20
                      and settled is not None and abs(settled - moved['offset']) <= 8
                      and (old is None or abs(old - before) > 8),
                      {'saved': before, 'start': start, 'moved': moved, 'settled': settled,
                       'saved_after': old, 'active': active})
                _screenshot(page, tmp_path, f'gesture-{label}-before-retry-{browser_engine}')
        finally:
            out = Path(os.environ.get('HISTORY_UI_EVIDENCE_DIR') or tmp_path)
            out.mkdir(parents=True, exist_ok=True)
            (out / f'gesture-phases-{browser_engine}.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
            browser.close()
    assert not failures, (failures, evidence)
