"""Tests for provider URL normalization and Ollama/OpenAI routing behavior."""

from __future__ import annotations

from calux_book.providers import OpenAIProvider, _normalize_openai_base_url


def test_normalize_ollama_host_adds_v1():
    assert _normalize_openai_base_url("http://localhost:11434") == "http://localhost:11434/v1"


def test_normalize_existing_v1_unchanged():
    assert _normalize_openai_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"


def test_normalize_plain_openai_host_adds_v1():
    assert _normalize_openai_base_url("https://api.openai.com") == "https://api.openai.com/v1"


def test_normalize_custom_path_kept():
    assert _normalize_openai_base_url("https://gateway.example.com/openai/v2") == "https://gateway.example.com/openai/v2"


def test_openai_provider_uses_normalized_base_url():
    provider = OpenAIProvider(api_key="", base_url="http://localhost:11434", model="qwen2.5:7b")
    assert provider.base_url == "http://localhost:11434/v1"
