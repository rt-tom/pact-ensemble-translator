# Хендофф: Glossary + Book Memory (Bible) — текущая архитектура и предложения

**Дата:** 2026-08-14
**Автор:** архитектор (Pact Translator v4)
**Цель:** детальный разбор текущей работы glossary + book_memory для независимого ревью/анализа.
**Ветка:** dev/v4.1-reasoning-transport (клон D:\pact\pact_translator_v4_1)

---

## ЧАСТЬ 1. ЧТО ЕСТЬ СЕЙЧАС (факты из кода и артефактов)

### 1.1. Glossary (glossary.json)

**Файл:** `D:\pact\pact_chapters\glossary.json` — плоский dict `{source: target}`, 147 записей, без locked-меток (locked-механизм в v4 НЕ используется).

**История:** 137 записей «authoritative» (из v3-эры, ручные) + 10 подтверждений от book_run 2026-08-13 (главы 1-3). Ни одной НОВОЙ записи от v4.1 прогонов не добавлено (см. 1.5).

**Как читается в пайплайн** (`_shared_runner_helpers.py:82` `_glossary_entries`):
- `dict {source: target | [targets]}` или `list [{source_term, target_terms}]` → `GlossaryEntry(source_term, target_terms)`
- Любое нечитаемое — молча дропается (пустой glossary = «нет ограничений», не ошибка)

**Как попадает в промпт генерации** (`v4_phase12_strict_runner.py:2817` + `_shared_runner_helpers.py:157` `_glossary_entries_for_chunk`):
- Per-chunk бюджет: запись попадает в `PromptBundle.glossary` ТОЛЬКО если source-термин присутствует в тексте чанка (owned+left+right, `_term_present` word-boundary regex IGNORECASE)
- **ИЛИ** она `always_include` (fail-closed: locked-ограничения не режутся):
  - tied to `required_risk_feature_codes` чанка (number_word / tone_profanity — замороженные regex)
  - несёт `glossary_conflict` (один source → >1 target)
  - narrator-имя при закреплённом narrator_gender
- `bundle_hash` выводится из *kept*-множества → cache/resume следуют за фильтром
- Артефакт диагностики: `glossary_budget_report.json` (kept/dropped per chunk)

**Как НАПОЛНЯЕТСЯ (B9, `glossary_candidates.py` + `v4_book_run.py`):**
- Сканер: токены ≥3 вхождений (terms), ≥2 (proper names), ≥4 буквы, не в EN_STOP_WORDS
- EN_STOP_WORDS — 217 function words (и, но, the, to...), **НЕ содержит частотных глаголов/существительных** (said, looked, going, like, made, things, books, will, little)
- `kind = proper_name` (капитализирован в середине предложения, title-case, никогда lowercase) ИЛИ `term`
- Consensus alignment (0 модельных вызовов): source → русские варианты из переводов; если dominant-вариант ≥0.8 (`consensus_ratio`) → `target`, иначе → `conflicts` (без target)
- B9-fix эвристики: ранжирование по candidate-specificity (не по raw частоте); proper_name target, совпадающий с established glossary VALUE другого ключа → дроп
- **Ledger** (`glossary_candidates.json`, NDJSON, append-only): ВСЕ кандидаты включая конфликтующие
- **Promotion** (`v4_book_run.py:505-575`): кандидат → proposed только если:
  - нет conflicts (alignment сошёлся)
  - есть target
  - cumulative ledger-запись имеет единый merged target (B9-F3: 2 главы с разными target = conflict навсегда)
  - термин: ≥2 глав (`term_min_chapters`) И ≥N вхождений; имя: порог по occurrences
  - нет established glossary с другим target
- **MemoryManager.promote** (`memory.py:51`): пишет в glossary.json только при реальном изменении (byte-preservation: не трогает файл, если merge ничего не изменил). `complete`/`accepted_degraded` → promote; failed → нет

### 1.2. Book Memory (book_memory.json + bible_renderer)

**Файл:** `D:\pact\pact_chapters\book_memory.json` — структура:
```json
{
  "version": 1,
  "pov": {"gender": "male", "source_name": "Blake Thorburn", "target_name": "Блэйк Торбёрн", ...},
  "characters": {"Blake Thorburn": {"type": "character", "gender": "male", "target": "Блэйк Торбёрн", ...}},
  "entities": {"June": {"type": "object", "gender": "unknown", "notes": ["Blake's hatchet/blade."], "chapters": [...]}},
  "address_register": [{"from": "Blake", "to": "Duncan", "register": "вы", "text": "..."}],
  "facts": [{"fact": "Power is treated as a form of currency...", "source_pids": [], "chapter": "0046"}],
  "chapters": ["0046_subordination-6-3.html", ...]
}
```
Текущее содержимое — **из v3-эры** (chapters 0046/0060/0100/0112/0148 — старые главы, НЕ bonds 1.1-1.3!).

**Как рендерится в библию** (`bible_renderer.py:176` `render_bible_section`):
- V4.1 A2: chapter-based — `render_bible_section(chapter_id, chapter_index, book_memory)`; entry из `chapter_index.json` (детерминированный per-chapter: characters/facts/address, без caps)
- Legacy fallback (`chapter_index.json` отсутствует или chapter_id не в индексе): полный дамп memory с caps (`_MAX_CHARACTERS`, etc.)
- narrator_gender всегда включается (fail-closed)
- **В прогонах глав 1-3 `chapter_index.json` НЕ строился** → использовался legacy full-memory render

**Как наполняется (B7/BM, `book_memory_candidates.py` + `v4_book_run.py:613`):**
- Детерминированно, 0 модельных вызовов (решение владельца 2026-08-08: «только автоматически, без моделей» — риск poison, урок «The Nurse: female»)
- Кандидаты: персонажи (имя ≥N раз/≥M глав) + пол (source явно: he/she/him/her) + факты
- `add_observation("book_memory", ...)` → `promote` (established/locked не перезаписываются)
- **Артефакт:** `book_memory_candidates.json` + book_run.json block

### 1.3. Названия глав (АРКИ)

**v3:** `arc_names.json` (в корне репо) — `{"Bonds": "Узы", "Subordination": "Подчинение", "Execution": "Казнь", ...}` (15 арков). В v3 промпт содержал секцию:
```
АРКИ:
- Bonds → Узы
- Subordination → Подчинение

ГЛОССАРИЙ:
...
```
(`pact_translate_v3.py:880` `arcs_text(cfg)` + `:1057` вставка в промпт).

**v4:** `arc_names.json` ЕСТЬ в корне, копируется gate_bench'ем (`v4_phase0c_gate_bench.py:64`), НО **в v4 промпт НЕ попадает** — секция «АРКИ» потеряна при переходе.

### 1.4. v3 правило «Не превращай мотоцикл в велосипед»

В v3 промпте было правило `- Не превращай мотоцикл в велосипед.` (pact_translate_v3.py:1047). В v4 это правило ОТСУТСТВУЕТ. Ревьюер трёх глав поймал ровно эту ошибку в v4 (bike → велосипед в 1.2, хотя в 1.1 это мотоцикл). **Владелец запретил хардкодить такие правила в промпт** (2026-08-14) — решение должно идти через book_memory (entity fact), не через статичные правила.

### 1.5. Факты из прогона book_run 0001-0003 (2026-08-13)

**Glossary:**
- 545 кандидатов в ledger: 429 без target (alignment conflicts), 116 с target
- Мусор: said (99 вхождений!), don't (35), looked (29), going (27), like (24), didn't (23), will (23), made (21), things (21) — частотные слова не в EN_STOP_WORDS
- 392 кандидата с конфликтами (72%) — переводы не сошлись (сказал/сказала/отрезала...)
- **В production glossary добавлено 0 новых записей** (10 «committed» = no-op подтверждения существующих: friends, hallway, Rosalyn...)
- 47 честных кандидатов главы 3 (bathroom, desk, enemies...) НЕ прошли — нужна 2-я глава (term_min_chapters=2)

**Book memory:**
- гл.1: 8 кандидатов, 1 committed (Master → character)
- гл.3: 38 кандидатов, 4 committed: **«English» (character, gender=female!), «Rosalyn» (character, gender=male!), «Shamanism» (character), «Beings» (character)** — это МУСОР: капитализированные слова классифицированы как персонажи; Rosalyn — женское имя с мужским родом (poison!)
- «English» якобы female — определение пола по соседним he/she в соседних PID, но сработало ложно

### 1.6. Мусор уже в production glossary (147 записей)

asked → спросил, body → тело, chest → груди, dark → тёмными, door → дверь, edge → края, every → каждый, home → дома, seat → сиденье, shoulder → плечо, voice → голос, twenty → двадцать — обычные словарные слова, попавшие в v3-эру (стабильный перевод, но не термины).

---

## ЧАСТЬ 2. ПРЕДЛОЖЕНИЯ (единый подход glossary + book_memory)

### 2.0. Принцип разделения authority (согласован владельцем + ревьюером трёх глав)

```
GLOSSARY          — «как это НАЗЫВАЕТСЯ»: диктует ФОРМУЛИРОВКУ (Other MUST → Иной)
BOOK MEMORY       — «что это за СУЩНОСТЬ» / «что мы знаем о мире»: диктует ФАКТ
                     (Blake's vehicle = motorcycle; переводчик сам выберет
                     мотоцикл/байк/его мотоцикл — но НЕ велосипед)
```

### 2.1. Glossary: определение и авто-promotion

**Определение (ревьюер):** «Glossary содержит элементы, для которых важно сохранить конкретное переводческое решение независимо от локального контекста»:
- имена собственные; географические названия; авторские термины мира; названия ритуалов/книг/организаций; устойчивые титулы/обращения

**НЕ должны попадать** (даже при 100% стабильном переводе): asked, door, body, looked — обычные слова.

**Изменения:**
1. **Отключить авто-promotion `kind=term`** по частоте/стабильности (сейчас: term_min_chapters=2 + occurrences → promote). Стабильность перевода — НЕ детектор термина (door→дверь стабильно, но не термин; настоящий термин может первое время переводиться нестабильно: Other → другой/иной/Иной).
2. **proper_name авто-promotion — ОСТАВИТЬ** (порог: ≥1 главы, occurrences ≥2-3; решить точный порог).
3. **Двухуровневость:**
   - `locked` (авторитетные, в промпт как обязательные): proper names + подтверждённые world terms + существующая терминология Pact
   - `candidate/observed` (система заметила, но НЕ имеет права диктовать): `{source, observed_targets, kind: term_candidate, locked: false, evidence_count}` — мониторинг consistency, позже ручной/строгий апгрейд до locked; НЕ становится prompt-командой автоматически (избежать самоусиливающейся ошибки)
4. **Очистить production glossary** от обычных слов (список в 1.6) — с явного разрешения владельца (правка production-данных).
5. **Seed/lock терминов мира** (ревьюер перечислил, но ПЕРЕД фиксацией русских эквивалентов — терминологический обзор, т.к. 3 главы = идеальный момент):
   ```
   Other → Иной        Practitioner → практик     Awakening → Пробуждение
   Familiar → фамильяр Famulus → Фамулус           Implement → орудие/инструмент [после выбора]
   Implementum → Имплементум Demesne → Владение    Forsworn → ... [оценить]
   Hillsglade House → Дом-на-Холме  Karma → ...
   ```

### 2.2. Book memory: сущности с aliases + world facts

**Проблемы сейчас:** entities без aliases (June: hatchet — нет «aliases: [blade, hatchet]»); пол по соседним he/she ложно (Rosalyn male, English female); B7 пишет мусор (Shamanism/Beings как персонажи); promotion пороги блокируют честные факты после 1 главы.

**Изменения:**
1. **Entity-записи с aliases** (ревьюер):
   ```yaml
   entity: blake_vehicle
   type: motorcycle
   canonical_ru: мотоцикл
   source_aliases: [motorcycle, bike]
   owner: Blake Thorburn
   ```
   → при «I can't ride my bike» модель получает НЕ «bike = мотоцикл» (глобально), а «у Блэйка vehicle = motorcycle» (контекстно). `bike → велосипед` = эталонный regression-тест для book_memory retrieval.
2. **Промоут по итогам ОДНОЙ главы** (владелец: «промоутить что-то нужно даже по итогам одной главы») — для source-подтверждённых фактов (не инференсных).
3. **Ужесточить классификацию персонажей** — не всякий капитализированный токен = персонаж; порог + явное подтверждение (он/она в соседних PID), fail-closed (урок «The Nurse: female»).
4. **World facts** (третья категория ревьюера): «Hillsglade House is sanctuary», «Rose exists in reflections», «Thorburn inheritance follows candidate order» — физически могут жить в book_memory.facts (не плодить отдельную систему).
5. **B7/BM сгенерировать chapter_index.json** (A2) — чтобы библия рендерилась chapter-based (per-chapter фильтрация), а не legacy full-dump.

### 2.3. Названия глав (АРКИ) — вернуть в v4

- `arc_names.json` уже есть в корне (15 арков: Bonds→Узы, ...)
- **Вернуть секцию «АРКИ:» в промпт генерации** (как в v3): это часть терминологии книги — перевод названий глав должен быть стабильным (Bonds = Узы во всех главах)
- Куда: в glossary-блок промпта (как locked-термины) или отдельной секцией — на решение
- Тест: перевод содержит «Узы» для Bonds

### 2.4. НЕ делать (запреты владельца)

- ❌ НЕ хардкодить «Не превращай мотоцикл в велосипед» в промпт (владелец 2026-08-14) — только через book_memory entity fact
- ❌ НЕ добавлять 4-й LLM-проход для ловли остаточных ошибок (ревьюер: плохой обмен времени на качество; сначала контекст существующих вызовов)
- ❌ НЕ тюнить модели на Bonds 1.1 (ревьюер: переводить 1.4/1.5/1.6 замороженной конфигурацией, собирать residual classes)

### 2.5. Приоритеты (предложение)

| # | Что | Категория | Почему |
|---|---|---|---|
| 1 | Вернуть АРКИ в промпт | код (быстро) | названия глав — часть терминологии, потеряна при переходе на v4 |
| 2 | Glossary: отключить term-auto-promotion + двухуровневость locked/candidate | код | убирает 429/545 мусора из кандидатов |
| 3 | Очистить production glossary от обычных слов | данные | 12+ записей мусора в промпт каждой генерации |
| 4 | Book memory: aliases + промоут по 1 главе + ужесточить классификацию персонажей | код | убирает poison (Rosalyn male, English female) |
| 5 | Терминологический обзор перед lock world-terms | процесс | 3 главы = идеальный момент (ревьюер) |
| 6 | Regression: bike→велосипед как тест book_memory retrieval | тест | эталонный кейс, показывает ценность обеих систем |

---

## ЧАСТЬ 3. ВОПРОСЫ ДЛЯ РЕВЬЮЕРА

1. Согласны ли с принципом разделения (glossary диктует формулировку, book_memory — факт)?
2. Достаточно ли «proper_name + locked world-terms» для glossary, или есть класс записей, который потеряется (устойчивые выражения? титулы? обращение ты/вы — уже в address_register)?
3. Двухуровневость locked/candidate — как долго кандидат живёт до апгрейда? Кто апгрейдит (человек, строгий порог, ревью)?
4. Пороги proper_name для 1-главы: ≥2 или ≥3 вхождений?
5. B7-классификация персонажей: как отделить «English» (язык) от персонажа? (сейчас capitalized + title-case = персонаж)
6. АРКИ: отдельная секция промпта или в glossary-блок?
7. Нужен ли ручной ввод терминов мира (seed) или полу-автоматический (предложения + владелец подтверждает)?

---

## Артефакты для проверки

- `D:\pact\pact_chapters\glossary.json` — 147 записей (12+ мусор)
- `D:\pact\pact_chapters\book_memory.json` — v3-эра + poison от B7
- `D:\pact\gate_bench_runs\v4_book_0001-0003_local\glossary_candidates.json` — 545 кандидатов (429 мусор)
- `D:\pact\gate_bench_runs\v4_book_0001-0003_local\book_run.json` — сводка по главам
- `arc_names.json` (корень репо) — названия арков
- Код: `pact_v4/phase1/glossary_candidates.py`, `pact_v4/phase1/book_memory_candidates.py`, `pact_v4/phase1/memory.py`, `pact_v4/runtime/bible_renderer.py`, `pact_v4/pipeline/_shared_runner_helpers.py`, `pact_v4/pipeline/v4_phase12_strict_runner.py`, `pact_full_pipeline_runner_v1/v4_book_run.py`
- План: `docs/plans/V4_1_AUDIT_B1_RU.md` §15 (карточка BM), `docs/plans/V4_B9_GLOSSARY_OBSERVATIONS_TASK_RU.md`

---

## ЧАСТЬ 4. РЕВЬЮ 2026-08-14: APPROVE WITH CHANGES (финальные решения)

### 4.0. ГЛАВНЫЙ РИСК — future leakage из book_memory (P0 blocker)

**Подтверждено:** book_memory.json содержит факты из глав 0046/0060/0100/0112/0148 (арки Subordination 6.3 / Void 7.5 / Duress 12.1 / Execution 13.4 / Judgment 16.12 — в романе ПОСЛЕ Bonds 1.x). При book-run 1-3 chapter_index.json НЕ строился → legacy full-memory render → модель при переводе глав 1-3 видела факты из глав 46-148 (примеры: «An implement cannot be changed once chosen», «Alexis wants a custom iron tattoo gun as her implement»).

**Инвариант (P0):**
```
Для chapter N:
  allowed memory = global immutable seed
                 + confirmed facts from chapters < N
                 + source-derived facts of chapter N (если нужны для chunking)
  NEVER: facts from chapters > N
```

**Действия:**
1. Сохранить старый book_memory.json как архив (book_memory_v3_archive.json)
2. Создать clean seed (pov.gender male + минимальные глобальные факты)
3. Прогнать 1.1 → update → 1.2 → update → 1.3 → update → 1.4... (причинный порядок)
4. B7 пишет chapter_id при каждом факте (уже есть) — проверять chapter_id < N при рендере

### 4.1. Glossary = формулировка, Book Memory = факт (закреплено)

Regression-пара:
- Glossary: Other → Иной (диктует формулировку)
- Book memory: Blake owns/rides a motorcycle (факт; при «my bike» модель сама резолвит — НЕ глобальное «bike = мотоцикл»)

### 4.2. Двухуровневость = границей ФАЙЛА, не полем locked

- `glossary.json` = ТОЛЬКО authoritative (всё внутри = locked, попадает в prompt)
- `glossary_candidates.json` = observations/proposals, НИКОГДА напрямую в prompt
- Поле `locked: true/false` внутри glossary.json НЕ вводить (риск бага: locked:false попадёт в PromptBundle)

### 4.3. Generic term auto-promotion — ОТКЛЮЧИТЬ (P1)

Сигнал «частота + стабильный alignment» не определяет terminology (545/3 главы → десятки тысяч шума на 148 главах). Возможно, вообще убрать активный generic-term scanner (оставить offline telemetry).

### 4.4. Proper names: auto-promotion остаётся, порог 2, но строгая классификация (P1)

- Порог: 2 source occurrences (1 → candidate). 3 теряет редкие реальные имена.
- capitalized + title-case НЕ достаточно (English/Shamanism/Beings стали персонажами)
- **proper_name != character**: glossary может распознать «Rosalyn Thorburn» как имя, НЕ утверждая, что это персонаж
- Character — отдельный high-precision detector (speaker attribution, vocative, kinship, known seed)

### 4.5. Gender — extreme conservative (P0, poison)

- «имя рядом с he/she» НЕ достаточно (Rosalyn → male, English → female — poison на 140 глав)
- Только high-precision evidence: «Rosalyn was my grandmother», «the woman, Rose», «Mr./Mrs. X», kinship, narrator
- **unknown безопаснее wrong**: gender: unknown лучше, чем ошибочный gender

### 4.6. Promotion после 1 главы: да, только SOURCE_EXPLICIT (P1)

- SOURCE_EXPLICIT (source прямо установил: «Blake pushed his motorcycle») → authoritative после 1 появления
- INFERRED (bike = alias того же мотоцикла — coreference) → candidate only
- НЕ time-based: candidate живёт в ledger сколько угодно; promote только по explicit evidence или owner-решению; после 20 глав без повторения → stale (не удалять)

### 4.7. Aliases — фаза 2, не обязательное условие (P2)

Механизм получения aliases не описан (bike = bicycle или motorcycle? — coreference). Для bike-кейса достаточно факта «Blake's established personal vehicle is a motorcycle» — сильная модель сама резолвит. Aliases — optimisation/retrieval hint позже.

### 4.8. chapter_index.json — обязателен, legacy fallback УБРАТЬ (P0)

```
production: chapter_index missing/corrupt → STOP (fail, не fail-open)
--allow-degraded: narrator + minimal global seed, accepted_degraded по explicit flag
```
НИКОГДА автоматически полный book_memory dump.

### 4.9. Glossary fail-open → FAIL (P0)

```
production: invalid glossary → FAIL (не «переводим без терминологии»)
--allow-degraded: run + loud artifact warning
```
То же для authoritative Book Memory.

### 4.10. АРКИ — deterministic renderer, НЕ LLM (P1)

- `Bonds → Узы` — чистая deterministic metadata: renderer сам пишет «Узы 1.3» из arc_names.json
- 0 токенов, 0 stochasticity, 100% consistency, простейший regression-тест
- Если заголовок обязан проходить через Translator — отдельная секция ARC:, но deterministic предпочтительнее

### 4.11. Очистка glossary — одноразовый review ВСЕХ 147 записей (P1)

- Не только 12 известных: классификация v3 была загрязнена; 147 — дёшево сейчас, дорого на 1500
- История: glossary_v3_archive.json (архив) + новый чистый glossary.json
- Идиомы/устойчивые выражения НЕ lock глобально (зависит от контекста); ты/вы — address_register, не glossary

### 4.12. Приоритеты (финальные, от ревьюера)

| # | Приоритет | Что |
|---|---|---|
| 1 | **P0** | Clean/rebuild Book Memory, запрет future leakage |
| 2 | **P0** | Убрать legacy full-memory fallback; chapter_index обязателен |
| 3 | **P0** | Остановить B7 poison: strict entity/gender promotion |
| 4 | **P1** | Отключить generic term auto-promotion |
| 5 | **P1** | Очистить authoritative glossary целиком (review 147) |
| 6 | **P1** | Вернуть arc mapping (deterministic) |
| 7 | **P1** | Source-explicit entity facts + causal book memory |
| 8 | **P2** | Aliases/retrieval improvements |
| 9 | **P2** | Terminology candidate workflow + lock review |

### 4.13. Regression suite (расширенный)



### 4.14. Вопросы, требующие проработки (после P0)

1. Механизм alias discovery (bike → motorcycle) — когда появится безопасный алгоритм
2. Классификация characters: конкретные правила high-precision detector
3. Где живёт chapter_index.json (строится per-book-run из артефактов глав < N)
