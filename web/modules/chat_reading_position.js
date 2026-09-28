/** The chat's one reading intent. Readiness comes from data/paint owners, not
 * a frame deadline. Frames only settle geometry after those owners are ready.
 * No fetch, pager, message cache or timer lives here.
 */
export function createChatReadingPosition({ initial, visible, alive, ready, feed, anchors, fallback, changed, afterWrite, activity, updateButton }) {
    let generation = 0, scheduled = false;
    let mutationDepth = 0;
    let viewportAnchor = null, width = feed.clientWidth;
    let intent = initial && initial.stick === false ? { ...initial } : null;
    let approximate = false;
    const state = {
        top: Math.max(0, Number(initial?.scrollTop) || 0),
        stick: initial ? initial.stick !== false : true,
        get generation() { return generation; },
        get pending() { return Boolean(intent); },
        get target() { return intent; },
        get approximate() { return approximate; },
        remember(force = false) {
            if (intent || !visible() || (!force && width !== feed.clientWidth)) return;
            width = feed.clientWidth; viewportAnchor = anchors.capture();
        },
        reflow() {
            if (intent) { state.position(); return; }
            if (!visible()) return;
            if (state.stick) feed.scrollTop = feed.scrollHeight;
            else anchors.restore(viewportAnchor);
            state.top = feed.scrollTop; state.remember(true); updateButton();
        },
        scroll() {
            if (!visible()) return;
            if (!intent) {
                state.top = feed.scrollTop;
                state.remember();
            }
            updateButton();
        },
        nearBottom: (threshold = 48) => feed.scrollHeight - feed.scrollTop - feed.clientHeight <= threshold,
        mutate(write, { forceFollow = false, remoteContent = false, excludeAnchorNode = null } = {}) {
            if (typeof write !== 'function') return undefined;
            if (!alive()) return false;
            if (mutationDepth) return write();
            if (intent || !visible()) {
                const result = write(); afterWrite();
                if (remoteContent && result && !state.stick) activity();
                return result;
            }
            const follow = forceFollow || (state.stick && state.nearBottom());
            const anchor = follow ? null : anchors.capture(excludeAnchorNode);
            const height = feed.scrollHeight, top = feed.scrollTop;
            mutationDepth++;
            let result;
            try { result = write(); afterWrite(); return result; }
            finally {
                mutationDepth--;
                if (visible()) {
                    if (follow) {
                        if (forceFollow || height !== feed.scrollHeight) feed.scrollTop = feed.scrollHeight;
                        else if (feed.scrollTop !== top) feed.scrollTop = top;
                    } else anchors.restore(anchor);
                    if (remoteContent && result && !follow) activity();
                    state.top = feed.scrollTop; state.stick = follow;
                    state.remember();
                    updateButton();
                }
            }
        },
        cancel() {
            generation++; scheduled = false; intent = null;
            const notify = approximate;
            approximate = false; state.remember(true);
            if (notify) changed();
        },
        export() { return intent ? { ...intent } : null; },
        bindGestures(navigate) {
            let touchY = null, pressedInside = false;
            const doc = feed.ownerDocument;
            // Keys scroll the focused control's scroller or, with nothing focused,
            // the one last pressed; neither sends keydown to the feed itself.
            const scrollsFeed = ({ target, key }) => feed.contains(target)
                ? !(key === ' ' && target.closest?.('button, a[href], summary, [role="button"]'))
                : pressedInside && (target === doc.body || target === doc.documentElement);
            const gesture = event => {
                if (event.type === 'pointerdown') {
                    pressedInside = feed.contains(event.target);
                    if (event.target === feed) { state.cancel(); state.stick = false; }
                    return; // a scrollbar drag owns position, not archive traversal
                }
                if (event.type === 'touchstart') { touchY = event.touches?.[0]?.clientY; return; }
                if (event.type === 'keydown' && (event.defaultPrevented
                        || event.target?.closest?.('input, textarea, select, [contenteditable="true"]')
                        || !['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)
                        || !scrollsFeed(event))) return;
                const y = event.touches?.[0]?.clientY;
                const direction = event.type === 'wheel' ? Math.sign(event.deltaY)
                    : event.type === 'keydown' ? (['ArrowUp', 'PageUp', 'Home'].includes(event.key) || (event.key === ' ' && event.shiftKey) ? -1 : 1)
                    : Math.sign((touchY ?? y) - y);
                if (event.type === 'touchmove') touchY = y;
                if (!direction) return;
                state.cancel(); state.stick = false;
                const token = generation;
                requestAnimationFrame(() => {
                    if (!alive() || !visible() || token !== generation) return;
                    state.stick = direction > 0 && feed.scrollHeight > feed.clientHeight && state.nearBottom();
                    state.top = feed.scrollTop;
                    navigate(direction);
                });
            };
            const types = ['wheel', 'touchstart', 'touchmove'];
            for (const type of types) feed.addEventListener(type, gesture, { passive: true });
            doc.addEventListener('keydown', gesture, { passive: true });
            doc.addEventListener('pointerdown', gesture, { passive: true, capture: true });
            return () => {
                for (const type of types) feed.removeEventListener(type, gesture);
                doc.removeEventListener('keydown', gesture);
                doc.removeEventListener('pointerdown', gesture, true);
            };
        },
        request() {
            if (!intent) intent = { scrollTop: state.top, stick: state.stick, historyAnchor: anchors.serialize() };
            state.position();
        },
        followAfterLayout() {
            const token = generation;
            let passes = 0;
            const apply = () => {
                if (!alive() || token !== generation) return;
                feed.scrollTop = feed.scrollHeight; state.top = feed.scrollTop;
                state.stick = true; state.remember(); updateButton();
                if (++passes < 2) requestAnimationFrame(apply);
            };
            requestAnimationFrame(apply);
        },
        position() {
            if (!intent || scheduled || !visible() || !ready()) return;
            scheduled = true;
            const token = generation, target = intent;
            let passes = 0;
            const apply = () => {
                if (token !== generation) return;
                if (!visible() || !ready()) { scheduled = false; return; }
                if (target.stick) feed.scrollTop = feed.scrollHeight;
                else {
                    const exact = anchors.restore(target.historyAnchor, { exact: true });
                    approximate = !exact && Boolean(target.historyAnchor);
                    if (!exact) {
                        if (!anchors.restore(target.historyAnchor, { cardOnly: true }) && !fallback(target)) {
                            feed.scrollTop = Math.max(0, Math.min(target.scrollTop || 0,
                                feed.scrollHeight - feed.clientHeight));
                        }
                    }
                }
                if (++passes < 2) { requestAnimationFrame(apply); return; }
                state.top = feed.scrollTop;
                state.stick = target.stick !== false;
                intent = null; scheduled = false;
                state.remember(true);
                changed();
            };
            requestAnimationFrame(apply);
        },
    };
    return state;
}
