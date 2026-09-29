export const DEFAULT_WELCOME_TEXT = 'Ouroboros has awakened';

export function welcomeText(value) {
    if (value?.mode === 'hidden') return null;
    if (value?.mode === 'custom') return typeof value.text === 'string' && value.text.trim()
        ? value.text : null;
    return value?.mode === 'default' ? DEFAULT_WELCOME_TEXT : null;
}

// Main's empty state. Its copy is the hidden `welcome` UI preference (no Settings
// control: docs/DESIGN.md "Chat authorship and System rows"), read when Main connects.
// Only a successful recent read whose own window reports complete coverage can
// confirm emptiness; a failed or partial read retracts it, and any insertion into
// the feed or later read decides again.
export function mountEmptyChatWelcome(messages) {
    const doc = messages.ownerDocument;
    let preference = null;
    let confirmedEmpty = false;
    let node = null;
    // Content is a top-level bubble or visible task card; the typing indicator and
    // the ephemeral reconnect notice are chrome, like this empty state itself.
    const hasContent = () => Array.from(messages.children).some((child) => child !== node
        && (child.classList.contains('chat-live-card') || (child.classList.contains('chat-bubble')
            && !child.classList.contains('typing-bubble') && !child.dataset.ephemeral)));
    const render = () => {
        const copy = welcomeText(preference);
        if (!confirmedEmpty || !copy || hasContent()) {
            node?.remove();
            node = null;
            return;
        }
        if (!node) {
            node = doc.createElement('div');
            node.className = 'chat-empty-welcome';
            node.dataset.welcomeState = 'ready';
            node.innerHTML = '<span class="chat-empty-welcome-label">Welcome</span>';
            node.appendChild(doc.createElement('p'));
            messages.insertBefore(node, messages.querySelector('.typing-bubble'));
        }
        node.lastElementChild.textContent = copy;  // owner copy is text, never markup
    };
    const observer = typeof MutationObserver === 'function' ? new MutationObserver(render) : null;
    observer?.observe(messages, { childList: true });
    return {
        // Called only with a successful read: before one nothing shows, and a failed
        // re-read keeps the choice already observed.
        setPreference(value) { preference = value || null; render(); },
        historyRead(complete) { confirmedEmpty = complete === true; render(); },
        dispose() { observer?.disconnect(); },
    };
}
