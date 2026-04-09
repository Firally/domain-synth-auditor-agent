"""
Decision Engine — rule-based агрегация скоров → ACCEPT / REJECT / NEEDS_REVIEW.

LLM НЕ принимает финальное решение. Только детерминированные правила и пороги.
Логика взята напрямую из audit_rubric.yaml → aggregation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from auditor.audit_stage import AuditResult
from auditor.domain_spec import DomainSpec


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

class Verdict(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass
class Decision:
    verdict: Verdict
    weighted_score: float
    scores: dict[str, float]          # check_id → score
    reasons: list[str]                # почему именно такое решение
    suggestions: list[str]            # подсказки для prompt improver


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class DecisionEngine:
    """
    Правила агрегации (из audit_rubric.yaml):

    ACCEPT:      weighted_score >= 0.72
             AND technical_quality >= 0.60
             AND domain_relevance  >= 0.65

    NEEDS_REVIEW: 0.50 <= weighted_score < 0.72
              OR  any single check in review zone

    REJECT:      weighted_score < 0.50
              OR any hard_reject
              OR technical_quality < 0.50
    """

    # Пороги финального решения
    ACCEPT_THRESHOLD = 0.72
    REVIEW_THRESHOLD = 0.50
    TQ_MIN_FOR_ACCEPT = 0.60   # technical_quality минимум для ACCEPT
    DR_MIN_FOR_ACCEPT = 0.65   # domain_relevance минимум для ACCEPT
    TQ_MIN_OVERALL = 0.50      # ниже → REJECT

    def __init__(self, spec: DomainSpec) -> None:
        self.spec = spec
        self._domain_suggestions = spec.domain.suggestions

    def decide(self, audit: AuditResult, has_object: bool = False) -> Decision:
        # --- Шаг 1: hard reject ---
        if audit.has_hard_reject:
            return Decision(
                verdict=Verdict.REJECT,
                weighted_score=0.0,
                scores={},
                reasons=["[HARD REJECT] " + r for r in audit.hard_reject_reasons],
                suggestions=["Safety/PII violation — image must be discarded"],
            )

        # --- Шаг 2: собираем скоры ---
        scores: dict[str, float] = {}
        for c in audit.checks:
            if not c.skipped:
                scores[c.check_id] = c.score

        # Базовые веса (из rubric)
        weights = {
            "prompt_adherence": 0.30,
            "domain_relevance": 0.30,
            "technical_quality": 0.20,
            "object_integration": 0.20,
        }

        # Если объекта нет — перераспределяем вес object_integration
        if not has_object or "object_integration" not in scores:
            extra = weights.pop("object_integration", 0.0)
            # Распределяем пропорционально оставшимся
            remaining = {k: v for k, v in weights.items() if k in scores}
            total_remaining = sum(remaining.values())
            for k in remaining:
                weights[k] += extra * (weights[k] / total_remaining)

        # Weighted score
        weighted = 0.0
        used_weight = 0.0
        for check_id, w in weights.items():
            if check_id in scores:
                weighted += scores[check_id] * w
                used_weight += w
        if used_weight > 0:
            weighted = weighted / used_weight  # нормализуем если не все проверки есть

        weighted = round(weighted, 4)

        # --- Шаг 3: решение ---
        tq = scores.get("technical_quality", 1.0)
        dr = scores.get("domain_relevance", 1.0)

        reasons: list[str] = []
        suggestions: list[str] = []

        # Собираем findings из всех проверок
        for c in audit.checks:
            for f in c.findings:
                reasons.append(f"[{c.check_id}] {f}")

        # REJECT?
        reject_conditions = []
        if weighted < self.REVIEW_THRESHOLD:
            reject_conditions.append(f"weighted_score={weighted:.3f} < {self.REVIEW_THRESHOLD}")
        if tq < self.TQ_MIN_OVERALL:
            reject_conditions.append(f"technical_quality={tq:.3f} < {self.TQ_MIN_OVERALL}")

        if reject_conditions:
            suggestions.extend(_suggest_from_scores(scores, self._domain_suggestions))
            return Decision(
                verdict=Verdict.REJECT,
                weighted_score=weighted,
                scores=scores,
                reasons=reasons + reject_conditions,
                suggestions=suggestions,
            )

        # ACCEPT?
        if (
            weighted >= self.ACCEPT_THRESHOLD
            and tq >= self.TQ_MIN_FOR_ACCEPT
            and dr >= self.DR_MIN_FOR_ACCEPT
        ):
            return Decision(
                verdict=Verdict.ACCEPT,
                weighted_score=weighted,
                scores=scores,
                reasons=reasons or ["All checks passed"],
                suggestions=[],
            )

        # NEEDS_REVIEW
        review_reasons = list(reasons)
        if weighted < self.ACCEPT_THRESHOLD:
            review_reasons.append(f"weighted_score={weighted:.3f} below accept threshold {self.ACCEPT_THRESHOLD}")
        if tq < self.TQ_MIN_FOR_ACCEPT:
            review_reasons.append(f"technical_quality={tq:.3f} below accept minimum {self.TQ_MIN_FOR_ACCEPT}")
        if dr < self.DR_MIN_FOR_ACCEPT:
            review_reasons.append(f"domain_relevance={dr:.3f} below accept minimum {self.DR_MIN_FOR_ACCEPT}")

        suggestions.extend(_suggest_from_scores(scores, self._domain_suggestions))
        return Decision(
            verdict=Verdict.NEEDS_REVIEW,
            weighted_score=weighted,
            scores=scores,
            reasons=review_reasons,
            suggestions=suggestions,
        )


# ---------------------------------------------------------------------------
# Suggestion generator
# ---------------------------------------------------------------------------

def _suggest_from_scores(
    scores: dict[str, float],
    domain_suggestions: dict[str, list[str]] | None = None,
) -> list[str]:
    """Генерирует подсказки для prompt improver на основе низких скоров.

    Использует suggestions из domain.yaml, если доступны.
    """
    ds = domain_suggestions or {}
    suggestions: list[str] = []

    thresholds = {
        "domain_relevance": 0.65,
        "technical_quality": 0.60,
        "prompt_adherence": 0.65,
        "object_integration": 0.60,
    }

    for check_id, threshold in thresholds.items():
        if scores.get(check_id, 1.0) < threshold:
            if check_id in ds:
                suggestions.extend(ds[check_id])
            else:
                suggestions.append(f"Improve {check_id} in prompt")

    return suggestions
