#!/usr/bin/env python3
"""
IntentResolver Evaluation — оценка точности выбора object_type из БД.

Метод: classification accuracy on labeled test set.
MySQL НЕ нужна — object_types подаются из YAML.

Метрики:
  - Type accuracy (overall, per-category, per-type P/R)
  - Count accuracy (exact match, ±2 tolerance)
  - JSON parse rate
  - Latency

Запуск:
  python scripts/run_intent_evals.py              # все кейсы
  python scripts/run_intent_evals.py --verbose     # детально
  python scripts/run_intent_evals.py --case intent_001
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

# Добавляем src в PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("intent_eval")


async def main(args: argparse.Namespace) -> None:
    from auditor.db_loader import ObjectType
    from auditor.intent_resolver import IntentResolver
    from auditor.model_gateway import ModelGateway

    # 1. Загружаем golden set
    evals_path = Path(__file__).resolve().parents[1] / "evals" / "intent_golden_set.yaml"
    if not evals_path.exists():
        logger.error(f"Golden set not found: {evals_path}")
        sys.exit(1)

    with open(evals_path) as f:
        data = yaml.safe_load(f)

    # 2. Создаём ObjectType list из YAML (без MySQL!)
    available_types = [
        ObjectType(id=t["id"], object_type=t["object_type"], description=t.get("description", ""))
        for t in data["object_types"]
    ]
    type_names = {t.object_type for t in available_types}
    logger.info(f"Available types: {[t.object_type for t in available_types]}")

    # 3. Фильтруем кейсы
    cases = data["cases"]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            logger.error(f"Case {args.case} not found")
            sys.exit(1)

    logger.info(f"Running {len(cases)} eval cases\n")

    # 4. Создаём resolver
    gateway = ModelGateway()
    resolver = IntentResolver(gateway)

    # 5. Прогоняем кейсы
    results = []

    for case in cases:
        case_id = case["id"]
        request = case["request"]
        expected_type = case["expected_type"]  # str or null
        expected_count = case["expected_count"]
        category = case.get("category", "unknown")

        t0 = time.perf_counter()
        try:
            intent = await resolver.resolve(request, available_types)
            elapsed = time.perf_counter() - t0

            actual_type = intent.object_type.object_type if intent.object_type else None
            actual_count = intent.count
            json_parsed = True

            type_match = actual_type == expected_type
            count_match = actual_count == expected_count
            count_close = abs(actual_count - expected_count) <= 2

            result = {
                "id": case_id,
                "request": request[:80],
                "category": category,
                "expected_type": expected_type,
                "actual_type": actual_type,
                "type_match": type_match,
                "expected_count": expected_count,
                "actual_count": actual_count,
                "count_match": count_match,
                "count_close": count_close,
                "edit_instruction": intent.edit_instruction[:80] if intent.edit_instruction else "",
                "json_parsed": json_parsed,
                "elapsed_s": round(elapsed, 2),
                "error": None,
            }

        except Exception as e:
            elapsed = time.perf_counter() - t0
            result = {
                "id": case_id,
                "request": request[:80],
                "category": category,
                "expected_type": expected_type,
                "actual_type": None,
                "type_match": False,
                "expected_count": expected_count,
                "actual_count": None,
                "count_match": False,
                "count_close": False,
                "edit_instruction": "",
                "json_parsed": False,
                "elapsed_s": round(elapsed, 2),
                "error": str(e),
            }

        results.append(result)

        if args.verbose:
            status = "✓" if result["type_match"] else "✗"
            count_status = "✓" if result["count_match"] else ("~" if result["count_close"] else "✗")
            print(
                f"  {status} {case_id}: type={result['actual_type']} "
                f"(expected={expected_type}) | "
                f"count={result['actual_count']} {count_status} "
                f"(expected={expected_count}) | "
                f"{result['elapsed_s']:.1f}s"
            )
            if result["error"]:
                print(f"    ERROR: {result['error']}")

    # 6. Вычисляем метрики
    total = len(results)
    valid = [r for r in results if not r["error"]]
    errors = total - len(valid)

    # Overall metrics
    type_correct = sum(1 for r in valid if r["type_match"])
    count_correct = sum(1 for r in valid if r["count_match"])
    count_close = sum(1 for r in valid if r["count_close"])
    json_parsed = sum(1 for r in valid if r["json_parsed"])

    type_accuracy = type_correct / len(valid) * 100 if valid else 0
    count_accuracy = count_correct / len(valid) * 100 if valid else 0
    count_tolerance = count_close / len(valid) * 100 if valid else 0
    parse_rate = json_parsed / len(valid) * 100 if valid else 0
    avg_latency = sum(r["elapsed_s"] for r in results) / total if total else 0

    # Per-category accuracy
    category_stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0})
    for r in valid:
        cat = r["category"]
        category_stats[cat]["total"] += 1
        if r["type_match"]:
            category_stats[cat]["correct"] += 1

    # Per-type precision / recall
    all_types = list(type_names) + [None]  # include null
    per_type: dict[str | None, dict] = {}
    for t in all_types:
        tp = sum(1 for r in valid if r["actual_type"] == t and r["expected_type"] == t)
        fp = sum(1 for r in valid if r["actual_type"] == t and r["expected_type"] != t)
        fn = sum(1 for r in valid if r["actual_type"] != t and r["expected_type"] == t)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        per_type[t or "null"] = {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}

    # 7. Вывод
    print(f"\n{'='*60}")
    print(f"  INTENT RESOLVER EVALUATION")
    print(f"{'='*60}")
    print(f"  Total cases:        {total}")
    print(f"  Errors:             {errors}")
    print(f"  JSON parse rate:    {parse_rate:.0f}%")
    print(f"  Type accuracy:      {type_accuracy:.1f}% ({type_correct}/{len(valid)})")
    print(f"  Count accuracy:     {count_accuracy:.1f}% (exact)")
    print(f"  Count accuracy ±2:  {count_tolerance:.1f}%")
    print(f"  Avg latency:        {avg_latency:.1f}s")
    print()

    # Per-category
    print(f"  Per-category type accuracy:")
    for cat, stats in sorted(category_stats.items()):
        acc = stats["correct"] / stats["total"] * 100 if stats["total"] else 0
        print(f"    {cat:20s}  {acc:5.1f}% ({stats['correct']}/{stats['total']})")
    print()

    # Per-type P/R
    print(f"  Per-type precision / recall / F1:")
    for t_name, stats in sorted(per_type.items(), key=lambda x: x[0] or ""):
        if stats["tp"] + stats["fp"] + stats["fn"] == 0:
            continue
        print(
            f"    {t_name:20s}  P={stats['precision']:.2f}  R={stats['recall']:.2f}  "
            f"F1={stats['f1']:.2f}  (TP={stats['tp']} FP={stats['fp']} FN={stats['fn']})"
        )

    # Thresholds
    print()
    type_ok = type_accuracy >= 80
    print(f"  Type accuracy >= 80%:  {'PASS' if type_ok else 'FAIL'} ({type_accuracy:.1f}%)")
    parse_ok = parse_rate >= 95
    print(f"  JSON parse >= 95%:     {'PASS' if parse_ok else 'FAIL'} ({parse_rate:.0f}%)")
    print(f"{'='*60}")

    # 8. Сохраняем JSON
    output = {
        "results": results,
        "summary": {
            "total": total,
            "errors": errors,
            "type_accuracy_pct": round(type_accuracy, 1),
            "count_accuracy_pct": round(count_accuracy, 1),
            "count_tolerance_pct": round(count_tolerance, 1),
            "json_parse_rate_pct": round(parse_rate, 1),
            "avg_latency_s": round(avg_latency, 2),
            "per_category": {
                cat: {"accuracy_pct": round(s["correct"] / s["total"] * 100, 1) if s["total"] else 0, **s}
                for cat, s in category_stats.items()
            },
            "per_type": per_type,
            "type_accuracy_pass": type_ok,
            "json_parse_pass": parse_ok,
        },
    }

    output_path = Path(__file__).resolve().parents[1] / "evals" / "intent_eval_results.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Results saved to: {output_path}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate IntentResolver accuracy on golden set")
    p.add_argument("--verbose", "-v", action="store_true", help="Show per-case results")
    p.add_argument("--case", default=None, help="Run single case by ID")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
