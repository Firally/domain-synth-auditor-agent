# C4 Context Diagram

Верхнеуровневое представление системы и её внешних зависимостей.

```mermaid
C4Context
    title Domain Synth Auditor --- System Context

    Person(operator, "Operator", "Запускает pipeline,<br/>проверяет NEEDS_REVIEW")

    System(auditor, "Domain Synth Auditor", "Генерирует/редактирует<br/>синтетические изображения<br/>с VLM-аудитом (DVF loop)")

    System_Ext(openrouter, "OpenRouter API", "Unified LLM/VLM gateway:<br/>Gemini, Gemma, Mistral, Nemotron")

    System_Ext(csv_source, "CSV / Image Source", "CSV с URL изображений<br/>для batch processing")

    System_Ext(filesystem, "Local Filesystem", "runs/, memory/, projects/<br/>Persistent storage")

    Rel(operator, auditor, "CLI: smoke_test, run_batch,<br/>run_evals")
    Rel(auditor, openrouter, "HTTPS: chat, vision,<br/>generate_image, edit_image")
    Rel(auditor, csv_source, "HTTP: download source images")
    Rel(auditor, filesystem, "Read/Write: configs,<br/>runs, memory")
```

## Actors

| Actor | Role |
|-------|------|
| **Operator** | Запускает pipeline через CLI скрипты, задаёт параметры, проверяет результаты |
| **OpenRouter API** | Единый API для всех LLM/VLM вызовов (text, vision, image gen/edit) |
| **CSV / Image Source** | Внешние изображения для edit mode (HTTP URLs) |
| **Local Filesystem** | Хранение конфигураций, результатов runs, persistent memory |
