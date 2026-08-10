# B1 — Аудит перевода: решения и измерения (конспект 2026-08-09)

> **Статус:** черновик для карточки B1. Основан на эмпирических тестах audit_v2/audit_v3/audit_v4 (скрипты в `D:\test folder\`).
> Обновляет устаревшие §6 (промпт аудита) и §8-B (single whole-chapter audit) основного плана.
> Хранение контекста/промпта: **скрипт `audit_v4.ps1`** — единственный source of truth промпта v4 (audit_v3 — архив, НЕ использовать для production-переноса).

---

## 1. Итоги тестов аудитора (глава 0001, перевод run_reasoning3_selection)

### 1.1. Матрица результатов

| Конфигурация | Chunks | Issues | TP | FP | poisoned FP | Precision | Recall (gold) |
|---|---|---|---|---|---|---|---|
| Qwen R0 whole-chapter (старый промпт) | 1 | 20 | ~5 | много | — | ~25% | — |
| Qwen R2048 whole-chapter (старый промпт) | 1 | 10 | ~2 | много | — | ~20% | — |
| Gemma R0 whole-chapter | 1 | 20 | ~2 | очень много | — | ~10% | — |
| Gemma R2048 whole-chapter | 1 | **1** | 1 | **0** | 0 | **100%** | низкий |
| Gemma R2048 chunk50 (v2) | 8 | 10 | 2 | 8 | 0 | 20% | низкий |
| Gemma R8192 chunk50 (Тест 2) | 1 | 2 | 2 | 0 | 0 | 100% | — |
| **Gemma R8192 chunk50 ctxfix (v3)** | 8 | **7** | **4** | 3 | 0 | **57%** | 50% |
| **Qwen R8192 chunk50 ctxfix (v3)** | 8 | 6 | 4 | 2 | 0 | 67% | ~50% |
| **Qwen R8192 prompt v4.1 (out-of-sample, Gemma-перевод run_006)** | 8 | 40 | ~24-27 | ~13-16 | 0 | **60-70%** | ~66% критических |

### 1.2. Ключевые выводы

1. **Qwen — не подходит как аудитор** (старый промпт): низкая precision, уверенные FP, `Two past twelve = 1:02` даже при инструкции в промпте.
2. **Gemma R2048 whole-chapter = 100% precision / низкий recall** (самый безопасный: «слишком мало репортит», но не врёт).
3. **Gemma chunked: precision 57%, recall 50%** — главная проблема не инфраструктура, а **контекст/семантика** (speaker, coreference, морфология).
4. **Спилл (reasoning → content)**: стохастичен, лечится адекватным `max_tokens` (llama считает reasoning+content ВМЕСТЕ) + RetryShrink.
5. **Потерянные TP**: p00032 (youngest→младшему), p00035 (preoccupied→поглощена собой), motorcycle→велосипед (long-range entity) — лечатся промптом v4 (правила 5/6/7) и chapter entity memory (скриптом, НЕ промптом).
6. **Стабильные FP (blind spots Gemma)**: русский эллипсис (p00075), морфологический синкретизм (p00151), локальная кореференция (p00309 он=кот/она=бабушка) — guardrails в промпте v4 (правила 9/10), кореференция НЕ лечится промптом (риск overfitting) → в verifier/entity layer.
7. **OUT-OF-SAMPLE (2026-08-10, Qwen v4.1 аудит Gemma-перевода run_006, НЕ тот перевод, на котором тюнили промпт)**: 40 findings, precision ~60-70% (24-27 TP / 13-16 FP), recall ~2/3 критических ошибок. **Промпт НЕ выучил конкретные PID** — Qwen нашла новые классы ошибок вне dev-набора (p00322 scene, p00338, p00371, p00117 идиома, p00244). Подтверждает решение НЕ тюнить промпт дальше.
   - **Новый FP-класс: dialogue tags** (said→позвала/буркнула/перебила: p00106/116/118/124/200) — литературная интерпретация speech verb ≠ fidelity defect; репортить в repair-промпт как expected-reject
   - **Систематические пропуски Qwen**: русский gender agreement (p00132, p00189), implicit modality (p00201 «I could see you handing»→«Я видел, как ты»), p00333 (slip→тихо уйдёт) — известные слабости, НЕ лечим промптом (риск overfit), полагаемся на verify-before-repair и повторные прогоны
   - **Severity у Qwen некалибрована** (реальные TP идут minor, стилистика major) → severity НЕ eligibility-фильтр для repair (см. §10 B2)

### 1.3. Решение владельца 2026-08-09: пары моделей локального режима

| Роль | Модель | Обоснование |
|---|---|---|
| **Переводчик (генератор)** | **Gemma 4 26B A4B Q4_K_XL** (local, sycl-edge) | основной локальный генератор (reasoning 2048, 49k) |
| **Аудитор** | **Qwen3.6-35B-A3B MTP Q4_K_XL** (local, sycl-edge) | **независимая модель → полное соответствие Kocmi «аудитор ≠ генератор»**; по тестам не хуже Gemma-аудита |

- **Qwen = аудитор, Gemma = переводчик** — финальное решение для локального режима (вместо same-model Gemma→Gemma).
- Qwen-аудит с промптом v4 — **в процессе тестирования** (результаты дополнят матрицу §1.1).
- Same-model Gemma→Gemma: **НЕ используется** в production (нарушение Kocmi); только diagnostic.
- Параметры Qwen-сервера: §3.4 основного плана (MTP, reasoning on, budget 8192, порт 8094).

---

## 2. Параметры аудита (зафиксированные решения)

| Параметр | Значение | Основание |
|---|---|---|
| Модель аудита | **Qwen3.6-35B-A3B MTP Q4_K_XL** (local) | **финальное решение 2026-08-09**: независимая от Gemma (Kocmi); Gemma-audit — diagnostic-only |
| Reasoning | **фиксированный 8192** на сервере (`--reasoning-budget 8192`) | reasoning — параметр СЕРВЕРА, не запроса; смена = перезапуск + identity-change |
| Chunking | **K-балансировка по входу**: `K = ceil(total/max_input)`, `target = total/K`, жадный добор без превышения лимита | нет короткого «хвоста» (урок: greedy по лимиту дал chunk из 4 пар) |
| `max_input_tokens` | **3600** = `reasoning_budget/2 × 0.88` (запас 12%) | формула «вход×2 = reasoning» подтверждена: R8192=8128 сработал, R4096=4096 нет |
| `max_tokens` | **12000** = reasoning + ~3500 на content | llama считает reasoning+content вместе; 3000 при R8192 → LENGTH с 0 content |
| **Полный input budget** | `fixed_prompt + narrator + entity + CONTEXT_ONLY + AUDIT_PAIRS ≤ calibrated_total` | ⚠️ `MaxInputTokens=3600` учитывает только pairs; entity-context (soft 500 / hard 800 токенов) вычитается из бюджета audit pairs или chunker учитывает полный prompt; overflow не обрезать молча — фиксировать невошедшие claims |
| RetryShrink | **по входу, не по парам**: level 1 = MaxInputTokens/2, level 2 = /3; каждый sub с уникальным суффиксом `_lvlN_subM` | исправлен баг одинаковых имён; K-балансировка subs |
| Результат chunk | `GOOD / LENGTH / SPILL / INVALID_JSON / EMPTY / FAILED_RETRIED` | failed chunk НИКОГДА ≠ issues=[] |
| `audit_complete` | false при любом failed chunk | честное покрытие главы |
| Захват reasoning | `delta.reasoning_content` (llama SSE) | сохранение в `*_reasoning.txt` per chunk |
| Usage tokens | `reasoning_tokens = 0` — llama НЕ отдаёт usage в SSE | не баг скрипта; метрика n/a для llama-server |

---

## 3. Контекст аудита (3 уровня)

### 3.1. Narrator/entity context (глобальный, fallback-only)

- **ТОЛЬКО канонические именованные персонажи** с полом из book_memory, присутствующие в главе
- **БЕЗ generic-описаний** («the nurse», «the man», «the woman», «the dog») — урок «The Nurse: female» дал 3 poisoned-FP (Rich-медбрат на самом деле male, `She smiled up at him`)
- Формат: `narrator: Blake Thorburn (gender male)` + `Name: gender` строки
- В промпте: «BOOK/CHAPTER CONTEXT — FALLBACK ONLY»; SOURCE evidence всегда выше

### 3.2. Иерархия evidence (правило 3 промпта v4)

```
1) explicit evidence в текущем SOURCE pair
2) explicit evidence в adjacent SOURCE pairs
3) chapter-local context
4) book/Bible context
5) inference
```
Внешний контекст НИКОГДА не делает верный перевод ошибочным, если SOURCE его поддерживает.

### 3.3. Chapter entity memory (для long-range consistency — отдельный слой, НЕ промпт)

- `motorcycle → велосипед` (p00097/98/236/324) — long-range entity error, НЕ видна локально в чанке
- Решение: компактная chapter entity map (`Blake's bike = motorcycle`, evidence PID) подаётся в промпт как дополнительный контекст
- Извлекается скриптом (entity extraction — отдельный этап/карточка, см. §7)

---

## 4. Промпт v4 (внесён в audit_v3.ps1)

### 4.1. Почему меняли (3 failure mode из reasoning Qwen)

1. **Ошибка замечена, но классифицирована как stylistic** → p00035 (`preoccupied` vs `поглощена собой`) — правило 7 (Character state/motive/trait)
2. **Ошибка замечена, но conservative threshold подавил** → p00093 (`didn't already know` vs `no longer knew`) — правило 6 (Negation/temporal/modality)
3. **Ошибка не стала кандидатом** (морфология) → p00032 (`youngest → младшему`) — правило 5 (Invented gender через морфологию, не только явные `man/woman`)

### 4.2. Ключевой принцип

> **НЕ ослабляем глобально «When uncertain, PASS»** (иначе вернутся FP).
> Добавляем узкие правила для классов, где reasoning УЖЕ показывает понимание ошибки, но подавляет её.

Плюс GENERAL DECISION RULE: «do NOT use "stylistic difference" or "when uncertain, PASS" to discard a candidate **after you have identified a concrete semantic difference** in a high-risk area».

### 4.3. Структура промпта (15 правил)

1. Silent verify each candidate
2. Final notes only (запрет acceptable/correct/wait/probably/maybe...)
3. SOURCE evidence highest priority (иерархия §3.2)
4. Speaker identity: I/me/my в диалоге = говорящий, не narrator
5. **Invented gender: морфология** (kinship, pronouns, adjectives, participles, past-tense, vnuk/vnuchka...) + примеры gender-neutral («youngest», «grandchild», «cousin»)
6. **Negation/temporal/aspect/modality** (already/still/yet/no longer/seemed/apparently/probably/must/might...) + примеры NOT equivalent
7. **Character state/motive/trait** (mental state, emotion, intention, certainty...) + пример preoccupied
8. Short/elliptical dialogue (Ten → Десяти = PASS)
9. Russian ellipsis (его рука поверх её [руки] = PASS)
10. Morphological syncretism (тётей: не судить по окончанию)
11. Generic descriptions ≠ canonical entities
12. Object/physical-detail fidelity (printed vs embroidered)
13. Do NOT over-police register/style
14. Issue selection: max 20; **не останавливаться после первой находки в PAIR** (несколько issues на id допустимы)
15. Pair IDs: копировать, не изобретать

---

## 5. Post-audit слой (verifier + repair-as-verifier)

### 5.1. Двухуровневый verifier (НЕ «magic deterministic semantic verifier»)

**Tier A — HARD deterministic filters (код, 0 вызовов)** — подтверждают/отклоняют формально:
- структура (PID missing/duplicate/malformed)
- точные числа/время (нормализация: «Two past twelve» = 00:02)
- явные дубли («в гости в гости»)
- имена/строки
- **direct current-source fact** (явное число, явно названный объект — без semantic edge)
- PID вне чанка / invalid category

> ⚠️ **НЕ входит в Tier A:** chapter entity relations (включая `bike = motorcycle`). presence проверенных anchor/alias spans НЕ превращает отношение в Tier A.

**Tier B — LLM semantic claim** — НЕ подтверждается «regex ничего не опроверг»:
- referent, invented_gender без явных следов, idiom omission, negation scope, cross-paragraph
- **chapter entity relations (всегда Tier B)**
- → второй semantic confirmation (см. repair-as-verifier) ИЛИ diagnostic/debt

### 5.2. Repair-as-verifier (Kocmi-safe: repair-модель = генератор, аудитор ≠ генератор)

```
Qwen auditor → HARD filters (Tier A) → repair/confirmation (Tier B)
```
Repair-промпт: «You are given a proposed fidelity issue. First verify the issue
against SOURCE and TRANSLATION. If the auditor is wrong, return PASS and make no
change. Only if confirmed, return the corrected translation.»

- бесплатный второй semantic decision (repair и так вызывается)
- две независимые semantic оценки, без третьей модели/фазы
- **НЕ нарушает Kocmi**: repair = Gemma (генератор), аудитор = Qwen

**Tier B должен получать:** полный current source PID, current translation PID, anchor evidence, alias evidence, достаточное окно между ними (или отдельные source windows) — но НЕ готовую формулировку как авторитетный факт.

### 5.3. Repair eligibility

- Tier A (кодом подтверждённые, direct source fact) → repair напрямую
- Tier B → через repair-as-verifier (включая все entity relations)
- **`chapter_entity_context` никогда не является самостоятельным repair evidence** — finding, зависящий от semantic entity relation, всегда проходит Tier B
- НЕ авто-ремонт: minor/medium/low confidence → debt/diagnostic
- failed audit chunk → debt, никогда «0 findings» (fail-closed)

---

## 6. Regression suite (gold set главы 0001)

### Должен находить (must-find)
| PID | Ошибка |
|---|---|
| p00010 | invented gender: wannabe-architect → девушкой |
| p00013 | changed_fact: printed → вышито |
| p00032 | invented gender: youngest → младшему (морфология) |
| p00035 | changed_fact: preoccupied → поглощена собой |
| p00093 | negation: didn't already know → уже не знал |
| p00132 | addition: в гости в гости |
| p00193 | invented gender: grandchild → внук |
| (entity) | motorcycle → велосипед (long-range, p00097/98/236/324) |

### Не должен находить (must-not-find)
| PID | Почему PASS |
|---|---|
| p00075 | её [руки] — восстановимый эллипсис |
| p00106 | Десяти — эллиптический ответ на «девяти лет?» |
| p00136 | поверила/окружённой — Молли говорит (female) |
| p00151 | тётей — винительный мн.ч., НЕ ошибка |
| p00184/197/208 | Медбрат — Rich male (source: he/him) |
| p00309 | он=кот, она=бабушка — корректен |

### Метрики оценки (не число issues!)
```
gold TP recall | gold negative rejection | new unknown issues (вручную → в gold)
```

---

## 7. Открытые вопросы / будущие карточки

1. **Qwen с промптом v4** — тест завершён: v4.2 вернул p00010, но породил FP-пачку (p00285/p00221/p00379/p00182); p00032 так и не пойман. **Решение 2026-08-10 (ревьюер+владелец): production = prompt v4.1 + harness v4.2 infra; заморозить тюнинг на главе 0001.**
2. ~~Same-model Gemma-аудит~~ — **закрыто решением владельца 2026-08-09**: Gemma переводит, Qwen аудитит (Kocmi-safe); same-model только diagnostic
3. **Chapter entity extraction** — карточка B1.2 (см. §10): Qwen source-only prepass, schema per-claim, валидация кодом, кэш per-chapter
4. **Speaker attribution** (metadata per PAIR) — только когда уверенно; иначе не аннотировать (wrong speaker = poison) — в B1 НЕ входит, fallback = overlap
5. ~~Параметры Qwen server~~ — **получены владельцем 2026-08-09**, зафиксированы в §3.4 основного плана и runtime_local.example.yaml
6. **Production audit v1** — замороженная конфигурация: prompt v4.1 (семантика) + harness v4.2 infra (debug fix, version metadata) + entity context + overlap (см. §10)

---

## 10. План фазы B — разделение на задачи (утверждён 2026-08-10)

> **Принцип:** B1 разделён на B1 (core) + B1.1 (Tier A) + B1.2/B1.3 (entity context отдельным треком после baseline). Lifecycle (запуск/остановка Qwen, swap, VRAM, reasoning-валидация) — **уже реализован в A1** (`model_lifecycle.py` + `build_strict_lifecycle`), B-карточки подключаются к нему, не переписывают.
>
> **Production audit v1 = prompt v4.1 (НЕ v4.2) + harness v4.2 infra + overlap + entity context (после B1.3).**

### B1 — ChunkedAuditEvaluator (core) [I+RV]

Перенос harness `audit_v4.ps1` в Python (`pact_v4/audit/chunked_audit.py`):

- **Chunking**: greedy по входным токенам (НЕ K-balance — переименовать честно), `max_input = 3600`, `max_tokens = 12000`, формулы из §2
- **Overlap (CONTEXT_ONLY)**: предшествующие пары из ОРИГИНАЛЬНОЙ главы, ~400 токенов (мин 2, макс 6 пар), модель не аудитит CONTEXT_ONLY
- **RetryShrink**: по входу (lvl1 = max_input/2, lvl2 = /3), каждый sub с уникальным суффиксом, overlap subs из оригинальной главы
- **Строгая валидация**: категории/severity/confidence/PID-в-чанке, fail-closed (failed chunk ≠ issues=[])
- **Dedup**: id+category, high-confidence wins
- **Debug metadata**: `_debug {chunk, reasoning_file}` прикрепляется к issue в момент сбора (фикс 4.2)
- **Версионирование**: `schema: pact-audit/v4` + `harness_version` + `prompt_version` раздельно
- **Промпт v4.1** в `render_qwen_audit_prompt` (замена QWEN_AUDIT_V1): зафиксированный текст из audit_v4.ps1 (v4.1 семантика — БЕЗ procedural gender check v4.2)
- **Интеграция с lifecycle**: расширить `LifecycleQwenAuditEvaluator.__call__` — принимает чанки+overlap+context, `context_size ≥ 49152` (сейчас 32768 — проверить!)
- **Полный input budget**: `fixed_prompt + narrator + entity + CONTEXT_ONLY + AUDIT_PAIRS ≤ calibrated_total` (soft 500 / hard 800 для entity)
- **Контекст 3 уровней**: narrator context (канонические имена, generic исключены) + BOOK CONTEXT fallback + CHAPTER ENTITY FACTS (схема §8.3)
- **Regression suite** (§6 gold set): 8 must-find + 6 must-not-find → pytest-контракты (mock backend, 0 реальных вызовов)

**Acceptance:** suite 8/8 gold TP + 6/6 gold negative rejection; chunking ровно 8 чанков на главу 0001; fail-closed проверен (mock LENGTH/INVALID_JSON → audit_complete=false)

**Non-goals:** repair (B2), Tier A (B1.1), entity extraction (B1.2), remote-аудит (B3)

### B1.1 — Tier A hard filters (код, 0 модельных вызовов) [I+RV]

`pact_v4/audit/hard_filters.py` — детерминированная фильтрация findings до repair:

- **Дубли**: «в гости в гости» — exact adjacent duplicate
- **Числа/время**: нормализация (Two past twelve = 00:02, девяти/десяти)
- **Direct current-source fact**: явное число/имя/объект в source → сверка
- **PID/category**: вне чанка / invalid → reject
- **`chapter_entity_context` НИКОГДА не Tier A** (всегда Tier B, §5.3)

**Acceptance:** p00132 → CONFIRMED (Tier A), «1:02»-FP → REJECTED, nurse-issue с source-фактом → REJECTED

**Non-goals:** semantic verification (Tier B — B2)

### B1.2 — ChapterEntityContext extractor (Qwen prepass) [I+RV]

`pact_v4/audit/entity_extractor.py` — source-only prepass (1 вызов на главу):

- **Экстрактор: Qwen** (решение ревьюера: не Gemma — коррелированный blind spot с переводом)
- **Вход**: source главы целиком (детерминированный, temp=0)
- **Выход**: schema per-claim (§8.3) — anchor span `verified` / alias mention `verified` / same_entity relation `candidate`
- **Валидация кодом (8 пунктов §8.3)**: PID существует, span дословно в source, нет translation-derived, canonical type в anchor, alias в своём PID, gender-evidence с referent-связью; неподтверждённое → candidate
- **Кэш per-chapter**: identity = source_hash + extractor_version
- **НЕ авторизует repair** (всегда Tier B)

**Acceptance:** глава 0001 → 2 сущности (Blake's vehicle, Rich) с корректными status

### B1.3 — Entity-context A/B + 8 кейсов (spike, не production) [I+RV]

Изолированный эксперимент, не влияет на production-путь:

- **A/B на одинаковых чанках**: без context / ручной gold / авто-extracted
- **8 кейсов §9.1**: 2 positive (recall), 4 negative (precision/FP), 2 provenance (poisoned, false validation)
- **Test leakage убран** (примеры в промпте — нейтральные, §9.3)

**Decision gate:** приемлемая precision (определяемо по 8 кейсам) → entity-context в production (B3); иначе — known limitation (p00236-класс остаётся ручным)

**Non-goals:** изменение промпта v4.1 (заморожен)

### B2 — Selective repair (batch) + repair-as-verifier [I+RV]

`pact_v4/repair/` — пост-аудит ремонт:

- **Repair-модель = генератор (Gemma local / DeepSeek remote)** — Kocmi-safe (аудитор ≠ ремонтник)
- **Repair-as-verifier**: «The audit issue is a candidate, not an established fact. First independently verify against SOURCE and TRANSLATION. If incorrect → return PASS, no change. Only repair after confirming.»
- **Eligibility (2026-08-10, out-of-sample ревью):** НЕ фильтровать по `severity` — у Qwen severity некалибрована (реальные TP идут minor, стилистика major). Eligible = `confidence=high` + allowed semantic categories; severity только в journal, не в eligibility. `changed_fact`/числовые — детерминированная проверка кодом (Tier A)
- **Ожидаемый FP-класс: dialogue tags** (said → позвала/буркнула/перебила, p00116/118/124/200) — repair должен отклонять: литературная интерпретация speech verb ≠ fidelity defect
- **Tier A findings** → repair напрямую; **Tier B** (включая entity relations) → verify-before-repair
- **Batch**: один вызов на группу findings (как старый repair), потом контекстный re-audit затронутых PID
- **Fail-closed**: failed repair chunk → debt, никогда не молчаливый PASS
- **НЕ ремонтирует**: low confidence / semantic вне allowed categories → debt/diagnostic

**Acceptance:** p00010/p00193-тип → repair после verify; p00106-тип (FP) → PASS без изменений; регресс: 1324+ suite

### B3 — Production-интеграция + remote-путь [I+RV]

- **Вставка ChunkedAuditEvaluator** в strict runner (замена gemma_russian_review/qwen_fidelity gate)
- **Journal/provenance**: audit chunk результаты, switch_records (уже в A1), entity context hash
- **Cache/resume identity**: source_hash + translation + audit prompt version + backend + reasoning
- **Gates**: audit_complete=false → debt/accepted_degraded (уже fail-closed)
- **Local**: Qwen R8192 через существующий lifecycle (вариант A — бесплатно)
- **Remote-путь (контракт, НЕ тестирован)**: opencode + Qwen-аудит через request_options; reasoningEffort high/medium — **пометить «не протестирован, тестировать после B-фазы»** (решение владельца)
- **Config**: runtime_local.example.yaml → qwen audit server_args (MTP, R8192, 49k) + max_input/max_tokens/overlap в config

**Acceptance:** полный локальный прогон главы: Gemma translate → Qwen audit → issues → verifier → repair; journal + resume работают; audit_complete честный

**Non-goals:** remote-аудит тестирование, tuning промпта

### Зависимости

```
B1 ──→ B1.1 (нужны findings из B1)
B1 ──→ B2 (repair опирается на findings + Tier A)
B1.2 ──→ B1.3 (extractor → A/B)
B1.3 ──→ B3 (только если A/B PASS; иначе B3 без entity)
B1 + B1.1 + B2 ──→ B3 (production сборка)
B3 ──→ C (formatting после production-аудита)
B3 ──→ owner-run валидация на новых главах
```

### Порядок реализации

1. **B1** (core, самый большой — developer, эталон: audit_v4.ps1)
2. **B1.1** (Tier A, независим после B1)
3. **B1.2** (entity extractor, может идти параллельно B1.1)
4. **B1.3** (A/B spike → decision gate)
5. **B2** (repair, после B1 + B1.1)
6. **B3** (production сборка, после B1+B1.1+B2 [+B1.3 если PASS])

**Параллельно можно:** B1.2 с B1.1; B1.3 с B2 (изолированный spike)

---

## 11. Карточка C — детерминированный formatting (после B-фазы)

> Источник: основной план §8-C. Маленькая карточка (уровень B1.1). Создаётся на доске ПОСЛЕ B3, не раньше.

**Объём:**
- `formatting`: убрать model-fallback (все вызовы моделей из formatting — против правила «formatting = 0 model calls»)
- Сохранить deterministic incident report
- Проверить на замороженных артефактах: сколько обязательных spans останется unresolved (ожидание ~0 на главу 0001 — whole-chapter перевод держит `<em>` 101/101)
- unresolved → debt, не тихая потеря
- Результат «0 model calls» ≠ успех, если chapter стал accepted_degraded из-за formatting debt

**Acceptance:** formatting на главе 0001 → 0 unresolved spans, 0 model calls, полный suite проходит

**Non-goals:** изменения HTML-рендера, tuning промпта, remote-путь

## 12. После B3+C — валидация на новых главах (owner-run, не карточка)

- Прогон **второй/третьей главы** (правило ревьюера 2026-08-10: не тюнить главу 0001; Bond-1.1 = dev-set, новые главы = real-world validation)
- **Правило изменения промпта:** только при повторяющемся классе ошибки (≥3 независимых случая) или критической systematic failure; одиночный FP → записать, repair должен отклонить, pipeline продолжает
- **Remote-аудит тестирование** (контракт в B3, тестирование после B-фазы — решение владельца)
- **PR #145 dev→main** — мерж только когда вся 4.1 готова (draft до этого)

## 13. Карточка M — монитор прогресса для 4.1 (ДО прогона 2 новых глав)

> Обнаружено при тестировании A-части (2026-08-10, run_006_local_gemma): `v4_phase_progress` рассчитан на chunked-поток (chunk_started/chunk_done, Step 6-8), в whole-chapter показывает почти ничего (один «chunk» + skipped 6/7/8). Для прогона новых глав нужен монитор, понимающий whole-chapter.

**Объём:**
- `v4_phase_progress.py` + `PhaseProgressWriter`: whole-chapter-события
  - `wc_generation_started` (pid_count, reasoning_budget, model)
  - `wc_retry_attempt` (attempt, reason: malformed/missing_pid/truncated/abort)
  - `wc_generation_done` (finish_reason, pid_count, duration)
  - `wc_validated` (json_ok, pids_ok, order_ok)
- Показывать: текущую retry-попытку и её причину, live-duration, статус валидации PID-контракта
- Сохранить диагностическую природу (read-only, не gate, crash-safe append-only)
- Для chunked-режима (B1-аудит) — существующие события остаются; добавить `audit_chunk_started/done` (8 чанков аудита) — монитор после B1 показывает и их

**Acceptance:** во время whole-chapter прогона монитор показывает ≥1 событие на каждую retry-попытку; после прогона — финальный статус с PID-валидацией; полный suite проходит

**Non-goals:** изменение pipeline-логики, resume, journal schema, terminal-политики

**Когда:** ДО прогона второй/третьей главы (§12). Может идти параллельно B-фазе (не зависит от B1).

## 14. Карточка W — whole-chapter артефакты (chunk_plan.json)

> Обнаружено при тестировании A-части (2026-08-10): `chunk_plan.json` пишется безусловно (runner:2305-2308), хотя в whole-chapter реальные границы чанков не используются — важен только упорядоченный PID-список (`WholeChapterPidMap.derive`). Файл в текущей форме вводит в заблуждение.

**Объём:**
- В whole-chapter режиме: писать `whole_chapter_pid_map.json` (schema: pid, order, snapshot_hash, source_hash) ВМЕСТО/ВДОБАВОК к chunk_plan.json
- chunk_plan.json в whole-chapter — либо не писать (источник истины = whole_chapter_pid_map.json), либо явно пометить `"mode": "whole-chapter-derived"` + `"note": "chunk boundaries not used"`
- Обратная совместимость: resume-логика не должна зависеть от наличия/отсутствия chunk_plan.json (проверить `_load_journal`/resume path)

**Acceptance:** whole-chapter прогон пишет whole_chapter_pid_map.json (400 PID, порядок exact); chunk_plan.json либо отсутствует, либо явно помечен; resume из нового формата работает; полный suite проходит

**Non-goals:** изменение генерации/PID-контракта, удаление chunk_plan из chunked-режима (он там нужен)

**Когда:** до/вместе с B3 (production-сборка) — чтобы артефакты нового пайплайна были честными с первого прогона

## 15. Карточка BM — book_memory-наполнение (междуглавная аккумуляция фактов, ДО прогона новых глав)

> Решение владельца 2026-08-10: добавить в v4.1, реализовать ДО прогона второй/третьей главы — чтобы библия накапливалась между главами. Инфраструктура готова (`MemoryManager.add_observation`/`promote` поддерживают категорию `book_memory`, memory.py:41-48), но B9 наполняет только glossary (решение владельца 2026-08-04), а B7 (факты) не реализован.

**Принцип:** детерминированно, 0 модельных вызовов (решение владельца 2026-08-08: «только автоматически, без моделей»). На базе существующего chapter_index — расширение с междуглавной аккумуляцией, НЕ LLM-extraction (риск poison book_memory, урок «The Nurse: female»).

**Объём:**
- Междуглавная аккумуляция фактов из chapter_index: после каждой главы с accepted terminal (`complete` / `accepted_degraded`) кандидаты фактов → `add_observation("book_memory", ...)` → `promote` (существующий путь, quarantined-фильтр работает)
- Категории кандидатов (детерминированные, из source главы + существующего chapter_index):
  - персонажи (имя встречается ≥N раз / в ≥M главах) + пол (если source явно устанавливает: he/she/him/her в соседних PID)
  - факты (привязка к персонажам/местам/терминам по ключам — структура fact entry в book_memory уже явная)
  - narrator/пол — только если source явно подтверждает (fail-closed, как locked)
- Conflict resolution через существующий `MemoryManager.promote` (established/locked не перезаписываются)
- Пороги — по аналогии с B9 (term_min_chapters / min-occurrences), калибруются после первого book-run
- Артефакты: book_run.json (уже пишется), промоут-события с evidence PID (для ревью/отката)
- **Безопасность:** кандидат без явного source-подтверждения НЕ промоутится (uncertain facts omitted — принцип из B1.2/§8.3); book_memory_hash меняется только при реальном промоуте

**Acceptance:** book-run 2 глав → в book_memory.json добавлены только source-подтверждённые факты (пол/персонажи/факты), established/locked не тронуты; ни одного translation-derived или инференсного факта; полный suite проходит

**Non-goals:** LLM-извлечение фактов, изменение glossary-механизма (B9), изменение schema book_memory (только добавление)

**Когда:** ДО прогона второй/третьей главы (§12). Зависит от A2 (chapter_index уже есть). Может идти параллельно B-фазе.

**Зависимость:** BM → owner-run новых глав (накопление начинается с первого book-run)

---

## 8. Обновление 2026-08-09: audit_v4 — новый harness + промпт v4

> Реализовано в `D:\test folder\audit_v4.ps1` (синтаксис проверен). Основание — разбор полного Qwen-run и reasoning по проблемным PID (p00035, p00093, p00136, p00138, p00163, p00184/186, p00236).

### 8.1. Что добавил ревьюер (10 пунктов) и как реализовано

| # | Рекомендация | Реализация в audit_v4.ps1 |
|---|---|---|
| 1 | **CONTEXT_ONLY left-overlap** между чанками | `Get-OverlapContext`: предыдущие пары из ОРИГИНАЛЬНОЙ главы до ~400 токенов (мин 2, макс 6 пар); блок «CONTEXT_ONLY — NEVER report issue»; валидация не пропускает context id |
| 2 | Overlap сохраняется при retry/split | под-чанки получают контекст из **оригинального pairList**, не из разрезанного child |
| 3 | **ChapterEntityContext** (source-derived) | `-EntityContext` + блок «CHAPTER ENTITY FACTS — SOURCE-DERIVED» (правило 9 промпта) |
| 4 | Entity facts один раз на главу | готовый файл `chapter_entity_context_0001.txt` (Blake's vehicle + Rich/nurse, evidence PID) |
| 5 | Bible ≠ Entity раздельно | два отдельных блока: BOOK CONTEXT (fallback) / CHAPTER ENTITY FACTS (source-derived); иерархия: current source > overlap > chapter facts > Bible > inference |
| 6 | Speaker/addressee не кодом | правило 4 промпта: speaker/addressee/referent — три роли; «Слышала?» = адресат (female), не говорящий |
| 7 | **Debug mapping** issue→chunk→reasoning | `_debug: {chunk, reasoning_file}` в каждом issue + `reasoning_file` в chunk-мета |
| 8 | Чанки НЕ уменьшать | MaxInputTokens 3600, K-балансировка без изменений (8 вызовов на главу) |
| 9 | Retry не плодит вызовы | только LENGTH/invalid JSON → split; нормальный путь = 1 вызов/чанк |
| 10 | Менять prompt+context, не модель | Qwen R8192, тот же перевод, те же бюджеты |

### 8.2. Новые элементы промпта v4 (vs v3.1)

- **CONTEXT_ONLY-пары** (передаются, но НЕ аудируются) — лечит p00136 (Молли-говорит, но под-чанк начинался с её реплики → модель решила, что это Blake)
- **CHAPTER ENTITY FACTS** — лечит p00236 (bike→велосипед: факт `motorcycle` установлен в p00007, вне чанка)
- **Правило 4: speaker/addressee/referent — разные роли** — лечит p00163 («Слышала?» — женская форма описывает АДРЕСАТА, не говорящего Питера)
- **Правило 8: object identity** (cart/trolley != table, motorcycle != bicycle)
- **Правило 16: conservative ≠ ignore proven difference** — «не отбрасывай доказанную разницу как small/nuance»
- Усилены правила 5 (морфология: vnuk/vnuchka, сын/дочь), 6 (asserted fact → perception/belief = семантическое изменение: "was right" != "thought I was right")

### 8.3. Chapter entity context — schema per-claim (v2, после ревью)

**Статусы:** проверяется не вся запись, а её части: anchor span — `verified`; alias mention span — `verified`; `same_entity` relation — `candidate`.

```yaml
- entity: Rich
  claims:
    - kind: gender
      value: male
      status: verified                  # source-подтверждено (he/him в evidence)
      evidence: [p00197]
    - kind: alias_relation
      value: man_in_scrubs = nurse = Rich
      status: candidate                 # модельная гипотеза, НЕ авто-repair
      evidence_windows: [[p00177, p00180], [p00197, p00208]]
```

**Минимальная валидация кодом:**
1. Schema/version/source hash/chapter ID
2. Каждый PID существует
3. Каждый quoted span дословно существует в source
4. Ни одного translation-derived span
5. Canonical type явно присутствует в anchor evidence
6. Alias surface присутствует в своём PID
7. Gender evidence содержит проверяемую связь с нужным referent (не просто he/him где-то)
8. Неподтверждённая semantic coreference → `candidate`, не `verified`

**Правило:** `chapter_entity_context` никогда не авторизует repair самостоятельно; finding, зависящий от semantic relation, всегда Tier B.

### 8.4. Regression-цели A/B (v3.1-old vs v4)

**Должен начать ловить:** p00032 (youngest→младшему), p00035 (preoccupied), p00093 (уже не знал), p00138 (was right→казалось), p00184/186 (trolley→столик), p00236 (bike→велосипед, через entity fact)

**Должен продолжать:** p00010, p00013, p00132, p00193

**Не должен повторить:** p00136 (Molly→narrator), p00163 (Слышала), p00106 (Десяти), p00075 (её [руки]), p00151 (тётей), nurse-male формы

### 8.5. Исправлен баг retry-shrink (из Qwen-run)

**Баг:** при успешном shrink `$pending` не очищался → условие `$pending.Count -eq 0` проваливалось → **ложный FAILED_RETRIED** при фактически полном покрытии (issues из под-чанков при этом собирались корректно — врал только статус/`audit_complete`).

**Фикс:** `$pending = @()` при `$okAllSubs` → `GOOD_RETRIED` + честный `audit_complete`.

**Урок:** issues из под-чанков не терялись, но статус чанка и `audit_complete` могли врать — проверять оба при разборе результатов.

---

## 8. Ссылки

- Harness: `D:\test folder\audit_v4.ps1` (промпт v4 внутри, единственный source of truth; audit_v3 — архив)
- Контекст полов: `D:\test folder\narrator_context_0001.txt` (канонические имена только)
- Entity context (ручной пример): `D:\test folder\chapter_entity_context_0001.txt`
- Результаты: `D:\test folder\audit_v3_gemma_r8192.json`, `audit_v3_gemma_r8192_ctxfix.json`, `audit_v4_qwen_r8192*.json` (+_review_and_recommendations.md)
- Разборы ревьюера: в чате 2026-08-09

---

## 9. B1.1 — entity-context: тест-набор (утверждён 2026-08-09)

> Экстрактор: **Qwen** (source-only prepass, 1 вызов на главу). Включение: **после** baseline B и чистого A/B. Entity-context никогда не авторизует repair (всегда Tier B).

### 9.1. 8 кейсов

| # | Тип | Кейс | Что измеряет |
|---|---|---|---|
| 1 | Positive | один объект, разные названия: motorcycle→bike | extraction recall |
| 2 | Positive | один человек, разные обозначения: man in scrubs→nurse→Rich | extraction recall |
| 3 | Negative | motorcycle и отдельный bicycle/bike в одной главе | FP: ложная связь |
| 4 | Negative | два разных nurse | FP |
| 5 | Negative | generic role совпадает с именем/ролью из book memory, но это другой персонаж | FP: poisoned |
| 6 | Negative | один повторяющийся термин = два разных объекта | FP |
| 7 | Provenance | book memory: неправильный gender, source опровергает | poisoned context |
| 8 | Provenance | все spans существуют, но evidence window не доказывает same_entity edge | ложная «валидация по наличию слов» |

**Кейс №8 ключевой** — отделяет проверку spans от проверки семантической связи.

### 9.2. Порядок

1. Baseline B: чистый chunked аудит Qwen БЕЗ entity-context (после удаления test leakage из audit_v4.ps1)
2. A/B: без контекста / ручной gold / авто-extraction (Qwen, 1 вызов на главу)
3. 8 кейсов выше
4. Только после приемлемой precision — entity-context в production (B1.1)

### 9.3. Test leakage (критично, убрать ДО прогонов)

В audit_v4.ps1 пример `Blake's bike / vehicle = motorcycle, evidence p00007` (строки ~469/484) **зашит в промпт** — Qwen ссылалась на него в reasoning и после этого репортила p00097/p00098. Текущий результат доказывает только «применение подсказки», не «извлечение». **Пример в промпте заменить на нейтральный (не из Pact-главы 0001); контекст — только через `-EntityContext`.**

## 16. Карточка AF — A-fix: reasoning-cap 32k в whole-chapter генерации (remote)

> **Обнаружено 2026-08-10** (диагностика по run_007_remote_deepseek): 2 из 3 попыток генерации упёрлись в **reasoning=32 000, finish=length, output=0** (пустой вызов, retry спасал). Причину установили эмпирически.

### 16.1. Диагноз (решающий тест replay)

| Запрос | Дата | Тело (техническая часть) | reasoning | finish |
|---|---|---|---|---|
| Gate 0 no_bible (curl) | 08-08 | `model` + `parts` + `reasoningEffort` (**без system/tools**) | 84 933 | stop |
| **replay (тот же файл тела, curl)** | **10-08** | `model` + `parts` + `reasoningEffort` (**без system/tools**) | **55 915** | stop ✅ |
| run_007 попытка 1 | 10-08 | `model` + `parts` + **`system`** + **`tools`** + `reasoningEffort` | 32 000 | length |
| run_007 попытка 2 | 10-08 | то же | 32 000 | length |
| Прямой API (без serve, «думай долго») | 10-08 | OpenAI-формат, `reasoning_effort: high` | **41 272** | stop ✅ |

**Выводы (факты, не предположения):**
1. **Relay и модель лимита 32k НЕ имеют** (прямой API 41k+; models.dev: output 384k; replay 55.9k через тот же serve 1.4.7)
2. **Лимит 32k создаёт наличие `system` + `tools` в теле запроса** через serve 1.4.7: при их присутствии serve применяет дефолтный бюджет вывода модели (~32k), reasoning считается внутри него; при «голом» теле (model+parts+reasoningEffort) — лимита нет
3. `--reasoning 3` → `reasoningEffort: "high"` (подтверждено кодом, opencode_backend.py:770)

### 16.2. Что чинить (A-fix, remote-путь)

- `opencode_backend._build_message_body` (opencode_backend.py:744-779) добавляет `system` (pact-v4-neutral/v1: «Follow the user's instructions exactly...») и `tools` (все disabled) — из-за них serve ставит 32k-бюджет
- **Варианты:** (а) не слать `system`/`tools` в генераторном запросе (нейтральный system не влияет на качество перевода; tools all-disabled и так безвредны — но их наличие включает cap); (б) проверить, принимает ли serve поле для явного бюджета >32k; (в) `--reasoning 2` (medium) — но Gate 0 medium = 74k reasoning > 32k, тоже упрётся
- **Retry уже спасает** (run_007: попытка 3 — reasoning 153, полный перевод), но 2 пустых вызова на главу = ~$0.01 и +9 мин времени — для production неприемлемо
- **Не менять:** промпт v4.1 (заморожен), reasoning-budget локального Gemma (server_args, §3.4)

**Acceptance:** whole-chapter remote-прогон (--reasoning 3) даёт finish=stop с 1-й попытки в ≥2 из 3 прогонов; reasoning >32k возможен без обрыва; полный suite проходит

**Non-goals:** изменение литературного промпта, локального Gemma-пути, аудит/repair

**Когда:** до production-прогонов на новых главах (§12); не блокирует B-фазу (B-фаза — local Qwen)
