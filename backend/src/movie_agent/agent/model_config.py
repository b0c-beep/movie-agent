"""Environment-based model selection for inference and live smoke tests."""

from __future__ import annotations

import os

DEFAULT_MODEL_PROVIDER = "openrouter"
DEFAULT_MODEL_NAME = "openai/gpt-oss-120b"
MODEL_API_KEYS = {
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def configured_model_provider() -> str:
    provider = os.getenv("MODEL_PROVIDER", DEFAULT_MODEL_PROVIDER).strip().lower()
    if provider not in MODEL_API_KEYS:
        raise ValueError(f"MODEL_PROVIDER must be one of: {', '.join(MODEL_API_KEYS)}")
    return provider


def configured_model_name() -> str:
    name = os.getenv("MODEL_NAME", DEFAULT_MODEL_NAME).strip()
    if not name:
        raise ValueError("MODEL_NAME cannot be empty")
    return name


def configured_model() -> str:
    return f"{configured_model_provider()}:{configured_model_name()}"


def configured_model_api_key_name() -> str:
    return MODEL_API_KEYS[configured_model_provider()]
