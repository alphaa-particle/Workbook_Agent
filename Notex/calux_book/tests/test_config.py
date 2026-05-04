"""Tests for calux_book.config — Settings loading and validation."""

from __future__ import annotations

import os

import pytest


class TestSettings:
    """Test Settings construction and properties."""

    def test_defaults(self, settings):
        assert settings.server_host == "0.0.0.0"
        assert settings.server_port == 8080
        assert settings.chunk_size == 50
        assert settings.chunk_overlap == 10
        assert settings.jwt_secret == "test-secret-key-for-unit-tests"

    def test_is_ollama_without_openai_key(self, settings):
        assert settings.is_ollama is True

    def test_is_ollama_with_openai_key(self, monkeypatch):
        from calux_book.config import Settings

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("OLLAMA_BASE_URL", "")
        s = Settings()
        assert s.is_ollama is False

    def test_base_url_ollama(self, settings):
        assert "11434" in settings.base_url

    def test_base_url_openai(self, monkeypatch):
        from calux_book.config import Settings

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        s = Settings()
        assert s.base_url == "https://api.openai.com/v1"

    def test_get_image_model_gemini(self, settings):
        settings.image_provider = "gemini"
        assert settings.get_image_model() == settings.gemini_image_model

    def test_get_image_model_glm(self, settings):
        settings.image_provider = "glm"
        assert settings.get_image_model() == settings.glm_image_model

    def test_get_image_model_zimage(self, settings):
        settings.image_provider = "zimage"
        assert settings.get_image_model() == settings.zimage_model


class TestValidateSettings:
    """Test validate_settings() function."""

    def test_valid_ollama_config(self, settings):
        from calux_book.config import validate_settings

        # Should not raise — ollama_base_url is set
        validate_settings(settings)

    def test_invalid_no_provider(self, monkeypatch):
        from calux_book.config import Settings, validate_settings

        monkeypatch.setenv("OPENAI_API_KEY", "")
        monkeypatch.setenv("OLLAMA_BASE_URL", "")
        monkeypatch.setenv("OPENAI_BASE_URL", "")
        s = Settings()
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            validate_settings(s)
