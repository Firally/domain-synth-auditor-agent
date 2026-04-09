#!/usr/bin/env python3
"""
Batch runner — обработка изображений из CSV или MySQL через edit/generate pipeline.

Запуск (CSV):
  python scripts/run_batch.py --csv data.csv --project tv_settings --limit 5
  python scripts/run_batch.py --csv data.csv --project tv_settings --url-column urls

Запуск (MySQL):
  python scripts/run_batch.py --source db --project tv_settings \\
      --request "Сделай из телевизоров картинки с экранами настроек, 10 штук"
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# Добавляем src в PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("batch_runner")


async def main(args: argparse.Namespace) -> None:
    from auditor import config
    from auditor.domain_spec import DomainSpec, GenerationTask
    from auditor.experiment_store import ExperimentStore
    from auditor.image_loader import ImageLoader
    from auditor.memory_store import MemoryStore
    from auditor.model_gateway import ModelGateway
    from auditor.pipeline import Pipeline

    # 1. Переключаем проект
    project_dir = Path(__file__).resolve().parents[1] / "projects" / args.project
    if not project_dir.exists():
        logger.error(f"Project directory not found: {project_dir}")
        sys.exit(1)

    config.PROJECT_DIR = project_dir
    logger.info(f"Project: {args.project} ({project_dir})")

    # 2. Загружаем domain spec
    spec = DomainSpec.load(project_dir)
    logger.info(f"Domain: {spec.domain.name} (mode={spec.domain.pipeline_mode})")

    # 3. Определяем инструкцию
    instruction = args.instruction or spec.domain.edit_instruction

    # 4. Загружаем изображения (CSV или MySQL)
    if args.source == "db":
        from auditor.db_loader import DBImageLoader, DBPool, ObjectTypeStore
        from auditor.intent_resolver import IntentResolver

        if not args.request:
            logger.error("--request is required when using --source db")
            sys.exit(1)

        gateway_for_intent = ModelGateway()
        async with await DBPool.create() as db:
            all_types = await ObjectTypeStore.get_all(db)
            if not all_types:
                logger.error("No object types in database!")
                sys.exit(1)

            resolver = IntentResolver(gateway_for_intent)
            intent = await resolver.resolve(args.request, all_types)

            if not intent.object_type:
                logger.error(f"Could not match request to object type. Available: {[t.object_type for t in all_types]}")
                sys.exit(1)

            logger.info(f"Intent: type={intent.object_type.object_type}, count={intent.count}")

            # Используем instruction из intent если не задана явно
            if not instruction:
                instruction = intent.edit_instruction

            records = await DBImageLoader.load(
                db,
                object_type_id=intent.object_type.id,
                limit=args.limit or intent.count,
            )
    else:
        if not args.csv:
            logger.error("--csv is required when using --source csv")
            sys.exit(1)
        logger.info(f"Loading images from {args.csv}...")
        records = await ImageLoader.from_csv(
            args.csv,
            url_column=args.url_column,
            limit=args.limit,
        )

    if not instruction and spec.domain.pipeline_mode == "edit":
        logger.error("No edit instruction found! Provide --instruction or set edit_instruction in domain.yaml")
        sys.exit(1)

    if not records:
        logger.error("No images loaded")
        sys.exit(1)

    logger.info(f"Loaded {len(records)} images")

    # 5. Создаём pipeline
    gateway = ModelGateway()
    store = ExperimentStore()
    memory = MemoryStore()
    pipeline = Pipeline(gateway, spec, store=store, memory=memory)

    # Определяем scene_id (первая сцена из каталога или default)
    scene_ids = spec.scenes.ids()
    default_scene = scene_ids[0] if scene_ids else "default"

    # 6. Обработка
    t0_total = time.perf_counter()
    results = {"accepted": 0, "rejected": 0, "needs_review": 0, "errors": 0}
    budget_total = 0.0
    output_dir = Path(args.output) if args.output else Path("runs") / f"batch_{args.project}_{int(time.time())}"
    output_dir.mkdir(parents=True, exist_ok=True)

    for rec in records:
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing image {rec.index + 1}/{len(records)}: {rec.source_url[:80]}")

        mode = spec.domain.pipeline_mode
        task = GenerationTask(
            scene_id=default_scene,
            mode=mode,
            source_image=rec.image_bytes if mode == "edit" else None,
            edit_instruction=instruction if mode == "edit" else "",
            max_iterations=args.max_iterations,
            notes=f"source: {rec.source_url}",
        )

        try:
            result = await pipeline.run(task)
            verdict = result.final_verdict
            budget_total += result.budget_spent

            # Сохраняем результат
            suffix = "accepted" if verdict == "ACCEPT" else "rejected"
            img_name = f"{rec.index:04d}_{suffix}.png"
            img_path = output_dir / img_name
            img_path.write_bytes(result.final_image)

            if verdict == "ACCEPT":
                results["accepted"] += 1
            elif verdict == "REJECT":
                results["rejected"] += 1
            else:
                results["needs_review"] += 1

            logger.info(
                f"  Result: {verdict} (score={result.final_score:.3f}, "
                f"iterations={result.iterations}, budget=${result.budget_spent:.4f})"
            )

        except Exception as e:
            logger.error(f"  ERROR processing image {rec.index}: {e}")
            results["errors"] += 1

    # 7. Итоги
    elapsed = time.perf_counter() - t0_total
    total = len(records)

    print(f"\n{'='*60}")
    print(f"  BATCH RESULTS: {args.project}")
    print(f"{'='*60}")
    print(f"  Total images:   {total}")
    print(f"  Accepted:       {results['accepted']} ({results['accepted']/total*100:.0f}%)" if total else "")
    print(f"  Rejected:       {results['rejected']}")
    print(f"  Needs review:   {results['needs_review']}")
    print(f"  Errors:         {results['errors']}")
    print(f"  Budget spent:   ${budget_total:.4f}")
    print(f"  Time elapsed:   {elapsed:.1f}s ({elapsed/total:.1f}s per image)" if total else "")
    print(f"  Output dir:     {output_dir}")
    print(f"{'='*60}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Batch runner for image edit/generation pipeline")
    p.add_argument("--source", choices=["csv", "db"], default="csv", help="Image source: csv (default) or db (MySQL)")
    p.add_argument("--csv", default=None, help="Path to CSV with image URLs (for --source csv)")
    p.add_argument("--request", default=None, help="Natural language request (for --source db)")
    p.add_argument("--project", required=True, help="Project name (directory in projects/)")
    p.add_argument("--url-column", default=None, help="CSV column with image URLs (auto-detect if omitted)")
    p.add_argument("--limit", type=int, default=None, help="Max images to process")
    p.add_argument("--instruction", default=None, help="Override edit instruction from domain.yaml")
    p.add_argument("--max-iterations", type=int, default=2, help="Max DVF iterations per image (default: 2)")
    p.add_argument("--output", default=None, help="Output directory for results")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
