# Spec: Observability & Evaluation

**Modules:** `src/auditor/experiment_store.py`, `scripts/run_evals.py`, `evals/`
**Role:** Метрики, логирование, трассировка, evaluation framework.

---

## 1. Metrics

### Per-Run Metrics (meta.json)

| Metric | Type | Description |
|--------|------|-------------|
| `total_latency_s` | float | Общее время выполнения run |
| `total_iterations` | int | Количество DVF итераций |
| `total_tool_calls` | int | Общее число tool calls (VLM, generation, improve) |
| `total_tokens_used` | int | Суммарное потребление токенов (0 для free моделей) |
| `final_verdict` | str | ACCEPT / REJECT / NEEDS_REVIEW / ERROR |
| `budget_spent` | float | Потраченный бюджет (USD) |

### Per-Iteration Metrics (decision.json)

| Metric | Type | Description |
|--------|------|-------------|
| `weighted_score` | float | Агрегированный score (0.0--1.0) |
| `scores` | dict | Per-check scores: `{check_id: score}` |
| `verdict` | str | Verdict данной итерации |
| `reasons` | list[str] | Причины вердикта |
| `suggestions` | list[str] | Рекомендации по улучшению |

### Global Metrics (memory.json)

| Metric | Type | Description |
|--------|------|-------------|
| `total_runs` | int | Общее число запусков |
| `total_accepted` | int | Количество ACCEPT |
| `total_rejected` | int | Количество REJECT |
| `total_needs_review` | int | Количество NEEDS_REVIEW |
| `acceptance_rate` per recipe | float | Rolling average per scene+object |

---

## 2. Logging

### Structured Logging

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s --- %(message)s",
    datefmt="%H:%M:%S",
)
```

### Log Points

| Logger | Level | Events |
|--------|-------|--------|
| `pipeline` | INFO | Start/end run, iteration progress, verdict |
| `pipeline` | WARNING | Budget approaching limit |
| `pipeline` | ERROR | BudgetExceeded, RuntimeError |
| `audit_stage` | INFO | Check start/complete with scores |
| `audit_stage` | WARNING | VLM parse error (neutral score applied) |
| `audit_stage` | ERROR | Check failure with structured error code |
| `model_gateway` | INFO | API call start, model used |
| `model_gateway` | WARNING | Fallback triggered |
| `batch_runner` | INFO | Per-image progress, final summary |

### Structured Error Messages

```
VLM_TIMEOUT: [domain_relevance] TimeoutError: Request timed out | suggestion: retry or switch to faster model
VLM_RATE_LIMITED: [safety_pii] RateLimitError: 429 | suggestion: wait 30s or switch model
VLM_PARSE_ERROR: [prompt_adherence] JSONDecodeError: ... | suggestion: simplify prompt, request cleaner JSON
```

---

## 3. Traces (Trajectory)

### trajectory.jsonl Format

Append-only JSONL файл в каждой `iteration_NNN/` директории.

```jsonl
{"timestamp":"2025-03-20T14:30:23","tool":"vision","model":"google/gemma-3-27b-it:free","check_id":"safety_pii","latency_ms":1820,"tokens_used":0,"result_score":1.0}
{"timestamp":"2025-03-20T14:30:25","tool":"vision","model":"google/gemma-3-27b-it:free","check_id":"prompt_adherence","latency_ms":2340,"tokens_used":0,"result_score":0.73}
{"timestamp":"2025-03-20T14:30:25","tool":"vision","model":"google/gemma-3-27b-it:free","check_id":"domain_relevance","latency_ms":2150,"tokens_used":0,"result_score":0.85}
{"timestamp":"2025-03-20T14:30:26","tool":"vision","model":"google/gemma-3-27b-it:free","check_id":"technical_quality","latency_ms":1950,"tokens_used":0,"result_score":0.78}
{"timestamp":"2025-03-20T14:30:27","tool":"generate_image","model":"google/gemini-3.1-flash-image-preview","check_id":"draft","latency_ms":4200,"tokens_used":0,"result_score":null}
```

### Trace Fields

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | ISO 8601 | Время вызова |
| `tool` | str | `vision`, `generate_image`, `edit_image`, `prompt_improve` |
| `model` | str | Фактически использованная модель (может быть fallback) |
| `check_id` | str | ID проверки или `"draft"` / `"improve"` |
| `latency_ms` | float | Время выполнения в мс |
| `tokens_used` | int | Потребление токенов (0 для free) |
| `result_score` | float \| null | Результат проверки (null для non-scoring calls) |

---

## 4. Evaluation Framework

### Golden Set (`evals/golden_set.yaml`)

12 тестовых кейсов с human labels:

| Category | Count | Examples |
|----------|-------|---------|
| ACCEPT | 10 | High-quality domain images (4-5/5 quality) |
| REJECT | 1 | Person in frame + foreign objects |
| NEEDS_REVIEW | 1 | Weak branding (3/5 quality) |

### Test Case Schema

```yaml
- id: "case_001"
  image_file: "image_abc.jpg"
  scene_id: "scene_01_counter_front"
  object_id: null
  expected_verdict: "ACCEPT"
  expected_checks:
    safety_pii: pass
    prompt_adherence: pass
    domain_relevance: pass
    technical_quality: pass
    object_integration: skip
  human_notes: "Clean counter, good brand elements visible"
```

### Evaluation Metrics

| Metric | Formula | Target |
|--------|---------|--------|
| Overall agreement | `matches / total` | >= 75% |
| REJECT recall | `TP_reject / (TP_reject + FN_reject)` | **>= 75%** (safety critical) |
| Per-class precision | `TP_class / (TP_class + FP_class)` | Reported |
| Per-class recall | `TP_class / (TP_class + FN_class)` | Reported |
| Per-class F1 | `2 * P * R / (P + R)` | Reported |
| Avg latency | `sum(elapsed) / count` | Reported |

### Eval Output (`evals/eval_results.json`)

```json
{
  "results": [
    {
      "id": "case_001",
      "expected": "ACCEPT",
      "actual": "ACCEPT",
      "match": true,
      "score": 0.82,
      "elapsed_s": 12.3,
      "check_results": {"safety_pii": "pass", "domain_relevance": "pass"},
      "check_mismatches": [],
      "reasons": [],
      "human_notes": "..."
    }
  ],
  "summary": {
    "total": 12,
    "valid": 12,
    "errors": 0,
    "agreement_pct": 83.3,
    "per_class": {
      "ACCEPT": {"precision": 0.90, "recall": 0.90, "f1": 0.90},
      "REJECT": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
      "NEEDS_REVIEW": {"precision": 0.50, "recall": 1.0, "f1": 0.67}
    },
    "avg_latency_s": 15.2,
    "reject_recall_ok": true
  }
}
```

### Running Evals

```bash
# Full eval
python scripts/run_evals.py --verbose

# Single case
python scripts/run_evals.py --case case_001

# Eval mode (skip real generation, use preloaded images)
# Set config.EVAL_MODE = True in run_evals.py
```

---

## 5. IntentResolver Evaluation

**Module:** `scripts/run_intent_evals.py`
**Golden Set:** `evals/intent_golden_set.yaml`

### Метод

Classification accuracy on labeled test set. MySQL НЕ нужна --- `ObjectType` list подаётся из YAML.

### Golden Set (18 test cases)

| Category | Count | Description |
|----------|-------|-------------|
| `direct_match` | 4 | Прямое название типа в запросе |
| `synonym` | 4 | Синонимы и перифразы |
| `english` | 2 | Запросы на английском |
| `no_count` | 2 | Без указания количества (default=10) |
| `no_match` | 2 | Несуществующий тип (expected: null) |
| `ambiguous` | 4 | Неоднозначные запросы |

### Metrics

| Metric | Formula | Target |
|--------|---------|--------|
| Type accuracy | correct_type / total | >= 80% |
| Count accuracy (exact) | correct_count / total | >= 70% |
| Count accuracy (±2) | abs(actual-expected) <= 2 / total | >= 85% |
| JSON parse rate | successful_parses / total | >= 95% |
| Per-type precision | TP / (TP + FP) | Reported |
| Per-type recall | TP / (TP + FN) | Reported |

### Output

Results saved to `evals/intent_eval_results.json` with per-case details and summary.

### Running

```bash
python scripts/run_intent_evals.py              # all cases
python scripts/run_intent_evals.py --verbose     # per-case details
python scripts/run_intent_evals.py --case intent_001  # single case
```

---

## 6. Health Checks

### smoke_test.py

| Check | What | Pass Criteria |
|-------|------|--------------|
| `test_spec()` | DomainSpec loading | No exceptions |
| `test_connectivity()` | OpenRouter chat API | Response received |
| `test_generate()` | Image generation | Image bytes > 0 |
| `test_audit()` | VLM audit pipeline | AuditResult with scores |
| `test_decision()` | Decision engine | Verdict in {ACCEPT, REJECT, NEEDS_REVIEW} |
| `test_store()` | Experiment store | Run directory created |

```bash
# Quick connectivity test
python scripts/smoke_test.py --connectivity-only

# Full E2E test
python scripts/smoke_test.py --scene scene_01_counter_front --object wb_rollup_poster
```
