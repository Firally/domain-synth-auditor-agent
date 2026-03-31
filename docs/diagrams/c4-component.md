# C4 Component Diagram

Детализация внутренних компонентов и их взаимодействий.

```mermaid
graph TB
    subgraph "CLI Layer"
        smoke[smoke_test.py]
        batch[run_batch.py]
        evals[run_evals.py]
    end

    subgraph "Orchestration"
        pipe[Pipeline<br/>DVF Loop]
        budget[BudgetTracker<br/>Cost Guard]
    end

    subgraph "Draft Stage"
        pb[PromptBuilder<br/>Positive + Negative]
        pi[PromptImprover<br/>LLM Fix Step]
    end

    subgraph "Verify Stage"
        audit[AuditStage<br/>5 Parallel Checks]
        de[DecisionEngine<br/>Rule-based Verdict]
    end

    subgraph "Infrastructure"
        gw[ModelGateway<br/>OpenRouter Client]
        kb[KnowledgeBase<br/>Reference Retrieval]
        mem[MemoryStore<br/>Recipes & Patterns]
        exp[ExperimentStore<br/>Trajectory Log]
    end

    subgraph "Data Layer"
        ds[DomainSpec<br/>Pydantic Models]
        il[ImageLoader<br/>CSV/URL Loader]
    end

    subgraph "External"
        or[OpenRouter API]
        csv[CSV Source]
    end

    smoke --> pipe
    batch --> pipe
    batch --> il
    evals --> pipe

    pipe --> pb
    pipe --> pi
    pipe --> audit
    pipe --> de
    pipe --> budget
    pipe --> mem
    pipe --> exp
    pipe --> gw

    audit --> gw
    pi --> gw
    kb --> ds

    pipe --> kb
    audit --> kb
    pb --> ds
    il --> csv

    gw --> or

    classDef external fill:#f5f5f5,stroke:#999
    class or,csv external
```

## Component Details

### Orchestration Layer
- **Pipeline** --- основной DVF loop. Координирует Draft/Verify/Fix. Управляет итерациями и бюджетом.
- **BudgetTracker** --- считает расходы по `COST_PER_CALL`. Hard stop при превышении `BUDGET_PER_RUN`.

### Draft Stage
- **PromptBuilder** --- собирает промпт из scene + object + domain style + reject hints. Используется только в generate mode.
- **PromptImprover** --- LLM call для улучшения промпта/инструкции на основе audit failures и memory hints. Fail-safe: возвращает исходный промпт при ошибке.

### Verify Stage
- **AuditStage** --- 5 параллельных проверок. Safety check блокирующий (выполняется первым). Остальные 4 --- concurrent. Structured error codes.
- **DecisionEngine** --- детерминистическая агрегация scores. Weighted average + per-check minimums. Без LLM.

### Infrastructure Layer
- **ModelGateway** --- единый клиент OpenRouter. 4 метода: `chat`, `vision`, `generate_image`, `edit_image`. 3-level fallback.
- **KnowledgeBase** --- retriever из reference_annotations. Similarity ranking, few-shot prompt hints.
- **MemoryStore** --- JSON persistence. Recipes (best prompts), reject patterns (counters), global stats.
- **ExperimentStore** --- filesystem logging. `meta.json`, `prompt.json`, `image.png`, `audit_result.json`, `trajectory.jsonl` per iteration.

### Data Layer
- **DomainSpec** --- Pydantic-модели для всех YAML конфигураций. Type-safe access.
- **ImageLoader** --- CSV reader с auto-detect URL column. Async download через httpx.
