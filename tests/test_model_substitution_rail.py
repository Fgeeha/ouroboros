"""A round another model answered is discarded and asked again, never accepted."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

from ouroboros import llm_claudexor as transport
from ouroboros import usage_accounting as ua
from ouroboros.llm import LLMClient
from ouroboros.llm_messages import drop_source_native_messages
from ouroboros.loop_llm_call import classify_llm_exception, provider_no_call_source
from ouroboros.loop_llm_call import _COOLDOWN_ERROR_KINDS
from ouroboros.loop_transport import (
    emit_model_substitution, fallback_chain_allowed, provider_recovery_hint,
)
from ouroboros.model_slots import MODEL_ACCOUNTS_KEY

from tests.test_llm_claudexor import Gateway, MODEL, ROUTE, result

OTHER_ACCOUNT = {**ROUTE, "credentialProfileId": "account-b", "accountFingerprint": "fingerprint-b"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """The transport harness of `test_llm_claudexor`, with its own data root."""
    root = tmp_path / "data"
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    monkeypatch.delenv(MODEL_ACCOUNTS_KEY, raising=False)
    monkeypatch.setattr(transport.config, "CLAUDEXOR_MODEL_POLL_INTERVAL_SEC", 0.001)
    gateway = Gateway()
    monkeypatch.setattr(transport, "ensure_owned_gateway", lambda: gateway)
    with ua.usage_scope(ua.UsageScope(drive_root=root, task_id="task-one", root_task_id="task-one")):
        yield root, gateway, LLMClient()


def substituted(route=None, *, served="cheaper-model", outcome="completed"):
    """One terminal generation the engine says came from another model."""
    value = result(outcome=outcome, route={**(route or ROUTE), "model": served})
    value["modelMismatch"] = {"requested": "exact-model", "observed": served}
    return value


def events(root, kind):
    path = root / "logs" / "events.jsonl"
    rows = [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row.get("type") == kind]


def call(client, messages=None):
    return client.chat(messages or [{"role": "user", "content": "go"}], MODEL, None, "high")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_substituted_round_is_discarded_and_asked_again(setup, asynchronous):
    root, gateway, client = setup
    gateway.results = [substituted(), result()]
    gateway.dispatch = ["response_received"] * 2
    messages = [{"role": "user", "content": "go"}]
    message, usage = (asyncio.run(client.chat_async(messages, MODEL, None, "high")) if asynchronous
                      else call(client, messages))
    # The accepted answer is the second generation, and the first one is gone.
    assert message["content"] == "Ответ 🐍"
    assert usage["claudexor"]["route"]["model"] == "exact-model"
    assert len(gateway.creates) == 2 and len(set(gateway.creates)) == 2
    assert len(gateway.acks) == 2  # the discarded bytes are acknowledged like any other
    disclosed = usage["claudexor"]["substituted"]
    assert disclosed == [{"requested": "exact-model", "observed": "cheaper-model",
                          "account": "account-a", "disposition": "redo"}]
    row, = events(root, "model_served_mismatch")
    assert row["requested"] == "exact-model" and row["observed"] == "cheaper-model"
    assert row["disposition"] == "redo" and row["redo"] == 1 and row["redos"] == 2
    assert row["discarded_usage"] == {"input_tokens": 20, "output_tokens": 7, "cached_input_tokens": 12}
    assert row["task_id"] == "task-one" and row["physical_attempt_id"]
    # Two settled physical attempts: the discarded generation was paid for too.
    rows = [entry for entry in (root / ua.LEDGER_REL).read_text().splitlines() if entry]
    assert len(rows) >= 2


def test_second_request_drops_the_substituting_account_preference(setup):
    root, gateway, client = setup
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "earlier",
         "nativeContinuation": {"route": dict(ROUTE), "format": "codex.responses.v1",
                                "payload": [{"type": "reasoning"}]}},
        {"role": "user", "content": "second"},
    ]
    gateway.results = [substituted(), result(route=OTHER_ACCOUNT)]
    gateway.dispatch = ["response_received"] * 2
    client.chat(history, MODEL, None, "high")
    first, second = (payload for payload, _ in gateway.uploads)
    # The first request prefers the account the conversation ran on; the redo
    # names none, so the engine alone decides where it lands.
    assert first["account"] == {"mode": "auto", "preferredProfileId": "account-a"}
    assert second["account"] == {"mode": "auto"}


def test_exhausted_redos_refuse_instead_of_returning_the_other_model(setup, monkeypatch):
    root, gateway, client = setup
    monkeypatch.setattr(transport.config, "get_model_substitution_redos", lambda: 1)
    gateway.results = [substituted(), substituted(OTHER_ACCOUNT)]
    gateway.dispatch = ["response_received"] * 2
    with pytest.raises(transport.ClaudexorModelError) as raised:
        call(client)
    error = raised.value
    assert error.code == "model_substituted" and error.retryable is False
    assert error.problem["context"] == {"requested_model": "exact-model",
                                        "observed_model": "cheaper-model",
                                        "reason": "redos_exhausted", "redos": 1}
    assert "cheaper-model" in str(error) and error.route["credentialProfileId"] == "account-b"
    assert len(gateway.creates) == 2  # one redo, then the refusal
    assert [row["disposition"] for row in events(root, "model_served_mismatch")] == ["redo", "redos_exhausted"]


def test_no_redo_is_spent_on_a_pinned_account(setup, monkeypatch):
    root, gateway, client = setup
    monkeypatch.setenv(MODEL_ACCOUNTS_KEY, json.dumps({"main": "account-a"}))
    gateway.results = [substituted()]
    with pytest.raises(transport.ClaudexorModelError) as raised:
        client.chat([{"role": "user", "content": "go"}], MODEL, None, "high", model_role="main")
    assert raised.value.problem["context"]["reason"] == "pinned_account"
    assert len(gateway.creates) == 1
    assert events(root, "model_served_mismatch")[0]["disposition"] == "pinned_account"


def test_no_redo_changes_the_bytes_of_an_admitted_candidate(setup):
    root, gateway, client = setup
    gateway.results = [substituted()]
    with ua.bind_physical_attempt_context(None, lambda request: True):
        with pytest.raises(transport.ClaudexorModelError) as raised:
            call(client)
    assert raised.value.problem["context"]["reason"] == "admitted_candidate"
    assert len(gateway.creates) == 1


def test_a_discarded_generation_never_becomes_the_live_turn(setup, monkeypatch):
    root, gateway, client = setup
    monkeypatch.setattr(transport, "owned_engine_version", lambda: "3.12.5")
    slot = transport.ModelTurnState()
    gateway.results = [substituted(), result()]
    gateway.dispatch = ["response_received"] * 2
    gateway.results[0]["nativeContinuation"] = {
        "route": dict(ROUTE), "format": "codex.turn.v1", "payload": {"turnState": "discarded"}}
    gateway.results[1]["nativeContinuation"] = {
        "route": dict(ROUTE), "format": "codex.turn.v1", "payload": {"turnState": "accepted"}}
    client.chat([{"role": "user", "content": "go"}], MODEL, None, "high", model_turn_state=slot)
    # Only the accepted generation owns the live turn; the discarded one would
    # have carried the substituting route straight back into the redo.
    assert slot.envelope == gateway.results[1]["nativeContinuation"]


def test_an_incomplete_answer_from_another_model_is_refused_too(setup, monkeypatch):
    root, gateway, client = setup
    monkeypatch.setattr(transport.config, "get_model_substitution_redos", lambda: 0)
    gateway.results = [substituted(outcome="incomplete")]
    gateway.dispatch = ["response_received"]
    with pytest.raises(transport.ClaudexorModelError) as raised:
        call(client)
    assert raised.value.code == "model_substituted"
    assert raised.value.problem["context"]["reason"] == "redos_exhausted"


def test_an_ordinary_answer_is_untouched_and_an_unknown_model_is_not_a_mismatch(setup):
    root, gateway, client = setup
    plain = result()
    plain["modelMismatch"] = None
    gateway.results = [plain]
    message, usage = call(client)
    assert message["content"] == "Ответ 🐍" and "substituted" not in usage["claudexor"]
    assert len(gateway.creates) == 1 and not events(root, "model_served_mismatch")


def test_the_refusal_is_its_own_kind_that_rotates_without_cooling_the_model():
    error = transport.ClaudexorModelError(
        {"code": "model_substituted", "message": "another model answered",
         "context": {"requested_model": "a", "observed_model": "b"}})
    classified = classify_llm_exception(error)
    assert classified.kind == "model_substituted" and classified.retry_same_request is False
    assert classified.provider_code == "model_substituted"
    # A substituting account must never cool the model the owner asked for, and
    # the configured chain stays available once the redos are spent.
    assert "model_substituted" not in _COOLDOWN_ERROR_KINDS
    assert fallback_chain_allowed(object(), "model_substituted", None, {}) is True


@pytest.mark.parametrize("usage, expected", [
    ({"_last_llm_error_kind": "model_substituted"}, "different model than the one requested"),
    ({"_last_llm_error_kind": "bad_request", "_last_llm_provider_code": "invalid_continuation"},
     "refused the stored continuation"),
    ({"_last_llm_error_kind": "bad_request", "_last_llm_provider_code": "unsupported_parameter"},
     "rejected the request shape"),
])
def test_the_owner_terminal_names_the_real_refusal(usage, expected):
    assert expected in provider_recovery_hint(usage)


def test_a_same_route_refusal_is_not_asked_a_second_time():
    for usage in ({"_last_llm_error_kind": "model_substituted"},
                  {"_last_llm_error_kind": "bad_request", "_last_llm_provider_code": "invalid_continuation"}):
        source, wall = provider_no_call_source(usage, deadline_exhausted=False)
        assert source == "same_route_refusal_no_resend" and wall is False
    # An ordinary refusal keeps its one salvage call.
    assert provider_no_call_source({"_last_llm_error_kind": "bad_request"}, deadline_exhausted=False) == ("", False)


def test_the_owner_row_names_what_happened_once_per_task_and_model():
    notes = []
    usage = {"_model_substitutions": [
        {"requested": "exact-model", "observed": "cheaper-model", "account": "account-a",
         "disposition": "redo"},
        {"requested": "exact-model", "observed": "cheaper-model", "account": "account-b",
         "disposition": "redo"},
    ]}
    for _ in range(2):
        emit_model_substitution(usage, task_id="task-one",
                                emit_progress=lambda text, **meta: notes.append((text, meta)))
    text, meta = notes[0]
    assert len(notes) == 1
    assert "cheaper-model answered instead of the requested exact-model" in text
    assert "Claudexor account account-a" in text and "asked again on another account" in text
    assert meta == {"card_row": "timeline", "card_row_id": "task-one:model_substitution:exact-model"}


def test_a_refusal_row_does_not_claim_the_round_was_redone():
    notes = []
    emit_model_substitution(
        {"_model_substitutions": [{"requested": "a", "observed": "b", "account": "",
                                   "disposition": "pinned_account"}]},
        task_id="task-one", emit_progress=lambda text, **meta: notes.append(text))
    assert "the account is pinned, so the round was not asked again" in notes[0]
    assert "Claudexor account" not in notes[0]


def test_dropping_one_source_continuation_keeps_every_other_message_intact():
    messages = [
        {"role": "user", "content": "ask"},
        {"role": "assistant", "content": "said", "tool_calls": [{"id": "a"}],
         "nativeContinuation": {"route": {"source": "codex"}, "payload": [{"type": "reasoning"}]}},
        {"role": "assistant", "content": "other engine",
         "nativeContinuation": {"route": {"source": "other"}, "payload": []}},
    ]
    original = deepcopy(messages)
    prepared, changed = drop_source_native_messages(messages, source="codex")
    assert messages == original  # the caller's history is never mutated in place
    assert len(changed) == 1 and changed[0]["old_route"] == {"source": "codex"}
    assert "nativeContinuation" not in prepared[1] and prepared[1]["tool_calls"] == [{"id": "a"}]
    assert prepared[1]["content"] == "said" and "nativeContinuation" in prepared[2]
    assert drop_source_native_messages(prepared, source="codex")[1] == []
