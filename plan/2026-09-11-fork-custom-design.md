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

- Correction (implementation): the token palette lives in `web/ui.css`
  (`:root`, then `.ouro-ui { color-scheme: dark }`), not in `style.css`, and
  `tests/test_web_typography_static.py` parses the `<link>` tags of
  `index.html` / `onboarding_template.html`: `web/ui.css` must stay the first
  sheet and no other linked sheet may declare a custom property name that
  `ui.css` declares. So there is no `theme_light.css` and no new `<link>`: the
  light overrides are an `html[data-theme="light"]` block inside `web/ui.css`
  (plus `html[data-theme="light"].ouro-ui { color-scheme: light; }`), and the
  onboarding page inherits them by already linking `ui.css` (its page-local
  `--bg/--panel/...` names get their own light block in `onboarding.css`).
  Every new `:root` token needs a `var()` reader in a linked sheet or in
  `web/modules/**/*.js` in the same change, and every `var(--x)` used must be
  declared (both directions are tested).
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

## Status, 2026-09-12

All four features are merged into `fork/custom`, each on its own branch with a
separate-context review recorded in
`/tmp/.../scratchpad/{theme,reasoning,i18n}-scope-receipt.json`.

Known gaps, deliberately left:

- `web/modules/chat.js` is 13 bytes over the shrink-only size ratchet. The lane
  blocks only in official-repository CI, which does not run on this fork. Pay it
  back before proposing any of this upstream.
- The Russian dictionary covers chrome (nav, tabs, buttons, headings, settings
  labels) but not most long body copy, notably the Accounts panel and the
  onboarding provider paragraphs. Untranslated strings stay English by design.
- Light mode: Skills, Marketplace and Widgets keep about 20 `rgba(250,250,250,…)`
  inks; those pages were tokenized but never screenshotted in light mode.
- Four pre-existing test failures on Python 3.13 (`test_plan_spec`,
  `test_rc_audit_fixture_suite`, two in `test_v7next_transplant`) and one in
  `test_process_custody`, all proven on the base commit.
