"""
Domain Spec — загрузка YAML-спеков в Pydantic-модели.

Даёт типизированный доступ к:
  - DomainConfig  : domain.yaml — все domain-специфичные строки
  - SceneCatalog  : канонические сцены
  - ObjectCatalog : объекты для добавления в сцену
  - AuditRubric   : правила и пороги аудита
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from auditor import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scene Catalog
# ---------------------------------------------------------------------------

class Scene(BaseModel):
    id: str
    name: str
    zone: str
    camera_view: str
    description: str = ""
    must_have: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    floor_variants: list[str] = Field(default_factory=list)
    ceiling_variants: list[str] = Field(default_factory=list)
    reference_images: list[str] = Field(default_factory=list)


class SceneCatalog(BaseModel):
    scenes: list[Scene]

    def get(self, scene_id: str) -> Scene:
        for s in self.scenes:
            if s.id == scene_id:
                return s
        raise KeyError(f"Scene not found: {scene_id!r}")

    def ids(self) -> list[str]:
        return [s.id for s in self.scenes]


# ---------------------------------------------------------------------------
# Object Catalog
# ---------------------------------------------------------------------------

class DomainObject(BaseModel):
    id: str
    name: str
    category: str
    description: str = ""
    allowed_zones: list[str] = Field(default_factory=list)
    placement: str = ""
    size_hint: str = ""
    color_palette: list[str] = Field(default_factory=list)
    reference_seen_in: list[str] = Field(default_factory=list)


class ObjectCatalog(BaseModel):
    objects: list[DomainObject]

    def get(self, object_id: str) -> DomainObject:
        for o in self.objects:
            if o.id == object_id:
                return o
        raise KeyError(f"Object not found: {object_id!r}")

    def ids(self) -> list[str]:
        return [o.id for o in self.objects]

    def for_zone(self, zone: str) -> list[DomainObject]:
        return [o for o in self.objects if zone in o.allowed_zones]


# ---------------------------------------------------------------------------
# Audit Rubric
# ---------------------------------------------------------------------------

class AuditThresholds(BaseModel):
    accept: float = 0.75
    review: float = 0.50


class AuditCheck(BaseModel):
    id: str
    name: str
    type: Literal["vlm", "rule_based", "hybrid"]
    description: str = ""
    thresholds: AuditThresholds = Field(default_factory=AuditThresholds)
    # raw data для специфических полей (hard_reject_rules, vlm_questions, etc.)
    raw: dict[str, Any] = Field(default_factory=dict)


class AggregationWeights(BaseModel):
    prompt_adherence: float = 0.30
    domain_relevance: float = 0.30
    technical_quality: float = 0.20
    object_integration: float = 0.20


class AuditRubric(BaseModel):
    checks: list[AuditCheck]
    weights: AggregationWeights = Field(default_factory=AggregationWeights)

    def get(self, check_id: str) -> AuditCheck:
        for c in self.checks:
            if c.id == check_id:
                return c
        raise KeyError(f"Check not found: {check_id!r}")


# ---------------------------------------------------------------------------
# DomainConfig — domain.yaml (все domain-специфичные строки)
# ---------------------------------------------------------------------------

class ReasonHint(BaseModel):
    triggers: list[str]
    hint: str


class RelevanceQuestion(BaseModel):
    key: str
    label: str = ""
    weight: float = 0.25
    extra: str = ""


class DomainConfig:
    """
    Загружает domain.yaml — все domain-специфичные строки,
    которые раньше были hardcoded в Python.

    Позволяет переключать домен без изменения кода.
    """

    def __init__(self, raw: dict) -> None:
        self._raw = raw
        self.domain_id: str = raw.get("domain_id", "unknown")
        self.name: str = raw.get("name", "Unknown Domain")
        self.description: str = raw.get("description", "")

        # Pipeline mode: "generate" (text→image) или "edit" (image+instruction→image)
        self.pipeline_mode: str = raw.get("pipeline_mode", "generate")
        self.edit_instruction: str = raw.get("edit_instruction", "").strip()

        # Prompt builder
        p = raw.get("prompt", {})
        self.style: str = p.get("style", "").strip()
        self.scene_intro: str = p.get("scene_intro", "").strip()
        self.negative_prompt: str = p.get("negative", "").strip()
        self.zone_descriptions: dict[str, str] = p.get("zone_descriptions", {})
        self.camera_views: dict[str, str] = p.get("camera_views", {})
        self.reason_hints: list[ReasonHint] = [
            ReasonHint(**h) for h in p.get("reason_hints", [])
        ]

        # Prompt improver
        imp = raw.get("improver", {})
        self.improver_system_prompt: str = imp.get("system_prompt", "").strip()
        self.improver_rules: str = imp.get("rules", "").strip()

        # Domain relevance check
        rel = raw.get("relevance_check", {})
        self.relevance_intro: str = rel.get("intro", "").strip()
        self.relevance_questions: list[RelevanceQuestion] = [
            RelevanceQuestion(**q) for q in rel.get("questions", [])
        ]
        self.relevance_low_findings: dict[str, str] = rel.get("low_score_findings", {})

        # Safety
        safety = raw.get("safety", {})
        self.safety_brand_field: str = safety.get("brand_field", "no_foreign_brands")
        self.safety_brand_description: str = safety.get(
            "brand_description",
            "are there competitor or unrelated brand logos?",
        )

        # Suggestions
        self.suggestions: dict[str, list[str]] = raw.get("suggestions", {})

        # Knowledge base
        kb = raw.get("knowledge_base", {})
        self.kb_markers_field: str = kb.get("markers_field", "wb_markers")
        self.kb_identity_field: str = kb.get("identity_field", "good_wb_identity")
        self.kb_reference_label: str = kb.get("reference_label", "reference images")

    @classmethod
    def load(cls, project_dir: Path | str | None = None) -> "DomainConfig":
        d = Path(project_dir) if project_dir else config.PROJECT_DIR
        domain_path = d / "domain.yaml"
        if not domain_path.exists():
            logger.warning(f"[domain] domain.yaml not found at {domain_path}, using defaults")
            return cls({})
        with open(domain_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        logger.info(f"[domain] Loaded domain: {raw.get('name', '?')} ({raw.get('domain_id', '?')})")
        return cls(raw)

    def __repr__(self) -> str:
        return f"DomainConfig(id={self.domain_id!r}, name={self.name!r})"


# ---------------------------------------------------------------------------
# DomainSpec — единая точка доступа
# ---------------------------------------------------------------------------

class DomainSpec:
    """Загружает все YAML-спеки домена из project directory."""

    def __init__(
        self,
        scenes: SceneCatalog,
        objects: ObjectCatalog,
        rubric: AuditRubric,
        domain_config: DomainConfig | None = None,
    ) -> None:
        self.scenes = scenes
        self.objects = objects
        self.rubric = rubric
        self.domain = domain_config or DomainConfig({})

    @classmethod
    def load(cls, docs_dir: Path | str | None = None) -> "DomainSpec":
        docs = Path(docs_dir) if docs_dir else config.PROJECT_DIR

        with open(docs / "scene_catalog.yaml", encoding="utf-8") as f:
            raw_scenes = yaml.safe_load(f)
        with open(docs / "object_catalog.yaml", encoding="utf-8") as f:
            raw_objects = yaml.safe_load(f)
        with open(docs / "audit_rubric.yaml", encoding="utf-8") as f:
            raw_rubric = yaml.safe_load(f)

        scenes = SceneCatalog(scenes=[
            Scene(**{**s, "reference_images": [str(x) for x in s.get("reference_images", [])]})
            for s in raw_scenes["scenes"]
        ])
        objects = ObjectCatalog(objects=[
            DomainObject(**{**o, "reference_seen_in": [str(x) for x in o.get("reference_seen_in", [])]})
            for o in raw_objects["objects"]
        ])

        # Парсим rubric
        raw_weights = raw_rubric.get("aggregation", {}).get("step_2_check_scores", {}).get("weights", {})
        weights = AggregationWeights(**raw_weights) if raw_weights else AggregationWeights()

        checks = []
        for c in raw_rubric["audit_checks"]:
            # Пороги могут быть в форме "0.75" или "<0.50" — нормализуем
            raw_thresh = c.get("thresholds", {})
            accept_val = _parse_threshold(raw_thresh.get("accept", 0.75))
            review_val = _parse_threshold(raw_thresh.get("review", 0.50))
            checks.append(AuditCheck(
                id=c["id"],
                name=c["name"],
                type=c["type"],
                description=c.get("description", ""),
                thresholds=AuditThresholds(accept=accept_val, review=review_val),
                raw=c,
            ))

        rubric = AuditRubric(checks=checks, weights=weights)

        # Загружаем domain.yaml из той же директории
        domain_config = DomainConfig.load(docs)

        return cls(scenes=scenes, objects=objects, rubric=rubric, domain_config=domain_config)

    def get_scene(self, scene_id: str) -> Scene:
        return self.scenes.get(scene_id)

    def get_object(self, object_id: str) -> DomainObject:
        return self.objects.get(object_id)

    def summary(self) -> str:
        return (
            f"DomainSpec: {len(self.scenes.scenes)} scenes, "
            f"{len(self.objects.objects)} objects, "
            f"{len(self.rubric.checks)} audit checks"
        )


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

class GenerationTask(BaseModel):
    """Входная задача для одного run'а."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    scene_id: str
    object_id: str | None = None
    max_iterations: int = 3
    notes: str = ""  # доп. указания (произвольный текст)

    # Edit mode fields
    mode: Literal["generate", "edit"] = "generate"
    source_image: Optional[bytes] = None       # исходное изображение для edit mode
    edit_instruction: str = ""                  # инструкция для edit mode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_threshold(value: Any) -> float:
    """'0.75' → 0.75, '<0.50' → 0.50."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lstrip("<>=")
    try:
        return float(s)
    except ValueError:
        return 0.5
