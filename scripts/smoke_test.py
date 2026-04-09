#!/usr/bin/env python3
"""
Smoke test — end-to-end проверка пайплайна.

Запуск:
  python scripts/smoke_test.py
  python scripts/smoke_test.py --scene scene_01_counter_front --object wb_rollup_poster
  python scripts/smoke_test.py --scene scene_03_fitting_room_front --no-object
  python scripts/smoke_test.py --connectivity-only   # только проверка подключения
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Добавляем src в PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("smoke_test")


def ok(msg: str) -> None:
    print(f"  ✓  {msg}")


def fail(msg: str) -> None:
    print(f"  ✗  {msg}")


def header(msg: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {msg}")
    print(f"{'─'*60}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_spec(docs_dir: Path) -> bool:
    header("1. Loading domain spec")
    try:
        from auditor.domain_spec import DomainSpec
        spec = DomainSpec.load(docs_dir)
        ok(spec.summary())
        ok(f"Scene IDs: {spec.scenes.ids()}")
        ok(f"Object IDs (first 5): {spec.objects.ids()[:5]}")
        return True
    except Exception as e:
        fail(f"DomainSpec.load() failed: {e}")
        return False


async def test_connectivity(gateway) -> bool:
    header("2. OpenRouter connectivity (chat)")
    try:
        resp = await gateway.chat("Reply with exactly: PONG")
        ok(f"Chat response: {resp[:80]}")
        return True
    except Exception as e:
        fail(f"Chat failed: {e}")
        return False


async def test_generate(gateway, prompt: str) -> bytes | None:
    header("3. Image generation")
    try:
        from auditor import config
        ok(f"Model: {config.IMAGE_GEN_MODEL}")
        ok(f"Prompt (first 120): {prompt[:120]}")
        image_bytes = await gateway.generate_image(prompt)
        ok(f"Image received: {len(image_bytes)} bytes")
        return image_bytes
    except Exception as e:
        fail(f"Image generation failed: {e}")
        return None


async def test_audit(gateway, spec, image_bytes: bytes, task) -> bool:
    header("4. Audit stage")
    try:
        from auditor.audit_stage import AuditStage
        auditor = AuditStage(gateway, spec)
        audit = await auditor.run(image_bytes, task)
        ok(f"Hard reject: {audit.has_hard_reject}")
        for c in audit.checks:
            status = "HARD_REJECT" if c.hard_reject else f"score={c.score:.3f}"
            ok(f"  [{c.check_id}] {status}")
            for f in c.findings[:2]:
                print(f"       → {f}")
        return True
    except Exception as e:
        fail(f"Audit failed: {e}")
        return False


async def test_decision(spec, audit) -> bool:
    header("5. Decision engine")
    try:
        from auditor.decision_engine import DecisionEngine
        engine = DecisionEngine(spec)
        decision = engine.decide(audit, has_object=True)
        ok(f"Verdict: {decision.verdict.value}")
        ok(f"Weighted score: {decision.weighted_score:.3f}")
        ok(f"Scores: {decision.scores}")
        if decision.reasons:
            ok(f"Reasons (first 3): {decision.reasons[:3]}")
        if decision.suggestions:
            ok(f"Suggestions: {decision.suggestions[:2]}")
        return True
    except Exception as e:
        fail(f"Decision engine failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> int:
    from auditor.model_gateway import ModelGateway
    from auditor.domain_spec import DomainSpec, GenerationTask
    from auditor.prompt_builder import PromptBuilder
    from auditor import config

    from auditor.config import PROJECT_DIR
    docs_dir = PROJECT_DIR
    passed = 0
    failed = 0

    print(f"\n{'='*60}")
    print("  Domain Synth Auditor — Smoke Test")
    print(f"{'='*60}")
    print(f"  Scene:  {args.scene}")
    print(f"  Object: {args.object_id or '(none)'}")

    # 1. Spec
    if not await test_spec(docs_dir):
        return 1
    spec = DomainSpec.load(docs_dir)
    passed += 1

    # Gateway
    gateway = ModelGateway()

    # 2. Connectivity
    if not await test_connectivity(gateway):
        failed += 1
        if args.connectivity_only:
            return 1
    else:
        passed += 1

    if args.connectivity_only:
        print(f"\n{'='*60}")
        print(f"  Connectivity-only mode. Done.")
        return 0

    # Build task & prompt
    task = GenerationTask(
        scene_id=args.scene,
        object_id=args.object_id,
        max_iterations=1,
    )
    prompt, negative = PromptBuilder(spec).build(task)

    # 3. Generate
    image_bytes = await test_generate(gateway, prompt)
    if image_bytes is None:
        failed += 1
        print("\n⚠  Image generation failed — skipping audit & decision.")
    else:
        passed += 1

        # 4. Audit
        from auditor.audit_stage import AuditStage
        auditor = AuditStage(gateway, spec)
        audit = await auditor.run(image_bytes, task)
        if await test_audit(gateway, spec, image_bytes, task):
            passed += 1
        else:
            failed += 1

        # 5. Decision
        if await test_decision(spec, audit):
            passed += 1
        else:
            failed += 1

        # 6. Save run
        header("6. Experiment store")
        try:
            from auditor.experiment_store import ExperimentStore
            from auditor.decision_engine import DecisionEngine
            store = ExperimentStore()
            store.start_run(task)
            decision = DecisionEngine(spec).decide(audit, has_object=bool(task.object_id))
            it_dir = store.save_iteration(prompt, negative, image_bytes, audit, decision)
            store.finish_run(decision.verdict.value, 1)
            ok(f"Run saved to: {it_dir.parent}")
            passed += 1
        except Exception as e:
            fail(f"Store failed: {e}")
            failed += 1

    # Summary
    print(f"\n{'='*60}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'='*60}\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke test for Domain Synth Auditor")
    parser.add_argument("--scene", default="scene_01_counter_front", help="Scene ID")
    parser.add_argument("--object", dest="object_id", default="wb_rollup_poster", help="Object ID")
    parser.add_argument("--no-object", dest="object_id", action="store_const", const=None,
                        help="Run without an object")
    parser.add_argument("--connectivity-only", action="store_true",
                        help="Only test OpenRouter connection, skip image gen")
    args = parser.parse_args()

    sys.exit(asyncio.run(main(args)))
