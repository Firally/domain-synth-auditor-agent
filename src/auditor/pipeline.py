"""
Pipeline — оркестратор цикла generate → audit → decide → improve.

Не является LLM-агентом. Это детерминированный workflow,
где агентные шаги (VLM-аудит, prompt improvement) изолированы внутри узлов.

Паттерн DVF (Draft → Verify → Fix):
  Draft  : PromptBuilder строит промпт (rule-based + memory hints)
  Verify : AuditStage проверяет сгенерированное изображение (VLM + rules)
  Fix    : PromptImprover улучшает промпт на основе audit failures (LLM)

Memory между runs:
  - Читается в начале: load_recipe() → отправная точка для промпта
  - Записывается в конце: save_recipe() + update_global_stats()
  - reject_patterns накапливаются при каждом REJECT
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from auditor import config
from auditor.audit_stage import AuditResult, AuditStage
from auditor.config import MAX_ITERATIONS
from auditor.decision_engine import Decision, DecisionEngine, Verdict
from auditor.domain_spec import DomainSpec, GenerationTask
from auditor.experiment_store import ExperimentStore
from auditor.knowledge_base import KnowledgeBase
from auditor.memory_store import MemoryStore
from auditor.model_gateway import ModelGateway
from auditor.prompt_builder import PromptBuilder
from auditor.prompt_improver import PromptImprover

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Budget Guard
# ---------------------------------------------------------------------------

class BudgetExceeded(Exception):
    """Превышен бюджет на run."""


class BudgetTracker:
    """
    Трекер расходов per-run. Считает оценочную стоимость каждого вызова
    на основе config.COST_PER_CALL. При превышении BUDGET_PER_RUN
    бросает BudgetExceeded.
    """
    def __init__(self, limit: float = config.BUDGET_PER_RUN) -> None:
        self.limit = limit
        self.spent: float = 0.0
        self.calls: list[dict] = []

    def track(self, model: str, tool: str) -> None:
        cost = config.COST_PER_CALL.get(model, 0.0)
        self.spent += cost
        self.calls.append({"model": model, "tool": tool, "cost": cost})
        logger.info(f"[budget] +${cost:.4f} ({tool}) → total ${self.spent:.4f} / ${self.limit:.2f}")
        if self.spent > self.limit:
            raise BudgetExceeded(
                f"Budget exceeded: ${self.spent:.4f} > ${self.limit:.2f} limit. "
                f"Calls: {len(self.calls)}"
            )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    task: GenerationTask
    iterations: int
    final_verdict: str
    final_score: float
    final_image: bytes
    run_dir: str
    history: list[dict]      # summary каждой итерации
    memory_loaded: bool = False  # был ли загружен рецепт из memory
    budget_spent: float = 0.0   # потрачено USD за run


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class Pipeline:
    def __init__(
        self,
        gateway: ModelGateway,
        spec: DomainSpec,
        store: ExperimentStore | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        self.gateway = gateway
        self.spec = spec
        self.kb = KnowledgeBase(spec)
        self.builder = PromptBuilder(spec)
        self.store = store or ExperimentStore()
        self.auditor = AuditStage(gateway, spec, kb=self.kb, store=self.store)
        self.engine = DecisionEngine(spec)
        self.improver = PromptImprover(gateway, domain=spec.domain)
        self.memory = memory or MemoryStore()

    async def run(
        self, task: GenerationTask, *, preloaded_image: bytes | None = None
    ) -> PipelineResult:
        """
        Запускает полный цикл для одной задачи.

        preloaded_image: если передано — пропускаем генерацию (EVAL_MODE).
        """
        run_dir = self.store.start_run(task)
        history: list[dict] = []
        final_image = b""
        decision: Decision | None = None
        budget = BudgetTracker()

        max_iter = task.max_iterations or MAX_ITERATIONS

        # ------------------------------------------------------------------
        # Загружаем исторический контекст из memory
        # ------------------------------------------------------------------
        recipe = self.memory.load_recipe(task.scene_id, task.object_id)
        memory_hints = self.memory.get_reject_hints(task.scene_id, top_n=3)
        memory_loaded = recipe is not None

        # Начальный промпт: зависит от mode
        if task.mode == "edit":
            # В edit mode "промпт" = инструкция редактирования
            current_prompt = task.edit_instruction
            negative = ""
            logger.info(f"[pipeline] EDIT mode: instruction={current_prompt[:100]}...")
        elif recipe and recipe.get("best_prompt"):
            logger.info(f"[pipeline] Using recipe from memory (acceptance_rate={recipe.get('acceptance_rate', 0):.0%})")
            current_prompt, negative = self.builder.build(task, reject_reasons=[])
            # Берём лучший промпт из memory как отправную точку
            current_prompt = recipe["best_prompt"]
        else:
            current_prompt, negative = self.builder.build(task, reject_reasons=memory_hints)

        # ------------------------------------------------------------------
        # Основной цикл DVF
        # ------------------------------------------------------------------
        for i in range(1, max_iter + 1):
            logger.info(f"\n{'='*60}")
            logger.info(
                f"[pipeline] Iteration {i}/{max_iter} | "
                f"scene={task.scene_id} | obj={task.object_id}"
            )
            logger.info(f"[pipeline] Prompt (first 200 chars): {current_prompt[:200]}")

            # DRAFT: используем текущий промпт (или preloaded_image в EVAL_MODE)
            if preloaded_image and i == 1:
                image_bytes = preloaded_image
                final_image = image_bytes
                logger.info(f"[pipeline] Using preloaded image ({len(image_bytes)} bytes) — EVAL_MODE")
            elif config.EVAL_MODE and i > 1:
                # В EVAL_MODE нет смысла генерировать повторно — один проход
                logger.info("[pipeline] EVAL_MODE: skipping re-generation, ending loop")
                break
            else:
                try:
                    t0 = time.perf_counter()
                    if task.mode == "edit" and task.source_image:
                        image_bytes = await self.gateway.edit_image(
                            source_image=task.source_image,
                            instruction=current_prompt,
                        )
                        tool_name = "edit_image"
                    else:
                        image_bytes = await self.gateway.generate_image(current_prompt)
                        tool_name = "generate_image"
                    gen_latency = (time.perf_counter() - t0) * 1000
                    final_image = image_bytes
                    logger.info(f"[pipeline] Image {tool_name} ({len(image_bytes)} bytes)")
                    self.store.log_tool_call(
                        tool=tool_name,
                        model=config.IMAGE_GEN_MODEL,
                        check_id="draft",
                        latency_ms=gen_latency,
                    )
                    budget.track(config.IMAGE_GEN_MODEL, tool_name)
                except BudgetExceeded:
                    logger.warning("[pipeline] Budget exceeded during generation, stopping")
                    break
                except Exception as e:
                    logger.error(f"[pipeline] Image generation failed: {e}")
                    history.append({"iteration": i, "error": str(e)})
                    continue

            # VERIFY: аудит
            audit = await self.auditor.run(image_bytes, task)
            logger.info(f"[pipeline] Audit done. Hard reject: {audit.has_hard_reject}")

            # Решение
            decision = self.engine.decide(audit, has_object=bool(task.object_id))
            logger.info(
                f"[pipeline] Decision: {decision.verdict.value} "
                f"(score={decision.weighted_score:.3f})"
            )
            if decision.reasons:
                logger.info(f"[pipeline] Reasons: {decision.reasons[:3]}")

            # Сохраняем итерацию
            self.store.save_iteration(current_prompt, negative, image_bytes, audit, decision)

            history.append({
                "iteration": i,
                "verdict": decision.verdict.value,
                "score": decision.weighted_score,
                "scores": decision.scores,
                "reasons": decision.reasons[:5],
                "prompt_len": len(current_prompt),
            })

            # Накапливаем reject-паттерны в memory
            if decision.verdict != Verdict.ACCEPT:
                for reason in decision.reasons[:3]:
                    for check in audit.checks:
                        if check.findings and any(r in reason for r in check.findings):
                            self.memory.add_reject_pattern(
                                task.scene_id, check.check_id, reason
                            )
                            break

            # Стоп при ACCEPT
            if decision.verdict == Verdict.ACCEPT:
                logger.info(f"[pipeline] ACCEPTED on iteration {i}")
                break

            # FIX: улучшаем промпт через LLM (DVF Fix-шаг)
            if i < max_iter:
                logger.info("[pipeline] Improving prompt via LLM (DVF Fix)...")
                t0 = time.perf_counter()
                current_prompt = await self.improver.improve(
                    current_prompt=current_prompt,
                    audit_result=audit,
                    memory_hints=memory_hints,
                )
                fix_latency = (time.perf_counter() - t0) * 1000
                self.store.log_tool_call(
                    tool="prompt_improve",
                    model=config.TEXT_MODEL,
                    check_id="fix",
                    latency_ms=fix_latency,
                )
                budget.track(config.TEXT_MODEL, "prompt_improve")
                # Обновляем negative из builder (только в generate mode)
                if task.mode != "edit":
                    _, negative = self.builder.build(task, reject_reasons=[])

        # ------------------------------------------------------------------
        # Финал: обновляем memory
        # ------------------------------------------------------------------
        final_verdict = decision.verdict.value if decision else "ERROR"
        final_score = decision.weighted_score if decision else 0.0

        self.memory.save_recipe(
            scene_id=task.scene_id,
            object_id=task.object_id,
            prompt=current_prompt,
            iterations_used=len(history),
            verdict=final_verdict,
        )
        self.memory.update_global_stats(final_verdict)

        self.store.finish_run(final_verdict, len(history))
        logger.info(f"[pipeline] Budget spent: ${budget.spent:.4f} / ${budget.limit:.2f}")

        logger.info(f"[pipeline] Memory: {self.memory.summary()}")

        return PipelineResult(
            task=task,
            iterations=len(history),
            final_verdict=final_verdict,
            final_score=final_score,
            final_image=final_image,
            run_dir=str(run_dir),
            history=history,
            memory_loaded=memory_loaded,
            budget_spent=round(budget.spent, 6),
        )
