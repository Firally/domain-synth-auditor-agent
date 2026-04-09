"""
Knowledge Base — явный retrieval из доменной базы знаний.

Отличие от DomainSpec (который просто хранит всё):
  KB *выбирает* релевантный контекст под конкретную задачу.

Используется в audit_stage для обогащения VLM-промптов:
  - zone-специфичные правила и пороги из audit_rubric.yaml
  - описания похожих reference-изображений как few-shot контекст
  - конкретные WB-маркеры, характерные для данной зоны

Это компонент, закрывающий требование курса «внешняя база знаний».
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from auditor import config
from auditor.domain_spec import DomainConfig, DomainSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes для retrieval-ответов
# ---------------------------------------------------------------------------

@dataclass
class ReferenceExample:
    """Аннотация одного референсного изображения из базы знаний."""
    file: str
    zone: str
    camera_view: str
    domain_markers: list[str]       # generic: wb_markers, damage_markers, etc.
    must_have_elements: list[str]
    quality_rating: int             # 1–5
    people_visible: bool
    good_identity: bool             # generic: good_wb_identity, good_damage_visibility, etc.
    optional_elements: list[str] = field(default_factory=list)


@dataclass
class SceneContext:
    """Контекст, возвращаемый KB для конкретной сцены."""
    scene_id: str
    zone: str
    must_have: list[str]
    forbidden: list[str]
    similar_references: list[ReferenceExample]   # топ-N референсов
    reference_prompt_hint: str                   # готовая строка для VLM


@dataclass
class AuditRuleContext:
    """Правила для конкретного audit check применительно к данной сцене."""
    check_id: str
    accept_threshold: float
    review_threshold: float
    scene_specific_hints: list[str]   # подсказки на основе reference данных


# ---------------------------------------------------------------------------
# Knowledge Base
# ---------------------------------------------------------------------------

class KnowledgeBase:
    """
    Компонент явного retrieval из доменной базы знаний.

    Хранит: reference_annotations.yaml (аннотации 12 реальных фото ПВЗ).
    Предоставляет: контекст и правила под конкретную задачу.
    """

    def __init__(self, spec: DomainSpec, docs_dir: Path | str | None = None) -> None:
        self.spec = spec
        self.dc: DomainConfig = spec.domain
        self.docs_dir = Path(docs_dir) if docs_dir else config.PROJECT_DIR
        self._markers_field = self.dc.kb_markers_field       # e.g. "domain_markers"
        self._identity_field = self.dc.kb_identity_field     # e.g. "good_domain_identity"
        self._reference_label = self.dc.kb_reference_label   # e.g. "reference examples"
        self._references: list[ReferenceExample] = self._load_references()
        logger.info(
            f"[kb] Loaded {len(self._references)} reference annotations "
            f"({sum(1 for r in self._references if r.good_identity and not r.people_visible)} high-quality)"
        )

    # ------------------------------------------------------------------
    # Загрузка данных
    # ------------------------------------------------------------------

    def _load_references(self) -> list[ReferenceExample]:
        """Загружает и парсит reference_annotations.yaml."""
        path = self.docs_dir / "reference_annotations.yaml"
        if not path.exists():
            logger.warning("[kb] reference_annotations.yaml not found")
            return []

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        refs: list[ReferenceExample] = []
        for r in data.get("references", []):
            notes_dict = _parse_audit_notes(r.get("audit_notes", []))

            quality_raw = str(notes_dict.get("quality_rating", "3/5"))
            quality = int(quality_raw.split("/")[0]) if "/" in quality_raw else 3

            people = str(notes_dict.get("people_visible", "false")).lower() == "true"
            identity_raw = str(notes_dict.get(self._identity_field, "true")).lower()
            good_id = identity_raw not in ("false", "weak")

            refs.append(ReferenceExample(
                file=r.get("file", ""),
                zone=r.get("zone", ""),
                camera_view=r.get("camera_view", ""),
                domain_markers=r.get(self._markers_field, []),
                must_have_elements=r.get("must_have_elements", []),
                quality_rating=quality,
                people_visible=people,
                good_identity=good_id,
                optional_elements=r.get("optional_elements", []),
            ))

        return refs

    # ------------------------------------------------------------------
    # Retrieval — основные публичные методы
    # ------------------------------------------------------------------

    def retrieve_scene_context(self, scene_id: str) -> SceneContext:
        """
        Основной retrieval: возвращает контекст для сцены.

        Включает: must_have, forbidden, похожие референсы, готовую строку-подсказку.
        Используется в audit_stage для обогащения VLM-промптов.
        """
        scene = self.spec.get_scene(scene_id)
        refs = self.find_similar_references(scene_id, n=3)
        hint = self.format_references_for_prompt(scene_id, n=2)

        return SceneContext(
            scene_id=scene_id,
            zone=scene.zone,
            must_have=scene.must_have,
            forbidden=scene.forbidden,
            similar_references=refs,
            reference_prompt_hint=hint,
        )

    def retrieve_audit_rules(self, check_id: str, scene_id: str) -> AuditRuleContext:
        """
        Возвращает правила для audit check + scene-специфичные подсказки.

        Обогащает стандартные пороги из rubric данными из reference аннотаций.
        """
        try:
            check = self.spec.rubric.get(check_id)
            accept_t = check.thresholds.accept
            review_t = check.thresholds.review
        except KeyError:
            accept_t, review_t = 0.75, 0.50

        refs = self.find_similar_references(scene_id, n=2)
        hints: list[str] = []

        if check_id == "domain_relevance":
            # Какие маркеры характерны для этой зоны?
            markers: set[str] = set()
            for ref in refs:
                markers.update(ref.domain_markers[:3])
            if markers:
                hints.append(
                    f"Expected domain markers for this zone: {', '.join(sorted(markers)[:5])}"
                )

        elif check_id == "prompt_adherence":
            # Что должно быть в кадре по reference?
            try:
                scene = self.spec.get_scene(scene_id)
                combined: set[str] = set(scene.must_have)
            except KeyError:
                combined = set()
            for ref in refs:
                combined.update(ref.must_have_elements[:3])
            if combined:
                hints.append(f"Key scene elements: {', '.join(sorted(combined)[:6])}")

        elif check_id == "technical_quality":
            if refs:
                avg_q = sum(r.quality_rating for r in refs) / len(refs)
                hints.append(f"Reference quality benchmark for this zone: {avg_q:.1f}/5")

        return AuditRuleContext(
            check_id=check_id,
            accept_threshold=accept_t,
            review_threshold=review_t,
            scene_specific_hints=hints,
        )

    def find_similar_references(self, scene_id: str, n: int = 3) -> list[ReferenceExample]:
        """
        Находит N наиболее похожих reference-изображений.

        Критерии ранжирования: совпадение zone > camera_view > quality > good_wb_identity.
        Исключает изображения с людьми (нельзя использовать как positive пример).
        """
        try:
            scene = self.spec.get_scene(scene_id)
            zone = scene.zone
            camera_view = scene.camera_view
        except KeyError:
            return []

        def _score(ref: ReferenceExample) -> float:
            if ref.people_visible:
                return -1.0
            s = 0.0
            if ref.zone == zone or zone in ref.zone or ref.zone in zone:
                s += 3.0
            if camera_view in ref.camera_view or ref.camera_view in camera_view:
                s += 1.0
            s += ref.quality_rating * 0.4
            if ref.good_identity:
                s += 1.0
            return s

        ranked = sorted(self._references, key=_score, reverse=True)
        return [r for r in ranked if _score(r) > 0][:n]

    def format_references_for_prompt(self, scene_id: str, n: int = 2) -> str:
        """
        Форматирует описания reference-изображений для VLM-промпта.

        Даёт модели few-shot контекст: «вот как выглядит хороший WB ПВЗ».
        """
        refs = self.find_similar_references(scene_id, n=n)
        if not refs:
            return ""

        lines = [f"Reference examples of good {self._reference_label}:"]
        for i, ref in enumerate(refs, 1):
            markers = ", ".join(ref.domain_markers[:3]) if ref.domain_markers else "domain markers"
            must = ", ".join(ref.must_have_elements[:3]) if ref.must_have_elements else "standard elements"
            lines.append(
                f"  Example {i} ({ref.zone} zone, {ref.camera_view} view, quality {ref.quality_rating}/5): "
                f"has {markers}. Must-have elements: {must}."
            )
        return "\n".join(lines)

    def summary(self) -> str:
        total = len(self._references)
        good = sum(1 for r in self._references if r.good_identity and not r.people_visible)
        return f"KnowledgeBase: {total} references ({good} high-quality, {total - good} flagged)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_audit_notes(notes_raw: list) -> dict:
    """Парсит audit_notes из YAML (может быть list[str] или list[dict])."""
    result: dict = {}
    for item in notes_raw:
        if isinstance(item, dict):
            result.update(item)
        elif isinstance(item, str) and ":" in item:
            k, _, v = item.partition(":")
            result[k.strip()] = v.strip()
    return result
