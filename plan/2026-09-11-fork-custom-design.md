# fork/custom — design

Date: 2026-09-11. Base: `managed/ouroboros` @ `eec71d53` (v7.0.0).

## Goal

A long-lived fork branch that adds owner-facing features without drifting from
upstream, so that `git merge managed/ouroboros` stays cheap.

Features, in execution order:

1. Makefile with the everyday commands, including running as a server.
2. Visible agent reasoning in the chat live card, collapsible per step.
3. Light theme in addition to the existing dark theme.
4. Russian UI language.

Out of scope: version bumps (upstream assigns versions), rewriting upstream
history, new event types in the supervisor taxonomy.

## Branch model

- `fork/custom` is the integration branch, created from the upstream tip.
- Each feature lives on `fork/<feature>` and is merged into `fork/custom` with
  `--no-ff`.
- Upstream sync: `make sync-upstream` = `git fetch managed` +
  `git merge managed/ouroboros`. Merge, never rebase, because upstream forbids
  rewriting published history and rebasing several features is a conflict trap.
- Every feature prefers new files and single hook points over inline rewrites
  of upstream code, to keep merge surface small.
- Commit messages: Russian, Conventional Commits, no tool trailers.

## Work roles

Each feature runs through three project agents defined in `.claude/agents/`:

- `developer` implements the smallest diff that satisfies the feature.
- `tester` writes or extends tests first where the change is testable, runs
  the exact CI lanes (`make check`), reports commands and exit codes.
- `code-reviewer` reviews the committed diff in a separate context against the
  eight-item Intent / Scope checklist in `docs/CHECKLISTS.md` and emits a JSON
  receipt validated by `scripts/validate_scope_receipt.py`.

## 1. Makefile

Keep the existing `test`, `test-v`, `lint`, `health`, `clean` targets (they
match CI). Add:

| target | does |
|---|---|
| `help` (default) | self-documenting list from `## ` comments |
| `install` | `uv sync --locked` + `npm ci` in `web/` |
| `run` | `uv run ouroboros server --host $(HOST) --port $(PORT)`; defaults 127.0.0.1:8765 |
| `run-desktop` | `uv run python launcher.py` |
| `test-web` | `node --test tests/*.test.js` in `web/` |
| `lint-web` | `npm run lint:undef` in `web/` |
| `check` | `lint` + `lint-web` + `test` + `test-web` |
| `docker-build`, `docker-run` | image `ouroboros-web`, port 8765, password from env |
| `sync-upstream` | fetch + merge `managed/ouroboros` |

Sections in usage order, one `.PHONY` block, English comments (fork of an
English repo).

## 2. Visible reasoning

Backend already extracts display reasoning
(`LLMClient.extract_display_reasoning`) and the delegated-harness timeline
already tags rows `textKind: "thinking"`. Both are flattened into untyped
progress strings; reasoning is dropped when the round also has visible text.

Change:

- `ouroboros/loop_messages.py::_emit_round_progress` always extracts display
  reasoning (subject to `OUROBOROS_REASONING_SUMMARY != "off"`) and emits it
  as a separate progress message stamped `progress_meta.reasoning = True`.
  Visible text keeps its current path.
- `ouroboros/agent.py::_emit_progress` accepts an optional `meta` mapping that
  is merged into `progress_meta`.
- `ouroboros/delegate_progress.py` emits `thinking` rows as a separate
  reasoning-stamped progress message; action rows keep the current line.
- No new event type: `progress_meta` rides the existing `send_message` frame
  through the WebSocket, `progress.jsonl`, `/api/logs/progress` and
  `/api/tasks/{id}/events` unchanged.
- `web/modules/log_events.js::summarizeChatLiveEvent` maps a reasoning-stamped
  row to phase `thinking`, headline "Thinking", `body`/`fullBody` = reasoning
  text, `activityPreview: ''` so the collapsed card summary keeps showing the
  last action. The existing expander renders it collapsed with Expand/Collapse.
  `summarizeLogEvent` mirrors the branch; a `reasoning` category chip is added
  to the Logs tab.
- Rows without the stamp (old logs) render as today.

Tests: `tests/test_narration_display.py`, `tests/test_delegate_progress_text.py`,
`web/tests/collapsed_activity.test.js`, `web/tests/log_category.test.js`.

## 3. Light theme

- `web/theme_light.css`: `html[data-theme="light"]` overrides of the `:root`
  tokens only, plus `color-scheme: light`. Linked after `style.css`. The
  onboarding page gets the same override block inlined (it cannot import).
- Hardcoded colors outside `:root` that break in light mode are replaced by
  tokens screen by screen, verified with screenshots. Not a blind sweep of all
  330 literals; upstream-visible surfaces first: chat, sidebar, settings,
  dashboard, logs.
- Persistence: new `theme` key (`"dark" | "light"`) in
  `ouroboros/gateway/ui_preferences.py::DEFAULT_UI_PREFERENCES`, applied on
  boot in `web/app.js` next to `sidebar_width`. Toggle in Settings → Behavior.
  The desktop launcher reads the same preference for the window background.
- `docs/DESIGN.md` "dark only" paragraph amended to describe the token
  override contract.

Tests: `tests/test_web_typography_static.py` must stay green (both directions
of the token contract), `tests/test_ui_preferences*.py` for the new key, one
Playwright screenshot pair per themed screen as PR evidence.

## 4. Russian language

Translation overlay, no call-site edits:

- `web/modules/i18n.js`: `applyLanguage(lang)` walks text nodes and
  `placeholder`/`title`/`aria-label` attributes, replaces exact English source
  strings via a dictionary, and keeps doing so through a `MutationObserver`.
  Chat message bodies, code, logs payloads and user input are excluded by
  selector. Unknown strings stay English.
- `web/i18n/ru.js`: dictionary keyed by the English source string. Strings with
  interpolation use a small pattern form (`"Retry in {n}s"`).
- `<html lang>` follows the language. Persistence: `language` key
  (`"en" | "ru"`) in UI preferences; selector in Settings → Behavior.
- Server error strings (`json_error`) stay English in this iteration.

Ceiling (`ponytail:`): the observer re-translates on every DOM mutation; if
the chat becomes sluggish, scope the observer to the sidebar/settings roots.

Tests: `web/tests/i18n.test.js` (dictionary application, exclusion selectors,
pattern strings), UI preference key test.
