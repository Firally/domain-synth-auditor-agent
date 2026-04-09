"""
Memory Store — кросс-итерационная память агента между runs.

Закрывает требование курса «сквозные state & memory»:
  сейчас память жила только внутри одного run (reject_reasons передавались
  между итерациями). Теперь знания накапливаются между разными запусками.

Что хранит (JSON-файл memory/memory.json):
  1. recipes    — лучший промпт + статистика для каждой scene+object
  2. reject_patterns — типичные причины отклонения по сцене
  3. global_stats    — общая статистика по всем runs

Жизненный цикл:
  pipeline.run() → memory.load_recipe()  (читаем в начале)
                 → memory.save_recipe()  (пишем в конце при ACCEPT)
                 → memory.add_reject_pattern() (пишем при каждом reject)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from auditor import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Memory Store
# ---------------------------------------------------------------------------

class MemoryStore:
    """
    Персистентная JSON-память агента между runs.

    Читается при старте run → даёт исторический контекст.
    Обновляется после завершения run → накапливает знания.
    """

    def __init__(self, memory_path: Path | str | None = None) -> None:
        self.memory_path = (
            Path(memory_path) if memory_path
            else config.MEMORY_DIR / "memory.json"
        )
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    # ------------------------------------------------------------------
    # Internal: load / save
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if self.memory_path.exists():
            try:
                data = json.loads(self.memory_path.read_text(encoding="utf-8"))
                logger.info(
                    f"[memory] Loaded from {self.memory_path} "
                    f"({len(data.get('recipes', {}))} recipes, "
                    f"{sum(len(v) for v in data.get('reject_patterns', {}).values())} patterns)"
                )
                return data
            except Exception as e:
                logger.warning(f"[memory] Failed to load, starting fresh: {e}")
        return {
            "recipes": {},
            "reject_patterns": {},
            "global_stats": {
                "total_runs": 0,
                "total_accepted": 0,
                "total_rejected": 0,
                "total_needs_review": 0,
            },
        }

    def _save(self) -> None:
        self.memory_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Recipes — лучший известный промпт для scene+object
    # ------------------------------------------------------------------

    def load_recipe(self, scene_id: str, object_id: str | None) -> dict | None:
        """
        Возвращает лучший известный рецепт для scene+object, если есть.

        Пример возврата:
          {"best_prompt": "...", "acceptance_rate": 0.67, "avg_iterations": 2.1, ...}
        """
        key = _recipe_key(scene_id, object_id)
        recipe = self._data["recipes"].get(key)
        if recipe:
            rate = recipe.get("acceptance_rate", 0)
            logger.info(
                f"[memory] Found recipe for '{key}' "
                f"(acceptance_rate={rate:.0%}, runs={recipe.get('runs_count', 0)})"
            )
        return recipe

    def save_recipe(
        self,
        scene_id: str,
        object_id: str | None,
        prompt: str,
        iterations_used: int,
        verdict: str,
    ) -> None:
        """
        Обновляет рецепт после завершения run.

        Скользящее среднее acceptance_rate и avg_iterations накапливается.
        best_prompt обновляется только при ACCEPT.
        """
        key = _recipe_key(scene_id, object_id)
        existing = self._data["recipes"].get(key, {})

        runs = existing.get("runs_count", 0) + 1
        prev_rate = existing.get("acceptance_rate", 0.0)
        prev_avg_iter = existing.get("avg_iterations", float(iterations_used))

        new_rate = (prev_rate * (runs - 1) + (1.0 if verdict == "ACCEPT" else 0.0)) / runs
        new_avg_iter = (prev_avg_iter * (runs - 1) + iterations_used) / runs

        # best_prompt обновляем только при успехе
        best_prompt = prompt if verdict == "ACCEPT" else existing.get("best_prompt", prompt)

        self._data["recipes"][key] = {
            "scene_id": scene_id,
            "object_id": object_id,
            "best_prompt": best_prompt,
            "acceptance_rate": round(new_rate, 3),
            "avg_iterations": round(new_avg_iter, 2),
            "runs_count": runs,
            "last_updated": datetime.now().isoformat(),
        }
        self._save()
        logger.info(
            f"[memory] Updated recipe for '{key}': "
            f"acceptance_rate={new_rate:.0%}, runs={runs}"
        )

    # ------------------------------------------------------------------
    # Reject patterns — типичные причины отклонений по сцене
    # ------------------------------------------------------------------

    def add_reject_pattern(self, scene_id: str, check_id: str, reason: str) -> None:
        """Регистрирует причину отклонения. Счётчик повторов накапливается."""
        if scene_id not in self._data["reject_patterns"]:
            self._data["reject_patterns"][scene_id] = []

        patterns: list[dict] = self._data["reject_patterns"][scene_id]

        # Ищем существующий паттерн с той же причиной
        for p in patterns:
            if p.get("check_id") == check_id and p.get("reason_text") == reason:
                p["count"] = p.get("count", 0) + 1
                p["last_seen"] = datetime.now().isoformat()
                self._save()
                return

        patterns.append({
            "scene_id": scene_id,
            "check_id": check_id,
            "reason_text": reason,
            "count": 1,
            "last_seen": datetime.now().isoformat(),
        })
        self._save()

    def get_reject_hints(self, scene_id: str, top_n: int = 3) -> list[str]:
        """
        Возвращает топ-N исторических причин отклонения для данной сцены.

        Используется prompt_builder и prompt_improver для генерации
        лучшего промпта с учётом прошлых ошибок.
        """
        patterns = self._data["reject_patterns"].get(scene_id, [])
        sorted_p = sorted(patterns, key=lambda p: p.get("count", 0), reverse=True)
        return [p["reason_text"] for p in sorted_p[:top_n]]

    # ------------------------------------------------------------------
    # Global stats
    # ------------------------------------------------------------------

    def update_global_stats(self, verdict: str) -> None:
        """Обновляет общую статистику после завершения run."""
        stats = self._data["global_stats"]
        stats["total_runs"] = stats.get("total_runs", 0) + 1
        if verdict == "ACCEPT":
            stats["total_accepted"] = stats.get("total_accepted", 0) + 1
        elif verdict == "REJECT":
            stats["total_rejected"] = stats.get("total_rejected", 0) + 1
        else:
            stats["total_needs_review"] = stats.get("total_needs_review", 0) + 1
        self._save()

    def get_global_stats(self) -> dict:
        return dict(self._data["global_stats"])

    def summary(self) -> str:
        stats = self.get_global_stats()
        total = stats.get("total_runs", 0)
        accepted = stats.get("total_accepted", 0)
        rate = accepted / total if total > 0 else 0.0
        recipes = len(self._data["recipes"])
        return (
            f"MemoryStore: {total} total runs, "
            f"acceptance_rate={rate:.0%}, "
            f"{recipes} saved recipes"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _recipe_key(scene_id: str, object_id: str | None) -> str:
    return f"{scene_id}+{object_id or 'none'}"
