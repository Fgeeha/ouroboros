"""#1351: direct sessions refuse declared inputs before fresh or retry side effects."""
from __future__ import annotations

import json

import pytest

from tests._delegated_transport_shared import (  # noqa: F401 - installs the offline actor fixture
    _LiveRunStub,
    _nanny_ctx,
    _owned_gateway_uses_each_test_transport,
)


@pytest.mark.parametrize("retry_of", [None, "pending-before-selection"])
@pytest.mark.parametrize("carrier", ["contract", "metadata_contract", "metadata"])
def test_declared_direct_start_refuses_before_any_start_work(tmp_path, monkeypatch, retry_of, carrier):
    from ouroboros import claudexor_daemon, delegate_custody
    from ouroboros.tools import delegate

    ctx = _nanny_ctx(tmp_path)
    if carrier == "contract":
        ctx.task_contract = {"input_sources": "declared"}
    elif carrier == "metadata_contract":
        ctx.task_metadata["task_contract"] = {"input_sources": "declared"}
    else:
        ctx.task_metadata["input_sources"] = "declared"

    def unexpected(*_args, **_kwargs):
        pytest.fail("declared direct start reached argument processing, custody, or transport")

    for name in ("_payload_selector_refusal", "deadline_expired", "bounded_max_seconds",
                 "prepare_work_order_start_binding", "prepare_delegate_start_actor",
                 "_resolve_retry_invocation", "_assignment_instructions", "_emit"):
        monkeypatch.setattr(delegate, name, unexpected)
    monkeypatch.setattr(delegate_custody, "custody_root", unexpected)
    monkeypatch.setattr(claudexor_daemon, "ensure_owned_gateway", unexpected)

    result = delegate._delegate_start(ctx, "Inspect the declared evidence", retry_of=retry_of)

    assert (result.status, result.code) == ("error", "TOOL_REPORTED_FAILURE")
    payload = json.loads(result.text)
    assert payload["status"] == "refused" and payload["ok"] is False
    assert payload["reason"] == "INPUT_SOURCE_SELECTION_UNSUPPORTED"
    assert payload["definitely_unrun"] is True and payload["host_fallback"] is False
    assert "scheduled API-model children only" in payload["detail"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("retry", [False, True])
def test_registered_direct_start_publishes_typed_selection_refusal(tmp_path, monkeypatch, retry):
    from ouroboros import claudexor_daemon, config, safety
    from ouroboros.tools import delegate
    from ouroboros.tools.registry import ToolRegistry

    monkeypatch.setattr(config, "runtime_settings", lambda: {
        "OUROBOROS_SUBAGENTS": {"enabled": True, "items": [{
            "subagent_id": "session", "recommended_use": "Inspect the source.",
            "route": {"kind": "agent_session", "target_id": "codex=gpt-5.6-sol"},
        }]},
    })
    monkeypatch.setattr(safety, "check_safety", lambda *_a, **_kw: (True, ""))

    def unexpected(*_args, **_kwargs):
        pytest.fail("registered declared start reached work-order preparation or gateway")

    monkeypatch.setattr(delegate, "prepare_work_order_start_binding", unexpected)
    monkeypatch.setattr(claudexor_daemon, "ensure_owned_gateway", unexpected)
    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    registry._ctx.task_id = "declared-child"
    registry._ctx.task_contract = {"input_sources": "declared"}
    args = {"prompt": "Inspect declared evidence"}
    args.update({"retry_of": "pending-before-selection"} if retry else {"subagent_id": "session"})

    result = registry.execute_result("delegate_start", args)

    assert (result.status, result.code) == ("error", "TOOL_REPORTED_FAILURE"), result.text
    assert json.loads(result.text)["reason"] == "INPUT_SOURCE_SELECTION_UNSUPPORTED"
    assert not (registry._ctx.drive_root / "logs" / "events.jsonl").exists()


@pytest.mark.parametrize("selection", [None, "shared"])
def test_ordinary_direct_start_and_retry_preserve_request_and_parent_context(tmp_path, monkeypatch, selection):
    from ouroboros.gateways import claudexor as gateway
    from ouroboros.tools import delegate

    requests = []

    class RetryStub(_LiveRunStub):
        def start_run(self, request, *, idempotency_key=""):
            requests.append((json.loads(json.dumps(request)), idempotency_key))
            if len(requests) == 1:
                raise gateway.ClaudexorUnavailable("daemon_unreachable", "offline lost response")
            return super().start_run(request, idempotency_key=idempotency_key)

    stub = RetryStub()
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=ordinary-model:high")
    monkeypatch.setattr(gateway, "ClaudexorGateway", lambda *_a, **_kw: stub)
    ctx = _nanny_ctx(tmp_path)
    ctx.task_contract = {"context": "PREVIOUS_CASE_CONTEXT", "constraints": "PARENT_AUTHORITY"}
    if selection is not None:
        ctx.task_contract["input_sources"] = selection
    prompt = "Continue ordinary work"

    lost = json.loads(delegate._delegate_start(ctx, prompt, max_seconds=120).text)
    assert lost["reason"] == "daemon_unreachable", lost
    token = lost["pending_invocation_id"]
    resumed = json.loads(delegate._delegate_start(ctx, prompt, retry_of=token).text)

    assert resumed["status"] == "started" and resumed["idempotent_recovery"] is True, resumed
    assert len(requests) == 2 and requests[0] == requests[1]
    request, key = requests[0]
    assert key == token and request["prompt"] == prompt
    assert (request["model"], request["effort"], request["maxSeconds"]) == ("ordinary-model", "high", 120)
    assert "PREVIOUS_CASE_CONTEXT" in json.dumps(request)
    assert "PARENT_AUTHORITY" in json.dumps(request)
