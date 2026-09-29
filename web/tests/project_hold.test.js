import test from 'node:test';
import assert from 'node:assert/strict';
import { computeHydratedDirectActivities, chatStatusCounts, computeDerivedChatStatus } from '../modules/chat_activity.js';
import { summarizeProjectActivities } from '../modules/project_activity.js';
import { handoffPhase } from '../modules/project_handoff.js';

const hold = { label: 'Waiting for Project verification', reason: 'project_routing_fence_lookup_failed', detail: 'Authority is unreadable.' };
const held = { activity_id: 'same-id', chat_id: 7, kind: 'managed_task', phase: 'queued', project_admission_hold: hold };

test('known non-Project wait keeps the scope label in Chat and Main handoff', () => {
    const scopeHold = { ...hold, label: 'Waiting for task scope verification' };
    const row = { ...held, chat_id: 1, project_admission_hold: scopeHold };
    const activities = computeHydratedDirectActivities(new Map(), [row], 1);
    assert.equal(computeDerivedChatStatus(chatStatusCounts(activities, [])).text, scopeHold.label);
    assert.equal(handoffPhase(row, null).text, scopeHold.label);
});

test('held task is stationary across hydrated Chat, Project and Main receipt', () => {
    const activities = computeHydratedDirectActivities(new Map(), [held], 7);
    const card = { root: { isConnected: true }, groupId: 'same-id', finished: false };
    const status = computeDerivedChatStatus(chatStatusCounts(activities, [card]));
    assert.deepEqual(status, { kind: 'online', text: hold.label, showDots: false });
    assert.equal(summarizeProjectActivities([{ ...held, required_question_unavailable: true }]).label, hold.label);
    assert.equal(summarizeProjectActivities([held]).motion, false);
    assert.deepEqual(handoffPhase(held, null), { text: hold.label, className: 'warn' });
    assert.equal(handoffPhase(held, { status: 'failed' }).text, 'Failed');
    assert.equal(handoffPhase(held, null, false).text, 'Activity unconfirmed');
});

test('same-ID recovery clears the hold; independent work and budget remain truthful', () => {
    const activities = computeHydratedDirectActivities(new Map(), [held], 7);
    const recovered = computeHydratedDirectActivities(activities, [{ ...held, phase: 'working', project_admission_hold: undefined }], 7);
    assert.equal(recovered.get('same-id').project_admission_hold, undefined);
    assert.equal(computeDerivedChatStatus(chatStatusCounts(recovered, [])).text, 'Working...');
    const sibling = { ...held, activity_id: 'sibling', phase: 'working', project_admission_hold: undefined };
    assert.equal(summarizeProjectActivities([held, sibling]).motion, true);
    assert.match(summarizeProjectActivities([held, sibling]).label, /Waiting for Project/);
    const paused = computeHydratedDirectActivities(new Map(), [{ ...held, phase: 'budget_paused' }], 7);
    assert.equal(computeDerivedChatStatus(chatStatusCounts(paused, [])).text, 'Paused (budget)');
});

test('Main handoff keeps a budget pause beside the Project wait, as the sidebar does', () => {
    const paused = { ...held, phase: 'budget_paused' };
    assert.deepEqual(handoffPhase(paused, null), { text: `Paused · ${hold.label}`, className: 'warn' });
    assert.equal(handoffPhase(paused, null).text, summarizeProjectActivities([paused]).label);
    assert.deepEqual(handoffPhase({ ...held, phase: 'budget_pausing' }, null), { text: `Pausing… · ${hold.label}`, className: 'warn' });
    assert.deepEqual(handoffPhase(held, null), { text: hold.label, className: 'warn' });
    assert.equal(handoffPhase(paused, { status: 'cancelled' }).text, 'Cancelled');
});
