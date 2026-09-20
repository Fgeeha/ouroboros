// A roster row is NAMED by a projection of its route. The Python owner is
// ouroboros/configured_subagents.py; this module pins the JS twin against the
// SAME table (tests/test_subagent_handles.py reads it too) and the one place
// the owner meets the name: the Review-lanes reviewer picker.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { rosterHandles, sameEngineAs, selectHtml, subagentHandle } from '../modules/route_editor_primitives.js';
import {
    SUBAGENT_CHOICE_PREFIX, encodeReviewerChoice, reviewerChoiceGroups,
} from '../modules/reviewer_slots.js';

const PARITY = JSON.parse(readFileSync(
    new URL('./fixtures/subagent_handle_parity.json', import.meta.url), 'utf8')).rosters;

for (const roster of PARITY) {
    test(`handle parity with Python: ${roster.case}`, () => {
        const labels = rosterHandles(roster.items);
        roster.expected.forEach((want, index) => {
            const row = roster.items[index];
            assert.equal(subagentHandle(row), want.handle);
            assert.equal(labels.get(row.subagent_id), want.roster);
            assert.equal(sameEngineAs(roster.items, index), want.same_engine_as ?? -1);
        });
    });
}

// The exact path reviewerPickerHtml takes: groups -> the shared select markup.
function pickerOptions(roster, row) {
    const html = selectHtml('data-slot-route', reviewerChoiceGroups({ roster, row }), encodeReviewerChoice(row));
    const group = html.match(/<optgroup label="Available subagents">([\s\S]*?)<\/optgroup>/)?.[1] || '';
    return [...group.matchAll(/<option value="([^"]*)"( selected)?>([^<]*)<\/option>/g)]
        .map((match) => ({ value: match[1], selected: Boolean(match[2]), label: match[3] }));
}

function rosterOf(count) {
    const efforts = ['', 'low', 'medium', 'high', 'xhigh'];
    return Array.from({ length: count }, (_, index) => (index % 2
        ? { subagent_id: `fast-scout_copy_${index}`, recommended_use: `Session notes ${index}`,
            route: { kind: 'agent_session', target_id: `codex=gpt-6-astra-${index}` },
            ...(efforts[index % 5] ? { effort: efforts[index % 5] } : {}) }
        : { subagent_id: index ? `fast-scout_copy_${index}` : 'fast-scout', recommended_use: '',
            route: { kind: 'api_model', target_id: `x-ai/grok-4.6-${index}` },
            ...(efforts[index % 5] ? { effort: efforts[index % 5] } : {}) }));
}

test('the reviewer picker names every row by its handle, identically with 1 row and with 10', () => {
    const ten = rosterOf(10);
    const row = { subagent_id: 'fast-scout', route: { kind: 'api_chat', target_id: '' } };
    const alone = pickerOptions(ten.slice(0, 1), row);
    const crowded = pickerOptions(ten, row);

    assert.deepEqual(alone, [{ value: `${SUBAGENT_CHOICE_PREFIX}fast-scout`, selected: true, label: 'x-ai/grok-4.6-0' }]);
    assert.equal(crowded.length, 10);
    // Scale invariant: a row's label is byte-identical however many siblings it has.
    assert.deepEqual(crowded[0], alone[0]);
    crowded.forEach((option, index) => {
        assert.equal(option.value, `${SUBAGENT_CHOICE_PREFIX}${ten[index].subagent_id}`, 'the stored id is the VALUE only');
        assert.ok(option.label.startsWith(subagentHandle(ten[index])), option.label);
        assert.doesNotMatch(option.label, /#|fast-scout|_copy_/, 'no stored label reaches the owner');
        assert.ok(option.label.length <= 80, `one compact line per row: ${option.label}`);
    });
    assert.equal(crowded[3].label, 'codex=gpt-6-astra-3/high — Session notes 3');
});

test('twins saved before the uniqueness rule stay distinguishable in the picker', () => {
    const twins = [
        { subagent_id: 'fast-scout', recommended_use: '', route: { kind: 'api_model', target_id: 'x-ai/grok-4.6' } },
        { subagent_id: 'fast-scout_copy_a1', recommended_use: '', route: { kind: 'api_model', target_id: 'x-ai/grok-4.6' } },
    ];
    const labels = pickerOptions(twins, { subagent_id: 'fast-scout' }).map((option) => option.label);
    assert.deepEqual(labels, ['x-ai/grok-4.6~fast-scout', 'x-ai/grok-4.6~fast-scout_copy_a1']);
});
