# Spec: Memory & Context

**Modules:** `src/auditor/memory_store.py`, `src/auditor/experiment_store.py`
**Role:** Управление session state, persistent memory и контекстным окном.

---

## 1. Session State (In-Memory)

### BudgetTracker (per-run)

```python
class BudgetTracker:
    limit: float       # BUDGET_PER_RUN ($0.10)
    spent: float       # Running total
    calls: list[dict]  # Log of {model, tool, cost}
```

- Создаётся при каждом `Pipeline.run()`
- Не сохраняется между runs
- Hard stop: `BudgetExceeded` exception

### Pipeline State (per-run)

| Variable | Lifetime | Description |
|----------|----------|-------------|
| `current_prompt` | Iteration | Текущий промпт (обновляется PromptImprover) |
| `negative` | Iteration | Negative prompt (обновляется PromptBuilder) |
| `recipe` | Run | Загруженный рецепт из MemoryStore |
| `memory_hints` | Run | Top-N reject patterns из MemoryStore |
| `iteration` | Run | Текущий номер итерации (0-based) |

### AuditResult (per-iteration)

| Field | Type | Description |
|-------|------|-------------|
| `checks` | `list[CheckResult]` | 5 check results |
| `image_bytes` | `bytes` | Проверяемое изображение |

---

## 2. Persistent Memory (MemoryStore)

**Storage:** `memory/memory.json`

### Schema

```json
{
  "recipes": {
    "<scene_id>+<object_id|none>": {
      "best_prompt": "string",
      "acceptance_rate": 0.75,
      "avg_iterations": 1.8,
      "runs_count": 4,
      "last_updated": "2025-03-20T12:00:00"
    }
  },
  "reject_patterns": {
    "<scene_id>": {
      "reason text": 3
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

### Memory Policy

| Operation | Policy | Details |
|-----------|--------|---------|
| Recipe save | Rolling average | `acceptance_rate`, `avg_iterations` --- weighted by runs_count |
| Best prompt | Conditional update | Обновляется **только** при ACCEPT verdict |
| Reject patterns | Counter increment | `reason -> count`, top-N используются как hints |
| Global stats | Append-only | Increment counters, never decrement |
| File I/O | Read-on-init, write-on-change | `_load()` при init, `_save()` при каждой записи |

### API

```python
# Recipes
load_recipe(scene_id, object_id) -> dict | None
save_recipe(scene_id, object_id, prompt, iterations_used, verdict)

# Reject patterns
add_reject_pattern(scene_id, check_id, reason)
get_reject_hints(scene_id, top_n=3) -> list[str]

# Global stats
update_global_stats(verdict)
get_global_stats() -> dict
```

---

## 3. Trajectory Log (ExperimentStore)

**Storage:** `runs/run_<timestamp>_<scene>_<object>/`

### Directory Structure

```
runs/
  run_20250320_143022_scene_01_wb_rollup_poster/
    meta.json               # Run metadata
    iteration_001/
      prompt.json           # {iteration, positive, negative}
      image.png             # Generated/edited image
      audit_result.json     # Full audit results
      decision.json         # {verdict, weighted_score, scores, reasons, suggestions}
      trajectory.jsonl      # Tool call log (append-only)
    iteration_002/
      ...
```

### Trajectory Entry Schema

```json
{
  "timestamp": "2025-03-20T14:30:25.123",
  "tool": "vision",
  "model": "google/gemma-3-27b-it:free",
  "check_id": "domain_relevance",
  "latency_ms": 2340,
  "tokens_used": 0,
  "result_score": 0.85
}
```

### Logged Tool Calls

| Tool | When | Extra Fields |
|------|------|-------------|
| `vision` | Each VLM audit check | `check_id`, `result_score` |
| `generate_image` | Draft step (generate mode) | |
| `edit_image` | Draft step (edit mode) | |
| `prompt_improve` | Fix step | |

### Run Metadata (meta.json)

```json
{
  "run_id": "run_20250320_143022_scene_01_wb_rollup_poster",
  "started_at": "2025-03-20T14:30:22",
  "scene_id": "scene_01_counter_front",
  "object_id": "wb_rollup_poster",
  "max_iterations": 3,
  "notes": "",
  "finished_at": "2025-03-20T14:31:45",
  "final_verdict": "ACCEPT",
  "total_iterations": 2,
  "metrics": {
    "total_latency_s": 83.2,
    "total_tool_calls": 12,
    "total_tokens_used": 0
  }
}
```

---

## 4. Context Budget

### VLM Context (per check)

Каждый VLM вызов получает:
- System: нет (embedded в prompt)
- Image: 1 изображение (data-URL, ~100-500KB base64)
- Prompt: ~200-500 tokens (check-specific question + reference hints)

### LLM Context (PromptImprover)

| Component | Approx. Tokens |
|-----------|----------------|
| System prompt | ~100 |
| Current prompt/instruction | ~100-300 |
| Audit failures list | ~50-200 |
| Memory hints (top-3) | ~50-100 |
| Improvement rules | ~100-200 |
| **Total** | **~400-900** |

### Context Window Utilization

- VLM модели (Gemma-3-27b, Mistral-small-3.1): 8K-32K context window --- используется <5%
- Text модели (Gemma-3-12b): 8K context --- используется ~10%
- Image gen (Gemini Flash): не ограничен context window (image-to-image)
- **Вывод:** context budget не является bottleneck для текущей архитектуры

---

## 5. Memory Lifecycle

```
Pipeline.run() START
    |
    +--> MemoryStore.load_recipe()     # Read: warm start
    +--> MemoryStore.get_reject_hints() # Read: historical patterns
    |
    [DVF Loop iterations]
    |    |
    |    +--> ExperimentStore.log_tool_call()  # Write: trajectory
    |    +--> ExperimentStore.save_iteration() # Write: artifacts
    |    +--> MemoryStore.add_reject_pattern() # Write: on reject
    |
    +--> MemoryStore.save_recipe()        # Write: final prompt
    +--> MemoryStore.update_global_stats() # Write: counters
    +--> ExperimentStore.finish_run()      # Write: meta.json
    |
Pipeline.run() END
```
