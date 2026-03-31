# Spec: Agent / Orchestrator (Pipeline)

**Module:** `src/auditor/pipeline.py`
**Role:** Центральный оркестратор DVF-цикла. Координирует Draft, Verify, Fix шаги, управляет бюджетом и итерациями.

---

## 1. Pipeline Steps

| Step | Component | LLM Call? | Description |
|------|-----------|-----------|-------------|
| **0. Init** | Pipeline | No | Load recipe, memory hints, create BudgetTracker |
| **1. Draft** | ModelGateway | Yes | Generate or edit image |
| **2. Verify** | AuditStage | Yes (5x) | Parallel VLM audit checks |
| **3. Decide** | DecisionEngine | **No** | Rule-based verdict |
| **4. Log** | ExperimentStore | No | Save iteration artifacts |
| **5. Fix** | PromptImprover | Yes | LLM refines prompt/instruction |

---

## 2. Transition Rules

```
START --> [Load Memory] --> DRAFT

DRAFT --> VERIFY (always)

VERIFY --> DECIDE (always)

DECIDE:
  if ACCEPT          --> EXIT (success)
  if hard_reject     --> EXIT (reject, no retry)
  if last_iteration  --> EXIT (final verdict)
  else               --> FIX

FIX --> DRAFT (next iteration)
```

### Mode-Specific Branching

| Condition | Generate Mode | Edit Mode |
|-----------|--------------|-----------|
| Initial prompt | `PromptBuilder.build()` | `task.edit_instruction` |
| Draft call | `gateway.generate_image(prompt)` | `gateway.edit_image(source, instruction)` |
| Fix output | Updated prompt + negative | Updated instruction only |
| Max iterations | `MAX_ITERATIONS (3)` | `EDIT_MAX_ITERATIONS (2)` |

---

## 3. Stop Conditions

| Condition | Action | Recoverable? |
|-----------|--------|-------------|
| `verdict == ACCEPT` | Return result | N/A (success) |
| `iteration == max_iterations` | Return last verdict | Yes (re-run) |
| `BudgetExceeded` | Stop pipeline | Yes (increase budget) |
| `hard_reject` (safety) | Immediate REJECT | No (image unsafe) |
| All fallback models fail | `RuntimeError` | Yes (retry later) |
| `EVAL_MODE == True` | Use preloaded image, skip generation | N/A |

---

## 4. Retry & Fallback Strategy

### Model-Level Fallback (in ModelGateway)

```
vision():       gemma-3-27b -> mistral-small-3.1 -> nemotron-nano-12b
chat():         gemma-3-12b -> mistral-small-3.1
generate/edit(): gemini-3.1-flash -> gemini-2.5-flash
```

Trigger: `_RETRYABLE_STATUS = {429, 500, 502, 503, 504}`

### Pipeline-Level Retry

- **No automatic retry** на уровне Pipeline. Каждая итерация --- один Draft+Verify+Fix цикл.
- При ошибке в Draft: exception propagates, `final_verdict = "ERROR"`
- При ошибке в Verify (одного чека): check score = 0.5 (neutral), pipeline продолжает
- При ошибке в Fix: `PromptImprover` возвращает исходный промпт (fail-safe)

### Memory-Based Warm Start

```python
recipe = self.memory.load_recipe(scene_id, object_id)
if recipe and recipe.get("best_prompt"):
    current_prompt = recipe["best_prompt"]  # Skip cold-start
    memory_loaded = True
```

---

## 5. Budget Management

### BudgetTracker

```python
class BudgetTracker:
    def track(self, model: str, tool: str) -> None:
        cost = config.COST_PER_CALL.get(model, 0.0)
        self.spent += cost
        self.calls.append({"model": model, "tool": tool, "cost": cost})
        if self.spent > self.limit:
            raise BudgetExceeded(f"Budget ${self.limit} exceeded: ${self.spent:.4f}")
```

### Cost Estimate per Run

| Operation | Calls per Iter | Cost per Call | Cost per Iter |
|-----------|---------------|---------------|---------------|
| generate/edit_image | 1 | $0.001 | $0.001 |
| vision (audit) | 4-5 | $0.00 | $0.00 |
| chat (improver) | 0-1 | $0.00 | $0.00 |
| **Total per iteration** | | | **~$0.001** |
| **3 iterations (max)** | | | **~$0.003** |

Budget limit ($0.10) позволяет ~100 iterations per run --- значительный запас.

---

## 6. Orchestration Internals

### Pipeline.run() Pseudocode

```python
async def run(self, task: GenerationTask) -> PipelineResult:
    # 0. Init
    budget = BudgetTracker(limit=BUDGET_PER_RUN)
    store.start_run(task)
    recipe = memory.load_recipe(task.scene_id, task.object_id)
    hints = memory.get_reject_hints(task.scene_id)

    # Set initial prompt
    if task.mode == "edit":
        current_prompt = task.edit_instruction
    elif recipe and recipe["best_prompt"]:
        current_prompt = recipe["best_prompt"]
    else:
        current_prompt, negative = builder.build(task)

    max_iter = EDIT_MAX_ITERATIONS if task.mode == "edit" else task.max_iterations

    # DVF Loop
    for i in range(max_iter):
        # 1. DRAFT
        if task.mode == "edit":
            image = await gateway.edit_image(task.source_image, current_prompt)
        else:
            image = await gateway.generate_image(current_prompt)
        budget.track(model, tool)

        # 2. VERIFY
        audit = await auditor.run(image, task)

        # 3. DECIDE
        decision = engine.decide(audit, has_object=bool(task.object_id))

        # 4. LOG
        store.save_iteration(current_prompt, negative, image, audit, decision)

        if decision.verdict == Verdict.ACCEPT:
            break

        # 5. FIX (if not last iteration)
        if i < max_iter - 1:
            current_prompt = await improver.improve(current_prompt, audit, hints)
            # Save reject patterns
            for reason in decision.reasons:
                memory.add_reject_pattern(task.scene_id, ...)

    # Finalize
    memory.save_recipe(task.scene_id, task.object_id, current_prompt, i+1, verdict)
    memory.update_global_stats(verdict)
    store.finish_run(verdict, i+1)

    return PipelineResult(...)
```

---

## 7. Dependencies

```
Pipeline
  ├── ModelGateway      (Draft, injected)
  ├── DomainSpec        (Config, injected)
  ├── KnowledgeBase     (Created from spec)
  ├── PromptBuilder     (Created from spec)
  ├── AuditStage        (Created with gateway + spec + kb)
  ├── DecisionEngine    (Created from spec)
  ├── PromptImprover    (Created with gateway + domain)
  ├── MemoryStore       (Optional, injected)
  └── ExperimentStore   (Optional, injected)
```
