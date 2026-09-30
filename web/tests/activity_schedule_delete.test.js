import assert from 'node:assert/strict';
import test from 'node:test';

// The DOM-free Activity helpers the rendered list and the lifecycle toast use.
globalThis.document = globalThis.document || { createElement: () => ({}) };
const { isRetainedSchedule, scheduleOutcomeToast, scheduleRowHtml, scheduleStatus } = await import('../modules/activity.js');

const consumedPending = {
    id: 'fu-1', name: 'Check the results', status: 'delete_pending', delete_pending: true,
    relation: 'related', enabled: false, trigger: { type: 'once', run_at: '2027-01-15T09:00:00Z' },
    completed_at: '2027-01-15T09:00:05Z', delete_requested_at: '2027-01-15T10:00:00Z',
};

test('an accepted deferred delete reads as pending, never as a refusal', () => {
    const outcome = { ok: true, changed: true, status: 'delete_deferred', audit: 'recorded',
        detail: 'an accepted run is still owed; the row will be removed once that run starts' };
    assert.deepEqual(scheduleOutcomeToast('delete', outcome),
        [`Schedule deletion is pending: ${outcome.detail}`, 'info']);
    // A consumed one-shot still refuses rearming, even when deletion is pending.
    const restore = { ok: false, changed: false, status: 'consumed_not_rearmed', audit: 'recorded',
        detail: 'already fired' };
    assert.deepEqual(scheduleOutcomeToast('restore', restore),
        ['Schedule restore did not change anything: already fired', 'error']);
    // Bookkeeping that changed while refusing a release still reads as refused.
    assert.equal(scheduleOutcomeToast('restore', { changed: true, status: 'stale_hold', audit: 'recorded' })[1], 'error');
    assert.equal(scheduleOutcomeToast('delete', { ok: true, changed: true, status: 'deleted', audit: 'recorded',
        running_or_queued: false }), null);
});

test('a deleted row still owing work stays in the standing list after reload', () => {
    assert.equal(scheduleStatus(consumedPending), 'delete_pending');
    // Older payload without the server word: the durable request still wins over "consumed".
    const legacy = { ...consumedPending };
    delete legacy.status;
    assert.equal(scheduleStatus(legacy), 'delete_pending');
    assert.equal(isRetainedSchedule(consumedPending), false, 'not collapsed into History');
    assert.equal(isRetainedSchedule({ ...consumedPending, status: 'consumed', delete_requested_at: '' }), true);
    const html = scheduleRowHtml(consumedPending);
    assert.match(html, /deletion waits for its task to finish/);
    assert.match(html, / · deletion pending · deletion waits for its task to finish · related/);
    assert.doesNotMatch(html.split('activity-row-actions')[1], /deletion waits/, 'wrapping detail stays with the description');
    assert.doesNotMatch(html, /data-act="schedule-toggle"/, 'a consumed one-shot cannot be rearmed');
    assert.match(html, /data-act="schedule-delete" data-id="fu-1"/);
    const independent = scheduleRowHtml({ ...consumedPending, relation: 'independent', trigger: { type: 'cron', expr: '0 9 * * *' },
        completed_at: '', next_run_at: '2027-01-16T09:00:00Z' });
    assert.match(independent, /deletion waits for its accepted run to start/);
    assert.match(independent, /data-action="restore">Cancel deletion</);
    assert.doesNotMatch(independent, />Enable</, 'cancelling Delete must be explicit');
    assert.doesNotMatch(independent, / · next /, 'a deleted row announces no next run');
});

test('the exact hold release stays reachable on a pending-delete continuation', () => {
    const held = { ...consumedPending, followup_hold: { hold_id: 'h-1', reason: 'origin_stopped' }, hold_persisted: true };
    const html = scheduleRowHtml(held);
    assert.match(html, /data-action="restore" data-hold-id="h-1">Restore hold</);
    assert.match(html, /deletion waits for its task to finish/);
    assert.doesNotMatch(html, />Enable</);
    const unknown = scheduleRowHtml({ ...held, relation: 'unknown' });
    assert.match(unknown, /resolve relationship in conversation/);
    assert.doesNotMatch(unknown, /data-act="schedule-toggle"/);
});
