"""Physical card ownership and actual line disclosures across room recreation."""
import json
import pytest

from tests.test_history_continuity_browser import (
    _bookmark_place, _deep_gesture_history, _nested_full_output_history, _open_nested_full_output, _OFFSET,
)
from tests.test_chat_history_paging_browser import _open, _open_project, _step, _idle, _screenshot, _FRAMES, _write, _result
from tests.test_chat_history_recovery_browser import _click_project
from tests.test_ui_smoke_playwright import direct_server_with_data as direct_server_with_data

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
def test_card_page_and_expanded_long_line_survive_reopen(direct_server_with_data, browser_engine, tmp_path):
    from playwright.sync_api import sync_playwright

    root = direct_server_with_data['data_dir']
    project, _ = _deep_gesture_history(root)
    progress = root / 'logs/progress.jsonl'
    rows = [json.loads(line) for line in progress.read_text().splitlines()]
    for row in rows:
        if row['task_id'] == 'deep-review':
            row['content'] += ' Full retained long narration.' * 6
    progress.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            card = page.locator(f'{feed} .chat-live-card[data-task-id="deep-review"]')
            for _ in range(8):
                if card.count():
                    break
                _step(page, feed, automatic=True)
            card.wait_for(state='attached')
            supplying = page.evaluate("""id => window.__historyReads.filter(r => r.chatId === id)
                .find(r => r.body?.messages.some(m => m.task_id === 'deep-review' && m.history_id))?.body.page_cursor""", project['chat_id'])
            assert supplying
            # Advance the pager focus while this card's supplying page is retained.
            _step(page, feed, automatic=True)
            focused = page.evaluate('() => window.__historyReads.at(-1).body.page_cursor')
            assert focused != supplying
            if card.get_attribute('data-expanded') != '1':
                card.locator(':scope > [data-live-summary-button]').click()
            card.locator('[data-review-section-toggle]').click()
            card.locator('[data-review-group-toggle]').click()
            card.locator('[data-review-attempt-toggle]').first.click()
            detail = card.locator('[data-review-attempt-detail]').first
            detail.wait_for(state='visible')

            def place(node):
                node.evaluate("""n => {
                    const feed = n.closest('.chat-messages');
                    feed.dispatchEvent(new WheelEvent('wheel', {deltaY:-1}));
                    feed.scrollTop += n.getBoundingClientRect().top - feed.getBoundingClientRect().top - 30;
                }""")
                page.evaluate(_FRAMES)
                return node.evaluate(_OFFSET)

            def reopen():
                page.locator('#project-panel-close').click()
                before = page.evaluate('() => window.__historyReads.length')
                _click_project(page, project)
                _idle(page, feed)
                page.evaluate(_FRAMES)
                return page.evaluate('start => window.__historyReads.slice(start).filter(r => r.cursor).map(r => r.cursor)', before)

            offset = place(detail)
            _screenshot(page, tmp_path, f'bookmark-review-before-{browser_engine}')
            cursors = reopen()
            assert cursors == [supplying], (supplying, focused, cursors)
            detail.wait_for(state='visible')
            assert abs(detail.evaluate(_OFFSET) - offset) <= 8
            assert 'could not be restored exactly' not in page.locator(feed).locator('..').inner_text()
            _screenshot(page, tmp_path, f'bookmark-review-after-{browser_engine}')

            line = card.locator('.chat-live-line.expandable').last
            line.locator('[data-live-line-toggle]').click()
            assert line.get_attribute('data-expanded') == '1'
            full_text = line.inner_text()
            assert len(full_text) > 150
            offset = place(line)
            anchor = page.evaluate('''async feed => {
                const {createTimelineAnchors} = await import('/static/modules/chat_render_batch.js');
                return createTimelineAnchors({messagesDiv:document.querySelector(feed), liveCardRecords:new Map()}).serializeTimelineAnchor();
            }''', feed)
            assert anchor['lineKey'] == line.get_attribute('data-live-line-key'), anchor
            assert anchor['lineExpanded'] is True
            _screenshot(page, tmp_path, f'bookmark-line-before-{browser_engine}')
            reopen()
            assert line.get_attribute('data-expanded') == '1'
            assert line.inner_text() == full_text
            assert abs(line.evaluate(_OFFSET) - offset) <= 8
            _screenshot(page, tmp_path, f'bookmark-line-after-{browser_engine}')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.evaluate(_FRAMES)
            note = page.locator(feed).locator('..').locator('.chat-history-status')
            assert note.is_visible()
            box = note.bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= 391
            assert box['y'] >= 0 and box['y'] + box['height'] < 844
            assert 'Shown messages may have gaps' in note.inner_text()
            assert page.locator(feed).locator('..').locator('.chat-load-older-note').count() == 1
            _screenshot(page, tmp_path, f'bookmark-status-narrow-{browser_engine}')

            # The saved Review detail no longer exists in the canonical result.
            # The same history page/card is still available: fallback is explicit.
            page.set_viewport_size({'width': 1280, 'height': 850})
            page.evaluate(_FRAMES)
            detail.wait_for(state='visible')
            place(detail)
            page.locator('#project-panel-close').click()
            result_path = root / 'task_results/deep-review.json'
            result = json.loads(result_path.read_text())
            result['review_projection']['panels'][0]['panel_id'] = 'replacement-panel'
            result_path.write_text(json.dumps(result))
            _click_project(page, project)
            _idle(page, feed)
            note = page.locator(feed).locator('..').locator('.chat-history-status')
            assert note.is_visible()
            assert 'Saved position could not be restored exactly.' in note.inner_text()
            assert page.locator(feed).locator('..').locator('.chat-load-older-note').count() == 1
            _screenshot(page, tmp_path, f'bookmark-approximate-{browser_engine}')
            page.locator('#project-panel-body .chat-scroll-bottom-btn').click()
            page.wait_for_function("feed => !document.querySelector(feed).parentElement.innerText.includes('could not be restored exactly')", arg=feed)
            _screenshot(page, tmp_path, f'bookmark-approximate-cleared-{browser_engine}')
            (tmp_path / f'bookmark-reads-{browser_engine}.json').write_text(json.dumps(
                page.evaluate('() => window.__historyReads'), indent=2))
        finally:
            browser.close()


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
def test_expanded_line_rehydrates_full_result_without_another_click(direct_server_with_data, browser_engine, tmp_path):
    from ouroboros.projects_registry import create_project
    from playwright.sync_api import sync_playwright

    root = direct_server_with_data['data_dir']
    project = create_project(root, 'full-line-room', name='Full result history')
    cid = project['chat_id']
    full = 'Retained result. ' * 320 + 'UNIQUE_FULL_RESULT_TAIL'
    lineage = {'subagent_task_id': 'full-child', 'parent_task_id': 'full-parent',
               'root_task_id': 'full-parent', 'delegation_role': 'subagent', 'subagent_role': 'Archive reader'}
    _write(root / 'logs/chat.jsonl', [
        {'direction': 'in', 'chat_id': cid, 'ts': f'2026-09-01T10:{index:02d}:00Z',
         'client_message_id': f'full-{index}', 'text': f'Full result context {index:02d}'} for index in range(60)])
    _write(root / 'logs/progress.jsonl', [
        {'chat_id': cid, 'ts': '2026-09-01T10:30:01Z', 'task_id': 'full-parent', 'content': 'Parent narration'},
        {'chat_id': cid, 'ts': '2026-09-01T10:30:02Z', 'task_id': 'full-child', **lineage,
         'subagent_event': 'completed', 'status': 'completed', 'content': 'Child finished',
         'result': full[:4000], 'result_truncated': True}])
    _result(root, 'full-parent', chat_id=cid, project_id=project['id'], result='Parent finished.')
    _result(root, 'full-child', chat_id=cid, project_id=project['id'], result=full, **lineage)
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            parent = page.locator(f'{feed} .chat-live-card[data-task-id="full-parent"]')
            child = page.locator(f'{feed} .chat-live-card[data-task-id="full-child"]')
            for card in (parent, child):
                if card.get_attribute('data-expanded') != '1':
                    card.locator(':scope > [data-live-summary-button]').click()
            line = child.locator(':scope > [data-live-timeline] > .chat-live-line.expandable')
            assert line.count() == 1
            assert 'UNIQUE_FULL_RESULT_TAIL' not in line.inner_text()
            line.locator('[data-live-line-toggle]').click()
            body = line.locator(':scope > .chat-live-line-body-full')
            body.get_by_text('UNIQUE_FULL_RESULT_TAIL', exact=False).wait_for(state='visible')
            assert line.get_attribute('data-expanded') == '1'
            line.evaluate("""node => {
                const feed = node.closest('.chat-messages');
                feed.dispatchEvent(new WheelEvent('wheel', {deltaY:-1}));
                feed.scrollTop += node.getBoundingClientRect().top - feed.getBoundingClientRect().top - 30;
            }""")
            page.evaluate(_FRAMES)
            offset = line.evaluate(_OFFSET)
            anchor = page.evaluate('''async feed => {
                const {createTimelineAnchors} = await import('/static/modules/chat_render_batch.js');
                return createTimelineAnchors({messagesDiv:document.querySelector(feed), liveCardRecords:new Map()}).serializeTimelineAnchor();
            }''', feed)
            assert anchor['lineKey'] == line.get_attribute('data-live-line-key'), anchor
            assert anchor['lineExpanded'] is True
            assert anchor['cardChain'][0]['taskId'] == 'full-child'
            _screenshot(page, tmp_path, f'bookmark-hydration-before-{browser_engine}')
            page.locator('#project-panel-close').click()
            _click_project(page, project)
            _idle(page, feed)
            body.get_by_text('UNIQUE_FULL_RESULT_TAIL', exact=False).wait_for(state='visible')
            assert line.get_attribute('data-expanded') == '1'
            assert full in body.inner_text()
            assert abs(line.evaluate(_OFFSET) - offset) <= 8
            _screenshot(page, tmp_path, f'bookmark-hydration-after-{browser_engine}')
        finally:
            browser.close()


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
def test_main_coverage_status_remains_readable_away_from_history_control(direct_server_with_data, browser_engine, tmp_path):
    from playwright.sync_api import sync_playwright

    _write(direct_server_with_data['data_dir'] / 'logs/chat.jsonl', [
        {'direction': 'in', 'chat_id': 1, 'ts': '2026-09-01T10:00:00Z',
         'client_message_id': f'main-{index}', 'text': f'Main saved message {index:04d}'} for index in range(1400)])
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1280, 'height':850})
            _open(page, direct_server_with_data['url'])
            for _ in range(5):
                _step(page, '#chat-messages', automatic=True)
            for width in (1280, 390):
                page.set_viewport_size({'width':width, 'height':850})
                page.locator('#chat-messages').evaluate('n => { n.scrollTop = n.scrollHeight / 2; }')
                page.evaluate(_FRAMES)
                note = page.locator('#page-chat .chat-page-header .chat-history-status')
                assert note.is_visible()
                assert 'Shown messages may have gaps' in note.inner_text()
                box = note.bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
                assert box['y'] >= 0 and box['y'] + box['height'] < 850
                assert page.locator('#page-chat .chat-load-older-note').count() == 1
                _screenshot(page, tmp_path, f'main-history-status-{width}-{browser_engine}')
        finally:
            browser.close()


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
def test_late_latest_cannot_erase_newer_physical_coverage(direct_server_with_data, browser_engine, tmp_path):
    from datetime import datetime, timedelta, timezone
    from ouroboros.projects_registry import create_project
    from playwright.sync_api import sync_playwright

    root = direct_server_with_data['data_dir']
    project = create_project(root, 'latest-race', name='Latest response race')
    log = root / 'logs/chat.jsonl'
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def append(first, last):
        with log.open('a') as stream:
            for index in range(first, last):
                stream.write(json.dumps({'direction': 'in', 'chat_id': project['chat_id'],
                    'ts': (start + timedelta(minutes=index)).isoformat(),
                    'client_message_id': f'race-{index}', 'text': f'Race retained message {index:04d}'}) + '\n')

    append(0, 150)
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1280, 'height':850})
            page.add_init_script('''(() => {
                const fetch = window.fetch.bind(window);
                window.fetch = async (input, init) => {
                    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
                    const arm = window.__holdLatest;
                    const hold = arm && url.pathname === '/api/chat/history' && !url.searchParams.has('cursor')
                        && Number(url.searchParams.get('chat_id')) === arm.chatId && arm.skip-- === 0;
                    if (hold) window.__holdLatest = null;
                    const response = await fetch(input, init);
                    if (hold) {
                        window.__heldLatestBody = await response.clone().json();
                        await new Promise(resolve => { window.__releaseLatest = resolve; });
                    }
                    return response;
                };
            })()''')
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            target = page.locator(f'{feed} [data-client-message-id="race-50"]')
            target.evaluate('''node => {
                const feed = node.closest('.chat-messages');
                feed.dispatchEvent(new WheelEvent('wheel', {deltaY:-1}));
                feed.scrollTop += node.getBoundingClientRect().top - feed.getBoundingClientRect().top - 50;
            }''')
            page.evaluate(_FRAMES)
            append(150, 151)
            page.evaluate('chatId => { window.__holdLatest = {chatId, skip:1}; }', project['chat_id'])

            def reconnect():
                page.evaluate('''() => {
                    const socket = window.__testSockets.find(s => s.readyState === WebSocket.OPEN);
                    if (!socket) throw new Error('No open socket to reconnect');
                    socket.close();
                }''')

            reconnect()
            page.wait_for_function('() => Boolean(window.__heldLatestBody && window.__releaseLatest)')
            held = page.evaluate('() => window.__heldLatestBody')
            assert held['messages'][-1]['client_message_id'] == 'race-150'
            append(151, 451)
            reconnect()
            newest = page.locator(f'{feed} [data-client-message-id="race-450"]')
            newest.wait_for(state='attached')
            page.evaluate('() => window.__releaseLatest()')
            _idle(page, feed)
            assert newest.count() == 1
            note = page.locator(feed).locator('..').locator('.chat-load-older-note')
            assert 'Shown messages may have gaps' in note.inner_text()
            assert 'Beginning' not in note.inner_text()
            _screenshot(page, tmp_path, f'latest-race-gap-{browser_engine}')
            for _ in range(6):
                if note.inner_text() == 'Beginning of saved history':
                    break
                _step(page, feed)
            assert note.inner_text() == 'Beginning of saved history'
            assert newest.count() == 1
            _screenshot(page, tmp_path, f'latest-race-filled-{browser_engine}')
            (tmp_path / f'latest-race-reads-{browser_engine}.json').write_text(json.dumps(
                {'held': held, 'reads': page.evaluate('() => window.__historyReads')}, indent=2))
        finally:
            browser.close()


def _bookmark_reconnect(page):
    page.evaluate("""() => {
        const socket = window.__testSockets.find(s => s.readyState === WebSocket.OPEN);
        if (!socket) throw new Error('No open socket to reconnect');
        socket.close();
    }""")


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('line_kind', ['receipt', 'narration'])
def test_nested_line_bookmark_uses_its_source_page(direct_server_with_data, browser_engine, line_kind, tmp_path):
    """A card's recent progress must not choose the page for its older line."""
    from datetime import datetime, timedelta, timezone
    from ouroboros.merge_receipts import card_row_text
    from ouroboros.projects_registry import create_project
    from tests.ui_chat_viewport_smoke import _emit_ws_frame
    from playwright.sync_api import sync_playwright

    root = direct_server_with_data['data_dir']
    project = create_project(root, 'source-page', name='Nested reading position')
    cid = project['chat_id']
    start = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    stamp = lambda minute: (start + timedelta(minutes=minute)).isoformat()
    receipt = {'receipt_id': 'source', 'number': 1347, 'revision': 3,
               'outcome': {'status': 'merged', 'merge_sha': 'a' * 40},
               'coverage': {'status': 'covers_head', 'gaps': []}}
    marker = 'PR #1347 merge:' if line_kind == 'receipt' else 'SOURCE_PAGE_NARRATION'
    text = card_row_text(receipt) if line_kind == 'receipt' else (
        'SOURCE_PAGE_NARRATION. ' + 'The original expanded activity remains readable. ' * 12)
    old = {'chat_id': cid, 'task_id': 'source-owner', 'ts': stamp(1), 'content': text}
    if line_kind == 'receipt':
        old.update(role='system', system_type='host_progress', narration=False,
                   card_row='reviews', card_row_id='merge-receipt:source', card_row_revision=3)
    _write(root / 'logs/progress.jsonl', [old, *[
        {'chat_id': cid, 'task_id': 'source-filler', 'ts': stamp(index + 2),
         'content': f'Archive activity {index:03d}'} for index in range(80)],
        {'chat_id': cid, 'task_id': 'source-owner', 'ts': stamp(83), 'content': 'RECENT_OWNER_PROGRESS'}])
    _write(root / 'logs/chat.jsonl', [
        {'chat_id': cid, 'direction': 'in', 'ts': stamp(index),
         'client_message_id': f'source-{index}', 'text': f'Reading context {index:03d}'} for index in range(100)])
    for task in ('source-owner', 'source-filler'):
        _result(root, task, chat_id=cid, project_id=project['id'], result='Completed retained task.',
                merge_receipts=[receipt] if task == 'source-owner' and line_kind == 'receipt' else [])
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            recent = page.evaluate('id => window.__historyReads.find(r => r.chatId === id).body', cid)
            assert any(row.get('text') == 'RECENT_OWNER_PROGRESS' for row in recent['messages'])
            assert not any(marker in row.get('text', '') for row in recent['messages'])
            card = page.locator(f'{feed} .chat-live-card[data-task-id="source-owner"]')
            if card.get_attribute('data-expanded') != '1':
                card.locator(':scope > [data-live-summary-button]').click()
            line = card.locator(':scope > [data-live-timeline] > .chat-live-line').filter(has_text=marker)
            if line_kind == 'receipt':
                # A late live receipt already has the same revision as its archive row.
                _emit_ws_frame(page, {**old, 'type': 'chat', 'is_progress': True})
                line.wait_for(state='visible')
                line.evaluate('''n => {
                    window.__sourceLine = n;
                    const range = document.createRange();
                    range.selectNodeContents(n.querySelector('.chat-live-line-title'));
                    getSelection().removeAllRanges(); getSelection().addRange(range);
                    window.__sourceSelection = getSelection().toString();
                }''')
                live_key = line.get_attribute('data-live-line-key')
            _step(page, feed)
            line.wait_for(state='attached')
            supplying = page.evaluate('''({id, marker}) => window.__historyReads.filter(r => r.chatId === id)
                .find(r => r.cursor && r.body?.messages.some(m => m.text?.includes(marker))).body''', {'id': cid, 'marker': marker})
            assert supplying['page_cursor'] != recent['page_cursor']
            source_row = next(row for row in supplying['messages'] if marker in row.get('text', ''))
            source = source_row['history_id']
            if line_kind == 'receipt':
                assert source_row['card_row_revision'] == 3 and source_row['text'] == text
            if line_kind == 'receipt':
                assert line.evaluate('n => n === window.__sourceLine'), 'equal revision adoption keeps the mounted node'
                assert line.get_attribute('data-live-line-key') == live_key
                assert page.evaluate('() => getSelection().toString() === window.__sourceSelection')
                page.evaluate('() => getSelection().removeAllRanges()')
                # A late, lower revision cannot roll back the visible receipt.
                _emit_ws_frame(page, {**old, 'type': 'chat', 'is_progress': True,
                                     'card_row_revision': 2, 'ts': stamp(99), 'content': 'Stale queued receipt'})
                assert all(part in line.inner_text() for part in text.splitlines())
            else:
                line.locator('[data-live-line-toggle]').click()
                assert line.get_attribute('data-expanded') == '1'
            offset = _bookmark_place(page, line, 1)
            anchor = page.evaluate('''async feed => {
                const {createTimelineAnchors} = await import('/static/modules/chat_render_batch.js');
                return createTimelineAnchors({messagesDiv:document.querySelector(feed), liveCardRecords:new Map()}).serializeTimelineAnchor();
            }''', feed)
            assert anchor['lineKey'] == line.get_attribute('data-live-line-key'), anchor
            _screenshot(page, tmp_path, f'source-page-{line_kind}-before-{browser_engine}')
            page.locator('#project-panel-close').click()
            before = page.evaluate('() => window.__historyReads.length')
            _click_project(page, project)
            _idle(page, feed)
            reads = page.evaluate('n => window.__historyReads.slice(n)', before)
            (tmp_path / f'source-page-{line_kind}-{browser_engine}.json').write_text(json.dumps({
                'source': source, 'anchor': anchor, 'recent': recent, 'supplying': supplying, 'reopen': reads}, indent=2))
            assert [read['cursor'] for read in reads if read['cursor']] == [supplying['page_cursor']]
            assert card.get_attribute('data-expanded') == '1'
            line.wait_for(state='visible')
            assert all(part.strip() in line.inner_text() for part in text.splitlines())
            if line_kind == 'narration':
                assert line.get_attribute('data-expanded') == '1'
            assert abs(line.evaluate(_OFFSET) - offset) <= 8
            assert 'could not be restored exactly' not in page.locator(feed).locator('..').inner_text()
            _screenshot(page, tmp_path, f'source-page-{line_kind}-after-{browser_engine}')
        finally:
            browser.close()


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
def test_protected_page_bookmark_survives_latest_rebase(direct_server_with_data, browser_engine, tmp_path):
    from datetime import datetime, timedelta, timezone
    from ouroboros.projects_registry import create_project
    from playwright.sync_api import sync_playwright

    root = direct_server_with_data['data_dir']
    project = create_project(root, 'retained-rebase', name='Retained reading page')
    log = root / 'logs/chat.jsonl'
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def append(first, last):
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a') as stream:
            for index in range(first, last):
                stream.write(json.dumps({'direction': 'in', 'chat_id': project['chat_id'],
                    'ts': (start + timedelta(minutes=index)).isoformat(),
                    'client_message_id': f'rebase-{index}', 'text': f'Rebase retained message {index:04d}'}) + '\n')

    append(0, 150)
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            supplying = page.evaluate('id => window.__historyReads.find(r => r.chatId === id).body.page_cursor', project['chat_id'])
            target = page.locator(f'{feed} [data-client-message-id="rebase-50"]')
            offset = _bookmark_place(page, target, 50)
            append(150, 450)
            before = page.evaluate('() => window.__historyReads.length')
            _bookmark_reconnect(page)
            page.wait_for_function('n => window.__historyReads.length >= n + 2 && window.__historyReads.slice(n).every(r => r.done)', arg=before)
            _idle(page, feed)
            rebased = page.evaluate('() => window.__historyReads.at(-1).body.page_cursor')
            assert rebased != supplying
            assert abs(target.evaluate(_OFFSET) - offset) <= 8
            _screenshot(page, tmp_path, f'composed-rebase-before-{browser_engine}')
            page.locator('#project-panel-close').click()
            before = page.evaluate('() => window.__historyReads.length')
            _click_project(page, project)
            _idle(page, feed)
            cursors = page.evaluate('n => window.__historyReads.slice(n).filter(r => r.cursor).map(r => r.cursor)', before)
            assert cursors == [supplying], {'supplying': supplying, 'rebased': rebased, 'restored': cursors}
            target.wait_for(state='attached')
            assert abs(target.evaluate(_OFFSET) - offset) <= 8
            assert 'could not be restored exactly' not in page.locator(feed).locator('..').inner_text()
            _screenshot(page, tmp_path, f'composed-rebase-after-{browser_engine}')
            (tmp_path / f'composed-rebase-reads-{browser_engine}.json').write_text(json.dumps(page.evaluate('() => window.__historyReads'), indent=2))
        finally:
            browser.close()


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('line_kind', ['narration', 'full-result'])
def test_live_origin_expanded_line_survives_history_adoption(direct_server_with_data, browser_engine, line_kind, tmp_path):
    from ouroboros.projects_registry import create_project
    from tests.ui_chat_viewport_smoke import _emit_ws_frame
    from playwright.sync_api import sync_playwright

    root = direct_server_with_data['data_dir']
    project = create_project(root, 'live-line-room', name='Live line adoption')
    cid = project['chat_id']
    full = 'Retained full result. ' * 320 + 'COMPOSED_FULL_RESULT_TAIL'
    lineage = {'subagent_task_id': 'adopt-child', 'parent_task_id': 'adopt-parent',
               'root_task_id': 'adopt-parent', 'delegation_role': 'subagent', 'subagent_role': 'History reader'}
    _write(root / 'logs/chat.jsonl', [
        {'direction': 'in', 'chat_id': cid, 'ts': f'2026-09-01T10:{index:02d}:00Z',
         'client_message_id': f'adopt-{index}', 'text': f'Live line context {index:02d}'} for index in range(60)])
    progress = root / 'logs/progress.jsonl'
    _write(progress, [{'chat_id': cid, 'ts': '2026-09-01T10:30:01Z', 'task_id': 'adopt-parent', 'content': 'Parent narration'}]
           if line_kind == 'full-result' else [])
    row = {'chat_id': cid, 'ts': '2026-09-01T10:30:02Z', 'task_id': 'adopt-child', **lineage,
           'subagent_event': 'completed', 'status': 'completed', 'content': 'Child finished',
           'result': full[:4000], 'result_truncated': True} if line_kind == 'full-result' else {
           'chat_id': cid, 'ts': '2026-09-01T10:30:02Z', 'task_id': 'adopt-parent',
           'content': 'Live narration. ' + 'This is retained expanded activity. ' * 10}
    _result(root, 'adopt-parent', chat_id=cid, project_id=project['id'], result='Parent finished.')
    _result(root, 'adopt-child', chat_id=cid, project_id=project['id'], result=full, **lineage)
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            _emit_ws_frame(page, {**row, 'type': 'chat', 'role': 'assistant', 'is_progress': True})
            parent = page.locator(f'{feed} .chat-live-card[data-task-id="adopt-parent"]')
            if parent.get_attribute('data-expanded') != '1':
                parent.locator(':scope > [data-live-summary-button]').click()
            owner = page.locator(f'{feed} .chat-live-card[data-task-id="{row["task_id"]}"]')
            if owner.get_attribute('data-expanded') != '1':
                owner.locator(':scope > [data-live-summary-button]').click()
            line = owner.locator(':scope > [data-live-timeline] > .chat-live-line.expandable').last
            line.locator('[data-live-line-toggle]').click()
            if line_kind == 'full-result':
                line.get_by_text('COMPOSED_FULL_RESULT_TAIL', exact=False).wait_for(state='visible')
            live_key = line.get_attribute('data-live-line-key')
            assert not live_key.startswith(('history-', 'terminal-')), live_key
            line.evaluate('n => { window.__adoptedLine = n; }')
            _bookmark_place(page, line)
            with progress.open('a') as stream:
                stream.write(json.dumps(row) + '\n')
            before = page.evaluate('() => window.__historyReads.length')
            _bookmark_reconnect(page)
            page.wait_for_function('n => window.__historyReads.length > n && window.__historyReads.slice(n).every(r => r.done)', arg=before)
            _idle(page, feed)
            assert line.get_attribute('data-live-line-key') == live_key
            assert line.evaluate('n => n === window.__adoptedLine'), 'adoption preserves mounted DOM identity'
            assert line.get_attribute('data-expanded') == '1'
            offset = _bookmark_place(page, line, 1)
            anchor = page.evaluate('''async feed => {
                const {createTimelineAnchors} = await import('/static/modules/chat_render_batch.js');
                return createTimelineAnchors({messagesDiv:document.querySelector(feed), liveCardRecords:new Map()}).serializeTimelineAnchor();
            }''', feed)
            assert anchor['lineKey'] == live_key and anchor['lineExpanded'] is True, anchor
            _screenshot(page, tmp_path, f'composed-{line_kind}-before-{browser_engine}')
            page.locator('#project-panel-close').click()
            _click_project(page, project)
            _idle(page, feed)
            assert line.get_attribute('data-live-line-key') != live_key, 'cold replay has its source-owned key'
            assert line.get_attribute('data-expanded') == '1'
            if line_kind == 'full-result':
                line.get_by_text('COMPOSED_FULL_RESULT_TAIL', exact=False).wait_for(state='visible')
                assert full in line.inner_text()
            else:
                assert row['content'].strip() in line.inner_text()
            assert abs(line.evaluate(_OFFSET) - offset) <= 8
            assert 'could not be restored exactly' not in page.locator(feed).locator('..').inner_text()
            _screenshot(page, tmp_path, f'composed-{line_kind}-after-{browser_engine}')
        finally:
            browser.close()


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
def test_review_bookmark_waits_for_necessary_task_detail(direct_server_with_data, browser_engine, tmp_path):
    from ouroboros.projects_registry import create_project
    from playwright.sync_api import sync_playwright

    root = direct_server_with_data['data_dir']
    project, _ = _deep_gesture_history(root)
    other = create_project(root, 'detail-other-room', name='Other detail room')
    progress = root / 'logs/progress.jsonl'
    rows = [json.loads(line) for line in progress.read_text().splitlines()]
    rows = [row for row in rows if row['task_id'] != 'deep-review']
    # This owner-bound carrier has no inline terminal projection; only the real
    # owner task-detail GET supplies the saved Review group.
    rows.insert(0, {'type': 'review_reference', 'chat_id': project['chat_id'], 'task_id': 'review-carrier',
                    'presentation_owner_task_id': 'deep-review', 'surface': 'task_acceptance',
                    'state_revision': 'a' * 64, 'ts': '2026-09-01T07:00:00+00:00'})
    rows.insert(1, {**rows[0], 'task_id': 'unrelated-review', 'presentation_owner_task_id': 'unrelated-review',
                    'ts': '2026-09-01T07:00:01+00:00'})
    _result(root, 'unrelated-review', chat_id=project['chat_id'], project_id=project['id'], result='Unrelated task')
    _write(progress, rows)
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            page.add_init_script("""(() => {
                const fetch = window.fetch.bind(window);
                window.__detailReads = [];
                window.__unrelatedReads = [];
                window.fetch = async (input, init) => {
                    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
                    if (url.pathname === '/api/tasks/unrelated-review' && window.__holdUnrelated) {
                        await new Promise(resolve => { window.__unrelatedReads.push(resolve); });
                    }
                    if (url.pathname === '/api/tasks/deep-review') {
                        const mode = window.__detailMode;
                        const read = {mode:mode || 'ready', done:false}; window.__detailReads.push(read);
                        if (mode === 'hold') await new Promise(resolve => { read.release = resolve; });
                        if (mode === 'error') { read.done = true; throw new TypeError('controlled task-detail failure'); }
                        const response = await fetch(input, init); read.done = true; return response;
                    }
                    return fetch(input, init);
                };
            })()""")
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            card = page.locator(f'{feed} .chat-live-card[data-task-id="deep-review"]')
            for _ in range(8):
                if card.count():
                    break
                _step(page, feed, automatic=True)
            card.wait_for(state='attached')
            if card.get_attribute('data-expanded') != '1':
                card.locator(':scope > [data-live-summary-button]').click()
            card.locator('[data-review-section-toggle]').click()
            card.locator('[data-review-group-toggle]').click()
            card.locator('[data-review-attempt-toggle]').first.click()
            detail = card.locator('[data-review-attempt-detail]').first
            detail.wait_for(state='visible')
            assert page.evaluate('() => window.__historyReads.some(r => r.body?.messages.some(m => m.system_type === "review_reference"))')
            assert page.evaluate('() => window.__historyReads.every(r => !r.body?.messages.some(m => m.presentation_owner_task_id === "deep-review" && m.review_projection))')

            def reopen_held(mode):
                offset = _bookmark_place(page, detail, 1)
                anchor = page.evaluate('''async feed => {
                    const {createTimelineAnchors} = await import('/static/modules/chat_render_batch.js');
                    return createTimelineAnchors({messagesDiv:document.querySelector(feed), liveCardRecords:new Map()}).serializeTimelineAnchor();
                }''', feed)
                assert anchor['reviewKey'] == 'reviewAttemptDetail', anchor
                page.locator('#project-panel-close').click()
                page.evaluate('mode => { window.__detailMode = mode; window.__detailReads = []; window.__holdUnrelated = true; window.__unrelatedReads = []; }', mode)
                _click_project(page, project)
                _idle(page, feed)
                page.wait_for_function('() => window.__detailReads.length > 0')
                # History is ready; hold detail across many paints, without a network deadline.
                for _ in range(8):
                    page.evaluate(_FRAMES)
                if detail.count():
                    (tmp_path / f'composed-review-unexpected-{browser_engine}.json').write_text(json.dumps(page.evaluate('''() => ({
                        reads:window.__detailReads.map(({release,...r}) => r),
                        rows:window.__historyReads.flatMap(r => r.body?.messages || []).filter(m => m.presentation_owner_task_id === 'deep-review'),
                        mode:window.__detailMode,
                    })'''), indent=2))
                assert detail.count() == 0, f'necessary Review detail must be absent while {mode}'
                return offset

            def release():
                page.evaluate("""() => {
                    window.__detailMode = null;
                    for (const read of window.__detailReads) read.release?.();
                }""")

            for mode in ('hold', 'error'):
                offset = reopen_held(mode)
                assert 'could not be restored exactly' not in page.locator(feed).locator('..').inner_text(), mode
                _screenshot(page, tmp_path, f'composed-review-{mode}-{browser_engine}')
                if mode == 'error':
                    retry = card.locator('[data-review-hydrate-retry]')
                    retry.wait_for(state='attached')
                    page.evaluate('() => { window.__detailMode = null; }')
                    retry.evaluate('n => n.click()')
                else:
                    release()
                detail.wait_for(state='visible')
                page.wait_for_function('() => window.__detailReads.every(r => r.done)')
                page.evaluate(_FRAMES)
                assert abs(detail.evaluate(_OFFSET) - offset) <= 8, mode
                assert page.evaluate('() => window.__unrelatedReads.length > 0'), 'an unrelated owner detail is still held'
                page.evaluate('() => { window.__holdUnrelated = false; window.__unrelatedReads.forEach(resolve => resolve()); }')
                _screenshot(page, tmp_path, f'composed-review-{mode}-restored-{browser_engine}')

            offset = reopen_held('hold')
            box = page.locator(feed).bounding_box()
            page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
            page.mouse.wheel(0, 360)
            page.wait_for_timeout(100)
            page.evaluate(_FRAMES)
            release()
            detail.wait_for(state='attached')
            page.wait_for_function('() => window.__detailReads.every(r => r.done)')
            page.evaluate(_FRAMES)
            assert abs(detail.evaluate(_OFFSET) - offset) > 8, 'real wheel cancels the held Review destination'
            assert 'could not be restored exactly' not in page.locator(feed).locator('..').inner_text()
            page.evaluate('() => { window.__holdUnrelated = false; window.__unrelatedReads.forEach(resolve => resolve()); }')
            _screenshot(page, tmp_path, f'composed-review-wheel-{browser_engine}')

            # Closing during detail hydration preserves the original destination;
            # a stale reply cannot write into the other room or consume its bookmark.
            offset = reopen_held('hold')
            page.locator('#project-panel-close').click()
            _open_project(page, other)
            release()
            page.evaluate('() => { window.__holdUnrelated = false; window.__unrelatedReads.forEach(resolve => resolve()); }')
            page.wait_for_function('() => window.__detailReads.every(r => r.done)')
            assert not page.locator('#project-panel-body').get_by_text('DEEP_REVIEW_DETAIL', exact=False).count()
            page.locator('#project-panel-close').click()
            _click_project(page, project)
            _idle(page, feed)
            detail.wait_for(state='visible')
            # Another task-detail consumer can render this Review while its own
            # hydration still holds the bookmark. Await that owner's completion.
            card.locator('[data-review-hydrate-status]').wait_for(state='detached')
            page.evaluate(_FRAMES)
            assert abs(detail.evaluate(_OFFSET) - offset) <= 8
            _screenshot(page, tmp_path, f'composed-review-switch-{browser_engine}')
            (tmp_path / f'composed-review-reads-{browser_engine}.json').write_text(json.dumps(page.evaluate('() => window.__detailReads.map(({release,...read}) => read)'), indent=2))
        finally:
            browser.close()


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
def test_live_revisioned_receipt_bookmark_survives_equal_replay(direct_server_with_data, browser_engine, tmp_path):
    import os
    from pathlib import Path
    from ouroboros.merge_receipts import card_row_text
    from ouroboros.projects_registry import create_project
    from tests.ui_chat_viewport_smoke import _emit_ws_frame
    from playwright.sync_api import sync_playwright

    root = direct_server_with_data['data_dir']
    project = create_project(root, 'receipt-bookmark', name='Receipt reading place')
    cid, task_id = project['chat_id'], 'receipt-owner'
    _write(root / 'logs/chat.jsonl', [
        {'direction': 'in', 'chat_id': cid, 'ts': f'2026-09-01T10:{index:02d}:00Z',
         'client_message_id': f'receipt-context-{index}', 'text': f'Receipt context {index:02d}'} for index in range(60)])
    progress = root / 'logs/progress.jsonl'
    _write(progress, [{'chat_id': cid, 'task_id': task_id, 'ts': '2026-09-01T10:30:00Z',
                      'content': 'Reading the retained pull request.'}])
    receipts = [{'receipt_id': name, 'number': 7, 'revision': 3,
                 'outcome': {'status': 'merged', 'merge_sha': 'c' * 40},
                 'coverage': {'status': 'covers_head', 'gaps': ['Retained receipt detail ' * 14]}}
                for name in ('neighbour', 'reading')]
    _result(root, task_id, chat_id=cid, project_id=project['id'], result='Completed.', merge_receipts=receipts)
    rows = [{'chat_id': cid, 'task_id': task_id, 'ts': '2026-09-01T10:30:01Z',
             'system_type': 'pr_merge_receipt', 'card_row': 'reviews',
             'card_row_id': f"merge-receipt:{receipt['receipt_id']}", 'card_row_revision': 3,
             'narration': False, 'text': card_row_text(receipt)} for receipt in receipts]
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            for row in rows:
                _emit_ws_frame(page, {**row, 'type': 'chat', 'role': 'system', 'is_progress': True, 'content': row['text']})
            card = page.locator(f'{feed} .chat-live-card[data-task-id="{task_id}"]')
            if card.get_attribute('data-expanded') != '1':
                card.locator(':scope > [data-live-summary-button]').click()
            lines = card.locator(':scope > [data-live-timeline] > .chat-live-line.result')
            assert lines.count() == 2, 'same text and time must not merge different receipts'
            line = lines.nth(1)
            # Receipt rows have no individual disclosure; their owning card does.
            assert line.locator('[data-live-line-toggle]').count() == 0
            live_key = line.get_attribute('data-live-line-key')
            assert not live_key.startswith('history-')
            line.evaluate('n => { window.__readingReceipt = n; }')
            _bookmark_place(page, line, 1)
            with progress.open('a') as stream:
                for row in rows:
                    stream.write(json.dumps(row) + '\n')
            before = page.evaluate('() => window.__historyReads.length')
            _bookmark_reconnect(page)
            page.wait_for_function('n => window.__historyReads.length > n && window.__historyReads.slice(n).every(r => r.done)', arg=before)
            _idle(page, feed)
            replay = page.evaluate('id => window.__historyReads.filter(r => r.chatId === id).at(-1).body', cid)
            assert [r['card_row_revision'] for r in replay['messages'] if r.get('card_row_id')] == [3, 3]
            assert line.evaluate('n => n === window.__readingReceipt')
            assert line.get_attribute('data-live-line-key') == live_key
            assert card.get_attribute('data-expanded') == '1'
            offset = _bookmark_place(page, line, 1)
            _screenshot(page, tmp_path, f'receipt-bookmark-before-{browser_engine}')
            page.locator('#project-panel-close').click()
            # Reflow the preceding receipt: restoring only the card offset is
            # observably different from restoring this exact typed row.
            page.set_viewport_size({'width': 900, 'height': 850})
            _click_project(page, project)
            _idle(page, feed)
            assert lines.count() == 2
            assert line.get_attribute('data-live-line-key') != live_key
            _screenshot(page, tmp_path, f'receipt-bookmark-after-{browser_engine}')
            restored_offset = line.evaluate(_OFFSET)
            evidence = Path(os.environ.get('HISTORY_UI_EVIDENCE_DIR') or tmp_path)
            (evidence / f'receipt-bookmark-{browser_engine}.json').write_text(json.dumps({
                'saved_offset': offset, 'restored_offset': restored_offset, 'live_key': live_key,
                'cold_key': line.get_attribute('data-live-line-key'), 'card_expanded': card.get_attribute('data-expanded'),
                'receipt_rows': [r for r in replay['messages'] if r.get('card_row_id')],
            }, indent=2))
            assert card.get_attribute('data-expanded') == '1'
            assert abs(restored_offset - offset) <= 8
            assert 'could not be restored exactly' not in page.locator(feed).locator('..').inner_text()
            text = line.inner_text()
            line.evaluate('n => { window.__coldReceipt = n; }')
            for revision in (2, 3):
                _emit_ws_frame(page, {**rows[1], 'type': 'chat', 'role': 'system', 'is_progress': True,
                    'card_row_revision': revision, 'ts': '2026-09-02T00:00:00Z', 'text': 'Stale queued receipt',
                    'content': 'Stale queued receipt'})
                assert line.inner_text() == text
            _emit_ws_frame(page, {**rows[1], 'type': 'chat', 'role': 'system', 'is_progress': True,
                'card_row_revision': 4, 'ts': '2026-08-01T00:00:00Z',
                'text': rows[1]['text'] + ' Current review evidence.', 'content': rows[1]['text'] + ' Current review evidence.'})
            assert 'Current review evidence.' in line.inner_text()
            assert line.evaluate('n => n === window.__coldReceipt')
            assert card.get_attribute('data-expanded') == '1'
            assert lines.count() == 2
            _screenshot(page, tmp_path, f'receipt-bookmark-newer-{browser_engine}')
        finally:
            browser.close()


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
def test_latest_waiting_for_history_yields_to_reading_inside_a_box(direct_server_with_data, browser_engine, tmp_path):
    """#1347 A: ↓ waits for an older read in flight; reading on inside a full output meanwhile keeps the place."""
    from playwright.sync_api import sync_playwright

    project = _nested_full_output_history(direct_server_with_data['data_dir'])
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            feed, _child, line, body = _open_nested_full_output(page, direct_server_with_data['url'], project)
            root = page.locator(feed)
            root.evaluate('feed => { feed.scrollTop = 0; }')
            body.evaluate('n => { n.scrollTop = Math.round((n.scrollHeight - n.clientHeight) / 2); }')
            page.evaluate(_FRAMES)
            # Turning back at the top edge reads an older page; that read is held.
            page.evaluate("() => { window.__historyFault = 'hold'; }")
            root.evaluate("feed => feed.dispatchEvent(new WheelEvent('wheel', {deltaY: -1}))")
            page.wait_for_function('() => Boolean(window.__heldHistory)')
            page.locator('#project-panel-body .chat-scroll-bottom-btn').click()
            rect, frame = body.bounding_box(), root.bounding_box()
            page.mouse.move(rect['x'] + rect['width'] / 2, max(rect['y'], frame['y']) + 40)
            page.wait_for_timeout(300)  # a new wheel burst, not the tail of the edge gesture
            before = {'body': body.evaluate('n => n.scrollTop'), 'line': line.evaluate(_OFFSET)}
            page.mouse.wheel(0, 240)
            page.wait_for_timeout(300)
            page.evaluate(_FRAMES)
            read = {'body': body.evaluate('n => n.scrollTop'), 'line': line.evaluate(_OFFSET)}
            assert read['body'] > before['body'] and abs(read['line'] - before['line']) <= 1, (before, read)
            page.evaluate('() => window.__releaseHistory()')
            _idle(page, feed)
            page.wait_for_timeout(200)
            page.evaluate(_FRAMES)
            after = {'body': body.evaluate('n => n.scrollTop'), 'line': line.evaluate(_OFFSET),
                     'gap': root.evaluate('n => n.scrollHeight - n.scrollTop - n.clientHeight')}
            (tmp_path / f'latest-yields-{browser_engine}.json').write_text(
                json.dumps({'before': before, 'read': read, 'after': after}, indent=2))
            _screenshot(page, tmp_path, f'latest-yields-to-box-{browser_engine}')
            assert after['gap'] > 200, ('the superseded ↓ must not pull the reader to the present', after)
            assert abs(after['line'] - read['line']) <= 8 and abs(after['body'] - read['body']) <= 2, (read, after)
        finally:
            browser.close()


def _retained_room(root, slug, count, **quiz):
    """A deep Project whose composer will hold a staged file, so hiding keeps its instance."""
    from datetime import datetime, timedelta, timezone
    from ouroboros.projects_registry import bind_task_to_project, create_project
    from ouroboros.task_results import write_task_result

    project = create_project(root, slug, name=slug.replace('-', ' ').title())
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _write(root / 'logs/chat.jsonl', [{'direction': 'in', 'chat_id': project['chat_id'], 'client_message_id': f'{slug}-{index}',
        'ts': (start + timedelta(minutes=index)).isoformat(), 'text': f'{slug} message {index:04d}. Reading an older discussion.'}
        for index in range(count)])
    if quiz:
        bind_task_to_project(root, 'continuity-question', project['id'], project['chat_id'], origin={'absent': 'system'})
        write_task_result(root, 'continuity-question', 'completed', project_id=project['id'], chat_id=project['chat_id'], owner_quiz=quiz)
    return project


def _stage_file(page):
    panel = page.locator('#project-panel')
    panel.locator('.chat-file-input-hidden').set_input_files([{'name': 'kept.txt', 'mimeType': 'text/plain', 'buffer': b'kept'}])
    panel.locator('.attach-name').filter(has_text='kept.txt').wait_for()


_GAP = 'n => n.scrollHeight - n.scrollTop - n.clientHeight'


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
def test_failed_restore_then_latest_keeps_a_retained_room_at_the_present(direct_server_with_data, browser_engine, tmp_path):
    """Restore error → ↓ → hide/reopen of a room kept by its staged file still follows new replies."""
    from playwright.sync_api import sync_playwright
    from tests.test_history_continuity_browser import _FAULT
    from tests.ui_chat_viewport_smoke import _emit_ws_frame

    project = _retained_room(direct_server_with_data['data_dir'], 'restore-latest', 1200)
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            page.add_init_script(f'({_FAULT})()')
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            for _ in range(3):
                _step(page, feed, automatic=True)
            _bookmark_place(page, page.locator(f'{feed} [data-client-message-id="restore-latest-650"]'), 80)
            page.locator('#project-panel-close').click()
            page.evaluate('fault => { window.__continuityFault = fault; }', {'chatId': project['chat_id'], 'fail': 'saved'})
            _click_project(page, project)
            page.wait_for_function("feed => document.querySelector(`${feed} .chat-load-older button`)?.textContent === 'Retry loading messages'", arg=feed)
            page.evaluate('() => { window.__continuityFault = null; }')
            _stage_file(page)
            page.locator('#project-panel-body .chat-scroll-bottom-btn').click()
            _idle(page, feed)
            page.evaluate(_FRAMES)
            assert page.locator(feed).evaluate(_GAP) <= 8
            page.locator('#project-panel-close').click()
            assert page.locator('.chat-instance-panel[data-pending-work="1"]').count() == 1
            _click_project(page, project)
            page.evaluate(_FRAMES)
            _emit_ws_frame(page, {'type': 'chat', 'chat_id': project['chat_id'], 'role': 'assistant',
                                  'content': 'LIVE_REPLY_AFTER_REOPEN', 'ts': '2026-09-27T22:00:00Z'})
            page.evaluate(_FRAMES)
            _screenshot(page, tmp_path, f'restore-error-latest-reopen-{browser_engine}')
            assert page.locator(feed).get_by_text('LIVE_REPLY_AFTER_REOPEN', exact=True).count() == 1
            assert page.locator(feed).evaluate(_GAP) <= 8, 'the reopened room keeps following the present'
        finally:
            browser.close()


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
def test_plain_reopen_voids_a_hidden_rooms_held_question_reveal(direct_server_with_data, browser_engine, tmp_path):
    """A reveal still awaiting detail when its retained room hid cannot move or focus a later plain reopen."""
    from playwright.sync_api import sync_playwright
    from tests.test_history_continuity_browser import _FAULT

    project = _retained_room(direct_server_with_data['data_dir'], 'held-reveal', 400, addressed={
        'quiz_id': 'addressed', 'state': 'expired_terminal', 'question': 'ADDRESSED_QUESTION',
        'options': ['Yes', 'No'], 'option_details': ['Proceed', 'Wait'], 'asked_at': '2026-09-01T02:00:00Z'})
    ask = """project => window.dispatchEvent(new CustomEvent('ouro:open-project', {
        detail: {project, task_id: 'continuity-question', quiz_id: 'addressed'}}))"""
    in_quiz = "() => Boolean(document.activeElement?.closest('[data-quiz-id=\"addressed\"]'))"
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            page.add_init_script(f'({_FAULT})()')
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            _stage_file(page)
            ids = page.locator(f'{feed} [data-client-message-id]').evaluate_all('ns => ns.map(n => n.dataset.clientMessageId)')
            target = page.locator(f'{feed} [data-client-message-id="{ids[len(ids) // 2]}"]')
            _bookmark_place(page, target, 80)
            page.locator('#project-panel-close').click()
            page.evaluate('() => { window.__holdQuestion = true; }')
            page.evaluate(ask, project)
            page.wait_for_function('() => Boolean(window.__releaseQuestion)')
            page.locator('#project-panel-close').click()
            assert page.locator('.chat-instance-panel[data-pending-work="1"]').count() == 1
            _click_project(page, project)
            _idle(page, feed)
            page.evaluate(_FRAMES)
            before = target.evaluate(_OFFSET)
            with page.expect_response(lambda response: '/api/tasks/continuity-question' in response.url):
                page.evaluate('() => { window.__holdQuestion = false; window.__releaseQuestion(); }')
            page.wait_for_timeout(150)
            page.evaluate(_FRAMES)
            _screenshot(page, tmp_path, f'held-reveal-after-plain-reopen-{browser_engine}')
            assert page.locator(f'{feed} [data-quiz-id="addressed"]').count() == 0
            assert abs(target.evaluate(_OFFSET) - before) <= 8 and not page.evaluate(in_quiz)
            page.evaluate(ask, project)  # a new addressed navigation still owns the viewport
            page.locator(f'{feed} [data-quiz-id="addressed"]').wait_for(state='visible')
            page.wait_for_function(in_quiz)
            _screenshot(page, tmp_path, f'held-reveal-new-reveal-{browser_engine}')
        finally:
            browser.close()
