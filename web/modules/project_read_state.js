// When a Project room has READ a visible revision (DESIGN "Project unread dot").
//
// A revision is read only when a history read covering it has crossed a real
// browser paint while the room stayed shown AND the reader is at the newest
// message — the one that arrived last, on screen (`isAtNewestMessage`), normally
// near the bottom of the conversation. Opening or refreshing a room while its
// reader is elsewhere is not reading; the chat instance reports each arrival at
// the newest message (a scroll, a page shown again, an older page applied) so the
// withheld acknowledgement is retried then, never by polling. This module owns
// the paint generation, the highest covered revision and that arrival edge; the
// chat instance owns reading history, and app.js alone posts the acknowledgement.

import { historyNodeOnScreen } from './chat_history_replay.js';

/**
 * @param {{
 *   read: (fresh: boolean) => Promise<boolean>,  // true when the recent source covered the read
 *   isShown: () => boolean,
 *   isReadingLatest: () => boolean,
 *   onReadingLatest?: () => void,
 * }} facts
 */
export function createProjectReadReceipt({ read, isShown, isReadingLatest, onReadingLatest = () => {} }) {
    let generation = 0;
    let coveredRevision = 0;
    let readingLatest = false;
    let settledLatest;  // the newest message the previous read named
    // Scroll edges report arrivals only; a discrete change (the page or the
    // window shown again, another newest message) reports the current position once.
    const note = ({ discrete = false } = {}) => {
        const latest = isReadingLatest();
        if (latest && (discrete || !readingLatest)) onReadingLatest();
        readingLatest = latest;
    };
    return {
        cancel() { generation += 1; },
        // Only a revision newer than any covered one needs a fresh read; the
        // receipt is still taken after this call's own paint either way.
        async refresh({ revision = 0 } = {}) {
            const own = ++generation;
            const target = Math.max(0, Number(revision) || 0);
            const covered = await read(target > coveredRevision);
            if (covered && target > coveredRevision) coveredRevision = target;
            if (!covered || own !== generation || !isShown()) return { painted: false, revision: target };
            // Two frames cover layout followed by paint/composite.
            await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const painted = own === generation && isShown();
            const atLatest = painted && isReadingLatest();
            // A withheld read lowers the edge, so the next arrival retries it.
            if (painted && !atLatest) readingLatest = false;
            return { painted, read: atLatest, revision: target };
        },
        note,
        // A read that names another newest message than the previous read moves
        // where the reader must be: the edge is taken again without a scroll.
        settle(latest) {
            const identity = JSON.stringify(latest) ?? '';
            if (settledLatest !== undefined && identity !== settledLatest) note({ discrete: true });
            settledLatest = identity;
        },
    };
}

// The feed the reader can see: its viewport less the chrome drawn over it, the
// header above and the composer below. Eviction keeps the whole viewport.
function readBand(viewport, header, composer) {
    let { top, bottom } = viewport.getBoundingClientRect();
    if (header?.getClientRects?.().length) top = Math.max(top, header.getBoundingClientRect().bottom);
    if (composer?.getClientRects?.().length) bottom = Math.min(bottom, composer.getBoundingClientRect().top);
    return { top, bottom };
}

// What shows a node: itself, or, inside a collapsed task card (no boxes of its
// own), the nearest enclosing card that is drawn. Collapsed cards need not be expanded.
function shownBy(node) {
    for (let shown = node; shown; shown = shown.parentElement?.closest?.('.chat-live-card')) {
        if (shown.getClientRects?.().length) return shown;
    }
    return node;
}

/**
 * Whether the reader is at the room's newest message. `latest` is the newest
 * recent read's `window.latest_message`, the standalone message that ARRIVED
 * last: absent names nothing (the room holds no standalone message), so the
 * bottom of the conversation decides; null (its arrival unknown) is never read.
 * A named message — in its ordinary place or not — must have reached the page
 * and must itself be on screen, clear of the header and composer drawn over the
 * feed. Being at the bottom never stands in for it: later card rows and the
 * owner's own messages can push it above the fold, and a late answer keeping its
 * original time (`out_of_order`) is not at the bottom at all. A named message
 * with no node on the page is not on screen.
 *
 * @param {undefined|null|{history_id: string, out_of_order?: boolean}} latest
 * @param {{ delivered: (id: string) => boolean, nodes: (id: string) => Array<Element>,
 *   viewport: Element, header?: Element|null, composer?: Element|null, atBottom: () => boolean }} facts
 */
export function isAtNewestMessage(latest, { delivered, nodes, viewport, header, composer, atBottom }) {
    if (latest === undefined) return atBottom();
    if (!latest?.history_id || !delivered(latest.history_id)) return false;
    const band = readBand(viewport, header, composer);
    return nodes(latest.history_id).some((node) => historyNodeOnScreen(shownBy(node), viewport, band));
}
