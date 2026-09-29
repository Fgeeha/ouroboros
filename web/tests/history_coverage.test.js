import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatHistoryPager, historyCoverage } from '../modules/chat_history.js';

const coverage = (from, to, upper = 100, extras = {}) => ({ v: 1, view: 'room',
    upper: { chat: upper, progress: 0 }, spans: {
        chat: { from, to, chain: 'retained', gaps: [], ...extras },
        progress: { from: 0, to: 0, chain: 'empty', gaps: [] },
    } });

test('page 3 at EOF plus recent is partial, despite overlapping row identities', () => {
    const recent = coverage(80, 100);
    assert.deepEqual(historyCoverage(recent, [coverage(0, 20)]), { complete: false, gaps: true, horizonGap: true });
    assert.equal(historyCoverage(recent, [coverage(0, 20), coverage(20, 80)]).complete, true);
    assert.equal(historyCoverage(recent, [coverage(20, 80)]).gaps, false, 'contiguous tail needs only ordinary load affordance');
});

test('read gaps, unknown metadata, disabled sources and incompatible coordinates never prove complete', () => {
    const recent = coverage(80, 100);
    for (const page of [coverage(0, 80, 100, { gaps: ['invalid_json'] }),
        coverage(0, 80, 100, { chain: 'replaced-prefix' }), { ...coverage(0, 80), view: 'other-view' }, null]) {
        const state = historyCoverage(recent, [page]);
        assert.equal(state.complete, false);
        assert.equal(state.gaps, true, 'unresolved bad metadata must remain disclosed at EOF');
    }
    assert.equal(historyCoverage({ ...recent, spans: { chat: null, progress: recent.spans.progress } }, []).complete, false);
    assert.equal(historyCoverage(null, [coverage(0, 100)]).complete, false);
});

test('a complete recent read cannot certify protected rows from a replaced archive chain', () => {
    const state = historyCoverage(coverage(0, 100), [coverage(0, 100, 100, { chain: 'replaced' })]);
    assert.equal(state.complete, false);
    assert.equal(state.gaps, true);
    const tainted = coverage(0, 50, 100, { gaps: ['invalid_json'] });
    assert.equal(historyCoverage(coverage(50, 100), [tainted]).gaps, true);
    assert.equal(historyCoverage(coverage(0, 100), [tainted]).complete, true, 'a clean reread heals its bytes');
});

test('a newer recent horizon does not heal bytes beyond the frozen old snapshot', () => {
    const recent = coverage(180, 200, 200);
    assert.equal(historyCoverage(recent, [coverage(0, 100)]).horizonGap, true);
    assert.equal(historyCoverage(recent, [coverage(0, 100), coverage(100, 180, 200)]).complete, true);
});

test('the first write of a previously empty source does not invent a gap', () => {
    const empty = coverage(0, 0, 0, { chain: 'empty' });
    assert.equal(historyCoverage(coverage(0, 100), [empty]).complete, true);
    for (const span of [null, { ...empty.spans.chat, gaps: ['read_error'] },
        { ...empty.spans.chat, from: 20, to: 20 }]) {
        assert.equal(historyCoverage(coverage(0, 100), [{ ...empty, spans: { ...empty.spans, chat: span } }]).gaps, true);
    }
});

test('same-rowcount reread refreshes spans, empty saved descriptors have no loaded authority', async () => {
    let current = coverage(0, 50, 100, { gaps: ['invalid_json'] });
    const make = () => createChatHistoryPager({
        fetchPage: () => ({ messages: [], page_cursor: 'p', next_cursor: null, has_more: false, coverage: current }),
        applyPage() {}, releasePage() {},
    });
    const pager = make(); await pager.latest();
    const saved = pager.exportResume();
    let resolve;
    const restored = createChatHistoryPager({ fetchPage: () => new Promise(done => { resolve = done; }), applyPage() {}, releasePage() {} });
    const pending = restored.restore(saved);
    assert.deepEqual(restored.getState().coverage, []);
    await Promise.resolve();
    current = coverage(0, 100);
    resolve({ messages: [], page_cursor: 'p', next_cursor: null, has_more: false, coverage: current });
    await pending;
    assert.equal(historyCoverage(current, restored.getState().coverage).complete, true);
    pager.destroy(); restored.destroy();
});

test('rotation keeps earlier prefix witnesses; a replaced later archive cannot complete coverage', () => {
    // Each span lists rolling prefix witnesses through its trailing segments
    // (symbolic here). A page is compatible when its own last witness is still
    // listed by the newest recent read.
    const at = (from, to, upper, chain) => coverage(from, to, upper, { chain });
    const pages = [at(0, 40, 100, 'a.ab.abc'), at(40, 80, 100, 'a.ab.abc')];
    assert.equal(historyCoverage(at(80, 100, 100, 'a.ab.abc'), pages).complete, true);
    assert.deepEqual(historyCoverage(at(80, 140, 140, 'ab.abc.abcd'), pages),
        { complete: true, gaps: false, horizonGap: false }, 'the live segment rotated and a new one began');
    const replaced = historyCoverage(at(80, 140, 140, "ab'.ab'c.ab'cd"), pages);
    assert.equal(replaced.complete, false);
    assert.equal(replaced.gaps, true, 'bytes read before a later archive was replaced are disclosed');
    assert.equal(historyCoverage(at(80, 140, 140, 'ab.abc.abcd'), [at(0, 80, 100, 'abc.x')]).gaps, true,
        'only the span\'s own last witness proves its coordinates');
});
