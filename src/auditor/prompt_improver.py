"""
Prompt Improver — LLM-based оптимизатор промптов.

Реализует паттерн DVF (Draft → Verify → Fix) из лекции A2_Agents:
  - Draft  : prompt_builder строит начальный промпт (rule-based)
  - Verify : audit_stage проверяет сгенерированное изображение
  - Fix    : prompt_improver улучшает промпт на основе причин reject (LLM)

В отличие от prompt_builder (только rule-based hints),
prompt_improver использует TEXT_MODEL для генерации конкретных исправлений,
обогащённых историческими reject-паттернами из MemoryStore.
"""
from __future__ import annotations

import logging

from auditor import config
from auditor.audit_stage import AuditResult
from auditor.domain_spec import DomainConfig
from auditor.model_gateway import ModelGateway

logger = logging.getLogger(__name__)

# Fallback defaults (used only if domain.yaml is missing)
_DEFAULT_SYSTEM_MSG = (
    "You are an expert at writing image generation prompts. "
    "Your task: improve the given prompt based on audit failures. "
    "Return ONLY the improved prompt text, nothing else. No explanations, no JSON."
)

_DEFAULT_RULES = """
Rules for improvement:
1. Keep the overall scene description but fix the specific issues identified
2. If technical quality failed: add 'sharp focus, high detail, no blur'
3. If object integration failed: describe the object's exact position and size more explicitly
4. Keep prompt under 200 words
5. Do not add NSFW content
"""


class PromptImprover:
    """
    DVF Fix-шаг: использует LLM для улучшения промпта.

    Принимает: текущий промпт + результат аудита + исторические подсказки.
    Возвращает: улучшенный промпт (или текущий при ошибке — fail-safe).
    """

    def __init__(self, gateway: ModelGateway, domain: DomainConfig | None = None) -> None:
        self.gateway = gateway
        self._system_msg = (
            domain.improver_system_prompt if domain and domain.improver_system_prompt
            else _DEFAULT_SYSTEM_MSG
        )
        self._rules = (
            domain.improver_rules if domain and domain.improver_rules
            else _DEFAULT_RULES
        )

    async def improve(
        self,
        current_prompt: str,
        audit_result: AuditResult,
        memory_hints: list[str] | None = None,
    ) -> str:
        """
        Генерирует улучшенный промпт через LLM.

        При любой ошибке LLM возвращает current_prompt (fail-safe).
        """
        failures = self._extract_failures(audit_result)
        if not failures and not memory_hints:
            logger.info("[improver] No failures to address, returning current prompt unchanged")
            return current_prompt

        failures_text = "\n".join(f"- {f}" for f in failures) if failures else "none"
        history_text = (
            "\n".join(f"- {h}" for h in memory_hints)
            if memory_hints else "none"
        )

        user_msg = (
            f"Current prompt:\n{current_prompt}\n\n"
            f"Audit failures (why the generated image was rejected):\n{failures_text}\n\n"
            f"Historical reject patterns for this scene (from past runs):\n{history_text}\n\n"
            f"{self._rules}\n"
            "Improved prompt:"
        )

        try:
            improved = await self.gateway.chat(
                prompt=user_msg,
                system=self._system_msg,
                model=config.TEXT_MODEL,
                fallback=config.TEXT_FALLBACK,
                temperature=0.6,
            )
            improved = improved.strip()

            # Санитарная проверка: ответ должен быть содержательным
            if improved and len(improved) > 30:
                logger.info(f"[improver] Generated improved prompt ({len(improved)} chars)")
                return improved
            else:
                logger.warning(f"[improver] LLM returned too short response: {improved!r}")
                return current_prompt

        except Exception as e:
            logger.error(f"[improver] LLM error: {e}, keeping current prompt")
            return current_prompt

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _extract_failures(self, audit_result: AuditResult) -> list[str]:
        """Извлекает конкретные причины провала из AuditResult."""
        failures: list[str] = []
        for check in audit_result.checks:
            if check.hard_reject:
                for f in check.findings:
                    failures.append(f"CRITICAL [{check.check_id}]: {f}")
            elif not check.skipped and check.score < 0.6:
                findings_str = "; ".join(check.findings[:2]) if check.findings else "low score"
                failures.append(
                    f"[{check.check_id}] score={check.score:.2f}: {findings_str}"
                )
        return failures
