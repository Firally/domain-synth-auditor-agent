# Spec: Tools & APIs (ModelGateway)

**Module:** `src/auditor/model_gateway.py`
**Role:** Единый клиент для всех LLM/VLM/image вызовов через OpenRouter API.

---

## 1. API Contracts

### `chat(prompt, *, model, fallback, system, temperature) -> str`

Текстовый запрос к LLM.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt` | `str` | required | User message |
| `model` | `str` | `TEXT_MODEL` (gemma-3-12b-it:free) | Primary model |
| `fallback` | `str` | `TEXT_FALLBACK` (mistral-small-3.1-24b:free) | Fallback model |
| `system` | `str \| None` | `None` | System prompt |
| `temperature` | `float` | `0.7` | Sampling temperature |
| **Returns** | `str` | | LLM response text |

### `vision(image_bytes, prompt, *, model, fallback, fallback2, mime, temperature) -> str`

VLM запрос: изображение + текст -> текст.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `image_bytes` | `bytes` | required | Image data |
| `prompt` | `str` | required | Question about image |
| `model` | `str` | `VISION_MODEL` (gemma-3-27b-it:free) | Primary VLM |
| `fallback` | `str` | `VISION_FALLBACK` (mistral-small-3.1-24b:free) | Fallback |
| `fallback2` | `str` | `VISION_FALLBACK2` (nemotron-nano-12b:free) | 2nd fallback |
| `mime` | `str` | `"image/png"` | MIME type |
| `temperature` | `float` | `0.2` | Low for deterministic audit |
| **Returns** | `str` | | VLM response text |

### `generate_image(prompt, *, model, fallback, size) -> bytes`

Генерация изображения из текста.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt` | `str` | required | Image description |
| `model` | `str` | `IMAGE_GEN_MODEL` (gemini-3.1-flash-image-preview) | Primary |
| `fallback` | `str` | `IMAGE_GEN_FALLBACK` (gemini-2.5-flash-image) | Fallback |
| `size` | `str` | `"1024x1024"` | Image dimensions |
| **Returns** | `bytes` | | Generated image (PNG/JPEG) |

### `edit_image(source_image, instruction, *, model, fallback, mime) -> bytes`

Редактирование изображения по текстовой инструкции.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `source_image` | `bytes` | required | Source image data |
| `instruction` | `str` | required | Edit instruction |
| `model` | `str` | `IMAGE_GEN_MODEL` | Primary |
| `fallback` | `str` | `IMAGE_GEN_FALLBACK` | Fallback |
| `mime` | `str` | `"image/jpeg"` | Source MIME type |
| **Returns** | `bytes` | | Edited image bytes |

---

## 2. Error Handling

### Retryable Status Codes

```python
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
```

При retryable error --- автоматический переход на fallback модель (до 3 уровней).

### Structured Error Codes

| Code | Trigger | Suggestion |
|------|---------|------------|
| `VLM_TIMEOUT` | `TimeoutError`, "timeout" in message | "retry or switch to faster model" |
| `VLM_RATE_LIMITED` | `RateLimitError`, "429" in message | "wait 30s or switch model" |
| `VLM_API_ERROR` | `APIError`, 5xx status | "check OpenRouter status page" |
| `VLM_PARSE_ERROR` | JSON decode failure | "simplify prompt, request cleaner JSON" |
| `IMAGE_DECODE_ERROR` | Image decode failure | "check source image format" |
| `INTERNAL_ERROR` | All other exceptions | "check logs" |

### Fallback Chain

```
Primary Model
    |-- success --> return result
    |-- retryable error --> Fallback Model
        |-- success --> return result
        |-- retryable error --> Fallback2 Model (vision only)
            |-- success --> return result
            |-- error --> raise RuntimeError
```

---

## 3. Timeout Policy

| Call Type | Timeout | Notes |
|-----------|---------|-------|
| `chat()` | OpenAI client default (~60s) | Text-only, fast |
| `vision()` | OpenAI client default (~60s) | Image + text input |
| `generate_image()` | OpenAI client default (~60s) | Image generation |
| `edit_image()` | OpenAI client default (~60s) | Image editing |
| `_fetch_image(url)` | httpx default (~30s) | Downloading result images |

Таймауты не настраиваются отдельно --- используются дефолты AsyncOpenAI клиента.

---

## 4. Side Effects

| Method | Side Effect | Scope |
|--------|-------------|-------|
| All methods | API call to OpenRouter | External, metered |
| `_fetch_image()` | HTTP download | External |
| None | No filesystem writes | ModelGateway is stateless |

ModelGateway **не пишет на диск** и **не модифицирует state**. Budget tracking выполняется Pipeline через `BudgetTracker`.

---

## 5. Protection Mechanisms

### Rate Limit Protection
- 3-level fallback chain: при 429 автоматически переключается на другую модель
- Разные провайдеры (Google, Mistral, NVIDIA) --- разные rate limit pools

### Cost Protection
- `COST_PER_CALL` dict --- оценочная стоимость каждой модели
- Free VLM модели ($0.00) для аудита --- основной расход только на image gen (~$0.001/call)
- BudgetTracker в Pipeline --- hard stop при превышении лимита

### Response Parsing Protection
- `_parse_image_response()` --- ищет image data в нескольких форматах (list content, data-URL, raw fields)
- Recursive `_extract_image_from_raw()` --- fallback парсинг для нестандартных ответов
- При невозможности извлечь изображение --- `RuntimeError` (не silent failure)

---

## 6. Model Registry

| Model ID | Type | Cost | Role |
|----------|------|------|------|
| `google/gemini-3.1-flash-image-preview` | Image gen/edit | ~$0.001 | Primary image model |
| `google/gemini-2.5-flash-image` | Image gen/edit | ~$0.003 | Fallback image model |
| `google/gemma-3-27b-it:free` | VLM | $0.00 | Primary VLM audit |
| `mistralai/mistral-small-3.1-24b-instruct:free` | VLM + Text | $0.00 | Fallback VLM + text |
| `nvidia/nemotron-nano-12b-v2-vl:free` | VLM | $0.00 | 2nd fallback VLM |
| `google/gemma-3-12b-it:free` | Text | $0.00 | Primary text (improver, intent resolver) |

---

## 7. IntentResolver (Agent Tool)

**Module:** `src/auditor/intent_resolver.py`

LLM-агент для разбора natural language запроса пользователя.

### Contract

```python
async def resolve(
    user_request: str,
    available_types: list[ObjectType],
) -> ResolvedIntent
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `user_request` | `str` | Запрос на любом языке |
| `available_types` | `list[ObjectType]` | Типы объектов из MySQL |
| **Returns** | `ResolvedIntent` | `object_type`, `count`, `edit_instruction` |

### LLM Call

- Model: `TEXT_MODEL` (gemma-3-12b-it:free) с fallback на `TEXT_FALLBACK`
- Temperature: `0.1` (детерминистический выбор)
- System prompt: task planner role
- Output: JSON `{object_type, count, edit_instruction}`

### Error Handling

- JSON parse failure → `ResolvedIntent(object_type=None, count=10, edit_instruction="")`
- Unmatched object_type → `object_type=None`, caller decides (error or fallback)
- LLM API failure → propagates through ModelGateway fallback chain

---

## 8. DBImageLoader (Database Tool)

**Module:** `src/auditor/db_loader.py`

### Contract

```python
async def load(
    db: DBPool,
    *,
    object_type_id: int,
    limit: int = 10,
    random_order: bool = True,
) -> list[ImageRecord]
```

- Queries MySQL: `SELECT url FROM images WHERE object_type_id = ? ORDER BY RAND() LIMIT ?`
- Downloads each URL via `download_image()` (shared with CSV loader)
- Returns `ImageRecord` with `metadata.db_image_id`, `metadata.object_type`
- Failures: per-image (logged, skipped), connection (propagated)
