"""
Prompt Builder — строит текстовый промпт для генерации изображения.

Источники:
  - Scene (zone, camera_view, must_have, forbidden)
  - DomainObject (placement, size_hint, color_palette)
  - DomainConfig (стиль, зоны, негативный промпт — из domain.yaml)
  - История итераций (для refinement)
"""
from __future__ import annotations

from auditor.domain_spec import DomainConfig, DomainSpec, GenerationTask, Scene, DomainObject


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class PromptBuilder:
    def __init__(self, spec: DomainSpec) -> None:
        self.spec = spec
        self.dc = spec.domain  # DomainConfig из domain.yaml

    def build(
        self,
        task: GenerationTask,
        *,
        reject_reasons: list[str] | None = None,
    ) -> tuple[str, str]:
        """
        Возвращает (positive_prompt, negative_prompt).

        reject_reasons — список причин из предыдущей итерации,
        которые используются для усиления нужных элементов.
        """
        scene = self.spec.get_scene(task.scene_id)
        obj = self.spec.get_object(task.object_id) if task.object_id else None

        positive = self._build_positive(scene, obj, reject_reasons or [], task.notes)
        negative = self._build_negative(scene)
        return positive, negative

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_positive(
        self,
        scene: Scene,
        obj: DomainObject | None,
        reject_reasons: list[str],
        notes: str,
    ) -> str:
        parts: list[str] = []

        # 1. Тип сцены (из domain.yaml)
        camera = self.dc.camera_views.get(scene.camera_view, scene.camera_view)
        zone_desc = self.dc.zone_descriptions.get(scene.zone, scene.zone)
        intro = self.dc.scene_intro or "Photo"
        parts.append(f"{intro}, {camera}, {zone_desc}.")

        # 2. Описание из каталога
        if scene.description:
            parts.append(scene.description.strip())

        # 3. Обязательные элементы сцены
        if scene.must_have:
            must_str = ", ".join(scene.must_have)
            parts.append(f"Must include: {must_str}.")

        # 4. Добавляемый объект
        if obj:
            parts.append(
                f"Add a {obj.name} ({obj.description.strip()}) "
                f"placed at {obj.placement}, size: {obj.size_hint}. "
                f"Colors: {', '.join(obj.color_palette)}."
            )

        # 5. Глобальный стиль (из domain.yaml)
        if self.dc.style:
            parts.append(self.dc.style)

        # 6. Усиление слабых мест из предыдущей итерации
        if reject_reasons:
            hints = self._reasons_to_hints(reject_reasons)
            if hints:
                parts.append("Important: " + "; ".join(hints) + ".")

        # 7. Пользовательские заметки
        if notes:
            parts.append(notes.strip())

        return " ".join(parts)

    def _build_negative(self, scene: Scene) -> str:
        extras = list(scene.forbidden)
        base = self.dc.negative_prompt or ""
        if extras:
            return base + ", " + ", ".join(extras) if base else ", ".join(extras)
        return base

    def _reasons_to_hints(self, reasons: list[str]) -> list[str]:
        """Конвертирует причины reject'а в подсказки, используя правила из domain.yaml."""
        hints: list[str] = []
        seen: set[str] = set()
        for r in reasons:
            r_lower = r.lower()
            for rh in self.dc.reason_hints:
                if rh.hint in seen:
                    continue
                if any(trigger in r_lower for trigger in rh.triggers):
                    hints.append(rh.hint)
                    seen.add(rh.hint)
        return hints
