// S3 (Q2/HQ1): the shared three-action task stop/hurry control — exact owner
// wording, action gating around a pending cancel, stable hurry request-id
// reuse, and the no-chat-bubble contract pinned at source for both surfaces.

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import {
    ACTION_FINALIZE,
    ACTION_HURRY,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_STOP_NOW,
    TASK_CONTROL_LABELS,
    TASK_CONTROL_TRIGGER_LABEL,
    hurryRequestId,
    isRootTaskRow,
    pauseTaskAction,
    stopPolicyFor,
    taskControlActions,
} from '../modules/task_control_menu.js';
import { ownerHurryProjection, summarizeChatLiveEvent, taskSoftStopPending } from '../modules/log_events.js';

const chat = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
const activity = readFileSync(new URL('../modules/activity.js', import.meta.url), 'utf8');
const menuSrc = readFileSync(new URL('../modules/task_control_menu.js', import.meta.url), 'utf8');

// --- the frozen owner dropdown (Q2/HQ1) ---

test('the dropdown offers exactly the owner-decided actions, in order', () => {
    // Q2/HQ1 froze Wrap up / Hurry up / Stop now; owner Batch4 (5A) added the
    // whole-tree Pause beside them, before the hard stop.
    assert.deepEqual(taskControlActions(), [ACTION_FINALIZE, ACTION_HURRY, ACTION_PAUSE, ACTION_STOP_NOW]);
    assert.equal(TASK_CONTROL_LABELS[ACTION_FINALIZE], 'Wrap up');
    assert.equal(TASK_CONTROL_LABELS[ACTION_HURRY], 'Hurry up');
    assert.equal(TASK_CONTROL_LABELS[ACTION_PAUSE], 'Pause');
    assert.equal(TASK_CONTROL_LABELS[ACTION_STOP_NOW], 'Stop now');
    // A paused member offers Resume, never a second Pause; a pending Stop wins.
    assert.deepEqual(taskControlActions({ budgetPaused: true }), [ACTION_RESUME, ACTION_STOP_NOW]);
    assert.equal(stopPolicyFor(ACTION_PAUSE), '', 'Pause is not a stop');
});

test('Pause sends one text-free request with a stable id and reports the truthful tree state', async () => {
    const calls = [];
    const toasts = [];
    const pause = async (id, requestId) => { calls.push([id, requestId]); return calls.length === 1
        ? { ok: true, state: 'requested' } : { ok: true, state: 'paused' }; };
    const toast = (text, kind) => toasts.push([text, kind]);
    assert.equal(await pauseTaskAction('root-1', { pause, toast }), true);
    assert.equal(await pauseTaskAction('root-1', { pause, toast }), true);
    assert.equal(calls[0][1], calls[1][1], 'a retry reuses the SAME request id');
    assert.match(calls[0][1], /^pause-/);
    assert.match(toasts[0][0], /^Pausing: .*work already sent finishes/);
    assert.match(toasts[1][0], /^Paused: the whole task tree is saved/);
    assert.equal(await pauseTaskAction('root-2', { pause: async () => { throw new Error('cancel_pending'); }, toast }), false);
    assert.match(toasts.at(-1)[0], /^Pause refused: cancel_pending/);
    // Both surfaces reach it through the shared menu, resolved from the anchor.
    assert.match(menuSrc, /anchor\.dataset\?\.id \|\| anchor\.closest\?\.\('\[data-task-id\]'\)/);
});

test('a pending cancel offers ONLY the hard escalation — hurry is never shown then', () => {
    // Q1: the hard stop stays reachable DURING the soft-stop wait as the
    // monotonic escalation of the SAME intent; HQ1: a pending cancel refuses
    // hurry, so the menu does not offer it.
    assert.deepEqual(taskControlActions({ cancelPending: true }), [ACTION_STOP_NOW]);
});

test('stop actions map to the wire stop_policy; hurry is not a stop', () => {
    assert.equal(stopPolicyFor(ACTION_FINALIZE), 'finalize_then_cancel');
    assert.equal(stopPolicyFor(ACTION_STOP_NOW), 'immediate');
    assert.equal(stopPolicyFor(ACTION_HURRY), '');
});

// --- stable request-id reuse (HQ1 idempotent retry) ---

test('hurryRequestId is stable per task and distinct across tasks', () => {
    const first = hurryRequestId('t-1');
    assert.equal(hurryRequestId('t-1'), first, 'a retry reuses the SAME id');
    assert.match(first, /^hurry-/);
    assert.notEqual(hurryRequestId('t-2'), first);
});

// --- no chat message, ever (HQ1) ---

test('owner_hurry is hidden from the chat timeline (visible=false)', () => {
    const view = summarizeChatLiveEvent({ type: 'owner_hurry', task_id: 't1', phase: 'applied' });
    assert.equal(view.visible, false);
    assert.equal(view.promote, false);
});

test('ownerHurryProjection is the shared card/detail projection', () => {
    const proj = ownerHurryProjection({ type: 'owner_hurry', task_id: 't1', phase: 'applied' });
    assert.equal(proj.applied, true);
    assert.equal(proj.taskId, 't1');
    assert.match(proj.label, /applied/);
});

test('the hurry path never creates a chat bubble in either surface', () => {
    // Pinned at source: the chat handler consumes owner_hurry BEFORE the
    // timeline summarizer and returns; the shared flow acknowledges via toast
    // only; neither surface routes hurry anywhere near addMessage.
    assert.match(chat, /eventType === 'owner_hurry'/);
    assert.match(chat, /ownerHurryProjection\(evt\)\.applied/);
    assert.match(menuSrc, /showToast\(/);
    assert.doesNotMatch(menuSrc, /addMessage|send_message|chat\.jsonl/);
});

// --- both surfaces share the ONE control module (owner parity) ---

test('Chat and Activity both wire the shared dropdown', () => {
    for (const source of [chat, activity]) {
        assert.match(source, /openTaskControlMenu\(/);
        assert.match(source, /hurryTaskAction\(/);
        assert.match(source, /requestStop\(/);
    }
    // The trigger renders the shared label on both surfaces.
    assert.match(chat, /TASK_CONTROL_TRIGGER_LABEL/);
    assert.match(activity, /TASK_CONTROL_TRIGGER_LABEL/);
    assert.equal(typeof TASK_CONTROL_TRIGGER_LABEL, 'string');
});

test('the dropdown replaced the old cancel confirm dialogs (dismiss = continue)', () => {
    // Q2: dismissing the menu continues the run — no separate confirm dialog
    // remains on either cancel path (Activity keeps its schedule-delete one).
    assert.doesNotMatch(chat, /Cancel this run and all its subagents\?/);
    assert.doesNotMatch(activity, /Cancel this task and all its subagents\?/);
    assert.match(menuSrc, /Escape/);
});

// --- pending soft stop presentation (Q1) ---

test('taskSoftStopPending distinguishes the soft episode from a hard cancel', () => {
    assert.equal(taskSoftStopPending({
        status: 'running', cancel_state: 'pending', stop_policy: 'finalize_then_cancel',
    }), true);
    assert.equal(taskSoftStopPending({ status: 'running', cancel_state: 'pending' }), false);
    assert.equal(taskSoftStopPending({
        status: 'cancelled', cancel_state: 'pending', stop_policy: 'finalize_then_cancel',
    }), false);
});

test('the chat card re-offers the escalation during a pending soft stop', () => {
    // Q1 pinned at source: after a soft 202 the trigger is re-enabled (the
    // pending menu offers only "Stop now"), while an immediate
    // stop keeps the button disabled until the terminal frame.
    assert.match(chat, /record\.cancelPendingPolicy === 'finalize'/);
    assert.match(chat, /cancelPending: Boolean\(record\.cancelPendingPolicy\)/);
});

// --- budget-paused resume offer (#322) ---

test('a budget-paused member offers Resume and the stop escalation only', () => {
    assert.deepEqual(
        taskControlActions({ budgetPaused: true }),
        [ACTION_RESUME, ACTION_STOP_NOW],
    );
    // A pending cancel outranks the pause: only the hard escalation remains.
    assert.deepEqual(
        taskControlActions({ budgetPaused: true, cancelPending: true }),
        [ACTION_STOP_NOW],
    );
    assert.equal(TASK_CONTROL_LABELS[ACTION_RESUME], 'Resume');
});

// --- Batch4 F14: the whole-tree Pause is a ROOT's action ---

test('a child row never offers the whole-tree Pause; its other actions stay', () => {
    assert.deepEqual(taskControlActions({ wholeTree: false }), [ACTION_FINALIZE, ACTION_HURRY, ACTION_STOP_NOW]);
    assert.deepEqual(taskControlActions({ wholeTree: false, budgetPaused: true }), [ACTION_RESUME, ACTION_STOP_NOW]);
    assert.deepEqual(taskControlActions({ wholeTree: false, cancelPending: true }), [ACTION_STOP_NOW]);
    assert.deepEqual(taskControlActions({ wholeTree: true }), [ACTION_FINALIZE, ACTION_HURRY, ACTION_PAUSE, ACTION_STOP_NOW]);
});

test('isRootTaskRow mirrors the server lineage rule (task_results.resolve_task_lineage)', () => {
    assert.equal(isRootTaskRow({ root_task_id: 'r1' }, 'r1'), true);
    assert.equal(isRootTaskRow({}, 'r1'), true, 'no lineage fields: its own root');
    assert.equal(isRootTaskRow({ root_task_id: 'r1', parent_task_id: 'r1', delegation_role: 'subagent' }, 'c1'), false);
    assert.equal(isRootTaskRow({ parent_task_id: 'r1' }, 'c1'), false, 'any parent makes it a child');
    assert.equal(isRootTaskRow({ root_task_id: 'c1', delegation_role: 'subagent' }, 'c1'), false);
    assert.equal(isRootTaskRow({ metadata: { parent_task_id: 'r1' } }, 'c1'), false, 'metadata carries lineage');
    assert.equal(isRootTaskRow({ parent_task_id: '', metadata: { parent_task_id: 'stale' } }, 'r1'), true,
        'an explicit empty parent overrides stale metadata, like the server');
    // A top-level hard-timeout retry: a root attempt only when both host markers agree.
    const retry = { root_task_id: 'orig', delegation_role: 'root', original_task_id: 'orig', timeout_retry_from: 'orig' };
    assert.equal(isRootTaskRow(retry, 'retry-1'), true);
    assert.equal(isRootTaskRow({ ...retry, timeout_retry_from: 'other' }, 'retry-1'), false);
    assert.equal(isRootTaskRow({ root_task_id: 'orig' }, 'retry-1'), false);
});

test('Activity marks root rows and hides Pause for a child; Chat cards stay root-only', () => {
    assert.match(activity, /isRootTaskRow\(t, q\.id \|\| t\.id\) \? ' data-root="1"' : ''/);
    assert.match(activity, /wholeTree: btn\.dataset\.root === '1'/);
    // Census rows are roots (the census lists root activities only).
    assert.match(activity, /data-id="\$\{esc\(a\.activity_id \|\| ''\)\}" data-root="1"/);
    // A chat card offers the menu only for a root (never a subagent card).
    assert.match(chat, /cancelRunEligibility\(\{\s*groupId: record\.groupId, isSubagent: record\.isSubagent/);
});
