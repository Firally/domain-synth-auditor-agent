"""
Audit Stage — параллельные проверки сгенерированного изображения.

Порядок:
  1. safety_pii  — сначала (hard reject → стоп, остальные не запускаем)
  2. остальные   — параллельно через asyncio.gather

Каждая проверка возвращает CheckResult с числовым скором [0.0–1.0]
и списком findings (строки с объяснениями).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PIL import Image, ImageFilter

from auditor.domain_spec import DomainConfig, DomainSpec, GenerationTask, RelevanceQuestion
from auditor.knowledge_base import KnowledgeBase
from auditor.model_gateway import ModelGateway

if TYPE_CHECKING:
    from auditor.experiment_store import ExperimentStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error codes (machine-readable, по лекции LLM Agent Tools)
# ---------------------------------------------------------------------------

class ErrorCode:
    VLM_TIMEOUT = "VLM_TIMEOUT"
    VLM_RATE_LIMITED = "VLM_RATE_LIMITED"
    VLM_API_ERROR = "VLM_API_ERROR"
    VLM_PARSE_ERROR = "VLM_PARSE_ERROR"
    IMAGE_DECODE_ERROR = "IMAGE_DECODE_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def _classify_error(e: Exception) -> str:
    """Классифицирует исключение в machine-readable error code."""
    name = type(e).__name__.lower()
    msg = str(e).lower()
    if "timeout" in name or "timeout" in msg:
        return ErrorCode.VLM_TIMEOUT
    if "ratelimit" in name or "rate_limit" in name or "429" in msg:
        return ErrorCode.VLM_RATE_LIMITED
    if "apierror" in name or "apiconnection" in name or "500" in msg or "502" in msg or "503" in msg:
        return ErrorCode.VLM_API_ERROR
    return ErrorCode.INTERNAL_ERROR


_ERROR_SUGGESTIONS: dict[str, str] = {
    ErrorCode.VLM_TIMEOUT: "retry with fallback model or increase timeout",
    ErrorCode.VLM_RATE_LIMITED: "wait and retry, or switch to fallback model",
    ErrorCode.VLM_API_ERROR: "retry with fallback model",
    ErrorCode.VLM_PARSE_ERROR: "VLM returned non-JSON, retry or simplify prompt",
    ErrorCode.IMAGE_DECODE_ERROR: "check image format/bytes, re-generate image",
    ErrorCode.INTERNAL_ERROR: "check logs for unexpected error",
}


def _structured_finding(check_id: str, error: Exception) -> str:
    """Формирует structured finding: ERROR_CODE: message | suggestion."""
    code = _classify_error(error)
    suggestion = _ERROR_SUGGESTIONS.get(code, "check logs")
    return f"{code}: [{check_id}] {type(error).__name__}: {error} | suggestion: {suggestion}"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    check_id: str
    score: float                        # 0.0 – 1.0
    hard_reject: bool = False           # только для safety_pii
    findings: list[str] = field(default_factory=list)
    skipped: bool = False               # например object_integration без объекта
    error_code: str | None = None       # machine-readable error code (None = no error)


@dataclass
class AuditResult:
    checks: list[CheckResult]
    image_bytes: bytes = field(default=b"", repr=False)

    def get(self, check_id: str) -> CheckResult | None:
        for c in self.checks:
            if c.check_id == check_id:
                return c
        return None

    @property
    def has_hard_reject(self) -> bool:
        return any(c.hard_reject for c in self.checks)

    @property
    def hard_reject_reasons(self) -> list[str]:
        return [f for c in self.checks if c.hard_reject for f in c.findings]


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------

class AuditStage:
    def __init__(
        self,
        gateway: ModelGateway,
        spec: DomainSpec,
        kb: KnowledgeBase | None = None,
        store: ExperimentStore | None = None,
    ) -> None:
        self.gateway = gateway
        self.spec = spec
        self.dc: DomainConfig = spec.domain  # domain.yaml config
        self.kb = kb or KnowledgeBase(spec)  # KB всегда присутствует
        self.store = store  # для trajectory logging

    async def _timed_vision(
        self, image_bytes: bytes, prompt: str, check_id: str
    ) -> str:
        """Обёртка над gateway.vision() с логированием latency в trajectory."""
        from auditor import config as _cfg
        t0 = time.perf_counter()
        result = await self.gateway.vision(image_bytes, prompt)
        latency_ms = (time.perf_counter() - t0) * 1000

        if self.store:
            self.store.log_tool_call(
                tool="vision",
                model=_cfg.VISION_MODEL,
                check_id=check_id,
                latency_ms=latency_ms,
            )
        return result

    async def run(self, image_bytes: bytes, task: GenerationTask) -> AuditResult:
        """Запускает все проверки и возвращает AuditResult."""

        # 1. Safety / PII — сначала (блокирующий)
        safety = await self._check_safety_pii(image_bytes)
        if safety.hard_reject:
            logger.info(f"[audit] Hard reject by safety_pii: {safety.findings}")
            return AuditResult(checks=[safety], image_bytes=image_bytes)

        # 2. Остальные проверки — параллельно
        scene = self.spec.get_scene(task.scene_id)
        obj = self.spec.get_object(task.object_id) if task.object_id else None

        tasks = [
            self._check_prompt_adherence(image_bytes, task, scene, obj),
            self._check_domain_relevance(image_bytes, scene_id=task.scene_id),
            self._check_technical_quality(image_bytes),
        ]
        if obj is not None:
            tasks.append(self._check_object_integration(image_bytes, obj))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        checks: list[CheckResult] = [safety]
        for r in results:
            if isinstance(r, Exception):
                code = _classify_error(r)
                logger.error(f"[audit] check failed ({code}): {r}")
                checks.append(CheckResult(
                    check_id="unknown",
                    score=0.5,
                    findings=[_structured_finding("parallel_check", r)],
                    error_code=code,
                ))
            else:
                checks.append(r)  # type: ignore[arg-type]

        return AuditResult(checks=checks, image_bytes=image_bytes)

    # ------------------------------------------------------------------
    # 1. Safety / PII
    # ------------------------------------------------------------------
    async def _check_safety_pii(self, image_bytes: bytes) -> CheckResult:
        # Brand check field from domain.yaml (e.g. "no_foreign_brands" for WB)
        brand_field = self.dc.safety_brand_field
        brand_desc = self.dc.safety_brand_description

        prompt = (
            "You are a safety and PII auditor for a synthetic image dataset. "
            "Answer ONLY in valid JSON. Check the image for the following issues:\n"
            "{\n"
            '  "no_people": "yes/no — are there any people, faces, or body parts visible?",\n'
            '  "no_pii_documents": "yes/no — are identity documents or papers with personal data visible?",\n'
            '  "no_tracking_labels": "yes/no — are there readable shipping labels or tracking numbers?",\n'
            '  "no_phone_screens": "yes/no — are there phone screens showing personal data?",\n'
            f'  "{brand_field}": "yes/no — {brand_desc}"\n'
            "}\n"
            'For each field: "yes" means the PROBLEM EXISTS (bad), "no" means no problem (good).'
        )
        try:
            raw = await self._timed_vision(image_bytes, prompt, "safety_pii")
            data = _parse_json_response(raw)
        except Exception as e:
            code = _classify_error(e)
            logger.warning(f"[safety_pii] {code}: {e}, defaulting to pass (safe-side)")
            return CheckResult(
                check_id="safety_pii", score=1.0,
                findings=[_structured_finding("safety_pii", e)],
                error_code=code,
            )

        violations = []
        for key, val in data.items():
            if isinstance(val, str) and val.strip().lower().startswith("yes"):
                violations.append(key)

        if violations:
            return CheckResult(
                check_id="safety_pii",
                score=0.0,
                hard_reject=True,
                findings=[f"Safety violation: {v}" for v in violations],
            )
        return CheckResult(check_id="safety_pii", score=1.0)

    # ------------------------------------------------------------------
    # 2. Prompt Adherence
    # ------------------------------------------------------------------
    async def _check_prompt_adherence(
        self,
        image_bytes: bytes,
        task: GenerationTask,
        scene,
        obj,
    ) -> CheckResult:
        must_have_str = ", ".join(scene.must_have) if scene.must_have else "not specified"
        object_name = obj.name if obj else "none"

        # Обогащаем промпт контекстом из KB (few-shot reference примеры)
        kb_context = self.kb.retrieve_audit_rules("prompt_adherence", task.scene_id)
        kb_hint = ""
        if kb_context.scene_specific_hints:
            kb_hint = "\nKnowledge base hints: " + "; ".join(kb_context.scene_specific_hints)

        prompt = (
            "You are evaluating a synthetic image for dataset quality. "
            "Answer ONLY in valid JSON with scores from 0.0 to 1.0.\n"
            f"Scene zone: {scene.zone}\n"
            f"Required elements: {must_have_str}\n"
            f"Added object: {object_name}"
            f"{kb_hint}\n\n"
            "{\n"
            '  "zone_clearly_visible": 0.0-1.0,\n'
            '  "must_have_elements_present": 0.0-1.0,\n'
            '  "added_object_visible_and_natural": 0.0-1.0,\n'
            '  "notes": "brief explanation"\n'
            "}"
        )
        try:
            raw = await self._timed_vision(image_bytes, prompt, "prompt_adherence")
            data = _parse_json_response(raw)
        except Exception as e:
            code = _classify_error(e)
            logger.warning(f"[prompt_adherence] {code}: {e}")
            return CheckResult(
                check_id="prompt_adherence", score=0.5,
                findings=[_structured_finding("prompt_adherence", e)],
                error_code=code,
            )

        zone_score = float(data.get("zone_clearly_visible", 0.5))
        must_have_score = float(data.get("must_have_elements_present", 0.5))
        obj_score = float(data.get("added_object_visible_and_natural", 0.5))

        # Weighted: zone 0.3, must_have 0.4, object 0.3
        if obj:
            total = zone_score * 0.3 + must_have_score * 0.4 + obj_score * 0.3
        else:
            total = zone_score * 0.4 + must_have_score * 0.6

        findings = []
        if zone_score < 0.6:
            findings.append(f"zone_visible={zone_score:.2f} — zone not clearly visible")
        if must_have_score < 0.6:
            findings.append(f"must_have={must_have_score:.2f} — required elements missing")
        if obj and obj_score < 0.6:
            findings.append(f"object_integration={obj_score:.2f} — object not natural")
        if data.get("notes"):
            findings.append(f"notes: {data['notes']}")

        return CheckResult(check_id="prompt_adherence", score=round(total, 3), findings=findings)

    # ------------------------------------------------------------------
    # 3. Domain Relevance
    # ------------------------------------------------------------------
    async def _check_domain_relevance(
        self, image_bytes: bytes, scene_id: str = ""
    ) -> CheckResult:
        # Обогащаем промпт маркерами для данной зоны из KB
        kb_hint = ""
        if scene_id:
            kb_context = self.kb.retrieve_audit_rules("domain_relevance", scene_id)
            ref_hint = self.kb.format_references_for_prompt(scene_id, n=1)
            hints = kb_context.scene_specific_hints
            if hints:
                kb_hint = "\nExpected markers for this scene: " + "; ".join(hints)
            if ref_hint:
                kb_hint += "\n" + ref_hint

        # Build questions from domain.yaml
        questions = self.dc.relevance_questions
        if not questions:
            # Fallback: one generic question
            questions = [RelevanceQuestion(key="domain_match", label="Matches domain", weight=1.0)]

        json_fields = ""
        extra_lines = ""
        for q in questions:
            json_fields += f'  "{q.key}": 0.0-1.0,\n'
            if q.extra:
                extra_lines += f"\n{q.key}: {q.extra}"

        intro = self.dc.relevance_intro or "Evaluate whether this image matches the expected domain."

        prompt = (
            f"{intro} "
            "Answer ONLY in valid JSON with scores 0.0-1.0:\n"
            "{\n"
            f"{json_fields}"
            '  "notes": "brief explanation"\n'
            "}"
            f"{extra_lines}{kb_hint}"
        )
        try:
            raw = await self._timed_vision(image_bytes, prompt, "domain_relevance")
            data = _parse_json_response(raw)
        except Exception as e:
            code = _classify_error(e)
            logger.warning(f"[domain_relevance] {code}: {e}")
            return CheckResult(
                check_id="domain_relevance", score=0.5,
                findings=[_structured_finding("domain_relevance", e)],
                error_code=code,
            )

        # Weighted scoring from domain.yaml questions
        total = 0.0
        for q in questions:
            val = float(data.get(q.key, 0.5))
            total += val * q.weight

        # Findings from domain.yaml low_score_findings
        findings = []
        low_findings = self.dc.relevance_low_findings
        for q in questions:
            val = float(data.get(q.key, 1.0))
            if val < 0.5 and q.key in low_findings:
                findings.append(f"{q.key}: low — {low_findings[q.key]}")
        if data.get("notes"):
            findings.append(f"notes: {data['notes']}")

        return CheckResult(check_id="domain_relevance", score=round(total, 3), findings=findings)

    # ------------------------------------------------------------------
    # 4. Technical Quality (rule-based + один VLM-вопрос про артефакты)
    # ------------------------------------------------------------------
    async def _check_technical_quality(self, image_bytes: bytes) -> CheckResult:
        findings: list[str] = []
        subscores: dict[str, float] = {}

        # --- Rule-based checks (PIL) ---
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            w, h = img.size

            # Resolution
            res_ok = w >= 512 and h >= 512
            subscores["resolution"] = 1.0 if res_ok else 0.0
            if not res_ok:
                findings.append(f"resolution: {w}x{h} < 512x512")

            # Blur (Laplacian variance)
            gray = img.convert("L")
            lap = gray.filter(ImageFilter.FIND_EDGES)
            lap_arr = list(lap.getdata())
            mean_v = sum(lap_arr) / len(lap_arr)
            var_v = sum((x - mean_v) ** 2 for x in lap_arr) / len(lap_arr)
            blur_ok = var_v > 100
            subscores["blur"] = min(1.0, var_v / 500)
            if not blur_ok:
                findings.append(f"blur: Laplacian var={var_v:.1f} < 100 — image too blurry")

            # Exposure (mean brightness)
            pixels = list(img.getdata())
            brightness = sum(sum(p) / 3 for p in pixels) / len(pixels)
            exposure_ok = 40 <= brightness <= 220
            subscores["exposure"] = 1.0 if exposure_ok else 0.3
            if not exposure_ok:
                findings.append(f"exposure: brightness={brightness:.1f} out of [40, 220]")

        except Exception as e:
            logger.warning(f"[technical_quality] {ErrorCode.IMAGE_DECODE_ERROR}: {e}")
            subscores = {"resolution": 0.5, "blur": 0.5, "exposure": 0.5}
            findings.append(
                f"{ErrorCode.IMAGE_DECODE_ERROR}: [technical_quality] {type(e).__name__}: {e} "
                f"| suggestion: {_ERROR_SUGGESTIONS[ErrorCode.IMAGE_DECODE_ERROR]}"
            )

        # --- VLM: obvious AI artifacts ---
        try:
            artifact_prompt = (
                "Look at this image carefully. "
                "Answer ONLY in valid JSON:\n"
                "{\n"
                '  "no_obvious_artifacts": 0.0-1.0,\n'
                '  "notes": "describe any artifacts if present"\n'
                "}\n"
                "no_obvious_artifacts: 1.0 = image looks clean and realistic. "
                "0.0 = severe artifacts (warped objects, extra limbs, fused surfaces, broken text)."
            )
            raw = await self._timed_vision(image_bytes, artifact_prompt, "technical_quality")
            data = _parse_json_response(raw)
            artifact_score = float(data.get("no_obvious_artifacts", 0.7))
            subscores["artifacts"] = artifact_score
            if artifact_score < 0.6:
                findings.append(f"artifacts: score={artifact_score:.2f} — {data.get('notes', '')}")
        except Exception as e:
            code = _classify_error(e)
            logger.warning(f"[technical_quality/artifacts] {code}: {e}")
            subscores["artifacts"] = 0.7
            findings.append(
                f"{code}: [technical_quality/artifacts] {type(e).__name__}: {e} "
                f"| suggestion: {_ERROR_SUGGESTIONS.get(code, 'check logs')}"
            )

        # Weighted total: resolution 0.20, blur 0.30, exposure 0.20, artifacts 0.30
        total = (
            subscores.get("resolution", 0.5) * 0.20
            + subscores.get("blur", 0.5) * 0.30
            + subscores.get("exposure", 0.5) * 0.20
            + subscores.get("artifacts", 0.5) * 0.30
        )
        return CheckResult(check_id="technical_quality", score=round(total, 3), findings=findings)

    # ------------------------------------------------------------------
    # 5. Object Integration (только если объект задан)
    # ------------------------------------------------------------------
    async def _check_object_integration(self, image_bytes: bytes, obj) -> CheckResult:
        prompt = (
            f"Evaluate how well the '{obj.name}' is integrated into this scene. "
            "Answer ONLY in valid JSON with scores 0.0-1.0:\n"
            "{\n"
            f'  "object_visible": 0.0-1.0,\n'
            f'  "natural_placement": 0.0-1.0,\n'
            f'  "lighting_consistent": 0.0-1.0,\n'
            f'  "not_artifact": 0.0-1.0,\n'
            '  "notes": "brief explanation"\n'
            "}\n"
            f"The object should be a {obj.description.strip()}"
        )
        try:
            raw = await self._timed_vision(image_bytes, prompt, "object_integration")
            data = _parse_json_response(raw)
        except Exception as e:
            code = _classify_error(e)
            logger.warning(f"[object_integration] {code}: {e}")
            return CheckResult(
                check_id="object_integration", score=0.5,
                findings=[_structured_finding("object_integration", e)],
                error_code=code,
            )

        total = (
            float(data.get("object_visible", 0.5)) * 0.30
            + float(data.get("natural_placement", 0.5)) * 0.35
            + float(data.get("lighting_consistent", 0.5)) * 0.20
            + float(data.get("not_artifact", 0.5)) * 0.15
        )
        findings = []
        if float(data.get("object_visible", 1)) < 0.5:
            findings.append("object_visible: low — object not clearly visible")
        if float(data.get("natural_placement", 1)) < 0.5:
            findings.append("natural_placement: low — object looks pasted/unnatural")
        if data.get("notes"):
            findings.append(f"notes: {data['notes']}")

        return CheckResult(check_id="object_integration", score=round(total, 3), findings=findings)


# ---------------------------------------------------------------------------
# JSON parsing helper
# ---------------------------------------------------------------------------

def _parse_json_response(text: str) -> dict:
    """Извлекает JSON из ответа модели (модель может добавить markdown-блок)."""
    # Пробуем напрямую
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Ищем ```json ... ``` блок
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Ищем первый { ... } в тексте
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning(
        f"[_parse_json] {ErrorCode.VLM_PARSE_ERROR}: Could not parse JSON from: {text[:200]} "
        f"| suggestion: {_ERROR_SUGGESTIONS[ErrorCode.VLM_PARSE_ERROR]}"
    )
    return {}
