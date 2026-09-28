import assert from 'node:assert/strict';
import test from 'node:test';
import { createReviewHydrator } from '../modules/review_presentation.js';
import { createChatReadingPosition } from '../modules/chat_reading_position.js';

function fixture(t, exact = true) {
    const previous = globalThis.requestAnimationFrame;
    const frames = [];
    globalThis.requestAnimationFrame = callback => frames.push(callback);
    t.after(() => { globalThis.requestAnimationFrame = previous; });
    let ready = false, visible = true, restored = 0, changes = 0;
    // Dispatch topology: the feed hears only events targeted inside it; the document hears all.
    const target = () => { const listeners = new Map();
        return { listeners, addEventListener: (type, fn) => listeners.set(type, fn), removeEventListener: type => listeners.delete(type) }; };
    const ownerDocument = { ...target(), body: {}, documentElement: {} };
    const feed = { scrollTop: 0, scrollHeight: 300, clientHeight: 500, clientWidth: 400, ownerDocument, ...target(),
        contains: node => node === feed || Boolean(node?.inFeed) };
    const dispatch = event => {
        if (feed.contains(event.target)) feed.listeners.get(event.type)?.(event);
        ownerDocument.listeners.get(event.type)?.(event);
    };
    const bookmark = { scrollTop: 2400, stick: false, historyAnchor: { historyId: 'chat:old', offset: 80 } };
    const reading = createChatReadingPosition({ initial: bookmark, feed, visible: () => visible,
        alive: () => true, ready: () => ready, anchors: { serialize: () => null, capture: () => null,
            restore: () => { restored++; feed.scrollTop = 80; return typeof exact === 'function' ? exact() : exact; } },
        fallback: () => false, changed() { changes++; }, afterWrite() {}, activity() {}, updateButton() {},
    });
    return { reading, feed, frames, bookmark, gesture: event => dispatch({ target: feed, ...event }),
        ready: () => { ready = true; }, hide: () => { visible = false; },
        changed: () => changes, restored: () => restored, frame: () => { const work = frames.splice(0); work.forEach(callback => callback()); } };
}

test('explicit navigation clears approximate position and immediately republishes status', t => {
    const f = fixture(t, false);
    f.ready(); f.reading.position(); f.frame(); f.frame();
    assert.equal(f.reading.approximate, true);
    assert.equal(f.changed(), 1);
    f.reading.cancel();
    assert.equal(f.reading.approximate, false);
    assert.equal(f.changed(), 2, 'the persistent notice clears without waiting for another history read');
});

test('an exact second positioning pass clears the earlier approximation', t => {
    let exact = false;
    const f = fixture(t, () => exact);
    f.ready(); f.reading.position(); f.frame();
    assert.equal(f.reading.approximate, true);
    exact = true;
    f.frame();
    assert.equal(f.reading.pending, false);
    assert.equal(f.reading.approximate, false);
});

test('a failed or delayed read schedules no layout polling and exports the original target', t => {
    const f = fixture(t);
    f.reading.request();
    for (let i = 0; i < 100; i++) f.frame();
    assert.equal(f.frames.length, 0);
    assert.deepEqual(f.reading.export(), f.bookmark);
    f.reading.mutate(() => { f.feed.scrollHeight = 400; });
    assert.equal(f.reading.stick, false, 'short/empty layout cannot opt the reader into follow');
    f.ready(); f.reading.position(); f.frame(); f.frame();
    assert.equal(f.reading.pending, false);
    assert.equal(f.reading.stick, false);
    assert.equal(f.restored(), 2);
});

test('wheel/latest/question cancellation wins even between positioning frames', t => {
    const f = fixture(t); f.ready(); f.reading.position(); f.frame();
    f.reading.cancel(); f.feed.scrollTop = 210;
    f.frame();
    assert.equal(f.feed.scrollTop, 210);
    assert.equal(f.reading.export(), null);
    f.reading.followAfterLayout(); f.reading.cancel(); f.frame();
    assert.equal(f.feed.scrollTop, 210, 'a superseded latest layout cannot pull the reader');
});

test('hidden/disposed layout retains intent for a later data-aware show', t => {
    const f = fixture(t); f.ready(); f.reading.position(); f.hide(); f.frame();
    assert.deepEqual(f.reading.export(), f.bookmark);
    assert.equal(f.restored(), 0);
});

test('only downward intent on a scrollable feed can resume follow', t => {
    const f = fixture(t), navigation = [];
    f.reading.bindGestures(direction => navigation.push(direction));
    f.gesture({ type: 'wheel', deltaY: 1 }); f.frame();
    assert.equal(f.reading.stick, false, 'empty/short geometry cannot opt into follow');
    f.feed.scrollHeight = 1000; f.feed.scrollTop = 500;
    f.gesture({ type: 'wheel', deltaY: -1 }); f.reading.scroll(); f.frame();
    assert.equal(f.reading.stick, false, 'upward intent at bottom stops follow');
    f.gesture({ type: 'wheel', deltaY: 1 }); f.frame();
    assert.equal(f.reading.stick, true);
    f.gesture({ type: 'keydown', key: ' ', shiftKey: true }); f.frame();
    assert.equal(f.reading.stick, false, 'Shift+Space is upward');
    f.gesture({ type: 'wheel', deltaY: 1 });
    f.gesture({ type: 'wheel', deltaY: -1 }); f.frame();
    assert.equal(f.reading.stick, false, 'latest gesture supersedes a queued downward frame');
    const generation = f.reading.generation;
    f.gesture({ type: 'keydown', key: ' ', target: { inFeed: true, closest: selector => selector.includes('button') ? {} : null } });
    f.gesture({ type: 'keydown', key: 'ArrowDown', defaultPrevented: true });
    f.frame();
    assert.equal(f.reading.generation, generation, 'control activation is not archive navigation');
    assert.deepEqual(navigation, [1, -1, 1, -1, -1]);
});

test('keys scroll the pressed feed or its focused control even though keydown never targets the feed', t => {
    const f = fixture(t), navigation = [];
    const control = { inFeed: true, closest: selector => selector.includes('button') ? {} : null };
    const body = f.feed.ownerDocument.body;
    f.reading.bindGestures(direction => navigation.push(direction));
    f.reading.request();
    f.gesture({ type: 'keydown', key: 'PageDown', target: body }); f.frame();
    assert.equal(f.reading.pending, true, 'keys after pressing elsewhere scroll another surface');
    f.gesture({ type: 'pointerdown', target: { inFeed: true } });
    assert.equal(f.reading.pending, true, 'pressing the text being read is not navigation');
    f.gesture({ type: 'keydown', key: 'PageDown', target: body }); f.frame();
    assert.equal(f.reading.pending, false, 'nothing focused: the last pressed feed scrolls');
    f.reading.request();
    f.gesture({ type: 'keydown', key: ' ', target: control }); f.frame();
    assert.equal(f.reading.pending, true, 'Space activates a focused control');
    f.gesture({ type: 'keydown', key: 'ArrowUp', target: control }); f.frame();
    assert.equal(f.reading.pending, false, 'arrow keys on a focused control scroll its feed');
    f.reading.request();
    f.gesture({ type: 'keydown', key: 'ArrowUp', target: { inFeed: true, closest: selector => selector.includes('textarea') ? {} : null } });
    f.gesture({ type: 'pointerdown', target: {} });
    f.gesture({ type: 'keydown', key: 'PageUp', target: body }); f.frame();
    assert.equal(f.reading.pending, true, 'editable caret keys and presses outside the feed keep the bookmark');
    assert.deepEqual(navigation, [1, -1]);
});

test('saved Review readiness distinguishes pending/error from confirmed absence and trails revisions', async () => {
    const reads = [], settled = [];
    const hydrator = createReviewHydrator({
        fetchDetail: id => new Promise((resolve, reject) => reads.push({ id, resolve, reject })),
        applyDetail: () => true,
        onSettled: id => settled.push([id, hydrator.ready(id)]),
    });
    const first = hydrator.hydrate('bookmark', 'a'.repeat(64));
    const unrelated = hydrator.hydrate('unrelated');
    assert.equal(hydrator.ready('bookmark'), false);
    assert.equal(hydrator.ready('no-detail-needed'), true);
    await Promise.resolve();
    reads[0].reject(new Error('temporarily unavailable'));
    await first;
    assert.equal(hydrator.ready('bookmark'), false, 'failure preserves the bookmark for Retry');
    const retry = hydrator.hydrate('bookmark', 'a'.repeat(64));
    const trailing = hydrator.hydrate('bookmark', 'b'.repeat(64));
    await Promise.resolve();
    reads[2].resolve({});
    await retry;
    assert.equal(hydrator.ready('bookmark'), false, 'a queued necessary revision is still pending');
    reads[3].resolve(null);
    await trailing;
    assert.equal(hydrator.ready('bookmark'), true, 'successful absence permits explicit fallback');
    assert.equal(hydrator.ready('unrelated'), false, 'unrelated detail is still held');
    assert.deepEqual(settled.filter(([id]) => id === 'bookmark'), [
        ['bookmark', false], ['bookmark', false], ['bookmark', true],
    ]);
    hydrator.clear();
    reads[1].resolve({});
    await unrelated;
    assert.equal(settled.some(([id]) => id === 'unrelated'), false, 'disposed requests cannot resume positioning');
});
