# Spec: Serving & Configuration

**Modules:** `src/auditor/config.py`, `scripts/`, `projects/`
**Role:** Запуск системы, конфигурация, управление секретами и моделями.

---

## 1. Startup

### Entry Points

| Script | Purpose | Command |
|--------|---------|---------|
| `scripts/smoke_test.py` | Connectivity + E2E test | `python scripts/smoke_test.py --scene scene_01_counter_front` |
| `scripts/run_batch.py` | Batch CSV processing | `python scripts/run_batch.py --csv data.csv --project tv_settings` |
| `scripts/run_evals.py` | Golden set evaluation | `python scripts/run_evals.py --verbose` |

### Startup Sequence

```
1. sys.path.insert(0, "src/")     # Add src to PYTHONPATH
2. logging.basicConfig()           # Configure logging
3. from auditor import config      # Load .env, set paths
4. config.PROJECT_DIR = ...        # Switch project (if needed)
5. DomainSpec.load(project_dir)    # Parse YAML configs
6. Pipeline(gateway, spec, ...)    # Create orchestrator
7. pipeline.run(task)              # Execute DVF loop
```

---

## 2. Configuration Hierarchy

```
.env                           # Secrets (OPENROUTER_API_KEY)
  |
  v
config.py                      # Defaults, model IDs, thresholds
  |
  v
projects/<domain>/domain.yaml  # Domain-specific overrides
  |
  v
CLI arguments                  # Runtime overrides (--limit, --instruction)
  |
  v
Environment variables          # BUDGET_PER_RUN, BUDGET_GLOBAL, PROJECT_DIR
```

### Priority (high to low)

1. Environment variables (`BUDGET_PER_RUN`, `PROJECT_DIR`)
2. CLI arguments (`--max-iterations`, `--instruction`)
3. Domain YAML (`domain.yaml`, `audit_rubric.yaml`)
4. `config.py` defaults

---

## 3. Secrets Management

| Secret | Source | Required |
|--------|--------|----------|
| `OPENROUTER_API_KEY` | `.env` file | Yes |

### `.env` Format

```env
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxx
BUDGET_PER_RUN=0.10
BUDGET_GLOBAL=5.00
PROJECT_DIR=projects/your_domain
```

### `.env.example`

```env
OPENROUTER_API_KEY=sk-or-v1-your-key-here
```

**Security:**
- `.env` в `.gitignore` (не попадает в репозиторий)
- `os.environ["OPENROUTER_API_KEY"]` --- hard fail при отсутствии (не default)
- API ключ передаётся в `AsyncOpenAI(api_key=...)`, не в headers

---

## 4. Model Versions

Все модели зафиксированы в `config.py`:

```python
# Image generation / editing
IMAGE_GEN_MODEL   = "google/gemini-3.1-flash-image-preview"
IMAGE_GEN_FALLBACK = "google/gemini-2.5-flash-image"

# VLM audit
VISION_MODEL     = "google/gemma-3-27b-it:free"
VISION_FALLBACK  = "mistralai/mistral-small-3.1-24b-instruct:free"
VISION_FALLBACK2 = "nvidia/nemotron-nano-12b-v2-vl:free"

# Text (prompt improvement)
TEXT_MODEL    = "google/gemma-3-12b-it:free"
TEXT_FALLBACK = "mistralai/mistral-small-3.1-24b-instruct:free"
```

**Versioning policy:**
- Модели зафиксированы строками (не latest/auto)
- `:free` суффикс --- бесплатные модели через OpenRouter
- Обновление моделей = изменение `config.py` + re-run evals

---

## 5. Project Configuration

### Структура проекта

```
projects/<domain_id>/
  domain.yaml                 # Основная конфигурация домена
  scene_catalog.yaml          # Каталог сцен
  object_catalog.yaml         # Каталог объектов
  audit_rubric.yaml           # Чеки и веса аудита
  reference_annotations.yaml  # Reference аннотации для KB
```

### domain.yaml --- ключевые секции

| Section | Purpose | Used By |
|---------|---------|---------|
| `domain_id`, `name` | Identification | All modules |
| `pipeline_mode` | `"generate"` or `"edit"` | Pipeline |
| `edit_instruction` | Default edit instruction | Pipeline (edit mode) |
| `prompt_builder.*` | Style, zones, camera views | PromptBuilder |
| `improver.*` | System prompt, rules | PromptImprover |
| `relevance_check.*` | Questions + weights | AuditStage |
| `safety.*` | Brand check config | AuditStage |
| `suggestions.*` | Per-check improvement tips | DecisionEngine |
| `knowledge_base.*` | Field names for KB retrieval | KnowledgeBase |

### Switching Projects

```python
# Via environment variable
export PROJECT_DIR=projects/tv_settings

# Via code (in run_batch.py)
config.PROJECT_DIR = Path("projects/tv_settings")
spec = DomainSpec.load(config.PROJECT_DIR)
```

---

## 6. Directory Layout

```
domain-synth-auditor/
  .env                    # Secrets (gitignored)
  .env.example            # Template
  .gitignore
  pyproject.toml          # Dependencies
  src/
    auditor/              # Core modules (11 files)
  projects/
    wb_pvz/               # Example domain (pickup points)
    tv_settings/          # TV settings domain
  scripts/
    smoke_test.py         # E2E test
    run_batch.py          # Batch processing
    run_evals.py          # Evaluation
  evals/
    golden_set.yaml       # 12 test cases
  docs/                   # Documentation
  memory/                 # Persistent memory (gitignored)
  runs/                   # Experiment logs (gitignored)
  images/                 # Source images
```

---

## 7. Dependencies

```toml
[project]
requires-python = ">=3.11"
dependencies = [
    "openai>=1.30",       # AsyncOpenAI for OpenRouter
    "pydantic>=2.0",      # Data models
    "pyyaml>=6.0",        # YAML configs
    "python-dotenv>=1.0", # .env loading
    "Pillow>=10.0",       # Image analysis (blur, exposure)
    "aiohttp>=3.9",       # Async HTTP (unused, httpx preferred)
]
```

Runtime: `httpx` (used by ImageLoader for CSV downloads, bundled with openai).
