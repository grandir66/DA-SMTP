"""Test ClaudeProvider con SDK Anthropic mockato."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from domarc_relay_admin.ai_assistant.providers.base import AiProviderError
from domarc_relay_admin.ai_assistant.providers.claude_provider import (
    ClaudeProvider, _calc_cost,
)


def test_calc_cost_haiku():
    # 1M input + 1M output token su Haiku → $1 + $5 = $6
    cost = _calc_cost("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(6.00, rel=1e-3)


def test_calc_cost_sonnet():
    cost = _calc_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert cost == pytest.approx(18.00, rel=1e-3)  # 3 + 15


def test_calc_cost_unknown_model_returns_zero():
    cost = _calc_cost("custom-model-xyz", 1000, 1000)
    assert cost == 0.0


def test_calc_cost_proportional():
    cost = _calc_cost("claude-haiku-4-5", 500_000, 100_000)
    # 0.5*1 + 0.1*5 = 0.5 + 0.5 = 1.0
    assert cost == pytest.approx(1.0, rel=1e-3)


def test_provider_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("MISSING_KEY_TEST", raising=False)
    with pytest.raises(AiProviderError, match="API key mancante"):
        ClaudeProvider(name="test", api_key_env="MISSING_KEY_TEST")


def test_provider_init_with_api_key(monkeypatch):
    monkeypatch.setenv("FAKE_TEST_KEY", "sk-ant-test-fake-12345")
    # Mock anthropic.Anthropic per non fare chiamate reali
    with patch("anthropic.Anthropic") as mock_class:
        provider = ClaudeProvider(name="test", api_key_env="FAKE_TEST_KEY")
        assert provider.name == "test"
        assert provider.kind == "claude"
        mock_class.assert_called_once()


def test_complete_with_structured_output(monkeypatch):
    monkeypatch.setenv("FAKE_TEST_KEY", "sk-ant-test-fake-12345")

    # Mock della risposta Anthropic
    mock_block = MagicMock()
    mock_block.type = "tool_use"
    mock_block.name = "respond_structured"
    mock_block.input = {"intent": "test", "urgenza": "ALTA", "summary": "ok"}

    mock_resp = MagicMock()
    mock_resp.content = [mock_block]
    mock_resp.usage.input_tokens = 100
    mock_resp.usage.output_tokens = 50
    mock_resp.stop_reason = "tool_use"

    with patch("anthropic.Anthropic") as mock_class:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_class.return_value = mock_client

        provider = ClaudeProvider(name="test", api_key_env="FAKE_TEST_KEY")
        resp = provider.complete(
            system="sei un classificatore",
            user="classifica questa mail",
            model="claude-haiku-4-5",
            json_schema={"type": "object", "properties": {"intent": {"type": "string"}}},
        )

        assert resp.parsed_json == {"intent": "test", "urgenza": "ALTA", "summary": "ok"}
        assert resp.input_tokens == 100
        assert resp.output_tokens == 50
        assert resp.cost_usd > 0
        assert resp.error is None
        assert resp.finish_reason == "tool_use"


def test_complete_handles_text_fallback(monkeypatch):
    """Se Claude torna text invece di tool_use, prova a parsare come JSON."""
    monkeypatch.setenv("FAKE_TEST_KEY", "sk-ant-test-fake-12345")

    mock_text = MagicMock()
    mock_text.type = "text"
    mock_text.text = '{"intent": "test_text", "urgenza": "BASSA"}'
    mock_resp = MagicMock()
    mock_resp.content = [mock_text]
    mock_resp.usage.input_tokens = 50
    mock_resp.usage.output_tokens = 20
    mock_resp.stop_reason = "stop"

    with patch("anthropic.Anthropic") as mock_class:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_class.return_value = mock_client

        provider = ClaudeProvider(name="test", api_key_env="FAKE_TEST_KEY")
        resp = provider.complete(
            system="x", user="y", model="claude-haiku-4-5",
            json_schema={"type": "object"},
        )
        assert resp.parsed_json == {"intent": "test_text", "urgenza": "BASSA"}
        assert resp.raw_text == '{"intent": "test_text", "urgenza": "BASSA"}'


def test_complete_returns_error_on_exception(monkeypatch):
    """Eccezione dell'SDK → AiResponse con error, NON solleva."""
    monkeypatch.setenv("FAKE_TEST_KEY", "sk-ant-test-fake-12345")

    with patch("anthropic.Anthropic") as mock_class:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("connection refused")
        mock_class.return_value = mock_client

        provider = ClaudeProvider(name="test", api_key_env="FAKE_TEST_KEY")
        resp = provider.complete(
            system="x", user="y", model="claude-haiku-4-5",
        )
        assert resp.error is not None
        assert "connection refused" in resp.error
        assert resp.finish_reason == "error"
        assert resp.parsed_json is None


def test_list_available_models():
    """Catalogo modelli Claude esposti nel dropdown UI."""
    with patch.dict(os.environ, {"FAKE_KEY": "sk-test"}):
        with patch("anthropic.Anthropic"):
            p = ClaudeProvider(name="t", api_key_env="FAKE_KEY")
            models = p.list_available_models()
            assert "claude-haiku-4-5" in models
            assert "claude-sonnet-4-6" in models
            assert "claude-opus-4-7" in models
