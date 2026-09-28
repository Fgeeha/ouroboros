"""Local capacity is a serving-instance fact, independent of training and health."""
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from ouroboros import capability_evidence as ce, config, local_model
from ouroboros.llm import LLMClient
from ouroboros.llm_local import local_context_limits


@pytest.fixture
def manager(monkeypatch, tmp_path):
    manager = local_model.LocalModelManager()
    manager._proc = SimpleNamespace(pid=123, poll=lambda: None)
    manager._status = "ready"
    monkeypatch.setattr(local_model, "get_manager", lambda: manager)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return manager


@pytest.mark.parametrize("unknown", [{}, {"context_length": None}, {"context_length": 0},
                                      {"context_length": -1}, {"context_length": "unknown"},
                                      ConnectionError("unavailable")])
def test_unknown_metadata_does_not_poison_cache_or_health(manager, monkeypatch, unknown):
    health = Mock(side_effect=[unknown, {"context_length": 131072}])
    monkeypatch.setattr(manager, "health_check", health)
    assert manager.get_context_length() == 0
    assert manager.status_dict()["context_length"] == 0
    assert manager.is_running
    assert manager.get_context_length() == 131072
    assert manager.get_context_length() == 131072
    assert health.call_count == 2


@pytest.mark.parametrize("window", [4096, 16384, "131072"])
def test_positive_reported_metadata_is_retained(manager, monkeypatch, window):
    health = Mock(return_value={"context_length": window})
    monkeypatch.setattr(manager, "health_check", health)
    assert manager.get_context_length() == int(window)
    assert manager.get_context_length() == int(window)
    health.assert_called_once()


def test_healthy_server_with_no_window_still_becomes_ready(manager, monkeypatch):
    import requests
    from ouroboros import server_process

    response = Mock()
    response.json.return_value = {"data": [{"id": "local-fixture"}]}
    session = Mock()
    session.get.return_value = response
    # Mock only HTTP; exercise the actual health parser and readiness consumer.
    session_scope = MagicMock()
    session_scope.__enter__.return_value = session
    monkeypatch.setattr(requests, "Session", lambda: session_scope)
    monkeypatch.setattr(server_process, "record_service_binding", lambda *a, **k: None)
    manager._status = "loading"
    manager._wait_for_healthy(timeout=1)
    assert manager.is_running
    assert manager.get_context_length() == 0
    assert manager.serving_context_evidence()["confirmed"] is False
    assert manager.status_dict()["error"] is None


@pytest.mark.parametrize("allow_fetch", [False, True])
@pytest.mark.parametrize("legacy_window", [None, 4096, 131072])
def test_unknown_local_probe_ignores_legacy_cache_and_cloud_metadata(
    manager, monkeypatch, tmp_path, allow_fetch, legacy_window,
):
    manager._context_length = 131072  # Training metadata is not a served window.
    fp = ce.route_fingerprint(provider="openrouter", model="remote/name")
    if legacy_window is not None:
        ce._store_evidence(tmp_path, "probes", fp, ce.CapabilityEvidence(
            legacy_window, ce.STATUS_CONFIRMED, ce.SOURCE_LOCAL_HEALTH, fp,
            "remote/name", "openrouter", ts=ce.utc_now_iso(),
        ).to_json())
    path = ce._store_path(tmp_path)
    before = path.read_bytes() if path.exists() else None
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: pytest.fail("cloud metadata"))
    monkeypatch.setattr(ce, "_generative_probe_window", lambda *a, **k: pytest.fail("generation probe"))
    evidence = ce.probe(tmp_path, provider="openrouter", model="remote/name", use_local=True,
                        allow_fetch=allow_fetch, allow_generative=True)
    assert evidence.window_tokens == 0
    assert evidence.status == ce.STATUS_UNPROBEABLE
    assert not ce.is_known(evidence)
    assert local_context_limits(65536) == (0, 2048)
    assert manager.is_running
    assert (path.read_bytes() if path.exists() else None) == before


def test_live_serving_capacity_reaches_main_and_review_consumers(manager, monkeypatch, tmp_path):
    from ouroboros.context_fit import resolve_context_fit_route
    from ouroboros.reviewer_window import resolve_reviewer_window

    monkeypatch.setattr(config, "runtime_settings", lambda: {"OUROBOROS_MODEL_CONTEXT_WINDOWS": {}})
    monkeypatch.setattr("ouroboros.reviewer_window._LAZY_ROUTE_LOCKS", {})
    monkeypatch.setenv("OUROBOROS_MODEL_CONTEXT_WINDOWS", "{}")
    manager._context_length = 131072
    for window in (16384, 8192, 4096, 0):
        manager._serving_context_length = window
        route, evidence = resolve_context_fit_route(
            {"model": "local-fixture", "use_local_model": True}, allow_fetch=False)
        assert route["use_local"] is True
        assert evidence.window_tokens == window
        assert ce.is_known(evidence) is (window > 0)
        reviewer = resolve_reviewer_window("local-fixture", use_local=True)
        assert reviewer.window_tokens == window
        assert reviewer.status == evidence.status
        assert local_context_limits(65536) == (window, window // 4 if window else 2048)
    assert not ce._store_path(tmp_path).exists()


def test_local_owner_assertion_remains_separate(manager, tmp_path):
    ce.record_owner_ack(tmp_path, provider="local", model="local-fixture", window_tokens=32768)
    evidence = ce.probe(tmp_path, provider="local", model="local-fixture", use_local=True, allow_fetch=False)
    assert evidence.window_tokens == 32768
    assert evidence.status == ce.STATUS_ASSERTED
    assert evidence.source == ce.SOURCE_OWNER_ACK
    assert manager.serving_context_evidence()["confirmed"] is False


def test_serving_measurement_keeps_input_and_instance_binding(manager, monkeypatch):
    import requests
    from ouroboros.local_model_server import input_fingerprint

    manager._context_length = 131072
    manager._serving_context_length = 16384
    manager._measurement_route = True
    payload = {"model": "local-model", "messages": [{"role": "user", "content": "source"}]}
    measured = {"supported": True, "input_is_exact": True, "input_tokens": 42,
                "context_window": 16384, "process_id": 123,
                "native_input_sha256": input_fingerprint(payload)}
    response = Mock()
    response.json.return_value = measured
    session_scope = MagicMock()
    session_scope.__enter__.return_value.post.return_value = response
    monkeypatch.setattr(requests, "Session", lambda: session_scope)
    assert manager.measure_prepared_input(payload) == measured
    changed_payload = {**payload, "messages": [{"role": "user", "content": "changed"}]}
    assert manager.measure_prepared_input(changed_payload)["supported"] is False
    manager._proc = SimpleNamespace(pid=456, poll=lambda: None)
    assert manager.measure_prepared_input(payload)["supported"] is False


@pytest.mark.parametrize("window", [0, 16384])
def test_running_local_dispatch_uses_serving_capacity_without_cloud_fallback(manager, monkeypatch, window):
    manager._context_length = 131072 if window else 4096
    manager._serving_context_length = window
    client = LLMClient(api_key="unused")
    sent = []

    def create(**payload):
        sent.append(payload)
        return SimpleNamespace(model_dump=lambda: {
            "choices": [{"message": {"role": "assistant", "content": "local answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 6000, "completion_tokens": 2, "total_tokens": 6002},
        })

    monkeypatch.setattr(client, "_get_local_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(client, "_resolve_remote_target", lambda *a, **k: pytest.fail("cloud fallback"))
    text = "Keep this source intact. " * 1000  # Exceeds the fictitious 4096 allowance.
    message, usage = client.chat(messages=[{"role": "user", "content": text}],
                                 model="local-fixture", max_tokens=65536, use_local=True)
    assert message["content"] == "local answer"
    assert usage["provider"] == "local"
    assert len(sent) == 1
    assert sent[0]["messages"][-1]["content"] == text
    assert sent[0]["max_tokens"] == (4096 if window else 2048)
    assert not ce._load(config.DATA_DIR)["probes"]


def test_actual_local_connection_error_is_not_a_synthetic_overflow(manager, monkeypatch):
    manager._context_length = 0
    monkeypatch.setattr(manager, "health_check", Mock(side_effect=ConnectionError("health unavailable")))
    client = LLMClient(api_key="unused")
    error = ConnectionError("local transport unavailable")
    create = Mock(side_effect=error)
    monkeypatch.setattr(client, "_get_local_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(client, "_resolve_remote_target", lambda *a, **k: pytest.fail("cloud fallback"))
    with pytest.raises(ConnectionError) as caught:
        client.chat(messages=[{"role": "user", "content": "source " * 5000}],
                    model="local-fixture", max_tokens=65536, use_local=True)
    assert caught.value is error
    create.assert_called_once()
