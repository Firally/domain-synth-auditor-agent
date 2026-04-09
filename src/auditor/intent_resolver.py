"""
Intent Resolver — LLM-агент для разбора запроса пользователя.

Анализирует текстовый запрос и определяет:
- Какой object_type из БД нужен
- Сколько изображений запрашивается
- Какую edit-инструкцию применить

Это агентный шаг: LLM принимает решение на основе
списка доступных типов объектов из MySQL.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from auditor.db_loader import ObjectType
from auditor.model_gateway import ModelGateway

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a task planner for an image processing pipeline.
Your job is to analyze a user's natural language request and extract structured parameters.

You have access to a database of images organized by object types.
The user will describe what they want to do with images, and you need to figure out:
1. Which object type from the database matches their request
2. How many images they want
3. What editing instruction to apply to the images

Respond ONLY with valid JSON, no markdown, no explanation."""

_USER_PROMPT_TEMPLATE = """\
User request: {request}

Available object types in the database:
{types_list}

Respond with JSON:
{{
  "object_type": "<exact name from the list above, or null if none match>",
  "count": <number of images the user wants, default 10 if not specified>,
  "edit_instruction": "<clear instruction for image editing based on user's request, in English>"
}}"""


@dataclass
class ResolvedIntent:
    """Результат разбора запроса пользователя."""
    object_type: ObjectType | None
    count: int
    edit_instruction: str
    raw_request: str


class IntentResolver:
    """LLM-агент: разбирает запрос пользователя → object_type + count + instruction."""

    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway

    async def resolve(
        self,
        user_request: str,
        available_types: list[ObjectType],
    ) -> ResolvedIntent:
        """
        Анализирует запрос пользователя через LLM.

        Args:
            user_request: текст запроса на любом языке
            available_types: список типов объектов из БД

        Returns:
            ResolvedIntent с определённым типом, количеством и инструкцией
        """
        types_list = self._format_types(available_types)

        prompt = _USER_PROMPT_TEMPLATE.format(
            request=user_request,
            types_list=types_list,
        )

        logger.info(f"[intent] Resolving intent for: {user_request[:100]}")

        response = await self.gateway.chat(
            prompt,
            system=_SYSTEM_PROMPT,
            temperature=0.1,
        )

        return self._parse_response(response, available_types, user_request)

    @staticmethod
    def _format_types(types: list[ObjectType]) -> str:
        """Форматирует список типов для LLM."""
        lines = []
        for t in types:
            desc = f" — {t.description}" if t.description else ""
            lines.append(f"- {t.object_type}{desc}")
        return "\n".join(lines) if lines else "(no object types in database)"

    @staticmethod
    def _parse_response(
        response: str,
        available_types: list[ObjectType],
        raw_request: str,
    ) -> ResolvedIntent:
        """Парсит JSON-ответ LLM."""
        # Извлекаем JSON из ответа (LLM может обернуть в ```json...```)
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"[intent] Failed to parse LLM response as JSON: {text[:200]}")
            return ResolvedIntent(
                object_type=None,
                count=10,
                edit_instruction="",
                raw_request=raw_request,
            )

        # Найти object_type по имени
        type_name = data.get("object_type")
        matched_type = None
        if type_name:
            type_name_lower = type_name.lower()
            for t in available_types:
                if t.object_type.lower() == type_name_lower:
                    matched_type = t
                    break

        count = data.get("count", 10)
        if not isinstance(count, int) or count < 1:
            count = 10

        edit_instruction = data.get("edit_instruction", "")
        if not isinstance(edit_instruction, str):
            edit_instruction = ""

        intent = ResolvedIntent(
            object_type=matched_type,
            count=count,
            edit_instruction=edit_instruction,
            raw_request=raw_request,
        )

        if matched_type:
            logger.info(
                f"[intent] Resolved: type={matched_type.object_type} (id={matched_type.id}), "
                f"count={count}, instruction={edit_instruction[:80]}"
            )
        else:
            logger.warning(f"[intent] Could not match object_type={type_name!r} to database")

        return intent
