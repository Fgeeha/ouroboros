import test from 'node:test';
import assert from 'node:assert/strict';
import { handoffPhase } from '../modules/project_handoff.js';

test('binding alone and offline activity never imply Working', () => {
    assert.equal(handoffPhase(null, null).text, 'Activity unconfirmed');
    assert.equal(handoffPhase({ phase: 'working' }, null, false).text, 'Activity unconfirmed');
    assert.equal(handoffPhase({ phase: 'working' }, null).text, 'Working');
});
test('terminal truth wins over stale census and survives offline', () => {
    for (const online of [true, false]) {
        assert.equal(handoffPhase({ phase: 'working' }, { status: 'completed' }, online).text, 'Done');
        assert.equal(handoffPhase(null, { status: 'failed' }, online).text, 'Failed');
    }
});
test('waiting, queued, paused and finalizing stay distinct', () => {
    assert.equal(handoffPhase({ phase: 'queued' }, null).text, 'Queued');
    assert.equal(handoffPhase({ phase: 'budget_paused' }, null).text, 'Paused');
    assert.equal(handoffPhase({ phase: 'finalizing' }, null).text, 'Finalizing…');
    assert.equal(handoffPhase({ phase: 'working', required_question: {} }, null).text, 'Waiting');
    assert.equal(handoffPhase(null, { status: 'interrupted' }).text, 'Activity unconfirmed');
});

// Exercise the actual controller, not a second phase state machine.
import { createProjectHandoffs } from '../modules/project_handoff.js';
class Element {
    constructor() { this.children = []; this.dataset = {}; this.className = ''; this.hidden = false;
        this.classList = { add: value => { this.className += ` ${value}`; } }; }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute() {}
    addEventListener() {}
    querySelector(selector) { return this.children.find(n => `.${n.className}` === selector) || null; }
}
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
function setup(fetchDetail) {
    const saved = globalThis.document;
    globalThis.document = { createElement: () => new Element() };
    const nodes = new Set(), starts = [], annotations = [];
    const feed = { contains: node => nodes.has(node), querySelectorAll: sel =>
        sel === '.msg-routing-annotation' ? annotations : starts };
    const controller = createProjectHandoffs({ feed, fetchDetail, mutate: fn => fn() });
    function mount(taskId = 't', handoffId = 'h') {
        const node = new Element(); nodes.add(node);
        const anchor = controller.mount(node, { taskId, projectId: 'p', projectName: 'Room', title: 'Work', handoffId });
        return { node, anchor, status: anchor.children[0].children[0] };
    }
    return { controller, nodes, starts, annotations, mount,
        done() { controller.destroy(); globalThis.document = saved; } };
}
const census = (activities = [], complete = true) => ({ active_chat_activities: activities,
    active_chat_activities_complete: complete, supervisor_ready: true });

test('canonical handoff identity deduplicates retries, not independent requests', () => {
    const h = setup(async () => null);
    try {
        const first = h.mount('old', 'origin-1');
        assert.equal(h.mount('retry', 'origin-1').anchor, first.node);
        assert.notEqual(h.mount('old', 'origin-2').anchor, first.node);
        const started = new Element(); started.dataset = { taskId: 'retry', projectId: 'p' };
        h.starts.push(started); h.controller.reconcile(); assert.equal(started.hidden, true);
        h.nodes.delete(first.node); h.controller.reconcile(); assert.equal(started.hidden, false);
    } finally { h.done(); }
});
test('slow terminal response survives unrelated census ticks; no repeated detail polling', async () => {
    let settle, calls = 0;
    const h = setup(() => { calls++; return new Promise(resolve => { settle = resolve; }); });
    try {
        const { status } = h.mount();
        h.controller.snapshot(census()); await flush();
        for (let i = 0; i < 10; i++) h.controller.snapshot(census());
        assert.equal(calls, 1);
        settle({ status: 'completed' }); await flush();
        assert.equal(status.textContent, 'Done');
        h.controller.snapshot(census()); await flush(); assert.equal(calls, 1);
    } finally { h.done(); }
});
test('failed detail stays unknown until reconnect, and new positive evidence invalidates an old read', async () => {
    let calls = 0, settle;
    const h = setup(() => { calls++; if (calls === 1) throw new Error('unavailable');
        return new Promise(resolve => { settle = resolve; }); });
    try {
        const { status } = h.mount();
        h.controller.snapshot(census()); await flush();
        h.controller.snapshot(census()); await flush(); assert.equal(calls, 1);
        h.controller.setConnected(false); h.controller.setConnected(true);
        h.controller.snapshot(census()); await flush(); assert.equal(calls, 2);
        h.controller.snapshot(census([{ activity_id: 't', phase: 'working' }]));
        settle({ status: 'completed' }); await flush(); assert.equal(status.textContent, 'Working');
        h.controller.snapshot(census([], false)); assert.equal(status.textContent, 'Activity unconfirmed');
    } finally { h.done(); }
});
test('explicit retry linkage selects successor without a second anchor; disposal ignores late result', async () => {
    const ids = []; let settle;
    const h = setup(id => { ids.push(id); return id === 't'
        ? { status: 'interrupted', superseded_by: 'r' }
        : new Promise(resolve => { settle = resolve; }); });
    try {
        const { status } = h.mount(); h.controller.snapshot(census()); await flush();
        assert.deepEqual(ids, ['t', 'r']);
        h.controller.destroy(); settle({ status: 'completed' }); await flush();
        assert.equal(status.textContent, 'Activity unconfirmed');
    } finally { h.done(); }
});
test('model waits use the current attempt and ignore resolved older waits', () => {
    const model_waits = { old: { state: 'waiting', task_attempt: 1 }, now: { state: 'resolved', task_attempt: 2 } };
    assert.equal(handoffPhase({ phase: 'working', model_waits, task_attempt: 2 }).text, 'Working');
    model_waits.now.state = 'waiting';
    assert.equal(handoffPhase({ phase: 'working', model_waits, task_attempt: 2 }).text, 'Waiting');
});
