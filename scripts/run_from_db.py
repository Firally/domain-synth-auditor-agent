#!/usr/bin/env python3
"""
DB Runner — запуск pipeline из MySQL по natural language запросу.

Агент:
  1. Получает текстовый запрос пользователя
  2. Через LLM определяет нужный object_type из БД
  3. Загружает N случайных изображений этого типа
  4. Прогоняет каждое через DVF pipeline

Запуск:
  python scripts/run_from_db.py \\
      --request "Сделай из телевизоров картинки с экранами настроек, 10 штук" \\
      --project tv_settings
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
logger = logging.getLogger("db_runner")


async def main(args: argparse.Namespace) -> None:
    from auditor import config
    from auditor.db_loader import DBImageLoader, DBPool, ObjectTypeStore
    from auditor.domain_spec import DomainSpec, GenerationTask
    from auditor.experiment_store import ExperimentStore
    from auditor.intent_resolver import IntentResolver
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

    # 3. Подключаемся к MySQL
    gateway = ModelGateway()

    async with await DBPool.create() as db:
        # 4. Загружаем типы объектов из БД
        all_types = await ObjectTypeStore.get_all(db)
        if not all_types:
            logger.error("No object types found in database! Populate object_types table first.")
            sys.exit(1)

        logger.info(f"Available object types: {[t.object_type for t in all_types]}")

        # 5. LLM определяет intent
        resolver = IntentResolver(gateway)
        intent = await resolver.resolve(args.request, all_types)

        if not intent.object_type:
            logger.error(
                f"Could not match request to any object type. "
                f"Available: {[t.object_type for t in all_types]}"
            )
            sys.exit(1)

        logger.info(f"\n{'='*60}")
        logger.info(f"  Intent resolved:")
        logger.info(f"    Object type : {intent.object_type.object_type} (id={intent.object_type.id})")
        logger.info(f"    Count       : {intent.count}")
        logger.info(f"    Instruction : {intent.edit_instruction[:100]}")
        logger.info(f"{'='*60}\n")

        # 6. Проверяем сколько изображений доступно
        available = await DBImageLoader.count(db, intent.object_type.id)
        actual_count = min(intent.count, available)
        if actual_count == 0:
            logger.error(
                f"No images found for object_type={intent.object_type.object_type}. "
                f"Populate the images table first."
            )
            sys.exit(1)

        if actual_count < intent.count:
            logger.warning(
                f"Requested {intent.count} images but only {available} available. "
                f"Processing {actual_count}."
            )

        # 7. Загружаем изображения из БД
        records = await DBImageLoader.load(
            db,
            object_type_id=intent.object_type.id,
            limit=actual_count,
        )
        if not records:
            logger.error("Failed to download any images")
            sys.exit(1)

        logger.info(f"Loaded {len(records)} images from database")

    # 8. Определяем edit instruction
    instruction = args.instruction or intent.edit_instruction or spec.domain.edit_instruction
    if not instruction and spec.domain.pipeline_mode == "edit":
        logger.error(
            "No edit instruction resolved! Provide --instruction or set edit_instruction in domain.yaml"
        )
        sys.exit(1)

    # 9. Создаём pipeline
    store = ExperimentStore()
    memory = MemoryStore()
    pipeline = Pipeline(gateway, spec, store=store, memory=memory)

    scene_ids = spec.scenes.ids()
    default_scene = scene_ids[0] if scene_ids else "default"

    # 10. Обработка
    t0_total = time.perf_counter()
    results = {"accepted": 0, "rejected": 0, "needs_review": 0, "errors": 0}
    budget_total = 0.0
    output_dir = Path(args.output) if args.output else Path("runs") / f"db_{args.project}_{int(time.time())}"
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
            notes=f"source: db image_id={rec.metadata.get('db_image_id', '?')}",
        )

        try:
            result = await pipeline.run(task)
            verdict = result.final_verdict
            budget_total += result.budget_spent

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

    # 11. Итоги
    elapsed = time.perf_counter() - t0_total
    total = len(records)

    print(f"\n{'='*60}")
    print(f"  DB BATCH RESULTS: {args.project}")
    print(f"{'='*60}")
    print(f"  Request:        {args.request[:80]}")
    print(f"  Object type:    {intent.object_type.object_type}")
    print(f"  Total images:   {total}")
    if total:
        print(f"  Accepted:       {results['accepted']} ({results['accepted']/total*100:.0f}%)")
    print(f"  Rejected:       {results['rejected']}")
    print(f"  Needs review:   {results['needs_review']}")
    print(f"  Errors:         {results['errors']}")
    print(f"  Budget spent:   ${budget_total:.4f}")
    if total:
        print(f"  Time elapsed:   {elapsed:.1f}s ({elapsed/total:.1f}s per image)")
    print(f"  Output dir:     {output_dir}")
    print(f"{'='*60}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run pipeline from MySQL database with natural language request"
    )
    p.add_argument(
        "--request", required=True,
        help="Natural language request (e.g. 'Сделай из телевизоров картинки с настройками, 10 штук')",
    )
    p.add_argument("--project", required=True, help="Project name (directory in projects/)")
    p.add_argument("--instruction", default=None, help="Override edit instruction from intent/domain.yaml")
    p.add_argument("--max-iterations", type=int, default=2, help="Max DVF iterations per image (default: 2)")
    p.add_argument("--output", default=None, help="Output directory for results")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
