import test from 'node:test';
import assert from 'node:assert/strict';
import { bindWelcomePreference, DEFAULT_WELCOME_TEXT, welcomeText } from '../modules/welcome_preference.js';

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
