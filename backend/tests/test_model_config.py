from __future__ import annotations

import pytest

from movie_agent.agent.model_config import (
    configured_model,
    configured_model_api_key_name,
    configured_model_name,
    configured_model_provider,
)


def test_openrouter_is_the_default_provider_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)

    assert configured_model_provider() == "openrouter"
    assert configured_model_name() == "openai/gpt-oss-20b"
    assert configured_model() == "openrouter:openai/gpt-oss-20b"
    assert configured_model_api_key_name() == "OPENROUTER_API_KEY"


def test_groq_remains_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "groq")
    monkeypatch.setenv("MODEL_NAME", "openai/gpt-oss-20b")

    assert configured_model() == "groq:openai/gpt-oss-20b"
    assert configured_model_api_key_name() == "GROQ_API_KEY"


def test_model_configuration_rejects_unknown_or_empty_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="MODEL_PROVIDER"):
        configured_model_provider()

    monkeypatch.setenv("MODEL_PROVIDER", "openrouter")
    monkeypatch.setenv("MODEL_NAME", " ")
    with pytest.raises(ValueError, match="MODEL_NAME"):
        configured_model_name()
