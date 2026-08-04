# V4 B7 — Библия + междуглавная память + book-run (task)

Backing spec:
- `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` (§4, Поток B+ — B7).
- `DECISIONS.md` (2026-08-03: порядок B4–B8, B7 — библия + междуглавная).
- `docs/audits/V4_PHASE12_STRICT_0001_RUN001_ANALYSIS_RU.md` (раздел 7: смена рода рассказчика в chunk0015–chunk0016 — ни один слой не поймал).
- Зависит от B4 (JSON-устойчивость, влита в `main`), B5 (mixed_script-политика, влита в `main`), B6 (quarantined retry, влита в `main`).

Target: `main`. Draft PR. Характер: REVIEW REQUIRED — добавление библии в промпты, междуглавная память, book-run wrapper.

## Зачем это отдельная карточка

run_001 выявил: смена рода рассказчика в chunk0015–chunk0016 («увидел» → «увидела») — ни один слой не поймал. Причина: библия (`book_memory.json`) загружается в `ChapterMemory`, но её содержимое (пол рассказчика, персонажи, факты) **никогда не попадает в промпты** (генерация, fidelity-гейт, аудит). `_glossary_entries(memory)` извлекает только glossary, не book_memory.

Это не просто «добавить библию в промпт» — это **фундаментальный гэп** в v4, который закрывает:
- Род рассказчика (narrator_gender)
- Персонажи (characters)
- Факты (facts)
- Address register (ты/вы)
- Междуглавная консистентность (аккумуляция знаний между главами)

## Что реализовать

### 1. Рендер библии в промпты

**Источники (как в B5):**
- `book_memory.json` — основной источник (персонажи, факты, адресный регистр, POV/род рассказчика).
- `glossary.json` — уже рендерится (не менять).
- Source-derived — уже есть (не менять).
- Manual config — уже есть (не менять).

**Что рендерить:**
- `pov.gender` — пол рассказчика (мужской/женский/неизвестный).
- `characters` — список персонажей с атрибутами (имя, пол, роль).
- `facts` — факты книги (локации, события, термины).
- `address_register` — ты/вы паттерны.

**Куда рендерить:**
- **Генерация** (`pact_v4/phase2/generation.py`): добавить `BIBLE` секцию в промпт после `GLOSSARY`.
- **Fidelity-гейт** (`pact_v4/phase2/cascade.py`): добавить `BIBLE` секцию в промпт Qwen fidelity.
- **Аудит** (`pact_v4/phase3/audit.py`): добавить `BIBLE` секцию в промпт qwen_chapter_audit.

**Формат рендера:**
```text
BIBLE:
- Narrator: male (pov.gender)
- Characters:
  * John (male, protagonist)
  * Mary (female, sister)
- Facts:
  * The story takes place in London, 1890.
  * John inherited the estate from his uncle.
- Address register:
  * Use "ты" for family members, "вы" for strangers.
```

**Бюджет токенов:** ограничить рендер библии (например, top-k персонажей, max 500 токенов), чтобы не раздувать промпт.

### 2. Narrator gender check

**Детерминированная проверка:**
- Извлечь `pov.gender` из `book_memory.json`.
- После сборки главы (после B2 repair / B6 retry) — сканировать финальный текст на самоотсылочные формы «я + глагол прошедшего времени».
- Проверить согласование рода: «я увидел» (мужской) vs «я увидела» (женский).
- Если несоответствие — добавить finding в `audit_findings.json` (категория `narrator_gender`).

**Интеграция:**
- Добавить в `pact_v4/_integrity_checks.py` функцию `check_narrator_gender(text, expected_gender)`.
- Вызывать в Step 8 (final integrity check) после formatting.
- Если finding — `integrity: failed`, терминал `accepted_degraded` (если PID-map валиден) или `failed`.

### 3. Междуглавная аккумуляция

**Логика promotion (как в DECISIONS 2026-08-03):**
- При `complete`: promote все observations в `book_memory.json` (текущая логика `MemoryManager.promote()`).
- При `accepted_degraded`: promote observations только из чанков, которые **не были карантинными** (решение владельца).
- При `failed` / `quarantined`: не promote (текущая логика).

**Реализация:**
- `MemoryManager.promote(status, quarantined_chunks=None)` — добавить параметр `quarantined_chunks` для фильтрации.
- Вызывать в strict-драйвере после терминального статуса.

**Identity:**
- `book_memory_hash` уже есть в снапшоте — смена библии инвалидирует cache/resume.
- Promotion меняет `book_memory.json` → следующая глава видит обновлённую библию.

### 4. Book-run wrapper

**Зачем:** для междуглавной аккумуляции нужен wrapper, который запускает главы по порядку на общем `--memory-dir`.

**Реализация:**
- Новый CLI: `python -m pact_full_pipeline_runner_v1.v4_book_run --memory-dir <dir> --chapters <list>`.
- Логика:
  1. Для каждой главы: вызвать `v4_phase12_strict_run.py` с общим `--memory-dir`.
  2. После каждой главы: вызвать `MemoryManager.promote(status, quarantined_chunks)`.
  3. Артефакты: `book_run.json` (история прогонов, статусы, promotion events).

**Identity:**
- Каждая глава — отдельный run с собственным `chapter_id`, `source_hash`, etc.
- `book_memory_hash` обновляется после promotion → следующая глава видит новую библию.

### 5. Тесты

- Unit: рендер библии в промпт (генерация/fidelity/аудит); narrator_gender check; promotion при complete/accepted_degraded.
- Integration: fake backend с библией (pov.gender=male) → генерация с женским родом → narrator_gender finding.
- Book-run: две главы подряд → promotion после первой → вторая видит обновлённую библию.
- Полный `tests/pact_v4/` зелёный.

## Вне scope (другие карточки)

- B8 (повторный прогон главы 0001) — отдельная карточка.
- Phase 1/2, cascade, risk — нельзя менять (кроме передачи библии в промпты).
- Транслитерация (альтернатива mixed_script) — не реализуем.

## Gate / Acceptance

1. Библия (`book_memory.json`) рендерится в промпты генерации/fidelity/аудита.
2. Narrator gender check: несоответствие рода → finding → `integrity: failed`.
3. Promotion при `complete`: все observations → `book_memory.json`.
4. Promotion при `accepted_degraded`: observations из non-quarantined чанков → `book_memory.json`.
5. Book-run wrapper: запуск глав по порядку, promotion после каждой.
6. DECISIONS.md — запись о библии/междуглавной (в том же коммите).

## Роль-сплит

Обычная V4-фаза: «реализует, второй — adversarial review». Перед стартом спросить, кто пишет код.

## Компактный промпт

```text
Реализуй v4 B7 (библия + междуглавная + book-run) из
docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md (Поток B+, B7).
Target: main. Draft PR. Рендер book_memory в промпты, narrator_gender check,
promotion при complete/accepted_degraded, book-run wrapper.
Не трогай v3, phase1/2, cascade, risk (кроме передачи библии в промпты).
```
