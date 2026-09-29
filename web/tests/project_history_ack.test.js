// Issue #1102, defect 3: app.js used to discard a rejected history paint with a
// bare `catch {}`. app.js is a boot script (top-level DOM wiring), so the ACK
// function is lifted out of its source and run against stubbed module state.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

// The span below is delimited by line breaks; normalize CRLF so a Windows
// checkout (core.autocrlf) reads the same bytes the delimiters expect.
const source = readFileSync(new URL('../app.js', import.meta.url), 'utf8').replace(/\r\n?/g, '\n');
const start = source.indexOf('async function acknowledgeProjectAfterPaint(');
const end = source.indexOf('\n}\n', start);
assert.ok(start >= 0 && end > start, 'acknowledgeProjectAfterPaint is a top-level function in app.js');
const ackSource = source.slice(start, end + 2);

function harness({ refreshHistory, page = {} }) {
    const acked = [], errors = [];
    const inst = {
        page: { hidden: false, isConnected: true, ...page },
        cancelHistoryPaint() {}, refreshHistory,
    };
    const context = vm.createContext({
        navState: { activeProjectId: 'p1' },
        projectInstances: new Map([['p1', inst]]),
        projectPaintRequests: new Map(),
        projectReveals: new Map(),
        state: { projectSeenRevision: {} },
        markProjectViewed: async (id, revision) => { acked.push([id, revision]); },
        console: { error: (...args) => errors.push(args) },
    });
    vm.runInContext(`${ackSource}\nglobalThis.ack = acknowledgeProjectAfterPaint;`, context);
    return { acked, errors, inst, context, open: () => context.ack({ id: 'p1', visible_revision: 5 }, inst) };
}

test('app.js no longer swallows a rejected history paint silently', () => {
    assert.doesNotMatch(ackSource, /catch\s*\{\s*\}/, 'no bare catch around the history paint');
    assert.match(ackSource, /paint\?\.read/, 'the read receipt (painted at the newest messages) gates the ACK');
});

test('a rejected history paint is reported and never acknowledged', async () => {
    const failure = new Error('history paint exploded');
    const h = harness({ refreshHistory: async () => { throw failure; } });
    await h.open();
    assert.deepEqual(h.acked, []);
    assert.equal(h.errors.length, 1, 'the rejection reaches the console instead of vanishing');
    assert.equal(h.errors[0].at(-1), failure);
    assert.equal(h.context.projectPaintRequests.size, 0, 'the failed request is released, so Retry can run again');
});

test('a failed history read resolves unpainted and is never acknowledged', async () => {
    const h = harness({ refreshHistory: async ({ revision }) => ({ painted: false, revision }) });
    await h.open();
    assert.deepEqual(h.acked, []);
    assert.deepEqual(h.errors, [], 'an unpainted receipt is an ordinary outcome, not a defect');
});

for (const [name, mutate] of [
    ['hidden while the read was in flight', inst => { inst.page.hidden = true; }],
    ['destroyed (detached) while the read was in flight', inst => { inst.page.isConnected = false; }],
]) test(`a paint on a panel ${name} acknowledges nothing`, async () => {
    const h = harness({ refreshHistory: async ({ revision }) => {
        mutate(h.inst);
        return { painted: true, revision };
    } });
    await h.open();
    assert.deepEqual(h.acked, [], 'a revision nobody saw is never acknowledged');
});

test('a painted, visible, connected panel acknowledges exactly the painted revision', async () => {
    const h = harness({ refreshHistory: async ({ revision }) => ({ painted: true, read: true, revision }) });
    await h.open();
    assert.deepEqual(h.acked, [['p1', 5]]);
});

test('a painted room whose reader is not at the newest messages acknowledges nothing', async () => {
    const h = harness({ refreshHistory: async ({ revision }) => ({ painted: true, read: false, revision }) });
    await h.open();
    assert.deepEqual(h.acked, [], 'opening or refreshing while reading older content is not reading');
    assert.equal(h.context.projectPaintRequests.size, 0, 'the next arrival at the newest messages can retry');
});

test('a question paint never rides an acknowledging request in flight; the reverse decides nothing', async () => {
    const pending = [];
    const h = harness({ refreshHistory: ({ revision }) => new Promise((resolve) => {
        pending.push(() => resolve({ painted: true, read: true, revision }));
    }) });
    const ordinary = h.open();
    const paintOnly = h.context.ack({ id: 'p1', visible_revision: 5 }, h.inst, { forcePaint: true, paintOnly: true });
    assert.equal(pending.length, 2, 'the question paint does not inherit the acknowledging request');
    const during = h.open();
    assert.equal(pending.length, 2, 'a request during the question paint rides it');
    pending[1]();
    await Promise.all([paintOnly, during]);
    assert.deepEqual(h.acked, [], 'the question paint, and whatever rode it, acknowledges nothing');
    pending[0]();
    await ordinary;
});

test('a paint-only open (a question revealed from Main) never acknowledges', async () => {
    const h = harness({ refreshHistory: async ({ revision }) => ({ painted: true, read: true, revision }) });
    await h.context.ack({ id: 'p1', visible_revision: 5 }, h.inst, { forcePaint: true, paintOnly: true });
    assert.deepEqual(h.acked, []);
});

// The shared cursor helpers are lifted the same way: markProjectViewed keeps the
// server's answer, and a re-read of the shared cursors only ever clears a dot.
function lift(name) {
    const from = source.indexOf(`function ${name}(`);
    const until = source.indexOf('\n}\n', from);
    assert.ok(from >= 0 && until > from, `${name} is a top-level function in app.js`);
    return source.slice(source.lastIndexOf('\n', from) + 1, until + 2);
}

function cursorHarness({ seen = {}, rows = [], answer = () => ({}) } = {}) {
    const posts = [], paints = [];
    const context = vm.createContext({
        state: { projectSeenRevision: { ...seen } },
        lastProjectRows: rows,
        paintProjectsNav: () => paints.push(rows.map((row) => [row.id, row._unread])),
        fetchJson: async (url, init = {}) => { posts.push([url, init.method || 'GET', init.body || '']); return answer(init); },
    });
    vm.runInContext(`${lift('markProjectViewed')}\n${lift('mergeProjectSeenRevisions')}
        let sharedSeenRead = null;\n${lift('refreshSharedProjectSeen')}
        globalThis.api = { markProjectViewed, mergeProjectSeenRevisions, refreshSharedProjectSeen,
            pending: () => sharedSeenRead };`, context);
    return { context, posts, paints, api: context.api };
}

test('an ACK keeps the server-confirmed cursor, not the requested revision', async () => {
    const rows = [{ id: 'p1', visible_revision: 7, _unread: true }];
    // The server clamps a request of 7 to what it had when the ACK landed (6).
    const h = cursorHarness({ rows, answer: () => ({ ok: true, project_seen_revision: { p1: 6 } }) });
    assert.equal(await h.api.markProjectViewed('p1', 7), true);
    assert.equal(h.context.state.projectSeenRevision.p1, 6);
    assert.equal(rows[0]._unread, true, 'revision 7 stays unread until a read covers it');
});

test('shared cursors from another client clear a stale dot and never revive a read one', async () => {
    const rows = [{ id: 'p1', visible_revision: 4, _unread: true }, { id: 'p2', visible_revision: 2, _unread: false }];
    const h = cursorHarness({ seen: { p2: 2 }, rows,
        answer: () => ({ project_seen_revision: { p1: 4, p2: 1 } }) });
    h.api.refreshSharedProjectSeen();
    h.api.refreshSharedProjectSeen();
    assert.equal(h.posts.length, 1, 'one shared-cursor read at a time');
    await h.api.pending();
    assert.deepEqual(rows.map((row) => row._unread), [false, false]);
    assert.equal(h.context.state.projectSeenRevision.p2, 2, 'an older cursor never moves this one back');
    assert.deepEqual(h.paints, [[['p1', false], ['p2', false]]]);
    assert.equal(h.api.pending(), null, 'the next snapshot may read again');
    h.api.mergeProjectSeenRevisions({ p1: 3 });
    assert.equal(rows[0]._unread, false, 'a stale answer cannot revive the dot');
});

test('a failed ACK or cursor read changes nothing', async () => {
    const rows = [{ id: 'p1', visible_revision: 4, _unread: true }];
    const h = cursorHarness({ rows, answer: () => { throw new Error('offline'); } });
    assert.equal(await h.api.markProjectViewed('p1', 4), false);
    h.api.refreshSharedProjectSeen();
    await h.api.pending();
    assert.equal(rows[0]._unread, true);
    assert.deepEqual(h.context.state.projectSeenRevision, {});
});

test('only the Project room paint path posts a read cursor', () => {
    const callers = [...source.matchAll(/markProjectViewed\(/g)].length;
    assert.equal(callers, 2, 'its definition and the one call inside acknowledgeProjectAfterPaint');
    assert.match(ackSource, /await markProjectViewed\(project\.id, revision\)/);
});

// A question opened from Main (DESIGN "Project unread dot"): landing on it is not
// reading the newer messages below it. The reveal is one transaction — its paint
// never joins an acknowledging request, and nothing is acknowledged until it ends.
function revealHarness() {
    const acked = [], calls = [], reveals = [];
    const pending = (list) => { let settle; const promise = new Promise((resolve) => { settle = resolve; }); list.push({ settle }); return promise; };
    const inst = {
        page: { hidden: false, isConnected: true }, generation: 0,
        cancelHistoryPaint() { this.generation += 1; },
        // Resolves when the test settles it; a cancelled paint lands unpainted, as chat.js's does.
        refreshHistory({ revision }) {
            const own = this.generation;
            const call = { revision, result: null };
            calls.push(call);
            return new Promise((resolve) => { call.settle = (result) => resolve(
                own === inst.generation ? { ...result, revision } : { painted: false, revision }); });
        },
        revealQuestion: () => pending(reveals),
    };
    const project = { id: 'p1', visible_revision: 5 };
    const context = vm.createContext({
        navState: { activeProjectId: 'p1' },
        projectInstances: new Map([['p1', inst]]),
        projectPaintRequests: new Map(), projectReveals: new Map(),
        lastProjectRows: [project],
        state: { projectSeenRevision: {} },
        markProjectViewed: async (id, revision) => { acked.push([id, revision]); },
        console: { error() {} },
    });
    vm.runInContext(`let projectNavigationGeneration = 1;\n${lift('freshProjectRow')}\n${ackSource}
        ${lift('revealProjectQuestion')}
        globalThis.api = { ack: acknowledgeProjectAfterPaint,
            reveal: (inst) => revealProjectQuestion(lastProjectRows[0], inst, projectNavigationGeneration, 't1', 'q1') };`, context);
    return { acked, calls, reveals, inst, project, context, api: context.api };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test('opening a question supersedes an acknowledgement in flight instead of joining it', async () => {
    const h = revealHarness();
    const ordinary = h.api.ack(h.project, h.inst);
    const revealed = h.api.reveal(h.inst);
    await flush();
    assert.equal(h.calls.length, 2, 'the question paint is its own request, not the one in flight');
    h.calls[0].settle({ painted: true, read: true });
    await ordinary;
    assert.deepEqual(h.acked, [], 'the superseded request acknowledges nothing');
    h.calls[1].settle({ painted: true, read: true });
    await flush();
    assert.equal(h.reveals.length, 1, 'the question is revealed after its paint');
    h.reveals[0].settle(true);
    await flush();
    h.calls[2].settle({ painted: true, read: false });
    await revealed;
    assert.deepEqual(h.acked, [], 'landing on the question is not reading the newer messages');
});

test('no acknowledgement decides while a question reveal is in progress; the reveal decides after it', async () => {
    const h = revealHarness();
    const revealed = h.api.reveal(h.inst);
    await flush();
    h.calls[0].settle({ painted: true, read: true });
    await flush();
    assert.equal(h.reveals.length, 1);
    // A state poll or an arrival edge during the reveal still paints, but reads nothing.
    const during = h.api.ack(h.project, h.inst);
    await flush();
    h.calls[1].settle({ painted: true, read: true });
    await during;
    assert.deepEqual(h.acked, [], 'the reader has not been placed yet');
    h.reveals[0].settle(true);
    await flush();
    assert.equal(h.calls.length, 3, 'the reveal ends with its own read decision');
    h.calls[2].settle({ painted: true, read: true });
    await revealed;
    assert.deepEqual(h.acked, [['p1', 5]], 'a reader at the newest messages after the reveal has read them');
});

// A panel closed with pending work (staged files, an upload) is hidden, not
// destroyed, and its paint is cancelled. Reopening it must start a read of its
// own: the cancelled request can only land unpainted, so joining it would leave
// the dot until the next revision or scroll.
test('a reopened pending-work survivor never joins the paint cancelled when it was hidden', async () => {
    const h = revealHarness();
    h.inst.hasPendingWork = () => true;
    h.inst.page.dataset = {};
    vm.runInContext(`${lift('cancelProjectPaint')}\n${lift('destroyProjectInstance')}
        globalThis.api.close = (pid) => { navState.activeProjectId = null; destroyProjectInstance(pid); };`, h.context);
    const before = h.api.ack(h.project, h.inst);
    await flush();
    h.api.close('p1');
    assert.equal(h.inst.page.hidden, true, 'the survivor is hidden, not destroyed');
    h.context.navState.activeProjectId = 'p1';
    h.inst.page.hidden = false;
    const reopened = h.api.ack(h.project, h.inst);
    assert.equal(h.calls.length, 2, 'the reopened panel reads again');
    h.calls[0].settle({ painted: true, read: true });
    await before;
    assert.deepEqual(h.acked, [], 'the cancelled paint acknowledges nothing');
    h.calls[1].settle({ painted: true, read: true });
    await reopened;
    assert.deepEqual(h.acked, [['p1', 5]]);
});
