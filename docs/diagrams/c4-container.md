# C4 Container Diagram

Основные контейнеры (процессы, хранилища) системы.

```mermaid
C4Container
    title Domain Synth Auditor --- Container Diagram

    Person(operator, "Operator")

    Container_Boundary(app, "Domain Synth Auditor") {
        Container(cli, "CLI Scripts", "Python", "smoke_test, run_batch,<br/>run_evals")
        Container(pipeline, "Pipeline (Orchestrator)", "Python async", "DVF loop: Draft->Verify->Fix")
        Container(audit, "Audit Stage", "Python async", "5 parallel VLM checks")
        Container(gateway, "Model Gateway", "Python async", "OpenRouter API client<br/>with fallback chains")
        Container(domain, "Domain Spec", "Pydantic", "YAML -> typed models")
        Container(kb, "Knowledge Base", "Python", "Reference retrieval<br/>+ few-shot context")
        Container(memory, "Memory Store", "JSON file", "Recipes, reject patterns,<br/>global stats")
        Container(expstore, "Experiment Store", "Filesystem", "runs/, trajectory.jsonl")
    }

    System_Ext(openrouter, "OpenRouter API")
    System_Ext(fs, "projects/ YAML configs")

    Rel(operator, cli, "Runs")
    Rel(cli, pipeline, "Creates tasks,<br/>invokes run()")
    Rel(pipeline, audit, "Sends image<br/>for verification")
    Rel(pipeline, gateway, "Draft: generate/<br/>edit image")
    Rel(audit, gateway, "VLM vision()<br/>calls")
    Rel(pipeline, kb, "Retrieve scene<br/>context")
    Rel(pipeline, memory, "Load/save<br/>recipes")
    Rel(pipeline, expstore, "Log iterations<br/>& trajectory")
    Rel(gateway, openrouter, "HTTPS API<br/>calls")
    Rel(domain, fs, "Load YAML<br/>configs")
    Rel(kb, domain, "Uses DomainSpec<br/>& references")
```

## Container Responsibilities

| Container | Responsibility | Technology |
|-----------|---------------|------------|
| **CLI Scripts** | Entry points, argument parsing, project switching | argparse, asyncio.run() |
| **Pipeline** | DVF orchestration, budget tracking, iteration control | async, BudgetTracker |
| **Audit Stage** | Parallel VLM checks, rule-based checks, score aggregation | asyncio.gather(), PIL |
| **Model Gateway** | API calls with fallback, response parsing, image extraction | AsyncOpenAI, httpx |
| **Domain Spec** | YAML parsing, Pydantic validation, type-safe config access | Pydantic v2, PyYAML |
| **Knowledge Base** | Reference retrieval, similarity ranking, prompt hint generation | In-memory search |
| **Memory Store** | Cross-run persistence: recipes, reject patterns, stats | JSON read/write |
| **Experiment Store** | Run logging, iteration artifacts, trajectory JSONL | Filesystem I/O |
