import assert from 'node:assert/strict';
import test from 'node:test';

import {
    planReviewGroupFromTaskDetail,
    renderReviewsSection,
} from '../modules/review_presentation.js';

const FINGERPRINT = '1ef46f0328c4a52973d7cd9aa23e7ab5301a257eab72379a4637ac67e0855852';

// Actor rows as the host stores them: a planned wait carries `pending_dispatch`
// and a prose window note, an answered slot carries `ok`, a real failure a code.
const AWAITING = {
    slot_id: 'triad_286lhb', model: 'codex=gpt-6-astra', ok: false,
    operation_state: 'pending_dispatch', late_result_pending: true, failure_code: '',
    error: 'Pending dispatch; the physical review operation is in flight (window 21600s)',
};
const ANSWERED = {
    slot_id: 'triad_w45a8z', model: 'Cursor Grok 4.6 Extra High Fast', ok: true,
    operation_state: 'late_settled', late_result_pending: false, failure_code: '', error: null,
};
const FAILED = {
    slot_id: 'triad_bkydwq', model: 'codex=gpt-6-astra', ok: false,
    operation_state: 'settled', late_result_pending: false, failure_code: 'run_failed',
    error: 'delegated review session run-e2336ca586e0 ended failed',
};

// Custody is activity only while the owning task runs: every live case states that fact.
function planGroup(wave, status = 'running') {
    return planReviewGroupFromTaskDetail({
        task_id: 'root',
        status,
        plan_review_state: {
            schema_version: 2,
            current_attempt: { fingerprint: FINGERPRINT, status: 'open', reason: '' },
            waves: [{
                request_fingerprint: FINGERPRINT,
                cycle_index: 1,
                aggregate: 'DEGRADED',
                closed: false,
                paid: true,
                reviewed_at: '2026-09-20T10:47:07.300000+00:00',
                counts: { configured: 3, parseable: 1, quorum: 2, blocking: 0, note: 3, need_evidence: 0 },
                ...wave,
            }],
            waves_omitted: 0,
        },
    }, 'root');
}

const expandedHtml = (group) => renderReviewsSection([group], {
    sectionExpanded: true,
    expandedGroups: new Set([group.id]),
    expandedAttempts: new Set([`${group.id}:${group.attempts[0].id}`]),
});

const availabilityLines = (attempt, prefix) => attempt.detailText
    .split('\n')
    .filter((line) => line.startsWith(prefix));

test('a plan wave whose reviewers may still answer reads as work in progress', () => {
    const group = planGroup({
        custody_pending: true,
        actors: [AWAITING, ANSWERED, { ...AWAITING, slot_id: 'triad_bkydwq' }],
    });
    const attempt = group.attempts[0];
    assert.equal(attempt.tone, 'working');
    assert.equal(group.tone, 'working');
    // The stored wave is untouched; only the sentence about it changes.
    assert.equal(attempt.verdict, 'DEGRADED');
    assert.equal(group.verdict, 'DEGRADED');
    assert.match(attempt.detailText, /^Verdict: none \(wave held open\)$/m);
    assert.deepEqual(availabilityLines(attempt, 'Awaiting answer:'), [
        'Awaiting answer: triad_286lhb · codex=gpt-6-astra',
        'Awaiting answer: triad_bkydwq · codex=gpt-6-astra',
    ]);
    assert.deepEqual(availabilityLines(attempt, 'Reviewer unavailable:'), []);
    assert.doesNotMatch(attempt.detailText, /Pending dispatch/);

    const html = expandedHtml(group);
    assert.match(html, /chat-review-group working/);
    assert.match(html, /chat-review-attempt working/);
    assert.equal(group.progress, 'in progress · 1 of 3 answered');
    assert.match(html, /chat-review-group-meta">in progress · 1 of 3 answered/);
    assert.match(html, /chat-review-attempt-meta">[^<]*· in progress · 1 of 3 answered/);
    assert.doesNotMatch(html, /DEGRADED|\d unavailable/);
});

test('a settled wave without quorum keeps its warning, its verdict and its unavailable reviewers', () => {
    const group = planGroup({
        custody_pending: false,
        actors: [{ ...FAILED, slot_id: 'triad_286lhb' }, FAILED],
    });
    const attempt = group.attempts[0];
    assert.equal(attempt.tone, 'warn');
    assert.equal(group.tone, 'warn');
    assert.equal(attempt.progress, '');
    assert.match(attempt.detailText, /^Verdict: DEGRADED$/m);
    assert.deepEqual(availabilityLines(attempt, 'Reviewer unavailable:'), [
        'Reviewer unavailable: triad_286lhb · codex=gpt-6-astra — run_failed',
        'Reviewer unavailable: triad_bkydwq · codex=gpt-6-astra — run_failed',
    ]);
    assert.deepEqual(availabilityLines(attempt, 'Awaiting answer:'), []);

    const html = expandedHtml(group);
    assert.match(html, /chat-review-group warn/);
    assert.match(html, /chat-review-group-meta">DEGRADED/);
    assert.doesNotMatch(html, /in progress/);

    // A slot that settled after the wave closed is a terminal answer too: only
    // the typed wait states change the wording.
    const late = planGroup({
        custody_pending: false,
        actors: [{ ...FAILED, operation_state: 'late_settled', late_result_pending: true }],
    });
    assert.deepEqual(availabilityLines(late.attempts[0], 'Reviewer unavailable:'), [
        'Reviewer unavailable: triad_bkydwq · codex=gpt-6-astra — run_failed',
    ]);
});

test('an in-flight wave names its failed slot and its awaited slot separately', () => {
    const group = planGroup({
        custody_pending: true,
        actors: [ANSWERED, FAILED, { ...AWAITING, slot_id: 'triad_qq41xk' }],
    });
    const attempt = group.attempts[0];
    assert.deepEqual(availabilityLines(attempt, 'Awaiting answer:'), [
        'Awaiting answer: triad_qq41xk · codex=gpt-6-astra',
    ]);
    assert.deepEqual(availabilityLines(attempt, 'Reviewer unavailable:'), [
        'Reviewer unavailable: triad_bkydwq · codex=gpt-6-astra — run_failed',
    ]);
    assert.match(attempt.detailText, /^Verdict: none \(wave held open\)$/m);
    // A slot that is neither answered nor awaited keeps the wave's warning.
    assert.equal(attempt.progress, 'in progress · 1 of 3 answered · 1 unavailable');
    assert.equal(group.progress, attempt.progress);
    assert.deepEqual([attempt.tone, group.tone], ['warn', 'warn']);
    const html = expandedHtml(group);
    assert.match(html, /chat-review-group warn/);
    assert.match(html, /chat-review-group-meta">in progress · 1 of 3 answered · 1 unavailable/);
});

test('a reviewer whose window expired stays unresolved instead of awaited', () => {
    const lost = { ...AWAITING, slot_id: 'triad_qq41xk', operation_state: 'custody_lost' };
    const group = planGroup({ custody_pending: true, actors: [ANSWERED, lost] });
    const attempt = group.attempts[0];
    assert.deepEqual(availabilityLines(attempt, 'No answer:'), [
        'No answer: triad_qq41xk · codex=gpt-6-astra — custody_lost',
    ]);
    assert.deepEqual(availabilityLines(attempt, 'Awaiting answer:'), []);
    assert.equal(group.progress, 'unresolved · 1 of 2 answered · 1 unavailable');
    assert.deepEqual([attempt.tone, group.tone], ['warn', 'warn']);
    assert.match(expandedHtml(group), /chat-review-group-meta">unresolved · 1 of 2 answered · 1 unavailable/);

    // One slot that is still merely awaited returns the wave to progress; the lost slot stays counted.
    const mixed = planGroup({ custody_pending: true, actors: [ANSWERED, lost, AWAITING] });
    assert.equal(mixed.progress, 'in progress · 1 of 3 answered · 1 unavailable');
    assert.equal(mixed.tone, 'warn');
    assert.match(expandedHtml(mixed), /chat-review-group-meta">in progress · 1 of 3 answered · 1 unavailable/);
});

test('a wave recorded without the typed custody fields renders exactly as before', () => {
    const group = planGroup({
        actors: [{ slot_id: 'slot_3', model: 'openai/gpt-5.6-sol', ok: false, failure_code: 'window_exhausted' }],
    });
    const attempt = group.attempts[0];
    assert.equal(attempt.tone, 'warn');
    assert.equal(attempt.progress, '');
    assert.equal(attempt.detailText, [
        'Verdict: DEGRADED',
        'Closed: no',
        'Reviewer panel dispatched: yes',
        'Findings: 0 blocking · 3 note · 0 need_evidence',
        'Reviewer unavailable: slot_3 · openai/gpt-5.6-sol — window_exhausted',
        'Cost unavailable',
    ].join('\n'));
    assert.match(expandedHtml(group), /chat-review-group-meta">DEGRADED · 1</);
});

test('a plan wave of a task that is not running is a recorded gap, never live work', () => {
    const wave = { custody_pending: true, actors: [AWAITING, ANSWERED, { ...AWAITING, slot_id: 'triad_bkydwq' }] };
    for (const status of ['completed', 'failed', 'cancelled', 'interrupted', '', null]) { // null: a frame without a status
        const group = planGroup(wave, status);
        const attempt = group.attempts[0];
        assert.equal(attempt.state, 'terminal', String(status));
        assert.equal(group.state === 'running' || group.activeCount > 0, false, String(status));
        assert.equal(attempt.progress, 'no verdict · 1 of 3 answered');
        assert.equal(group.progress, 'no verdict · 1 of 3 answered');
        assert.equal(attempt.tone, 'neutral');
        assert.equal(group.tone, 'neutral');
        assert.equal(attempt.verdict, 'DEGRADED'); // the stored wave is untouched
        assert.doesNotMatch(expandedHtml(group), /in progress/);
    }
    // The same wave under a running task IS live work (the guard fires in both directions).
    const live = planGroup(wave, 'running');
    assert.equal(live.attempts[0].state, 'running');
    assert.equal(live.progress, 'in progress · 1 of 3 answered');
    assert.equal(live.tone, 'working');
    // A real failure beside the wait stays loud after the task ended.
    const mixed = planGroup({ custody_pending: true, actors: [AWAITING, ANSWERED, FAILED] }, 'completed');
    assert.equal(mixed.progress, 'no verdict · 1 of 3 answered · 1 unavailable');
    assert.equal(mixed.tone, 'warn');
});
