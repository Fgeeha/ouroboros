// Owner Batch4: the Continue press keeps ONE action nonce per task across
// retries and reloads, reads the server's offer, and points a stale card to
// its accepted successor instead of offering a second Continue.

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import { continueNonce, continueOfferView, continueTaskAction, syncContinueAction } from '../modules/task_continue.js';
import { ElementStub, installDom, restoreDom } from './chat_dom_fixture.js';

function memoryStorage() {
    const map = new Map();
    return { getItem: (k) => (map.has(k) ? map.get(k) : null), setItem: (k, v) => map.set(k, String(v)), map };
}

test('the action nonce is created once per task and survives a reload (same storage)', async () => {
    const storage = memoryStorage();
    const first = continueNonce('task-1', storage);
    assert.match(first, /^[A-Za-z0-9_-]{8,128}$/);
    assert.equal(continueNonce('task-1', storage), first, 'a retry or a reload reuses the SAME nonce');
    assert.notEqual(continueNonce('task-2', storage), first, 'another task is another action');
    const reloaded = await import('../modules/task_continue.js?reload-test');
    assert.equal(reloaded.continueNonce('task-1', storage), first);
});

test('lost answers retain the page action when storage reads or writes throw', async () => {
    for (const failingMethod of ['getItem', 'setItem']) {
        const storage = memoryStorage();
        storage[failingMethod] = () => { throw new Error('storage unavailable'); };
        const seen = [];
        const request = async (_id, nonce) => {
            seen.push(nonce);
            if (seen.length === 1) throw new Error('answer lost');
            return { successor_task_id: 'accepted-root' };
        };
        const id = `storage-failure-${failingMethod}`;
        assert.equal(await continueTaskAction(id, { request, storage, toast() {} }), '');
        assert.equal(await continueTaskAction(id, { request, storage, toast() {} }), 'accepted-root');
        assert.equal(seen[0], seen[1]);
        const recoveredStorage = memoryStorage();
        assert.equal(continueNonce(id, recoveredStorage), seen[0]);
        assert.equal(recoveredStorage.getItem(`ouro_continue_nonce:${id}`), seen[0]);
    }
});

test('a throwing localStorage accessor still permits a stable page action', () => {
    const prior = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
    try {
        Object.defineProperty(globalThis, 'localStorage', { configurable: true,
            get() { throw new Error('access denied'); } });
        const first = continueNonce('accessor-failure');
        assert.equal(continueNonce('accessor-failure'), first);
    } finally {
        if (prior) Object.defineProperty(globalThis, 'localStorage', prior);
        else delete globalThis.localStorage;
    }
});

test('the card offers Continue only as the server says, and otherwise points to the successor', () => {
    assert.deepEqual(continueOfferView({ continuation_offer: { eligible: true, cause: 'owner_restart' } }),
        { kind: 'offer', cause: 'owner_restart' });
    assert.deepEqual(continueOfferView({ continuation_offer: { eligible: false, refusal: 'stopped_by_owner' } }),
        { kind: 'none' });
    assert.deepEqual(continueOfferView({ continuation_offer: { eligible: false, successor_task_id: 's-1' } }),
        { kind: 'successor', successorId: 's-1' });
    assert.deepEqual(continueOfferView({}), { kind: 'none' });
});

test('a lost answer is retried under the SAME nonce; an already accepted press names its successor', async () => {
    const storage = memoryStorage();
    const seen = [];
    const toasts = [];
    const toast = (text, kind) => toasts.push([text, kind]);
    let attempt = 0;
    const request = async (id, nonce) => {
        seen.push([id, nonce]);
        attempt += 1;
        if (attempt === 1) throw Object.assign(new Error('network timeout'), { body: {} });
        return { ok: true, successor_task_id: 'root-1-cabc', held: attempt === 2 };
    };
    assert.equal(await continueTaskAction('root-1', { request, storage, toast }), '');
    assert.match(toasts[0][0], /^Continue not confirmed: network timeout/);
    assert.equal(await continueTaskAction('root-1', { request, storage, toast }), 'root-1-cabc');
    assert.equal(seen[0][1], seen[1][1], 'the retry carries the same action nonce');
    assert.match(toasts[1][0], /waits until the interrupted task's own work has settled/);
    const refused = async () => { throw Object.assign(new Error('continue refused: already_continued'), {
        body: { reason_code: 'already_continued', successor_task_id: 'root-1-cabc' } }); };
    assert.equal(await continueTaskAction('root-9', { request: refused, storage, toast }), 'root-1-cabc');
});

test('chat reaches the Continue action through the one settled-card seam', () => {
    const chat = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    assert.match(chat, /import \{ syncSettledItems \} from '\.\/settled_card\.js';/);
    const seam = readFileSync(new URL('../modules/settled_card.js', import.meta.url), 'utf8');
    assert.match(seam, /syncContinueAction\(record, detail\)/);
    assert.match(seam, /return syncResultFilesItem\(record, detail\)/);
});

function settledRootCard(taskId, { isSubagent = false } = {}) {
    const root = new ElementStub('div', globalThis.document);
    root.isConnected = true;
    root.classList.add('chat-live-card');
    const deep = (node, key) => {
        for (const child of node.children) {
            if (Object.hasOwn(child.dataset, key)) return child;
            const hit = deep(child, key);
            if (hit) return hit;
        }
        return null;
    };
    root.querySelector = (selector) => (selector === '[data-continue-task]' ? deep(root, 'continueTask') : null);
    return { root, groupId: taskId, isSubagent };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

test('an event row never erases the offer; a root that ended without an answer reads its detail once', async () => {
    const { prior } = installDom();
    try {
        const reads = [];
        const read = async (id) => {
            reads.push(id);
            return { status: 'cancelled', continuation_offer: { eligible: true, cause: 'snapshot_restore' } };
        };
        const card = settledRootCard('crashed-root');
        // A replayed terminal event carries no offer: it asks the full detail once.
        assert.equal(syncContinueAction(card, { type: 'task_done', status: 'cancelled' }, { read }), false);
        await flush();
        assert.deepEqual(reads, ['crashed-root']);
        const button = card.root.querySelector('[data-continue-task]');
        assert.equal(button?.textContent, 'Continue');
        // Later event rows neither re-read nor remove the shown action.
        syncContinueAction(card, { type: 'task_eval', status: 'cancelled' }, { read });
        syncContinueAction(card, { type: 'task_metrics_event' }, { read });
        await flush();
        assert.deepEqual(reads, ['crashed-root']);
        assert.equal(card.root.querySelector('[data-continue-task]'), button);
        // The full detail still decides: the accepted successor replaces the offer.
        syncContinueAction(card, { continuation_offer: { eligible: false, successor_task_id: 'crashed-root-c1' } });
        assert.equal(button.textContent, 'Continued as crashed-root-c1');
        assert.equal(button.disabled, true);

        // A finished answer, a live row and a child card never read the detail.
        for (const [record, row] of [[settledRootCard('done-root'), { type: 'task_done', status: 'completed' }],
            [settledRootCard('live-root'), { type: 'task_done', status: 'running' }],
            [settledRootCard('child', { isSubagent: true }), { type: 'task_done', status: 'failed' }]]) {
            syncContinueAction(record, row, { read });
        }
        await flush();
        assert.deepEqual(reads, ['crashed-root']);
    } finally {
        restoreDom(prior);
    }
});

test('a failed detail read lets the next terminal row retry', async () => {
    const { prior } = installDom();
    try {
        let calls = 0;
        const read = async () => {
            calls += 1;
            if (calls === 1) throw new Error('offline');
            return { status: 'failed', continuation_offer: { eligible: true, cause: 'provider_unavailable' } };
        };
        const card = settledRootCard('flaky-root');
        syncContinueAction(card, { type: 'task_done', status: 'failed' }, { read });
        await flush();
        assert.equal(card.root.querySelector('[data-continue-task]'), null);
        syncContinueAction(card, { type: 'task_done', status: 'failed' }, { read });
        await flush();
        assert.equal(calls, 2);
        assert.equal(card.root.querySelector('[data-continue-task]')?.textContent, 'Continue');
    } finally {
        restoreDom(prior);
    }
});

test('a replayed history row states the offer: shown after a reload without opening, never read', async () => {
    const { prior } = installDom();
    try {
        const reads = [];
        const read = async (id) => { reads.push(id); return null; };
        // The history projection carries the host's offer on the settled root's rows.
        const eligible = settledRootCard('restarted-root');
        syncContinueAction(eligible, { system_type: 'task_summary', task_terminal_status: 'cancelled',
            continuation_offer: { eligible: true, cause: 'owner_restart' } }, { read });
        assert.equal(eligible.root.querySelector('[data-continue-task]')?.textContent, 'Continue');
        const claimed = settledRootCard('continued-root');
        syncContinueAction(claimed, { is_progress: true, task_terminal_status: 'cancelled',
            continuation_offer: { eligible: false, refusal: 'already_continued', successor_task_id: 'continued-root-c9' } },
        { read });
        const pointer = claimed.root.querySelector('[data-continue-task]');
        assert.equal(pointer?.textContent, 'Continued as continued-root-c9');
        assert.equal(pointer?.disabled, true);
        // An ineligible root's rows say so too: no button and no detail read.
        const stopped = settledRootCard('stopped-root');
        syncContinueAction(stopped, { system_type: 'task_summary', task_terminal_status: 'cancelled',
            continuation_offer: { eligible: false, refusal: 'stopped_by_owner' } }, { read });
        assert.equal(stopped.root.querySelector('[data-continue-task]'), null);
        await flush();
        assert.deepEqual(reads, []);
    } finally {
        restoreDom(prior);
    }
});

test('a live best-effort completion reads its detail once (a technical limit may have ended it)', async () => {
    const { prior } = installDom();
    try {
        const reads = [];
        const read = async (id) => {
            reads.push(id);
            return { status: 'completed', continuation_offer: { eligible: true, cause: 'round_limit' } };
        };
        const limited = settledRootCard('limited-root');
        syncContinueAction(limited, { type: 'task_done', status: 'completed', reason_code: 'round_limit',
            outcome_axes: { execution: { status: 'best_effort' } } }, { read });
        const clean = settledRootCard('clean-root');
        syncContinueAction(clean, { type: 'task_done', status: 'completed',
            outcome_axes: { execution: { status: 'ok' } } }, { read });
        await flush();
        assert.deepEqual(reads, ['limited-root'], 'a clean completion never reads');
        assert.equal(limited.root.querySelector('[data-continue-task]')?.textContent, 'Continue');
    } finally {
        restoreDom(prior);
    }
});

test('chat replays settled rows through the one seam, not a card-opening detail read', () => {
    const chat = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    const summary = chat.slice(chat.indexOf('function appendTaskSummaryToLiveCard'),
        chat.indexOf('function renderLiveCardMeta'));
    assert.match(summary, /syncSettledItems\(record, msg\);/);
});
