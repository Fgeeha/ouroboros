import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { installDom, restoreDom, ElementStub } from './chat_dom_fixture.js';
const row = (id, hour) => ({ role: 'assistant', text: id, ts: `2026-09-26T${hour}:00:00.000Z`,
  history_id: `chat:${id}`, history_position: { source: 'chat', offset: Number(id) } });
const page = (messages, cursor, next = null) => ({ messages, page_cursor: cursor, next_cursor: next,
  has_more: next !== null, window: { complete: !next, truncated_by: next ? ['quota'] : [] } });
const tick = () => new Promise(resolve => setImmediate(resolve));

async function probe(framesBeforeResponse) {
  let answerRecent;
  const pendingRecent = new Promise(resolve => { answerRecent = resolve; });
  const calls = [];
  const { prior, mount } = installDom(async url => {
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
    ws: { on() { return () => {}; }, isConnected: () => true, send() {} },
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
  answerRecent(page(Array.from({ length: 15 }, (_, i) => row(String(900 + i), '21')), 'latest:0', 'older:1'));
  for (let i = 0; i < 35; i++) await frame();
  await painted;
  const messages = feed.children.filter(node => node.dataset.historyId);
  const target = messages.find(node => node.dataset.historyId === 'chat:104');
  const result = { pendingBookmark, framesBeforeResponse, requests: calls, scrollTop: feed.scrollTop,
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
