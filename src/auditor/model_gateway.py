"""
Model Gateway — единая точка вызова моделей через OpenRouter.

Поддерживает:
  - chat()         : текстовые задачи (prompt improvement, structured output)
  - vision()       : VLM-аудит (изображение + вопросы → ответы)
  - generate_image(): генерация изображений через Gemini image model
  - fallback       : при 429/503/400 автоматически повторяет с fallback-моделью
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Optional

import httpx
from openai import AsyncOpenAI, APIStatusError

from auditor import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=config.OPENROUTER_API_KEY,
        base_url=config.OPENROUTER_BASE_URL,
        default_headers=config.OPENROUTER_HEADERS,
    )


def _image_to_data_url(image_bytes: bytes, mime: str = "image/png") -> str:
    b64 = base64.b64encode(image_bytes).decode()
    return f"data:{mime};base64,{b64}"


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class ModelGateway:
    """Единая точка вызова всех моделей через OpenRouter."""

    def __init__(self) -> None:
        self._client = _make_client()

    # ------------------------------------------------------------------
    # Chat (текст → текст)
    # ------------------------------------------------------------------
    async def chat(
        self,
        prompt: str,
        *,
        model: str = config.TEXT_MODEL,
        fallback: str = config.TEXT_FALLBACK,
        system: str | None = None,
        temperature: float = 0.7,
    ) -> str:
        """Текстовый запрос к LLM. Возвращает content ответа."""
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        return await self._chat_with_fallback(
            messages=messages,
            model=model,
            fallback=fallback,
            temperature=temperature,
        )

    # ------------------------------------------------------------------
    # Vision (изображение + вопросы → текст)
    # ------------------------------------------------------------------
    async def vision(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        model: str = config.VISION_MODEL,
        fallback: str = config.VISION_FALLBACK,
        fallback2: str = config.VISION_FALLBACK2,
        mime: str = "image/png",
        temperature: float = 0.2,
    ) -> str:
        """
        VLM-запрос: изображение + текстовый prompt → текстовый ответ.
        Изображение передаётся как data-URL (base64).
        Три варианта модели: primary → fallback → fallback2.
        """
        data_url = _image_to_data_url(image_bytes, mime)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        return await self._chat_with_fallback(
            messages=messages,
            model=model,
            fallback=fallback,
            fallback2=fallback2,
            temperature=temperature,
        )

    # ------------------------------------------------------------------
    # Image generation
    # ------------------------------------------------------------------
    async def generate_image(
        self,
        prompt: str,
        *,
        model: str = config.IMAGE_GEN_MODEL,
        fallback: str = config.IMAGE_GEN_FALLBACK,
        size: str = config.IMAGE_SIZE,
    ) -> bytes:
        """
        Генерация изображения через OpenRouter (Gemini image models).
        Возвращает bytes PNG/JPEG.
        """
        messages = [{"role": "user", "content": prompt}]
        return await self._image_request(messages, model=model, fallback=fallback, tag="image_gen")

    async def edit_image(
        self,
        source_image: bytes,
        instruction: str,
        *,
        model: str = config.IMAGE_GEN_MODEL,
        fallback: str = config.IMAGE_GEN_FALLBACK,
        mime: str = "image/jpeg",
    ) -> bytes:
        """
        Редактирование изображения по инструкции (Gemini image editing).

        Отправляет исходное изображение + текстовую инструкцию,
        получает отредактированное изображение.
        """
        data_url = _image_to_data_url(source_image, mime)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": instruction},
                ],
            }
        ]
        return await self._image_request(messages, model=model, fallback=fallback, tag="image_edit")

    # ------------------------------------------------------------------
    # Shared image request logic (generate + edit)
    # ------------------------------------------------------------------
    async def _image_request(
        self,
        messages: list[dict],
        *,
        model: str,
        fallback: str,
        tag: str = "image",
    ) -> bytes:
        """Общая логика для generate_image и edit_image с fallback."""
        for attempt, current_model in enumerate([model, fallback]):
            try:
                logger.info(f"[{tag}] model={current_model}, attempt={attempt+1}")
                result = await self._client.chat.completions.create(
                    model=current_model,
                    messages=messages,
                )
                image_bytes = await self._parse_image_response(result)
                if image_bytes:
                    return image_bytes

                raise ValueError(
                    f"No image found in response from {current_model}. "
                    f"Response choices: {[str(c.message.content)[:100] for c in result.choices]}"
                )

            except APIStatusError as e:
                if e.status_code in _RETRYABLE_STATUS and attempt == 0:
                    logger.warning(f"[{tag}] {e.status_code} from {current_model}, trying fallback")
                    continue
                raise
            except Exception as e:
                if attempt == 0:
                    logger.warning(f"[{tag}] error from {current_model}: {e}, trying fallback")
                    continue
                raise

        raise RuntimeError(f"{tag} failed on both primary and fallback models")

    async def _parse_image_response(self, result) -> bytes | None:
        """Извлекает bytes изображения из ответа модели."""
        for choice in result.choices:
            msg = choice.message
            # Случай 1: content — список частей
            if isinstance(msg.content, list):
                for part in msg.content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part["image_url"]["url"]
                        return await self._fetch_image(url)
            # Случай 2: content — data-url строка
            if isinstance(msg.content, str) and msg.content.startswith("data:image"):
                _, b64data = msg.content.split(",", 1)
                return base64.b64decode(b64data)

        # Случай 3: изображение в extra полях ответа (некоторые модели)
        raw = result.model_dump()
        return _extract_image_from_raw(raw)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _chat_with_fallback(
        self,
        messages: list[dict],
        model: str,
        fallback: str,
        fallback2: str | None = None,
        temperature: float = 0.7,
    ) -> str:
        candidates = [m for m in [model, fallback, fallback2] if m]
        for attempt, current_model in enumerate(candidates):
            try:
                logger.info(f"[chat] model={current_model}, attempt={attempt+1}")
                resp = await self._client.chat.completions.create(
                    model=current_model,
                    messages=messages,
                    temperature=temperature,
                )
                content = resp.choices[0].message.content or ""
                return content

            except APIStatusError as e:
                if e.status_code in _RETRYABLE_STATUS and attempt < len(candidates) - 1:
                    logger.warning(f"[chat] {e.status_code} from {current_model}, trying next fallback")
                    continue
                raise

        raise RuntimeError("Chat failed on all models")

    async def _fetch_image(self, url: str) -> bytes:
        """Скачивает изображение по URL (поддерживает data: и https:)."""
        if url.startswith("data:"):
            _, b64data = url.split(",", 1)
            return base64.b64decode(b64data)
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.content


def _extract_image_from_raw(raw: dict) -> bytes | None:
    """
    Рекурсивно ищет image_url или data-url в сыром dict-ответе модели.
    Некоторые модели возвращают изображение в нестандартных полях.
    """
    def _search(obj: Any) -> bytes | None:
        if isinstance(obj, str):
            if obj.startswith("data:image"):
                try:
                    _, b64data = obj.split(",", 1)
                    return base64.b64decode(b64data)
                except Exception:
                    pass
        elif isinstance(obj, dict):
            # Явные image_url поля
            if obj.get("type") == "image_url":
                url = obj.get("image_url", {}).get("url", "")
                if url.startswith("data:image"):
                    try:
                        _, b64data = url.split(",", 1)
                        return base64.b64decode(b64data)
                    except Exception:
                        pass
            for v in obj.values():
                result = _search(v)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = _search(item)
                if result:
                    return result
        return None

    return _search(raw)
