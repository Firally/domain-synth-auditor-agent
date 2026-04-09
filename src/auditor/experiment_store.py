"""
Experiment Store — JSON-логирование runs и итераций на диск.

Структура:
  runs/
    run_YYYYMMDD_HHMMSS_<scene>/
      meta.json
      iteration_001/
        prompt.json
        image.png
        audit_result.json
        decision.json
        trajectory.jsonl   ← append-only лог каждого tool-call (новое)
      iteration_002/
        ...

Trajectory logging (по лекции "How To Evaluate Agents"):
  trajectory.jsonl — full record of agent execution:
  каждый VLM-вызов, его модель, latency, tokens, результат.
  Позволяет воспроизвести и отладить любое решение аудитора.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from auditor import config
from auditor.audit_stage import AuditResult, CheckResult
from auditor.decision_engine import Decision
from auditor.domain_spec import GenerationTask

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class ExperimentStore:
    def __init__(self, runs_dir: Path | str | None = None) -> None:
        self.runs_dir = Path(runs_dir) if runs_dir else config.RUNS_DIR
        self.run_dir: Path | None = None
        self._iteration: int = 0
        self._run_start_time: float = 0.0
        self._total_tokens: int = 0
        self._total_tool_calls: int = 0

    def start_run(self, task: GenerationTask) -> Path:
        """Создаёт директорию для нового run'а и сохраняет meta.json."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"run_{ts}_{task.scene_id}"
        if task.object_id:
            name += f"_{task.object_id}"
        self.run_dir = self.runs_dir / name
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._iteration = 0
        self._run_start_time = time.time()
        self._total_tokens = 0
        self._total_tool_calls = 0

        meta = {
            "run_id": name,
            "started_at": datetime.now().isoformat(),
            "scene_id": task.scene_id,
            "object_id": task.object_id,
            "max_iterations": task.max_iterations,
            "notes": task.notes,
        }
        _write_json(self.run_dir / "meta.json", meta)
        logger.info(f"[store] Run started: {self.run_dir}")
        return self.run_dir

    def save_iteration(
        self,
        prompt: str,
        negative_prompt: str,
        image_bytes: bytes,
        audit: AuditResult,
        decision: Decision,
    ) -> Path:
        """Сохраняет все артефакты одной итерации."""
        if self.run_dir is None:
            raise RuntimeError("Call start_run() first")

        self._iteration += 1
        it_dir = self.run_dir / f"iteration_{self._iteration:03d}"
        it_dir.mkdir(exist_ok=True)

        # Промпт
        _write_json(it_dir / "prompt.json", {
            "iteration": self._iteration,
            "positive": prompt,
            "negative": negative_prompt,
        })

        # Изображение
        if image_bytes:
            (it_dir / "image.png").write_bytes(image_bytes)

        # Результат аудита
        _write_json(it_dir / "audit_result.json", _audit_to_dict(audit))

        # Решение
        _write_json(it_dir / "decision.json", {
            "iteration": self._iteration,
            "verdict": decision.verdict.value,
            "weighted_score": decision.weighted_score,
            "scores": decision.scores,
            "reasons": decision.reasons,
            "suggestions": decision.suggestions,
        })

        # Trajectory: одна запись на всю итерацию (агрегированная)
        self._append_trajectory(it_dir, {
            "event": "iteration_complete",
            "iteration": self._iteration,
            "verdict": decision.verdict.value,
            "weighted_score": decision.weighted_score,
            "check_scores": {
                c.check_id: c.score for c in audit.checks
            },
            "hard_reject": audit.has_hard_reject,
            "reasons": decision.reasons[:3],
        })

        logger.info(
            f"[store] Iteration {self._iteration} saved: "
            f"verdict={decision.verdict.value}, score={decision.weighted_score:.3f}"
        )
        return it_dir

    def log_tool_call(
        self,
        tool: str,
        model: str,
        check_id: str,
        latency_ms: float,
        result_score: float | None = None,
        tokens_used: int = 0,
        extra: dict | None = None,
    ) -> None:
        """
        Логирует один tool-call в trajectory.jsonl текущей итерации.

        По лекции "How To Evaluate Agents": trajectory = full record of
        agent execution — какие инструменты вызывались, с какими параметрами,
        что вернули, сколько времени заняло.
        """
        if self.run_dir is None or self._iteration == 0:
            return
        it_dir = self.run_dir / f"iteration_{self._iteration:03d}"
        if not it_dir.exists():
            return

        self._total_tokens += tokens_used
        self._total_tool_calls += 1

        entry: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "tool": tool,
            "model": model,
            "check_id": check_id,
            "latency_ms": round(latency_ms, 1),
            "tokens_used": tokens_used,
        }
        if result_score is not None:
            entry["result_score"] = round(result_score, 3)
        if extra:
            entry.update(extra)

        self._append_trajectory(it_dir, entry)

    def finish_run(self, final_verdict: str, total_iterations: int) -> None:
        """Обновляет meta.json финальным результатом и агрегированными метриками."""
        if self.run_dir is None:
            return
        meta_path = self.run_dir / "meta.json"
        meta = json.loads(meta_path.read_text())
        elapsed = time.time() - self._run_start_time
        meta["finished_at"] = datetime.now().isoformat()
        meta["final_verdict"] = final_verdict
        meta["total_iterations"] = total_iterations
        meta["metrics"] = {
            "total_latency_s": round(elapsed, 1),
            "total_tool_calls": self._total_tool_calls,
            "total_tokens_used": self._total_tokens,
        }
        _write_json(meta_path, meta)
        logger.info(f"[store] Run finished: verdict={final_verdict}, iterations={total_iterations}")

    def _append_trajectory(self, it_dir: Path, entry: dict) -> None:
        """Append-only запись в trajectory.jsonl текущей итерации."""
        trajectory_path = it_dir / "trajectory.jsonl"
        line = json.dumps({"timestamp": datetime.now().isoformat(), **entry}, ensure_ascii=False)
        with open(trajectory_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _audit_to_dict(audit: AuditResult) -> dict:
    return {
        "has_hard_reject": audit.has_hard_reject,
        "checks": [
            {
                "check_id": c.check_id,
                "score": c.score,
                "hard_reject": c.hard_reject,
                "skipped": c.skipped,
                "findings": c.findings,
            }
            for c in audit.checks
        ],
    }
