# Data Flow Diagram

## Token & Data Flow

```mermaid
flowchart LR
    subgraph Input
        YAML[domain.yaml<br/>scene_catalog.yaml<br/>object_catalog.yaml<br/>audit_rubric.yaml]
        REF[reference_annotations.yaml]
        CSV[CSV + Image URLs]
        MEM_IN[memory.json]
    end

    subgraph Processing
        DS[DomainSpec<br/>Pydantic parse]
        KB[KnowledgeBase<br/>Reference retrieval]
        PB[PromptBuilder<br/>Text assembly]
        GW_GEN[ModelGateway<br/>generate_image /<br/>edit_image]
        GW_VLM[ModelGateway<br/>vision]
        GW_TXT[ModelGateway<br/>chat]
        AUDIT[AuditStage<br/>5 checks]
        DE[DecisionEngine<br/>Rule-based]
        PI[PromptImprover<br/>LLM refine]
    end

    subgraph Output
        RUNS[runs/<br/>images + JSON]
        MEM_OUT[memory.json<br/>updated]
        TRAJ[trajectory.jsonl]
        RESULT[PipelineResult<br/>verdict + image]
    end

    subgraph External
        OR[OpenRouter API]
    end

    YAML --> DS
    REF --> KB
    CSV --> IL[ImageLoader]
    MEM_IN --> PIPE[Pipeline]

    DS --> PB
    DS --> KB
    DS --> AUDIT
    KB --> PB
    KB --> AUDIT
    IL --> PIPE

    PB --> GW_GEN
    PIPE --> GW_GEN
    GW_GEN --> OR
    OR --> GW_GEN

    GW_GEN -->|image_bytes| AUDIT
    AUDIT --> GW_VLM
    GW_VLM --> OR
    OR --> GW_VLM

    AUDIT -->|AuditResult| DE
    DE -->|Decision| PIPE

    PIPE -->|audit failures| PI
    PI --> GW_TXT
    GW_TXT --> OR
    OR --> GW_TXT
    PI -->|improved prompt| PB

    PIPE --> RUNS
    PIPE --> MEM_OUT
    PIPE --> TRAJ
    PIPE --> RESULT
```

## Data Types at Boundaries

| Boundary | Data Type | Format |
|----------|-----------|--------|
| YAML -> DomainSpec | Domain config | YAML -> Pydantic models |
| CSV -> ImageLoader | Image records | CSV + HTTP download -> `ImageRecord` |
| Pipeline -> ModelGateway | Prompts / images | `str` / `bytes` |
| ModelGateway -> OpenRouter | API request | JSON (OpenAI-compatible) |
| OpenRouter -> ModelGateway | API response | JSON with text / image data-URL / image URL |
| AuditStage -> DecisionEngine | Check results | `AuditResult` (list of `CheckResult`) |
| Pipeline -> MemoryStore | Recipes & patterns | Python dict -> JSON file |
| Pipeline -> ExperimentStore | Run artifacts | JSON files + PNG images + JSONL trajectory |

## Budget Flow

```mermaid
flowchart TD
    CALL[API Call] --> BT[BudgetTracker.track]
    BT --> COST{cost lookup<br/>COST_PER_CALL}
    COST --> ADD[spent += cost]
    ADD --> CHECK{spent > BUDGET_PER_RUN?}
    CHECK -->|no| OK[Continue]
    CHECK -->|yes| STOP[BudgetExceeded<br/>Pipeline stops]
```
