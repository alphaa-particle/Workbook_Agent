"""LLM providers for Calux Book — Gemini, GLM, ZImage.

All providers implement the ``LLMProvider`` protocol for image generation
and text generation.
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger("calux_book.providers")


class LLMProvider(ABC):
    """Abstract base for LLM / image-generation providers."""

    @abstractmethod
    async def generate_image(
        self, model: str, prompt: str, user_id: str = "",
    ) -> str:
        """Generate an image and return the saved file path."""

    @abstractmethod
    async def generate_text_with_model(self, prompt: str, model: str) -> str:
        """Generate text using a specific model."""

    @abstractmethod
    async def generate_from_prompt(self, prompt: str) -> str:
        """Generate text from a single prompt using the default LLM."""


# ---------------------------------------------------------------------------
# OpenAI-compatible provider (also handles Ollama)
# ---------------------------------------------------------------------------

class OpenAIProvider(LLMProvider):
    """Uses the ``openai`` Python SDK (Apache-2.0 compatible)."""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = _normalize_openai_base_url(base_url)
        self.model = model

    async def generate_image(self, model: str, prompt: str, user_id: str = "") -> str:
        raise NotImplementedError("OpenAI provider does not support image generation directly")

    async def generate_text_with_model(self, prompt: str, model: str) -> str:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url or None)
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            timeout=300,
        )
        return resp.choices[0].message.content or ""

    async def generate_from_prompt(self, prompt: str) -> str:
        return await self.generate_text_with_model(prompt, self.model)


# ---------------------------------------------------------------------------
# Google Gemini provider
# ---------------------------------------------------------------------------

class GeminiProvider(LLMProvider):
    """Uses Google GenAI SDK (Apache-2.0) for image generation and text."""

    def __init__(self, google_api_key: str, text_provider: LLMProvider | None = None) -> None:
        self.api_key = google_api_key
        self._text_provider = text_provider

    async def generate_image(
        self, model: str, prompt: str, user_id: str = "",
    ) -> str:
        if not self.api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set")

        from google import genai

        client = genai.Client(api_key=self.api_key)

        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                logger.info("Generating image with Gemini model %s (attempt %d)", model, attempt)
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                )
                if not response.candidates or not response.candidates[0].content:
                    last_err = RuntimeError("No candidates generated")
                    continue

                image_data: bytes | None = None
                for part in response.candidates[0].content.parts:
                    if hasattr(part, "inline_data") and part.inline_data:
                        image_data = part.inline_data.data
                        break

                if not image_data:
                    last_err = RuntimeError("No image data in response")
                    continue

                filename = f"infograph_{time.time_ns()}.png"
                upload_dir = Path("./data/uploads") / user_id if user_id else Path("./data/uploads")
                upload_dir.mkdir(parents=True, exist_ok=True)
                filepath = upload_dir / filename
                filepath.write_bytes(image_data)
                logger.info("Infographic saved to %s", filepath)
                return str(filepath)

            except Exception as e:
                logger.error("Image generation attempt %d failed: %s", attempt, e)
                last_err = e
                if attempt < 3:
                    await _sleep(2)

        raise RuntimeError(f"Image generation failed after 3 attempts: {last_err}")

    async def generate_text_with_model(self, prompt: str, model: str) -> str:
        if not self.api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set")

        from google import genai

        client = genai.Client(api_key=self.api_key)
        response = client.models.generate_content(model=model, contents=prompt)

        if not response.candidates or not response.candidates[0].content:
            raise RuntimeError("No text generated")

        parts_text = []
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                parts_text.append(part.text)

        result = "".join(parts_text)
        if not result:
            raise RuntimeError("Empty response from model")
        return result

    async def generate_from_prompt(self, prompt: str) -> str:
        if self._text_provider:
            return await self._text_provider.generate_from_prompt(prompt)
        raise RuntimeError("No text provider configured for GeminiProvider")


# ---------------------------------------------------------------------------
# GLM Image provider (ZhipuAI)
# ---------------------------------------------------------------------------

class GLMImageProvider(LLMProvider):
    """GLM image generation via ZhipuAI API."""

    API_URL = "https://open.bigmodel.cn/api/paas/v4/images/generations"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def generate_image(
        self, model: str, prompt: str, user_id: str = "",
    ) -> str:
        if not self.api_key:
            raise RuntimeError("GLM_API_KEY is not set")

        token = self._generate_token()
        payload = {"model": model, "prompt": prompt, "size": "1280x1280"}

        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                self.API_URL,
                json=payload,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            result = resp.json()

        if result.get("error", {}).get("code"):
            raise RuntimeError(
                f"GLM API error ({result['error']['code']}): {result['error'].get('message', '')}"
            )

        data_list = result.get("data", [])
        if not data_list or not data_list[0].get("url"):
            raise RuntimeError("No image URL in response")

        image_url = data_list[0]["url"]
        return await self._download_and_save(image_url, user_id)

    async def generate_text_with_model(self, prompt: str, model: str) -> str:
        raise NotImplementedError("GLM Image client does not support text generation")

    async def generate_from_prompt(self, prompt: str) -> str:
        raise NotImplementedError("GLM Image client does not support text generation")

    def _generate_token(self) -> str:
        """Generate JWT from ``id.secret`` API key format."""
        import jose.jwt as jose_jwt

        parts = self.api_key.split(".")
        if len(parts) != 2:
            raise ValueError("Invalid GLM API key format (expected id.secret)")
        api_id, api_secret = parts
        now = int(time.time())
        claims = {"api_key": api_id, "exp": now + 3600, "timestamp": now}
        return jose_jwt.encode(claims, api_secret, algorithm="HS256")

    async def _download_and_save(self, url: str, user_id: str) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content

        filename = f"infograph_{time.time_ns()}.png"
        upload_dir = Path("./data/uploads") / user_id if user_id else Path("./data/uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        filepath = upload_dir / filename
        filepath.write_bytes(data)
        return str(filepath)


# ---------------------------------------------------------------------------
# Z-Image provider (Alibaba Tongyi Wanxiang)
# ---------------------------------------------------------------------------

class ZImageProvider(LLMProvider):
    """Alibaba Z-Image generation API."""

    API_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/image-generation/generation"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def generate_image(
        self, model: str, prompt: str, user_id: str = "",
    ) -> str:
        if not self.api_key:
            raise RuntimeError("ZIMAGE_API_KEY is not set")

        payload = {
            "model": model,
            "input": {"prompt": prompt},
            "parameters": {"size": "1280*1280"},
        }
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                self.API_URL,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            )
            result = resp.json()

        code = result.get("code", "")
        if code and code != "200":
            raise RuntimeError(f"Z-Image API error ({code}): {result.get('message', '')}")

        results_list = result.get("output", {}).get("results", [])
        if not results_list or not results_list[0].get("url"):
            raise RuntimeError("No image URL in response")

        image_url = results_list[0]["url"]
        return await self._download_and_save(image_url, user_id)

    async def generate_text_with_model(self, prompt: str, model: str) -> str:
        raise NotImplementedError("Z-Image client does not support text generation")

    async def generate_from_prompt(self, prompt: str) -> str:
        raise NotImplementedError("Z-Image client does not support text generation")

    async def _download_and_save(self, url: str, user_id: str) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content

        filename = f"infograph_{time.time_ns()}.png"
        upload_dir = Path("./data/uploads") / user_id if user_id else Path("./data/uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        filepath = upload_dir / filename
        filepath.write_bytes(data)
        return str(filepath)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_provider(cfg: Any) -> LLMProvider:
    """Create the appropriate LLMProvider based on config."""
    from .config import Settings
    assert isinstance(cfg, Settings)

    # Always create text provider (OpenAI-compatible)
    text_provider = OpenAIProvider(
        api_key=cfg.openai_api_key,
        base_url=cfg.base_url,
        model=cfg.openai_model if not cfg.is_ollama else cfg.ollama_model,
    )

    if cfg.image_provider == "glm":
        if not cfg.glm_api_key:
            raise ValueError("GLM_API_KEY is required when IMAGE_PROVIDER is 'glm'")
        return GLMImageProvider(cfg.glm_api_key)
    elif cfg.image_provider == "zimage":
        if not cfg.zimage_api_key:
            raise ValueError("ZIMAGE_API_KEY is required when IMAGE_PROVIDER is 'zimage'")
        return ZImageProvider(cfg.zimage_api_key)
    elif cfg.image_provider == "gemini":
        return GeminiProvider(cfg.google_api_key, text_provider)
    else:
        raise ValueError(
            f"Unknown image provider: {cfg.image_provider} (supported: gemini, glm, zimage)"
        )


def create_text_provider(cfg: Any) -> LLMProvider:
    """Create a text-only provider for chat/transformations."""
    from .config import Settings
    assert isinstance(cfg, Settings)
    return OpenAIProvider(
        api_key=cfg.openai_api_key,
        base_url=cfg.base_url,
        model=cfg.openai_model if not cfg.is_ollama else cfg.ollama_model,
    )


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


def _normalize_openai_base_url(base_url: str) -> str:
    """Normalize base URL for OpenAI-compatible chat completions.

    The OpenAI SDK targets ``/chat/completions`` relative to ``base_url``.
    Ollama and most OpenAI-compatible gateways expect ``/v1/chat/completions``,
    so a plain host URL (e.g. ``http://localhost:11434``) must be upgraded
    to include ``/v1``.
    """
    raw = (base_url or "").strip()
    if not raw:
        return ""

    parts = urlsplit(raw)
    path = (parts.path or "").rstrip("/")

    # Already points at a v1 API root.
    if path.endswith("/v1") or path == "/v1":
        return raw.rstrip("/")

    # Plain host/base path -> append /v1 for compatibility.
    if path in ("", "/"):
        return urlunsplit((parts.scheme, parts.netloc, "/v1", parts.query, parts.fragment))

    # Custom path provided by user; keep as-is.
    return raw.rstrip("/")
