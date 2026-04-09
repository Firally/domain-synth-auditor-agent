"""
Конфигурация: загрузка .env, модельная стратегия, пути.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parents[2]  # repo root в воркдереве
DOCS_DIR = ROOT_DIR / "docs"  # legacy, used only for course docs (governance, product-proposal)
IMAGES_DIR = ROOT_DIR / "images"
RUNS_DIR = ROOT_DIR / "runs"

# ---------------------------------------------------------------------------
# Multi-domain: PROJECT_DIR указывает на текущий проект
# Каждый проект содержит: domain.yaml, scene_catalog.yaml, object_catalog.yaml,
# audit_rubric.yaml, reference_annotations.yaml, images/
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(os.environ.get(
    "PROJECT_DIR",
    str(ROOT_DIR / "projects" / "wb_pvz"),
))

# ---------------------------------------------------------------------------
# Переменные окружения
# ---------------------------------------------------------------------------
load_dotenv(ROOT_DIR / ".env")

OPENROUTER_API_KEY: str = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

# Заголовки, рекомендуемые OpenRouter для идентификации приложения
OPENROUTER_HEADERS: dict[str, str] = {
    "HTTP-Referer": "https://github.com/domain-synth-auditor-agent",
    "X-Title": "Domain Synth Auditor PoC",
}

# ---------------------------------------------------------------------------
# Модельная стратегия (только бесплатные / почти бесплатные через OpenRouter)
# ---------------------------------------------------------------------------

# Генерация изображений (image output, через OpenRouter)
# gemini-3.1-flash-image-preview: img=$0, prompt=$0.0000005 — наш primary
# gemini-2.5-flash-image: img=$0.0000003 — fallback
IMAGE_GEN_MODEL = "google/gemini-3.1-flash-image-preview"
IMAGE_GEN_FALLBACK = "google/gemini-2.5-flash-image"

# VLM-аудит (image + text input → text output, бесплатные)
VISION_MODEL = "google/gemma-3-27b-it:free"
VISION_FALLBACK = "mistralai/mistral-small-3.1-24b-instruct:free"
VISION_FALLBACK2 = "nvidia/nemotron-nano-12b-v2-vl:free"

# Текстовые задачи (prompt improvement, structured output)
TEXT_MODEL = "google/gemma-3-12b-it:free"
TEXT_FALLBACK = "mistralai/mistral-small-3.1-24b-instruct:free"

# ---------------------------------------------------------------------------
# Параметры генерации
# ---------------------------------------------------------------------------
IMAGE_SIZE = "1024x1024"
IMAGE_N_PER_ITERATION = 1  # сколько картинок генерируем за одну итерацию

# ---------------------------------------------------------------------------
# Параметры pipeline
# ---------------------------------------------------------------------------
MAX_ITERATIONS = 3  # максимум итераций в одном run (generate mode)
EDIT_MAX_ITERATIONS = 2  # максимум итераций для edit mode (сходится быстрее)

# ---------------------------------------------------------------------------
# Пути для memory и evals
# ---------------------------------------------------------------------------
MEMORY_DIR = ROOT_DIR / "memory"
EVALS_DIR = ROOT_DIR / "evals"

# ---------------------------------------------------------------------------
# Eval mode: если True — пропускаем реальную генерацию (экономия токенов)
# ---------------------------------------------------------------------------
EVAL_MODE: bool = False  # переключить на True при запуске run_evals.py

# ---------------------------------------------------------------------------
# Budget guard — лимит расходов на один run и глобально
# Оценочная стоимость вызовов (USD):
#   - VLM (gemma-3-27b-it:free)   : $0.00 (бесплатная)
#   - Text (gemma-3-12b-it:free)  : $0.00 (бесплатная)
#   - Image gen (gemini-flash)    : ~$0.001 per call (prompt tokens ~минимально)
#   - Image gen (gemini-2.5-flash): ~$0.003 per call (fallback, чуть дороже)
# ---------------------------------------------------------------------------
COST_PER_CALL: dict[str, float] = {
    VISION_MODEL: 0.0,
    VISION_FALLBACK: 0.0,
    VISION_FALLBACK2: 0.0,
    TEXT_MODEL: 0.0,
    TEXT_FALLBACK: 0.0,
    IMAGE_GEN_MODEL: 0.001,
    IMAGE_GEN_FALLBACK: 0.003,
}

# Лимит на один run (USD). При превышении — pipeline останавливается.
BUDGET_PER_RUN: float = float(os.environ.get("BUDGET_PER_RUN", "0.10"))

# Глобальный лимит (USD) — считается через memory_store global_stats
BUDGET_GLOBAL: float = float(os.environ.get("BUDGET_GLOBAL", "5.00"))

# ---------------------------------------------------------------------------
# MySQL (опционально — нужно только для загрузки изображений из БД)
# ---------------------------------------------------------------------------
MYSQL_HOST: str = os.environ.get("MYSQL_HOST", "localhost")
MYSQL_PORT: int = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER: str = os.environ.get("MYSQL_USER", "root")
MYSQL_PASSWORD: str = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DATABASE: str = os.environ.get("MYSQL_DATABASE", "synth_auditor")
