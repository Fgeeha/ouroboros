/** A compact Main transfer receipt; existing census/task details own its phase. */
import { projectReference } from './project_reference.js';
import { activeModelWaits } from './model_wait.js';
import { isTerminalTaskDetail, taskTerminalPhase, taskPresentation } from './log_events.js';

// The gateway's typed receipt word (ouroboros/project_handoff.py RECEIPT_STATES).
// The binding is committed under every word; only these two prove that the Main
// history row is owed or already delivered.
export const DURABLE_RECEIPTS = new Set(['durable', 'already_delivered']);

/** Owner wording for a non-durable receipt; '' when nothing needs saying. */
export function receiptNotice(status) {
    if (DURABLE_RECEIPTS.has(status)) return '';
    return {
        unregistered: 'Project binding saved; the Main history receipt was sent but is not protected against a restart.',
        unavailable: 'Project binding saved; the Main history receipt could not be sent.',
        origin_unproven: 'Project binding saved; this conversation has no recorded Main origin, so no history receipt was written.',
    }[status] || 'Project binding saved; the Main history receipt is unconfirmed.';
}

export function handoffPhase(activity, detail, connected = true) {
    if (isTerminalTaskDetail(detail)) {
        const view = taskPresentation(taskTerminalPhase(detail));
        return { text: view.headline, className: view.phase };
    }
    if (!connected || !activity) return { text: 'Activity unconfirmed', className: 'neutral' };
    if (activity.required_question || activeModelWaits(activity.model_waits || {}, false, activity.task_attempt || 0).length) {
        return { text: 'Waiting', className: 'warn' };
    }
    const phases = { thinking: 'Thinking', queued: 'Queued', budget_paused: 'Paused', finalizing: 'Finalizing…', working: 'Working' };
    const text = phases[activity.phase];
    return text ? { text, className: activity.phase === 'budget_paused' ? 'warn' : 'working' }
        : { text: 'Activity unconfirmed', className: 'neutral' };
}

// One anchor per handoff identity. Two node kinds can carry it: the converted
// live card (`card`, the request's own position) and the durable receipt row
// (`receipt`). The card outranks the receipt whatever order they arrive in, an
// earlier node outranks a later one of the same kind, and every outranked node
// stays hidden in `row.shadows` so evicting the anchor restores the next one
// instead of dropping the transfer from the feed.
export function createProjectHandoffs({ feed, fetchDetail, mutate }) {
    const rows = new Map();
    let connected = true, destroyed = false, complete = false;
    let activities = new Map();
    const inFeed = node => feed.contains(node);
    const current = row => !destroyed && inFeed(row.node) && rows.get(row.id) === row;
    const matching = (taskId, projectId) => [...rows.values()].find(row =>
        row.projectId === projectId && row.subjects.has(taskId) && inFeed(row.node));
    function paint(row) {
        const phase = handoffPhase(activities.get(row.taskId), row.detail, connected);
        row.status.textContent = phase.text;
        row.status.className = `chat-live-phase ${phase.className}`;
    }
    function reconcileStarted(node) {
        node.hidden = Boolean(matching(node.dataset.taskId, node.dataset.projectId));
    }
    function reconcileAnnotation(note) {
        const [projectId, , taskId] = (note.dataset.destinationKey || '').split('|');
        const represented = Boolean(matching(taskId, projectId)
            && ['scheduled', 'delivered'].includes(note.dataset.annotationStatus));
        note.hidden = represented;
        const actions = note.parentElement?.querySelector('.msg-routing-actions');
        if (actions) actions.hidden = represented;
    }
    // A promoted shadow keeps its own painted heading; its subjects merge back.
    function promote(row) {
        while (row.shadows.length) {
            const next = row.shadows.shift();
            if (!inFeed(next.node)) continue;
            next.node.hidden = false;
            rows.set(row.id, { ...next, subjects: new Set([...row.subjects, ...next.subjects]),
                shadows: row.shadows, detail: row.detail, epoch: row.epoch + 1, pending: false, checked: false });
            return true;
        }
        rows.delete(row.id);
        return true;
    }
    function sweep() {
        let changed = false;
        for (const row of [...rows.values()]) if (!inFeed(row.node)) changed = promote(row) || changed;
        return changed;
    }
    /** With a node: only that node's projection. Without: every dependent node. */
    function reconcile(node) {
        if (destroyed) return;
        if (node) {
            if (node.dataset?.systemType === 'project_started') reconcileStarted(node);
            const note = node.querySelector?.('.msg-routing-annotation');
            if (note) reconcileAnnotation(note);
            if (node.dataset?.systemType !== 'project_handoff') return;
        }
        sweep();
        for (const started of feed.querySelectorAll('[data-system-type="project_started"]')) reconcileStarted(started);
        for (const note of feed.querySelectorAll('.msg-routing-annotation')) reconcileAnnotation(note);
    }
    function resolve(row) {
        if (!current(row) || !complete || !connected || activities.has(row.taskId)
            || row.pending || row.checked || isTerminalTaskDetail(row.detail)) return;
        const taskId = row.taskId, epoch = row.epoch;
        row.pending = true;
        row.checked = true;
        Promise.resolve().then(() => fetchDetail(taskId)).then(detail => {
            if (!current(row) || epoch !== row.epoch || taskId !== row.taskId) return;
            // The retained task result, never project activity, names a retry.
            const effectiveRetry = detail?.task_id !== taskId
                && (detail?.original_task_id === taskId || detail?.retry_lineage?.some(item => item.task_id === taskId));
            const successor = String((effectiveRetry ? detail.task_id : '') || detail?.superseded_by || detail?.retry_task_id || '');
            if (successor && successor !== taskId) {
                if (row.subjects.has(successor)) return; // malformed cyclic lineage stays unknown
                row.subjects.add(successor);
                row.taskId = successor;
                row.checked = false;
                row.detail = null;
            } else if (isTerminalTaskDetail(detail)) row.detail = detail;
            mutate(() => paint(row));
        }).catch(() => {
            // A failed read remains unknown until a real re-entry/reconnect,
            // not another costly detail request on each census tick.
        }).finally(() => {
            row.pending = false;
            if (current(row) && row.taskId !== taskId) resolve(row);
        });
    }
    function mount(node, { taskId, projectId, projectName, title, handoffId, kind = 'receipt', receipt = '' }) {
        if (!taskId || !projectId || destroyed) return node;
        const id = handoffId || `legacy:${JSON.stringify([taskId, projectId])}`;
        node.dataset.projectId = projectId;
        node.dataset.handoffId = id;
        node.dataset.systemType = 'project_handoff';
        node.classList.add('project-handoff');
        const body = node.querySelector('.message') || node;
        const line = document.createElement('div');
        line.className = 'project-handoff-heading';
        const status = document.createElement('span');
        const name = document.createElement('span');
        name.className = 'project-handoff-title';
        name.textContent = title || projectName || 'Project';
        line.append(status, name);
        // A converted card whose receipt is not durable is an honest live chip,
        // never a claim that Main history holds this transfer (it will not
        // survive a reload as an anchor; the binding and the pointer do).
        if (kind === 'card' && receipt && !DURABLE_RECEIPTS.has(receipt)) {
            node.dataset.receipt = receipt;
            node.classList.add('project-handoff--unsaved');
        }
        body.replaceChildren(line, projectReference({ id: projectId, name: projectName }, { layout: 'inline', taskId }));
        const row = { id, node, status, kind, taskId, projectId, subjects: new Set([taskId]),
            shadows: [], detail: null, pending: false, checked: false, epoch: 0 };
        const prior = rows.get(id);
        if (prior && inFeed(prior.node) && prior.node !== node) {
            // Duplicate delivery is not evidence that a differently named
            // execution supersedes its subject: one anchor, the rest shadowed.
            if (kind === 'card' && prior.kind !== 'card') {
                node.hidden = false;
                prior.node.hidden = true;
                rows.set(id, { ...row, subjects: new Set([...prior.subjects, taskId]),
                    shadows: [prior, ...prior.shadows], detail: prior.detail });
                paint(rows.get(id));
                return node;
            }
            node.hidden = true;
            prior.subjects.add(taskId);
            prior.shadows.push(row);
            return prior.node;
        }
        rows.set(id, row);
        paint(row);
        // addMessage appends after mounting; the normal reconcile/snapshot path
        // resolves it once the feed owns the node.
        return node;
    }
    function snapshot(data) {
        if (destroyed) return;
        complete = data?.active_chat_activities_complete === true && data.supervisor_ready === true;
        const incoming = new Map((data?.active_chat_activities || []).map(a => [String(a.activity_id || ''), a]));
        for (const row of rows.values()) {
            if (incoming.has(row.taskId) && !activities.has(row.taskId)) {
                row.epoch++;
                row.checked = false;
            }
        }
        activities = incoming;
        // An evicted anchor is the only reason the feed-wide projections move on a
        // census tick; a steady feed repaints its rows and nothing else.
        mutate(() => { if (sweep()) reconcile(); for (const row of rows.values()) paint(row); });
        for (const row of rows.values()) resolve(row);
    }
    return { mount, reconcile, snapshot,
        setConnected(value) {
            if (connected !== value) {
                connected = value;
                for (const row of rows.values()) { row.epoch++; row.checked = false; }
            }
            mutate(() => { for (const row of rows.values()) paint(row); });
        },
        destroy() { destroyed = true; rows.clear(); activities.clear(); },
    };
}
