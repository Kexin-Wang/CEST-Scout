"""LLM_PRESET picks one of the three evaluated open-weight models (or OpenAI); explicit variables win."""
from __future__ import annotations

import pytest

from cest_scout import config

_KEYS = ("LLM_PRESET", "OPENAI_BASE_URL", "LLM_DISABLE_THINKING", "OPENAI_MODEL_LITERATURE", "OPENAI_MODEL_CHEMIST",
         "OPENAI_MODEL_PHYSICIST", "OPENAI_MODEL_EXPERIMENTALIST")


def _env(monkeypatch, **values):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "EMPTY")
    monkeypatch.setenv("NCBI_EMAIL", "someone@example.com")
    for k, v in values.items():
        monkeypatch.setenv(k, v)


def test_local_preset_sets_every_agent_model_and_turns_thinking_off(monkeypatch):
    _env(monkeypatch, LLM_PRESET="qwen3.8-27b", OPENAI_BASE_URL="http://localhost:8000/v1",
         OPENAI_MODEL_PHYSICIST="deepseek-v4-flash")
    s = config.load_settings()
    assert (s.model_literature, s.model_chemist, s.model_experimentalist) == ("qwen3.8-27b",) * 3
    assert s.model_physicist == "deepseek-v4-flash" and s.disable_thinking


def test_openai_stays_the_default_without_a_preset(monkeypatch):
    _env(monkeypatch)
    s = config.load_settings()
    assert s.model_literature == "gpt-5.5" and not s.disable_thinking


def test_unknown_preset_or_missing_endpoint_is_refused(monkeypatch):
    _env(monkeypatch, LLM_PRESET="llama-4")
    with pytest.raises(RuntimeError, match="deepseek-v4-flash"):
        config.load_settings()
    _env(monkeypatch, LLM_PRESET="deepseek-v4-flash")
    with pytest.raises(RuntimeError, match="OPENAI_BASE_URL"):
        config.load_settings()
