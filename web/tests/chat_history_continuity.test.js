import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { installDom, restoreDom, ElementStub } from './chat_dom_fixture.js';
const row = (id, hour) => ({ role: 'assistant', text: id, ts: `2026-09-26T${hour}:00:00.000Z`,
  history_id: `chat:${id}`, history_position: { source: 'chat', offset: Number(id) } });
const page = (messages, cursor, next = null) => ({ messages, page_cursor: cursor, next_cursor: next,
  has_more: next !== null, window: { complete: !next, truncated_by: next ? ['quota'] : [] } });
const tick = () => new Promise(resolve => setImmediate(resolve));

async function probe(framesBeforeResponse, sendStatus = '') {
  let answerRecent;
  const pendingRecent = new Promise(resolve => { answerRecent = resolve; });
  const calls = [];
  const { prior, mount } = installDom(async url => {
    if (String(url) === '/api/chat/upload') return { ok: true, json: async () => ({ ok: true, filename: 'kept.txt', path: 'u/kept.txt' }) };
    if (!String(url).startsWith('/api/chat/history')) return { ok: true, json: async () => ({ active_direct_turns: [] }) };
    const cursor = new URL(String(url), 'http://local').searchParams.get('cursor');
    calls.push(cursor);
    const value = cursor ? page(Array.from({ length: 15 }, (_, i) => row(String(100 + i), '12')), 'saved:3')
      : await pendingRecent;
    return { ok: true, json: async () => value };
  });
  let frames = [];
  globalThis.requestAnimationFrame = fn => { frames.push(fn); return frames.length; };
  const oldSocket = globalThis.WebSocket;
  globalThis.WebSocket = { OPEN: 1 };
  const oldRect = ElementStub.prototype.getBoundingClientRect;
  let feed;
  ElementStub.prototype.getBoundingClientRect = function () {
    if (this === feed) return { top: 0, bottom: 400, left: 0, right: 600, width: 600, height: 400 };
    if (this.parentNode === feed) {
      const top = feed.children.indexOf(this) * 100 - feed.scrollTop;
      return { top, bottom: top + 100, left: 0, right: 600, width: 600, height: 100 };
    }
    return oldRect.call(this);
  };
  const instance = createChatInstance({
    ws: { on() { return () => {}; }, isConnected: () => true, ws: { readyState: 1 },
      send: () => ({ status: sendStatus, clientMessageId: 'sent-1' }) },
    state: { activePage: 'chat', projectChatIds: new Set(), unreadCount: 0 }, updateUnreadBadge() {},
    stateSnapshots: { begin: () => ({ generation: 1, requestedAt: Date.now() }),
      gate() { return Promise.resolve(this.begin()); }, isCurrent: () => true, apply() {} },
    chatId: 2, idPrefix: 'chat', mountEl: mount, asPanel: true,
    initialScrollState: { scrollTop: 2400, stick: false, historyAnchor: { id: 'chat:104', offset: 80 },
      history: { focus: 3, pages: Array.from({ length: 4 }, (_, index) => ({
        id: `history-page-1-${index}`, chain: 1, index, requestCursor: `saved:${index}`,
        nextCursor: index < 3 ? `saved:${index + 1}` : null, hasMore: index < 3, rows: 15,
      })) } },
  });
  feed = document.byId.get('chat-messages');
  Object.defineProperty(feed, 'scrollHeight', { configurable: true, get() { return this.children.length * 100; }, set() {} });
  let top = 0;
  Object.defineProperty(feed, 'scrollTop', { configurable: true, get() { return top; },
    set(value) { top = Math.max(0, Math.min(Number(value), this.scrollHeight - this.clientHeight)); } });
  async function frame() { const batch = frames; frames = []; for (const fn of batch) fn(); await tick(); }
  instance.restoreScrollPosition();
  const painted = instance.refreshHistory({ revision: 1 });
  const pendingBookmark = instance.getScrollState();
  await tick();
  for (let i = 0; i < framesBeforeResponse; i++) await frame();
  const input = document.byId.get('chat-input'), files = document.byId.get('chat-file-input');
  if (sendStatus) {
    if (sendStatus === 'failed') {
      document.body = new ElementStub('body', document); // the failure toast's host
      files.files = [{ name: 'kept.txt', type: 'text/plain' }];
      for (const listener of files.listeners.get('change')) listener({ target: files });
    }
    input.value = 'Sent while the saved place loads';
    for (const listener of input.listeners.get('keydown')) listener({ key: 'Enter', target: input, preventDefault() {} });
    for (let i = 0; i < 5; i++) await tick();
  }
  answerRecent(page(Array.from({ length: 15 }, (_, i) => row(String(900 + i), '21')), 'latest:0', 'older:1'));
  for (let i = 0; i < 35; i++) await frame();
  await painted;
  const messages = feed.children.filter(node => node.dataset.historyId);
  const target = messages.find(node => node.dataset.historyId === 'chat:104');
  const result = { pendingBookmark, framesBeforeResponse, requests: calls, scrollTop: feed.scrollTop,
    bottom: feed.scrollHeight - feed.clientHeight,
    echo: (rows => (at => at < 0 ? null : at - rows.length)(rows.map(node => node.dataset.clientMessageId).lastIndexOf('sent-1')))(
      feed.children.filter(node => !node.classList.contains('typing-bubble'))),
    draft: input.value, staged: instance.hasPendingWork(),
    targetOffset: target?.getBoundingClientRect().top, savedOffset: 80,
    mountedHistoryIds: messages.map(node => node.dataset.historyId),
    gapMarker: feed.querySelector('.chat-load-newer') !== null,
    historyNote: (feed.parentNode.querySelector('.chat-panel-statusbar').querySelector('.chat-load-older-note') || feed.querySelector('.chat-load-older').querySelector('.chat-load-older-note'))?.textContent };
  instance.destroy();
  ElementStub.prototype.getBoundingClientRect = oldRect;
  restoreDom(prior); globalThis.WebSocket = oldSocket;
  return result;
}
for (const frames of [0, 60]) test(`saved deep page waits for data (${frames} frames), not a frame deadline`, async () => {
    const result = await probe(frames);
    assert.equal(result.targetOffset, 80);
    assert.equal(result.pendingBookmark.historyAnchor.id, 'chat:104', 'close while pending retains the original target');
    assert.equal(result.pendingBookmark.scrollTop, 2400);
    assert.deepEqual(result.requests, [null, 'saved:3']);
    assert.match(result.historyNote, /Shown messages may have gaps/);
});
for (const status of ['sent', 'queued']) test(`an accepted Send (${status}) supersedes a saved place still loading`, async () => {
    const result = await probe(0, status);
    assert.equal(result.echo, -1, 'the local echo is the newest row');
    assert.equal(result.scrollTop, result.bottom, 'the late read cannot pull the reader back to the old place');
    assert.notEqual(result.targetOffset, 80);
});
test('a Send that fails keeps the saved place, the draft and the attachment', async () => {
    const result = await probe(0, 'failed');
    assert.equal(result.targetOffset, 80);
    assert.deepEqual([result.echo, result.draft, result.staged], [null, 'Sent while the saved place loads', true]);
});


test('canonical legacy origin adopts the retained node despite a different source label', async () => {
    const origin = { role: 'user', text: 'Original request', ts: '2026-09-01T00:00:00Z',
        client_message_id: '', origin_projected: true, origin_id: 'binding-source-ref' };
    let rows = [origin];
    const { prior, mount } = installDom(async url => ({ ok: true, json: async () =>
        String(url).startsWith('/api/chat/history') ? page(rows, 'latest') : { active_direct_turns: [] } }));
    const instance = createChatInstance({ ws: { on() { return () => {}; }, isConnected: () => true, send() {} },
        state: { activePage: 'chat', projectChatIds: new Set(), unreadCount: 0 }, updateUnreadBadge() {},
        stateSnapshots: { begin: () => ({ generation: 1 }), gate() { return Promise.resolve(this.begin()); },
            isCurrent: () => true, apply() {} },
        chatId: 2, idPrefix: 'chat', mountEl: mount, asPanel: true });
    try {
        await instance.refreshHistory({ revision: 1 });
        const feed = document.byId.get('chat-messages');
        const kept = feed.querySelector('.chat-bubble');
        assert.ok(kept.querySelector('.saved-project-context'));
        rows = [{ ...origin, source: 'web', history_id: 'chat:0', origin_projected: false }];
        await instance.refreshHistory({ revision: 2 });
        assert.equal(feed.querySelectorAll('.chat-bubble').filter(node => node.dataset.messageKey).length, 1);
        assert.equal(feed.querySelector('.chat-bubble'), kept);
        assert.equal(kept.dataset.historyId, 'chat:0');
        assert.equal(kept.querySelector('.saved-project-context'), null);
    } finally { instance.destroy(); restoreDom(prior); }
});

for (const shallow of [false, true]) test(shallow
    ? 'a partial recent window whose present ↓ need not read still lets a retained room reopen at its live place'
    : 'a clean latest read after a source-unavailable recent window restores readiness for a retained reopen', async () => {
    const span = (from, to) => ({ from, to, chain: 'c', gaps: [] });
    const coverage = (chat, progress) => ({ v: 1, view: 'v', upper: { chat: chat.to, progress: 0 }, spans: { chat, progress } });
    const empty = { from: 0, to: 0, chain: 'empty', gaps: [] };
    const rows = first => Array.from({ length: 15 }, (_, i) => row(String(first + i), '12'));
    const reads = [
        { ...page(rows(100), 'latest:0', 'older:1'), coverage: coverage(span(100, 115), empty) },
        // HTTP 200: a source is unreadable, so readable rows arrive without a page boundary.
        { messages: rows(shallow ? 100 : 200), reason_code: 'history_source_unavailable', page_cursor: null, next_cursor: null,
            has_more: true, window: { complete: false, truncated_by: ['progress_source_unavailable'] },
            coverage: coverage(shallow ? span(100, 115) : span(200, 215), null) },
        { ...page(rows(200), 'latest:1', 'older:2'), coverage: coverage(span(200, 215), empty) },
    ];
    const calls = [];
    const { prior, mount } = installDom(async url => {
        if (!String(url).startsWith('/api/chat/history')) return { ok: true, json: async () => ({ active_direct_turns: [] }) };
        calls.push(new URL(String(url), 'http://local').searchParams.get('cursor'));
        const body = reads.shift();
        return { ok: true, json: async () => body };
    });
    const instance = createChatInstance({ ws: { on() { return () => {}; }, isConnected: () => true, send() {} },
        state: { activePage: 'chat', projectChatIds: new Set(), unreadCount: 0 }, updateUnreadBadge() {},
        stateSnapshots: { begin: () => ({ generation: 1 }), gate() { return Promise.resolve(this.begin()); },
            isCurrent: () => true, apply() {} },
        chatId: 2, idPrefix: 'chat', mountEl: mount, asPanel: true });
    const feed = document.byId.get('chat-messages');
    const status = () => ({ button: feed.querySelector('.chat-load-older').querySelector('.chat-load-older-btn').textContent,
        note: feed.parentNode.querySelector('.chat-panel-statusbar').querySelector('.chat-load-older-note')?.textContent || '' });
    try {
        await instance.refreshHistory({ revision: 1 });
        await instance.refreshHistory({ revision: 2 });
        assert.deepEqual(status(), { button: 'Retry loading messages', note: 'Some saved history could not be loaded.' });
        document.byId.get('chat-scroll-bottom').click();
        for (let i = 0; i < 10; i++) await tick();
        if (shallow) {
            assert.deepEqual(calls, [null, null], 'the present is loaded, so ↓ reads nothing');
            assert.equal(status().button, 'Retry loading messages', 'the partial read stays disclosed');
        } else {
            assert.deepEqual(calls, [null, null, null], '↓ reads the present once');
            assert.notEqual(status().button, 'Retry loading messages', 'the clean latest window supersedes the failed recent read');
            assert.doesNotMatch(status().note, /could not be loaded/);
        }
        // A hidden pending-work room reopens at the same revision without another read.
        const before = calls.length;
        instance.restoreScrollPosition();
        await instance.refreshHistory({ revision: 2 });
        assert.equal(calls.length, before);
        assert.ok('disclosures' in instance.getScrollState(), 'the reopened reading intent was applied, not left pending');
    } finally { instance.destroy(); restoreDom(prior); }
});
