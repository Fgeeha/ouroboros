"""Focused prompt-catalog projection tests for Available subagents."""

from __future__ import annotations

import json

import pytest


def _row(
    row_id: str,
    *,
    kind: str,
    target: str,
    recommendation: str,
    effort: str = "",
    profile: str = "",
) -> dict:
    route = {"kind": kind, "target_id": target}
    if kind == "agent_session":
        route["credential_profile_id"] = profile
    return {
        "subagent_id": row_id,
        "name": row_id.replace("-", " ").title(),
        "recommended_use": recommendation,
        "route": route,
        "effort": effort,
    }


def _settings(*rows: dict, enabled: bool = True) -> dict:
    return {
        "OUROBOROS_SUBAGENTS": json.dumps({"enabled": enabled, "items": list(rows)}),
    }


def test_catalog_projects_every_saved_row_in_owner_order_verbatim():
    from ouroboros.subagent_runtime import model_visible_subagent_catalog

    verbatim = "Use exact owner wording.\nKeep punctuation: a/b, quotes, and cost $0."
    settings = _settings(
        _row(
            "api-scout",
            kind="api_model",
            target="google/gemini-3.7-flash",
            recommendation=verbatim,
            effort="low",
        ),
        _row(
            "auto-session",
            kind="agent_session",
            target="claude=claude-fable-5",
            recommendation="Use the automatic account pool.",
            effort="high",
        ),
        _row(
            "pinned-session",
            kind="agent_session",
            target="cursor=cursor-grok-4.6-high",
            recommendation="Use this pinned account.",
            profile="cursor-owner",
        ),
    )

    catalog = model_visible_subagent_catalog(settings)

    # Facts only: selection prose lives in prompts/SYSTEM.md, and the list
    # fingerprint and provenance stay host-side (snapshots carry them).
    assert set(catalog) == {"rows"}
    # Rows are named by their handle - the route plus the row's own set facets.
    assert [row["subagent_id"] for row in catalog["rows"]] == [
        "google/gemini-3.7-flash/low", "claude=claude-fable-5/high",
        "cursor=cursor-grok-4.6-high/@cursor-owner",
    ]
    assert catalog["rows"][0]["recommended_use"] == verbatim
    assert list(catalog["rows"][0])[-1] == "recommended_use"
    assert catalog["rows"][0]["route_class"] == "API model"
    assert catalog["rows"][0]["requested_model"] == "google/gemini-3.7-flash"
    assert catalog["rows"][0]["requested_effort"] == "low"
    assert catalog["rows"][1]["route_class"] == "Agent session"
    assert catalog["rows"][1]["requested_target"] == "claude=claude-fable-5"
    assert catalog["rows"][1]["mutating_access"] == "full"
    assert "credential_profile_id" not in catalog["rows"][1]
    assert catalog["rows"][2]["requested_effort"] == "(not explicitly set)"
    assert catalog["rows"][2]["credential_profile_id"] == "cursor-owner"
    assert not any("account_policy" in row for row in catalog["rows"])


@pytest.mark.parametrize(
    "settings",
    [
        {"OUROBOROS_SUBAGENTS": "not-json"},
        {
            **_settings(enabled=False),
            "OUROBOROS_SUBAGENT_HARNESS": "codex=gpt-5.6-sol:high",
        },
        _settings(),
    ],
)
def test_catalog_omits_unsaved_invalid_disabled_and_empty(settings):
    from ouroboros.subagent_runtime import model_visible_subagent_catalog

    assert model_visible_subagent_catalog(settings) == {}


def test_catalog_omits_a_real_undecided_candidate_rejected_by_new_id_dispatch():
    from ouroboros.configured_subagents import SOURCE_UNDECIDED, resolve_configured_subagents
    from ouroboros.subagent_runtime import (
        SubagentSelectionError,
        model_visible_subagent_catalog,
        select_subagent_snapshot,
    )

    settings = {"OUROBOROS_MODEL_HEAVY": "owner/unsaved-candidate"}
    resolution = resolve_configured_subagents(settings)
    assert resolution.source == SOURCE_UNDECIDED
    assert resolution.config is not None and resolution.config.items
    assert model_visible_subagent_catalog(settings) == {}
    with pytest.raises(SubagentSelectionError) as refused:
        select_subagent_snapshot(settings, subagent_id="legacy-heavy")
    assert refused.value.code == "subagent_configuration_unsaved"


def _context_env(tmp_path):
    class FakeEnv:
        def drive_path(self, path):
            return tmp_path / path

        def repo_path(self, path):
            return tmp_path / "repo" / path

        @property
        def repo_dir(self):
            return tmp_path / "repo"

        @property
        def drive_root(self):
            return tmp_path

    for path in ("state", "logs", "memory", "repo/docs", "repo/prompts", "repo/web"):
        (tmp_path / path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "repo/prompts/SYSTEM.md").write_text("System", encoding="utf-8")
    (tmp_path / "repo/BIBLE.md").write_text("Bible", encoding="utf-8")
    (tmp_path / "repo/docs/ARCHITECTURE.md").write_text("Architecture", encoding="utf-8")
    (tmp_path / "repo/docs/DEVELOPMENT.md").write_text("Development", encoding="utf-8")
    (tmp_path / "state/state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "logs/events.jsonl").write_text("", encoding="utf-8")
    return FakeEnv()


def test_catalog_is_semi_stable_while_dated_history_stays_dynamic(tmp_path, monkeypatch):
    from ouroboros.context import _capture_context_core
    from ouroboros.context_fit import _render_context_system_content
    from ouroboros.memory import Memory

    env = _context_env(tmp_path)
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    owner_text = "Use this exact owner description, verbatim.\nSecond line stays intact."
    saved = _settings(_row(
        "builder",
        kind="agent_session",
        target="codex=gpt-5.6-sol",
        recommendation=owner_text,
        effort="high",
    ))
    monkeypatch.setattr("ouroboros.config.load_settings", lambda: saved)
    (tmp_path / "state/reviewer_slot_last_execution.json").write_text(json.dumps({
        "triad": {
            "ts": "2026-08-18T01:02:03+00:00",
            "status": "ok",
            "requested": {"profile_id": "review-requested"},
            "effective": {"profile_id": "review-applied"},
        },
    }), encoding="utf-8")
    (tmp_path / "state/subagent_last_delegation.json").write_text(json.dumps({
        "ts": "2026-08-18T02:00:00+00:00",
        "route": "codex",
        "requested_model": "gpt-5.6-sol",
        "applied_model": "gpt-5.6-sol",
        "requested_profile": "delegate-requested",
        "applied_profile": "delegate-applied",
        "selected_subagent_id": "builder",
        "run_id": "run-1",
    }), encoding="utf-8")

    core = _capture_context_core(
        env, Memory(drive_root=tmp_path),
        {"id": "task-1", "type": "task", "text": "work"},
        None, None,
    )
    blocks = _render_context_system_content(env, core, mode="max")
    catalog_text = core.semi_stable_text.split("## Available subagents\n\n", 1)[1]
    catalog, _end = json.JSONDecoder().raw_decode(catalog_text)

    assert blocks[1]["text"] == core.semi_stable_text
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert blocks[2]["text"] == core.dynamic_text
    assert "cache_control" not in blocks[2]
    assert catalog["rows"][0]["recommended_use"] == owner_text
    assert '"subagent_id": "codex=gpt-5.6-sol/high"' in core.semi_stable_text
    assert "builder" not in core.semi_stable_text, "the stored key is not model-facing"
    assert "2026-08-18T01:02:03+00:00" not in core.semi_stable_text
    assert "reviewer_slots_last" not in core.semi_stable_text
    assert "subagent_last_delegation" not in core.semi_stable_text
    assert "2026-08-18T01:02:03+00:00" in core.dynamic_text
    assert "reviewer_slots_last" in core.dynamic_text
    assert "subagent_last_delegation" in core.dynamic_text
    # An old receipt without a typed identity shows its recorded route target.
    assert '"selected_subagent_id": "codex=gpt-5.6-sol"' in core.dynamic_text
    for profile in (
        "review-requested", "review-applied", "delegate-requested", "delegate-applied",
    ):
        assert profile in core.dynamic_text
    assert "configured_route" not in core.dynamic_text
