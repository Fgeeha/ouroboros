import test from 'node:test';
import assert from 'node:assert/strict';
import { bindWelcomePreference, DEFAULT_WELCOME_TEXT, mountEmptyChatWelcome, welcomeText } from '../modules/welcome_preference.js';
import { installDom, restoreDom } from './chat_dom_fixture.js';

test('welcome mode resolves exactly without treating custom text as markup', () => {
    assert.equal(welcomeText({ mode: 'default', text: 'ignored' }), DEFAULT_WELCOME_TEXT);
    assert.equal(welcomeText({ mode: 'hidden', text: 'saved' }), null);
    assert.equal(welcomeText({ mode: 'custom', text: '<b>Привет</b>\nhello' }), '<b>Привет</b>\nhello');
    assert.equal(welcomeText({ mode: 'custom', text: '   ' }), null);
    assert.equal(welcomeText(null), null);
});

function field() {
    const listeners = new Map();
    return {
        value: '', disabled: false, dataset: {}, textContent: '',
        addEventListener(type, callback) { listeners.set(type, callback); },
        removeEventListener(type) { listeners.delete(type); },
        fire(type) { listeners.get(type)?.(); },
        get listeners() { return listeners; },
    };
}

function fixture(client) {
    const mode = field(), text = field(), save = field(), status = field();
    const nodes = { '[data-welcome-mode]': mode, '[data-welcome-text]': text,
        '[data-welcome-save]': save, '[data-welcome-status]': status };
    const root = { querySelector: () => ({ querySelector: (key) => nodes[key] }) };
    const events = [];
    const previous = globalThis.window, previousEvent = globalThis.CustomEvent;
    globalThis.window = { dispatchEvent: (event) => events.push(event) };
    globalThis.CustomEvent = class { constructor(type, options) { this.type = type; this.detail = options.detail; } };
    const dispose = bindWelcomePreference(root, client);
    return { mode, text, save, status, events, dispose: () => {
        dispose(); globalThis.window = previous; globalThis.CustomEvent = previousEvent;
    } };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test('settings reads and saves separately, and does not save blank custom copy', async () => {
    const calls = [];
    const f = fixture({
        uiPreferences: async () => ({ welcome: { mode: 'default', text: '' } }),
        saveUiPreferences: async (value) => { calls.push(value); return { ok: true, welcome: value.welcome }; },
    });
    try {
        await tick();
        assert.equal(f.mode.value, 'default');
        assert.equal(f.text.disabled, true);
        f.mode.value = 'custom'; f.mode.fire('change');
        f.text.value = '  '; f.text.fire('input'); f.save.fire('click');
        assert.equal(calls.length, 0);
        f.text.value = 'Привет <script>alert(1)</script>'; f.text.fire('input'); f.save.fire('click');
        await tick();
        assert.deepEqual(calls, [{ welcome: { mode: 'custom', text: 'Привет <script>alert(1)</script>' } }]);
        assert.equal(f.events[0].type, 'ouro:welcome-changed');
        assert.equal(f.events[0].detail.text, calls[0].welcome.text);
        f.mode.value = 'hidden'; f.mode.fire('change'); f.save.fire('click'); await tick();
        assert.equal(calls[1].welcome.mode, 'hidden');
        f.mode.value = 'default'; f.mode.fire('change'); f.save.fire('click'); await tick();
        assert.equal(calls[2].welcome.mode, 'default');
    } finally { f.dispose(); }
});

test('a failed preference read cannot authorize overwriting it and disposes handlers', async () => {
    const f = fixture({ uiPreferences: async () => { throw new Error('offline'); },
        saveUiPreferences: () => { throw new Error('should not save'); } });
    try {
        await tick();
        assert.equal(f.save.disabled, true);
        assert.match(f.status.textContent, /offline/);
    } finally { f.dispose(); }
    assert.equal(f.save.listeners.size, 0);
});

test('the Main empty state needs a complete successful read and an empty feed', () => {
    const { prior, mount } = installDom();
    const previousObserver = globalThis.MutationObserver;
    let mutated = null;
    globalThis.MutationObserver = class {
        constructor(callback) { mutated = callback; }
        observe() {}
        disconnect() { mutated = null; }
    };
    const listeners = new Map();
    const win = { addEventListener: (type, fn) => listeners.set(type, fn), removeEventListener: (type) => listeners.delete(type) };
    const doc = globalThis.document;
    const node = (className, dataset = {}) => {
        const element = doc.createElement('div');
        element.className = className;
        Object.assign(element.dataset, dataset);
        return element;
    };
    try {
        const messages = node('');
        mount.appendChild(messages);
        const typing = node('chat-bubble assistant typing-bubble');
        messages.appendChild(typing);
        const welcome = mountEmptyChatWelcome(messages, win);
        const shown = () => messages.children.find((child) => child.classList.contains('chat-empty-welcome'));
        welcome.setPreference({ mode: 'default', text: '' });
        assert.equal(shown(), undefined, 'no history read has confirmed emptiness');
        welcome.historyRead(false);
        assert.equal(shown(), undefined, 'a partial or failed read confirms nothing');
        welcome.historyRead(true);
        assert.equal(shown().dataset.welcomeState, 'ready');
        assert.equal(shown().lastElementChild.textContent, DEFAULT_WELCOME_TEXT);
        assert.ok(messages.children.indexOf(shown()) < messages.children.indexOf(typing));
        listeners.get('ouro:welcome-changed')({ detail: { mode: 'custom', text: '<b>x</b>\nnext' } });
        assert.equal(shown().lastElementChild.innerHTML, '&lt;b&gt;x&lt;/b&gt;\nnext', 'owner copy stays text');
        messages.appendChild(node('chat-bubble system', { ephemeral: '1' })); mutated();
        assert.ok(shown(), 'the reconnect notice is chrome, not conversation');
        const card = node('chat-live-card');
        messages.appendChild(card); mutated();
        assert.equal(shown(), undefined, 'a visible task card is content');
        card.remove(); mutated();
        assert.ok(shown());
        const bubble = node('chat-bubble user');
        messages.appendChild(bubble); mutated();
        assert.equal(shown(), undefined, 'a late message removes the empty state');
        bubble.remove(); welcome.historyRead(false);
        assert.equal(shown(), undefined, 'a later failed read retracts it');
        welcome.historyRead(true);
        welcome.setPreference({ mode: 'hidden', text: 'kept' });
        assert.equal(shown(), undefined);
        welcome.dispose();
        assert.equal(listeners.size, 0);
        assert.equal(mutated, null);
    } finally {
        globalThis.MutationObserver = previousObserver;
        restoreDom(prior);
    }
});
