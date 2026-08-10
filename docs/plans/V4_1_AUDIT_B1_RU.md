# B1 — Аудит перевода: решения и измерения (конспект 2026-08-09)

> **Статус:** черновик для карточки B1. Основан на эмпирических тестах audit_v2/audit_v3 (скрипты в `D:\test folder\`).
> Обновляет устаревшие §6 (промпт аудита) и §8-B (single whole-chapter audit) основного плана.
> Хранение контекста/промпта: **скрипт `audit_v3.ps1`** — единственный источник правды текущей версии промпта.

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
| **Qwen R8192 chunk50 ctxfix (v3)** | 8 | (идет прогон) | — | — | — | — | — |
| Qwen R8192 prompt v4 | 8 | (следующий A/B) | — | — | — | — | — |

### 1.2. Ключевые выводы

1. **Qwen — не подходит как аудитор** (старый промпт): низкая precision, уверенные FP, `Two past twelve = 1:02` даже при инструкции в промпте.
2. **Gemma R2048 whole-chapter = 100% precision / низкий recall** (самый безопасный: «слишком мало репортит», но не врёт).
3. **Gemma chunked: precision 57%, recall 50%** — главная проблема не инфраструктура, а **контекст/семантика** (speaker, coreference, морфология).
4. **Спилл (reasoning → content)**: стохастичен, лечится адекватным `max_tokens` (llama считает reasoning+content ВМЕСТЕ) + RetryShrink.
5. **Потерянные TP**: p00032 (youngest→младшему), p00035 (preoccupied→поглощена собой), motorcycle→велосипед (long-range entity) — лечатся промптом v4 (правила 5/6/7) и chapter entity memory (скриптом, НЕ промптом).
6. **Стабильные FP (blind spots Gemma)**: русский эллипсис (p00075), морфологический синкретизм (p00151), локальная кореференция (p00309 он=кот/она=бабушка) — guardrails в промпте v4 (правила 9/10), кореференция НЕ лечится промптом (риск overfitting) → в verifier/entity layer.

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
| Модель аудита | **Gemma 4 26B A4B Q4_K_XL** (local, sycl-edge) | единственная стабильная; Qwen — открытый вопрос |
| Reasoning | **фиксированный 8192** на сервере (`--reasoning-budget 8192`) | reasoning — параметр СЕРВЕРА, не запроса; смена = перезапуск + identity-change |
| Chunking | **K-балансировка по входу**: `K = ceil(total/max_input)`, `target = total/K`, жадный добор без превышения лимита | нет короткого «хвоста» (урок: greedy по лимиту дал chunk из 4 пар) |
| `max_input_tokens` | **3600** = `reasoning_budget/2 × 0.88` (запас 12%) | формула «вход×2 = reasoning» подтверждена: R8192=8128 сработал, R4096=4096 нет |
| `max_tokens` | **12000** = reasoning + ~3500 на content | llama считает reasoning+content вместе; 3000 при R8192 → LENGTH с 0 content |
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
- конфликт issue с явным source/entity фактом (nurse=Rich male)
- PID вне чанка / invalid category

**Tier B — LLM semantic claim** — НЕ подтверждается «regex ничего не опроверг»:
- referent, invented_gender без явных следов, idiom omission, negation scope, cross-paragraph
- → второй semantic confirmation (см. repair-as-verifier) ИЛИ diagnostic/debt

### 5.2. Repair-as-verifier (Kocmi-safe: repair-модель = генератор, аудитор ≠ генератор)

```
Gemma auditor → HARD filters (Tier A) → repair/confirmation (Tier B)
```
Repair-промпт: «You are given a proposed fidelity issue. First verify the issue
against SOURCE and TRANSLATION. If the auditor is wrong, return PASS and make no
change. Only if confirmed, return the corrected translation.»

- бесплатный второй semantic decision (repair и так вызывается)
- две независимые semantic оценки, без третьей модели/фазы
- **НЕ нарушает Kocmi** при repair = генератор (DeepSeek/Gemma) и аудитор = другая модель (Gemma/Qwen)

### 5.3. Repair eligibility

- Tier A (кодом подтверждённые) → repair напрямую
- Tier B → через repair-as-verifier
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

1. **Qwen с промптом v4** — тест идёт (A/B): подтвердить recall p00032/35/93 без роста FP → финальная валидация пары Gemma→Qwen (решение по моделям уже принято: §1.3)
2. ~~Same-model Gemma-аудит~~ — **закрыто решением владельца 2026-08-09**: Gemma переводит, Qwen аудитит (Kocmi-safe); same-model только diagnostic
3. **Chapter entity extraction** — отдельный этап (entity map с evidence PID, gender, aliases) → для long-range consistency (motorcycle) и Tier B invented_gender; можно кэшировать per-chapter
4. **Speaker attribution** (metadata per PAIR) — только когда уверенно; иначе не аннотировать (wrong speaker = poison)
5. ~~Параметры Qwen server~~ — **получены владельцем 2026-08-09**, зафиксированы в §3.4 основного плана и runtime_local.example.yaml
6. Harness audit_v4.ps1 → production-интеграция в B1: переносить логику (K-балансировка, RetryShrink, overlap, fail-closed) в Python-код pipeline

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

### 8.3. Chapter entity context (файл chapter_entity_context_0001.txt)

```
- entity: Blake's vehicle
  aliases: motorcycle, bike
  established_type: motorcycle
  evidence: p00007, p00011
- entity: Rich (the nurse / man in scrubs)
  gender: male
  aliases: the nurse, Rich, Nurse Rich
  evidence: p00197, p00208, p00264, p00285
```

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

- Harness: `D:\test folder\audit_v3.ps1` (промпт v4 внутри, единственный источник правды)
- Контекст полов: `D:\test folder\narrator_context_0001.txt` (канонические имена только)
- Результаты: `D:\test folder\audit_v3_gemma_r8192.json`, `audit_v3_gemma_r8192_ctxfix.json` (+_review_and_recommendations.md)
- Разборы ревьюера: в чате 2026-08-09
