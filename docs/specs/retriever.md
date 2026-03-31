# Spec: Retriever (KnowledgeBase)

**Module:** `src/auditor/knowledge_base.py`
**Role:** Retrieval-компонент --- находит релевантные reference-примеры для VLM-аудита и prompt building.

---

## 1. Data Sources

| Source | Format | Content |
|--------|--------|---------|
| `reference_annotations.yaml` | YAML | Аннотированные reference изображения: зона, ракурс, quality, domain markers |
| `DomainSpec` | Pydantic models | Scene catalog, audit rubric, domain config |

### Reference Annotation Schema

```yaml
references:
  - file: "image_001.jpg"
    zone: counter
    camera_view: front
    domain_markers: ["brand_logo", "brand_colors"]  # domain-configurable field name
    must_have_elements: ["counter", "led_lighting"]
    quality_rating: 5          # 1-5
    people_visible: false
    good_domain_identity: true # domain-configurable field name (kb_identity_field in domain.yaml)
    optional_elements: ["queue_barriers"]
```

---

## 2. Index

**Type:** In-memory list (loaded at init)

```python
self._refs: list[ReferenceExample]  # Parsed from YAML
```

Индекс загружается однократно при создании `KnowledgeBase`. Нет embedding-based retrieval --- explicit matching по полям.

---

## 3. Search: `find_similar_references(scene_id, n=3)`

**Algorithm:** Score-based ranking

```
score = 0
+3  if zone matches scene.zone
+2  if camera_view matches scene.camera_view
+quality_rating  (1-5)
+2  if good_identity (domain-configurable field)
-10 if people_visible  (penalty)
```

**Returns:** Top-N `ReferenceExample` отсортированных по score (descending).

### Параметры поиска

| Parameter | Default | Description |
|-----------|---------|-------------|
| `scene_id` | required | ID сцены для matching |
| `n` | 3 | Количество reference для возврата |

---

## 4. Reranking

Reranking не используется. Scoring --- single-pass ranking по feature overlap. Обоснование: маленький каталог (десятки reference), не требуется semantic search.

---

## 5. Output Formats

### `SceneContext`

```python
class SceneContext(BaseModel):
    scene_id: str
    zone: str
    must_have: list[str]
    forbidden: list[str]
    similar_references: list[ReferenceExample]  # Top-N
    reference_prompt_hint: str  # Ready for VLM
```

`reference_prompt_hint` --- текстовая строка, вставляемая в VLM prompt:
```
"Reference examples for this domain show:
 - image_001.jpg: counter zone, front view, quality 5/5, markers: wb_logo, purple_accents
 - image_003.jpg: counter zone, corner view, quality 4/5, markers: led_sign"
```

### `AuditRuleContext`

```python
class AuditRuleContext(BaseModel):
    check_id: str
    accept_threshold: float
    review_threshold: float
    scene_specific_hints: list[str]
```

---

## 6. Integration Points

| Consumer | Method | Purpose |
|----------|--------|---------|
| `AuditStage._check_domain_relevance()` | `format_references_for_prompt()` | Few-shot context для VLM |
| `AuditStage._check_prompt_adherence()` | `retrieve_scene_context()` | Must-have / forbidden lists |
| `Pipeline.run()` | `retrieve_scene_context()` | Reference hints для prompt building |

---

## 7. Limitations

- **No semantic search** --- matching только по zone / camera_view / quality fields
- **No image embeddings** --- reference images не используются как visual few-shot (только текстовые описания)
- **Degraded mode** --- если `reference_annotations.yaml` пуст, KB работает без few-shot. Все методы возвращают пустые списки, `reference_prompt_hint = ""`
- **Domain-specific fields** --- identity и markers field настраиваются через `domain.yaml` (`kb_markers_field`, `kb_identity_field`)
- **Static index** --- reference'ы загружаются один раз, не обновляются runtime
