# System Design: Domain Synth Auditor Agent

## 1. Overview

**Domain Synth Auditor** --- PoC-система для генерации и аудита синтетических изображений с использованием LLM/VLM агентов. Система реализует паттерн **DVF (Draft -> Verify -> Fix)** --- итеративный цикл, в котором:

1. **Draft** --- генерация или редактирование изображения через Gemini (OpenRouter)
2. **Verify** --- параллельный VLM-аудит по 5 чекам (safety, prompt adherence, domain relevance, technical quality, object integration)
3. **Fix** --- LLM-рефайнмент промпта на основе найденных дефектов

Архитектура --- **гибридная агентная**: жёсткий workflow (DAG) с VLM-агентными шагами внутри узлов. Финальное решение (ACCEPT / REJECT / NEEDS_REVIEW) принимается **детерминистически** (rule-based engine), без LLM.

---

## 2. Key Architectural Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| AD-1 | OpenRouter как единая точка доступа к моделям | Единый API, fallback между провайдерами, бесплатные VLM-модели |
| AD-2 | VLM-as-Judge (не human-in-the-loop) | Масштабируемость; человек проверяет только NEEDS_REVIEW |
| AD-3 | Rule-based Decision Engine | Предсказуемость, воспроизводимость, нет hallucination risk |
| AD-4 | YAML-driven domain config | Новый домен = новый `projects/<domain>/` без изменений кода |
| AD-5 | Cross-run memory (JSON) | Recipes + reject patterns переиспользуются между запусками |
| AD-6 | Budget guard с hard stop | Предотвращение cost overrun на бесплатных/дешёвых моделях |
| AD-7 | 3-level fallback chains | primary -> fallback -> fallback2 для устойчивости к rate limits |
| AD-8 | Structured error codes | Машиночитаемые ошибки для автоматической обработки |

---

## 3. Module Composition

```
src/auditor/
  config.py            # Конфигурация, пути, модели, бюджеты
  domain_spec.py       # Pydantic-модели: Scene, Object, AuditCheck, DomainConfig, GenerationTask
  knowledge_base.py    # Retriever: reference annotations -> few-shot контекст для VLM
  prompt_builder.py    # Сборка текстовых промптов из scene + object + domain style
  prompt_improver.py   # LLM Fix step: рефайнмент промпта по audit failures
  model_gateway.py     # Unified API gateway: chat, vision, generate_image, edit_image
  audit_stage.py       # Parallel VLM audit: 5 checks -> AuditResult
  decision_engine.py   # Rule-based aggregation: scores -> Verdict
  pipeline.py          # Orchestrator: DVF loop, budget tracking, memory
  experiment_store.py  # Trajectory logging: runs, iterations, tool calls
  memory_store.py      # Persistent memory: recipes, reject_patterns, global_stats
  image_loader.py      # CSV/URL loader для batch processing
```

---

## 4. Execution Flow (DVF Loop)

### Generate mode (`pipeline_mode: generate`)

```
GenerationTask(scene_id, object_id)
    |
    v
[Load recipe from MemoryStore]  -- warm start if exists
    |
    v
[PromptBuilder.build()] --> (positive_prompt, negative_prompt)
    |
    v
+--[ ITERATION LOOP (max=3) ]--------------------------------------+
|                                                                    |
|  1. DRAFT: ModelGateway.generate_image(prompt) --> image_bytes     |
|                                                                    |
|  2. VERIFY: AuditStage.run(image_bytes, task) --> AuditResult      |
|     - safety_pii         [BLOCKING, hard_reject]                   |
|     - prompt_adherence   [PARALLEL, VLM]                           |
|     - domain_relevance   [PARALLEL, VLM]                           |
|     - technical_quality  [PARALLEL, PIL + VLM]                     |
|     - object_integration [PARALLEL, VLM, skip if no object]        |
|                                                                    |
|  3. DECIDE: DecisionEngine.decide(audit) --> Decision              |
|     - weighted_score >= 0.72 AND tq >= 0.60 AND dr >= 0.65        |
|       --> ACCEPT (break loop)                                      |
|     - weighted_score < 0.50 OR tq < 0.50                          |
|       --> REJECT                                                   |
|     - otherwise --> NEEDS_REVIEW                                   |
|                                                                    |
|  4. SAVE: ExperimentStore.save_iteration()                         |
|                                                                    |
|  5. FIX (if not last iter & not ACCEPT):                           |
|     PromptImprover.improve(prompt, audit, memory_hints) --> prompt' |
|     MemoryStore.add_reject_pattern(scene, check, reason)           |
+-------------------------------------------------------------------+
    |
    v
[MemoryStore.save_recipe()]
[MemoryStore.update_global_stats()]
    |
    v
PipelineResult(verdict, score, image, iterations, budget_spent)
```

### Edit mode (`pipeline_mode: edit`)

Отличия от generate:
- Начальный промпт = `edit_instruction` (не PromptBuilder)
- Draft step = `ModelGateway.edit_image(source_image, instruction)`
- Fix step = улучшение инструкции редактирования (не промпта генерации)
- `max_iterations = EDIT_MAX_ITERATIONS (2)` --- сходится быстрее

---

## 5. State & Memory

### Session State (in-memory)

| Component | State | Lifetime |
|-----------|-------|----------|
| `BudgetTracker` | `spent`, `calls[]` | Single run |
| `Pipeline` | `current_prompt`, `iteration` | Single run |
| `AuditResult` | `checks[]`, `image_bytes` | Single iteration |

### Persistent Memory (`memory/memory.json`)

```json
{
  "recipes": {
    "scene_01+brand_poster": {
      "best_prompt": "...",
      "acceptance_rate": 0.75,
      "avg_iterations": 1.8,
      "runs_count": 4,
      "last_updated": "2025-..."
    }
  },
  "reject_patterns": {
    "scene_01": {
      "domain_relevance: missing brand element": 3,
      "technical_quality: blurry image": 1
    }
  },
  "global_stats": {
    "total_runs": 12,
    "total_accepted": 8,
    "total_rejected": 3,
    "total_needs_review": 1
  }
}
```

**Memory Policy:**
- Recipes обновляются rolling average (не перезаписываются)
- `best_prompt` обновляется только при ACCEPT
- Reject patterns --- counter-based, top-N подаются в PromptImprover как hints
- Global stats --- append-only counters

### Trajectory Log (`runs/<run_id>/trajectory.jsonl`)

Каждый tool call логируется:
```json
{"timestamp": "...", "tool": "vision", "model": "gemma-3-27b-it:free", "check_id": "domain_relevance", "latency_ms": 2340, "tokens_used": 0, "result_score": 0.85}
```

---

## 6. Retrieval (Knowledge Base)

`KnowledgeBase` реализует **explicit retrieval** из `reference_annotations.yaml`:

1. **Source:** YAML файл с аннотированными reference изображениями (зона, ракурс, quality rating, domain markers)
2. **Index:** In-memory list of `ReferenceExample` (загружается при инициализации)
3. **Search:** `find_similar_references(scene_id, n=3)` --- ранжирование по:
   - Zone match (primary)
   - Camera view match
   - Quality rating (higher = better)
   - Domain identity (e.g., `good_wb_identity`)
   - People visible = negative score
4. **Output:** `SceneContext` с `reference_prompt_hint` --- готовая строка для VLM prompt

**Degraded mode:** Если `reference_annotations.yaml` пуст (как в tv_settings), KB работает без few-shot примеров. VLM получает только domain-level контекст.

---

## 7. Tool Integrations (ModelGateway)

| Tool | Model | Purpose | Cost |
|------|-------|---------|------|
| `chat()` | gemma-3-12b-it:free | Текстовые задачи, prompt improvement | $0.00 |
| `vision()` | gemma-3-27b-it:free | VLM audit checks | $0.00 |
| `generate_image()` | gemini-3.1-flash-image-preview | Text -> image | ~$0.001 |
| `edit_image()` | gemini-3.1-flash-image-preview | Image + text -> edited image | ~$0.001 |

**Fallback chain:** Каждый tool имеет 2--3 уровня fallback. При `_RETRYABLE_STATUS` (429, 500, 502, 503, 504) автоматически переключается на fallback модель.

**Error handling:** Structured error codes (`VLM_TIMEOUT`, `VLM_RATE_LIMITED`, `VLM_API_ERROR`, `VLM_PARSE_ERROR`, `IMAGE_DECODE_ERROR`, `INTERNAL_ERROR`) с actionable suggestions.

---

## 8. Failure Modes & Guardrails

### Failure Modes

| Failure | Detection | Recovery |
|---------|-----------|----------|
| VLM timeout | `TimeoutError` | Fallback model (3 levels) |
| Rate limit (429) | HTTP status | Fallback model |
| API error (5xx) | HTTP status | Fallback model |
| VLM parse error | JSON decode fail | Score = 0.5 (neutral), warning logged |
| Budget exceeded | `BudgetTracker` | `BudgetExceeded` exception, pipeline stops |
| Safety violation | `hard_reject` flag | Immediate REJECT, no further iterations |
| All models fail | All fallbacks exhausted | `RuntimeError`, logged as `final_verdict: ERROR` |

### Defense Mechanisms

1. **Budget Guard** --- hard stop при `BUDGET_PER_RUN` ($0.10) и `BUDGET_GLOBAL` ($5.00)
2. **Safety-first audit** --- `safety_pii` check блокирующий, выполняется до остальных
3. **Deterministic decisions** --- NO LLM для ACCEPT/REJECT, только rule-based thresholds
4. **Fail-safe PromptImprover** --- при ошибке LLM возвращает исходный промпт (не ломает pipeline)
5. **Iteration cap** --- `MAX_ITERATIONS=3` / `EDIT_MAX_ITERATIONS=2`
6. **Neutral fallback scores** --- при VLM parse error score=0.5, не hard reject

### Quality Control

- **5 параллельных аудит-чеков** с weighted aggregation
- **Per-check thresholds** (accept / review) из `audit_rubric.yaml`
- **Global thresholds** в DecisionEngine (0.72 accept, 0.50 review)
- **Minimum floors** для critical checks (tq >= 0.60, dr >= 0.65 для ACCEPT)
- **Golden set evaluation** --- 12 test cases, REJECT recall >= 75% requirement

---

## 9. Operational Constraints

### Latency

| Operation | Typical | Worst-case | Notes |
|-----------|---------|-----------|-------|
| `generate_image()` | 4–8s | 20s | Зависит от Gemini Flash |
| `vision()` (один чек) | 1.5–3s | 10s | Free VLM может быть медленнее |
| Parallel audit (4 чека) | 3–5s | 12s | asyncio.gather, bottleneck = slowest check |
| `chat()` (PromptImprover) | 1–2s | 5s | Gemma-3-12b-it:free |
| `IntentResolver.resolve()` | 1–2s | 5s | LLM chat с JSON output |
| Одна DVF итерация | 8–15s | 35s | Draft + Verify + Fix |
| Полный run (3 итерации) | 20–40s | 90s | Worst: все fallback срабатывают |

### Cost

| Scenario | Est. Cost | Notes |
|----------|-----------|-------|
| Одна DVF итерация | ~$0.001 | Только image gen; VLM/text бесплатны |
| Полный run (3 итерации) | ~$0.003 | Один REJECT + два ACCEPT |
| Батч 50 изображений | ~$0.10–0.15 | edit mode, 2 итерации каждое |
| Budget guard | $0.10/run | Hard stop при превышении |

### Reliability

| Dimension | Approach | Status |
|-----------|----------|--------|
| Model availability | 3-level fallback chains | Primary + 2 fallbacks на каждый tool |
| Rate limits | Auto-fallback на 429 | Разные провайдеры (Google / Mistral / NVIDIA) |
| Parse failures | Neutral score (0.5) + warning | Pipeline продолжается, не падает |
| Budget overrun | BudgetExceeded exception | Hard stop, логируется |
| Data persistence | JSON files (memory, runs) | Нет транзакций, возможна частичная запись при crash |
| External DB (MySQL) | Optional, opt-in | Система работает без БД, DB errors не блокируют CSV-режим |

### Параметры генерации/аудита

| Constraint | Value | Configurable |
|------------|-------|-------------|
| Max iterations (generate) | 3 | `config.MAX_ITERATIONS` |
| Max iterations (edit) | 2 | `config.EDIT_MAX_ITERATIONS` |
| Budget per run | $0.10 | env `BUDGET_PER_RUN` |
| Budget global | $5.00 | env `BUDGET_GLOBAL` |
| Image size | 1024x1024 | `config.IMAGE_SIZE` |
| Min resolution (audit) | 512x512 | Hardcoded in `_check_technical_quality` |
| Blur threshold (Laplacian) | 100.0 | Hardcoded in `_check_technical_quality` |
| VLM temperature | 0.2 | `ModelGateway.vision()` default |
| Text temperature | 0.7 | `ModelGateway.chat()` default |

---

## 10. Multi-Domain Architecture

Каждый домен --- это директория `projects/<domain_id>/` с файлами:

```
projects/<domain_id>/
  domain.yaml                 # DomainConfig: style, prompts, thresholds, instructions
  scene_catalog.yaml          # SceneCatalog: zones, views, must-have elements
  object_catalog.yaml         # ObjectCatalog: objects to place in scenes
  audit_rubric.yaml           # AuditRubric: checks, weights, thresholds
  reference_annotations.yaml  # KnowledgeBase: annotated reference images
```

**Switching domains:** `PROJECT_DIR` env var или `config.PROJECT_DIR = ...` в коде. Все модули читают конфигурацию из `DomainSpec.load(project_dir)`.

**Примеры доменов:**
- `wb_pvz` --- generate mode: генерация интерьеров розничной точки выдачи
- `tv_settings` --- edit mode: замена контента на ТВ-экранах

---

## 11. Database Integration (MySQL)

Помимо CSV, система поддерживает **MySQL как источник изображений**.

### Схема БД

```sql
object_types (id, object_type, description)
images       (id, url, object_type_id FK)
results      (id, source_image_id FK, verdict, weighted_score, iterations, output_path)  -- заготовка
```

### Агентный шаг: IntentResolver

Пользователь описывает задачу текстом (например, *«сделай из телевизоров картинки с экранами настроек, 10 штук»*). **IntentResolver** через LLM:

1. Получает список `object_types` из БД
2. Анализирует запрос через `ModelGateway.chat()` с `temperature=0.1`
3. Возвращает `ResolvedIntent`: object_type + count + edit_instruction

### Data Flow (DB mode)

```
User request (text)
    |
    v
IntentResolver.resolve()  --> ResolvedIntent
    |                          (object_type, count, instruction)
    v
DBImageLoader.load()      --> list[ImageRecord]
    |                          (N random images of that type)
    v
Pipeline.run() per image  --> PipelineResult
```

### Компоненты

| Module | Class | Responsibility |
|--------|-------|---------------|
| `db_loader.py` | `DBPool` | Async MySQL connection pool (aiomysql) |
| `db_loader.py` | `ObjectTypeStore` | CRUD для таблицы `object_types` |
| `db_loader.py` | `DBImageLoader` | Загрузка + скачивание изображений по `object_type_id` |
| `intent_resolver.py` | `IntentResolver` | LLM-агент: текст запроса → structured intent |

### Конфигурация

```env
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=
MYSQL_DATABASE=synth_auditor
```

MySQL опциональна --- без неё CSV-пайплайн работает как прежде.
