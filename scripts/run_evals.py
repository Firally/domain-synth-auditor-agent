#!/usr/bin/env python3
"""
run_evals.py — запуск evaluation framework на golden set.

Закрывает требование курса «evals + human check on gold set».

Что делает:
  1. Загружает evals/golden_set.yaml (12 тест-кейсов с человеческими метками)
  2. Для каждого кейса: запускает audit на реальном изображении
  3. Применяет decision_engine → получает verdict
  4. Сравнивает с ожидаемым expected_verdict (human_label)
  5. Выводит детальный отчёт: agreement%, precision, recall по классам

Три типа graders (по лекции "How To Evaluate Agents"):
  - Verifiable: rule-based checks (technical_quality, safety_pii)
  - LLM-as-Judge: VLM checks (domain_relevance, prompt_adherence)
  - Human: human_label из golden_set.yaml (наш gold standard)

Usage:
  python scripts/run_evals.py
  python scripts/run_evals.py --verbose
  python scripts/run_evals.py --case eval_001   # один кейс
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

# Добавляем src в PYTHONPATH
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auditor.audit_stage import AuditStage
from auditor import config as auditor_config
from auditor.config import PROJECT_DIR, IMAGES_DIR
from auditor.decision_engine import DecisionEngine, Verdict
from auditor.domain_spec import DomainSpec, GenerationTask
from auditor.knowledge_base import KnowledgeBase
from auditor.model_gateway import ModelGateway

# В eval-режиме пропускаем генерацию изображений (экономия токенов)
auditor_config.EVAL_MODE = True

logging.basicConfig(
    level=logging.WARNING,   # в eval режиме меньше шума
    format="%(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("run_evals")


# ---------------------------------------------------------------------------
# Load golden set
# ---------------------------------------------------------------------------

def load_golden_set(evals_dir: Path) -> list[dict]:
    path = evals_dir / "golden_set.yaml"
    if not path.exists():
        print(f"[error] golden_set.yaml not found at {path}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("eval_cases", [])


# ---------------------------------------------------------------------------
# Run single eval case
# ---------------------------------------------------------------------------

async def run_case(
    case: dict,
    auditor: AuditStage,
    engine: DecisionEngine,
    root_dir: Path,
    verbose: bool = False,
) -> dict:
    """Запускает один eval кейс, возвращает результат."""
    case_id = case["id"]
    image_path = root_dir / case["image_path"]

    if not image_path.exists():
        return {
            "id": case_id,
            "error": f"Image not found: {image_path}",
            "expected": case.get("expected_verdict"),
            "actual": "ERROR",
            "match": False,
        }

    image_bytes = image_path.read_bytes()

    task = GenerationTask(
        scene_id=case["scene_id"],
        object_id=case.get("object_id"),
    )

    t0 = time.time()
    audit = await auditor.run(image_bytes, task)
    decision = engine.decide(audit, has_object=bool(case.get("object_id")))
    elapsed = time.time() - t0

    expected = case.get("expected_verdict", "UNKNOWN")
    actual = decision.verdict.value
    match = (actual == expected)

    # Проверка expected_checks (детальная)
    check_results: dict[str, str] = {}
    expected_checks = case.get("expected_checks", {})
    check_mismatches: list[str] = []

    for check_id, expected_pass in expected_checks.items():
        check_result = audit.get(check_id)
        if check_result is None:
            check_results[check_id] = "missing"
            continue

        # Определяем: pass = не hard_reject И score >= 0.5
        actual_pass = "pass" if (not check_result.hard_reject and check_result.score >= 0.5) else "fail"
        check_results[check_id] = actual_pass

        if actual_pass != expected_pass:
            check_mismatches.append(
                f"{check_id}: expected={expected_pass}, actual={actual_pass} (score={check_result.score:.2f})"
            )

    result = {
        "id": case_id,
        "description": case.get("description", ""),
        "expected": expected,
        "actual": actual,
        "match": match,
        "score": decision.weighted_score,
        "elapsed_s": round(elapsed, 1),
        "check_results": check_results,
        "check_mismatches": check_mismatches,
        "reasons": decision.reasons[:3],
        "human_notes": case.get("human_notes", ""),
    }

    if verbose:
        icon = "✓" if match else "✗"
        print(f"  {icon} {case_id}: expected={expected}, actual={actual} "
              f"(score={decision.weighted_score:.3f}, {elapsed:.1f}s)")
        if not match:
            print(f"    reasons: {decision.reasons[:2]}")
        if check_mismatches:
            print(f"    check mismatches: {check_mismatches}")

    return result


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(results: list[dict]) -> dict:
    """Считает agreement%, precision/recall по классам."""
    total = len(results)
    errors = sum(1 for r in results if r.get("error"))
    valid = [r for r in results if not r.get("error")]

    # Overall agreement
    matches = sum(1 for r in valid if r["match"])
    agreement = matches / len(valid) if valid else 0.0

    # Per-class precision/recall
    classes = ["ACCEPT", "REJECT", "NEEDS_REVIEW"]
    class_metrics: dict[str, dict] = {}

    for cls in classes:
        tp = sum(1 for r in valid if r["expected"] == cls and r["actual"] == cls)
        fp = sum(1 for r in valid if r["expected"] != cls and r["actual"] == cls)
        fn = sum(1 for r in valid if r["expected"] == cls and r["actual"] != cls)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        count_expected = sum(1 for r in valid if r["expected"] == cls)
        class_metrics[cls] = {
            "count": count_expected,
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
        }

    # Avg latency
    avg_latency = sum(r.get("elapsed_s", 0) for r in valid) / len(valid) if valid else 0

    return {
        "total": total,
        "errors": errors,
        "valid": len(valid),
        "agreement_pct": round(agreement * 100, 1),
        "per_class": class_metrics,
        "avg_latency_s": round(avg_latency, 1),
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(results: list[dict], metrics: dict) -> None:
    print("\n" + "=" * 60)
    print("EVAL RESULTS")
    print("=" * 60)

    # Summary
    print(f"\nTotal cases : {metrics['total']}")
    print(f"Errors      : {metrics['errors']}")
    print(f"Valid cases : {metrics['valid']}")
    print(f"Agreement   : {metrics['agreement_pct']}%")
    print(f"Avg latency : {metrics['avg_latency_s']}s per case")

    # Per-class
    print("\nPer-class metrics:")
    for cls, m in metrics["per_class"].items():
        if m["count"] == 0:
            continue
        print(
            f"  {cls:15s} | n={m['count']:2d} | "
            f"precision={m['precision']:.0%} | "
            f"recall={m['recall']:.0%} | "
            f"F1={m['f1']:.0%}"
        )

    # Failures
    failures = [r for r in results if not r.get("match") and not r.get("error")]
    if failures:
        print(f"\nMismatches ({len(failures)}):")
        for r in failures:
            print(f"  ✗ {r['id']}: expected={r['expected']}, actual={r['actual']}")
            print(f"    {r.get('description', '')}")
            if r.get("reasons"):
                print(f"    reasons: {r['reasons'][:2]}")

    print("\n" + "=" * 60)

    # Pass/fail verdict
    if metrics["agreement_pct"] >= 80:
        print("✅ EVAL PASSED (agreement >= 80%)")
    else:
        print("⚠️  EVAL BELOW TARGET (agreement < 80%)")

    # Check REJECT recall — критично для safety
    reject_recall = metrics["per_class"].get("REJECT", {}).get("recall", 0)
    if reject_recall < 0.75:
        print(f"⚠️  REJECT recall = {reject_recall:.0%} — below 75% safety threshold!")
    else:
        print(f"✅ REJECT recall = {reject_recall:.0%} — safety check OK")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    root_dir = ROOT
    evals_dir = root_dir / "evals"

    print("Loading golden set...")
    cases = load_golden_set(evals_dir)

    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"[error] Case '{args.case}' not found in golden_set.yaml")
            sys.exit(1)

    print(f"Loaded {len(cases)} eval cases")

    # Init components
    print("Initializing components...")
    spec = DomainSpec.load(PROJECT_DIR)
    kb = KnowledgeBase(spec)
    gateway = ModelGateway()
    auditor = AuditStage(gateway, spec, kb=kb)
    engine = DecisionEngine(spec)

    print(f"Running evals{' (verbose)' if args.verbose else ''}...\n")

    results: list[dict] = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['id']}: {case.get('description', '')[:60]}")
        result = await run_case(case, auditor, engine, root_dir, verbose=args.verbose)
        results.append(result)

        if result.get("error"):
            print(f"  ✗ ERROR: {result['error']}")

    # Compute and print metrics
    metrics = compute_metrics(results)
    print_report(results, metrics)

    # Save results to JSON
    out_path = evals_dir / "eval_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"metrics": metrics, "results": results}, f, ensure_ascii=False, indent=2)
    print(f"Results saved to {out_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run evals on golden set")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output per case")
    parser.add_argument("--case", help="Run single case by ID (e.g. eval_001)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args))
