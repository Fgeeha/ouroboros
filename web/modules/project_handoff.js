/** A compact Main transfer receipt; existing census/task details own its phase. */
import { projectReference } from './project_reference.js';
import { activeModelWaits } from './model_wait.js';
import { isTerminalTaskDetail, taskTerminalPhase, taskPresentation } from './log_events.js';

export function handoffPhase(activity, detail, connected = true) {
    if (isTerminalTaskDetail(detail)) {
        const view = taskPresentation(taskTerminalPhase(detail));
        return { text: view.headline, className: view.phase };
    }
    if (!connected || !activity) return { text: 'Activity unconfirmed', className: 'neutral' };
    if (activity.required_question || activeModelWaits(activity.model_waits || {}, false, activity.task_attempt || 0).length) {
        return { text: 'Waiting', className: 'warn' };
    }
    const phases = { queued: 'Queued', budget_paused: 'Paused', finalizing: 'Finalizing…', working: 'Working' };
    const text = phases[activity.phase];
    return text ? { text, className: activity.phase === 'budget_paused' ? 'warn' : 'working' }
        : { text: 'Activity unconfirmed', className: 'neutral' };
}

export function createProjectHandoffs({ feed, fetchDetail, mutate }) {
    const rows = new Map();
    let connected = true, destroyed = false, complete = false;
    let activities = new Map();
    const current = row => !destroyed && feed.contains(row.node) && rows.get(row.id) === row;
    const matching = (taskId, projectId) => [...rows.values()].find(row =>
        row.projectId === projectId && row.subjects.has(taskId));
    function paint(row) {
        const phase = handoffPhase(activities.get(row.taskId), row.detail, connected);
        row.status.textContent = phase.text;
        row.status.className = `chat-live-phase ${phase.className}`;
    }
    function reconcile() {
        for (const [id, row] of rows) if (!feed.contains(row.node)) rows.delete(id);
        for (const node of feed.querySelectorAll('[data-system-type="project_started"]')) {
            node.hidden = Boolean(matching(node.dataset.taskId, node.dataset.projectId));
        }
        for (const note of feed.querySelectorAll('.msg-routing-annotation')) {
            const [projectId, , taskId] = (note.dataset.destinationKey || '').split('|');
            const represented = Boolean(matching(taskId, projectId)
                && ['scheduled', 'delivered'].includes(note.dataset.annotationStatus));
            note.hidden = represented;
            const actions = note.parentElement?.querySelector('.msg-routing-actions');
            if (actions) actions.hidden = represented;
        }
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
            const successor = String(detail?.superseded_by || detail?.retry_task_id || '');
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
    function mount(node, { taskId, projectId, projectName, title, handoffId }) {
        if (!taskId || !projectId || destroyed) return node;
        const id = handoffId || `legacy:${JSON.stringify([taskId, projectId])}`;
        const prior = rows.get(id);
        if (prior && feed.contains(prior.node)) {
            // Preserve the first chronological anchor; duplicate delivery is not
            // evidence that a differently named execution supersedes its subject.
            if (node !== prior.node) node.hidden = true;
            prior.subjects.add(taskId);
            return prior.node;
        }
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
        body.replaceChildren(line, projectReference({ id: projectId, name: projectName }, { layout: 'footer', taskId }));
        const row = { id, node, status, taskId, projectId, subjects: new Set([taskId]),
            detail: null, pending: false, checked: false, epoch: 0 };
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
        mutate(() => { reconcile(); for (const row of rows.values()) paint(row); });
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
