# V4 B9 — Генератор кандидатов глоссария + add_observation в проде (task)

Backing spec:
- `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` (§4, Поток B+ — B9; §5 — порядок и guardrails).
- `DECISIONS.md` (2026-08-04: решение владельца — B9 отдельной задачей, параллельна Потоку D, не блокирует B8/D1; 6 provisional-записей v3-глоссария не включаются).
- v3-механика: генератор кандидатов в `pact_translate_v3.py` (v3-глоссарий: `glossary/established.json`, `locked.json`, `provisional.json`, `conflicts.json`).
- `pact_v4/phase1/memory.py` — `MemoryManager.add_observation` (примитив уже есть, в проде не вызывается), `promote` (B7, работает в `v4_book_run`).

Target: `main`. Draft PR. Характер: REVIEW REQUIRED — новый код в production book-run (сбор наблюдений), изменение lifecycle памяти.

## Зачем это отдельная карточка

Глоссарий в v4 пополняется **вручную** (правка `glossary.json`). `add_observation`
существует в `MemoryManager` (memory.py:41), но вызывается только в тестах.
v3-генератор кандидатов (`glossary_candidates.*.json`) в v4 не перенесён —
наблюдениям неоткуда взяться, а `promote` (B7) при пустых наблюдениях ничего
не переносит. B9 закрывает гэп: **кто и как собирает кандидатов терминов в
проде**, чтобы междуглавная аккумуляция (B7) работала по-настоящему.

Проверено (2026-08-04): `observations.json` не входит в `chapter_memory.json`
snapshot (там только glossary + book_memory) и не участвует в промптах/гейтах/
`bundle_hash`/identity — B9 не влияет на результат переводов и гейтов, поэтому
не блокирует B8-прогон и D1.

## Что реализовать

### 1. Генератор кандидатов (аналог v3)

Детерминированный, без моделей (частотный/regex-скан по тексту):

- Вход: финальный текст главы (кандидаты по чанкам или по всей главе — как в v3).
- Правила: source-термины (латиница) с частотой ≥ порога, отсутствующие в
  `glossary.json` / `book_memory.json` (variants/characters), с контекстом
  (пример употребления, глава/chunk_ids).
- Исключения: уже established/locked в glossary; имена персонажей из библии;
  токены из mixed_script-allowlist (B5) — не кандидаты.

Три разных контракта, их нельзя смешивать (review B9-RV3, HIGH):

1. **Запись генератора (candidate)** — `{source, kind, occurrences,
   chunk_ids, context}` (`pact_v4/phase1/glossary_candidates.py:325-345`):
   `kind` — `"proper_name"`/`"term"`, `chunk_ids` — отсортированный список
   уникальных чанков, где термин встретился (пуст, если нет pid-уровневого
   входа). `target`/`type` в этой записи НЕТ.
2. **Aligned record / ledger-строка** — кандидат + поля консенсус-выравнивания
   (`matching_pid_count`, `variants`, `target`, `consensus_share`,
   `conflicts`); в append-only ledger `glossary_candidates.json`
   (`GlossaryCandidateLedger`) каждая строка — per-chapter наблюдение, слияние
   по `candidate_key(source, kind)` даёт кумулятивную запись
   `{source, kind, total_occurrences, chapters: [{chapter_id, chunk_ids,
   count}], variants, target, targets_seen, conflicts, first_context}`.
3. **Observation payload** — то, что `_auto_promote_glossary` передаёт в
   `MemoryManager.add_observation("glossary", source, {target, type,
   chunk_id})`: `source` — КЛЮЧ наблюдения (glossary-key), `type` == `kind`
   кандидата, `chunk_id` — ПЕРВЫЙ отсортированный чанк главы (B7-совместимый
   payload для quarantined-фильтра `promote`). Это НЕ формат кандидата и НЕ
   формат ledger-строки.

### 2. Вызов add_observation в проде

- Точка вызова: `pact_full_pipeline_runner_v1/v4_book_run.py` — после каждой
  главы, **до** `MemoryManager.promote(status, quarantined_chunks)`.
- Категории наблюдений:
  - `glossary` — кандидаты терминов из финального текста главы;
  - `book_memory` — опционально: наблюдения о персонажах/фактах (решение
    владельца: ограничить scope B9 glossary-only или включить book_memory).
- `promote` (B7) уже переносит наблюдения с conflict resolution
  (established/locked не перезаписываются) и фильтрацией quarantined.

### 3. Политика наблюдений — V-финал (решение владельца 2026-08-04)

- **Без модельных вызовов**: генератор кандидатов и консенсус-выравнивание —
  детерминированные, модель не участвует (старый Вариант B с модельным target
  отменён).
- **Кандидаты**: частотные имена/термины из source-текста главы, отсутствующие в
  `glossary.json` / `book_memory.json`.
- **target**: извлекается консенсус-выравниванием по готовому переводу
  (pid→pid, доля варианта >= 0.8).
- **Авто-промоут** по v3-порогам: proper >= 2, term >= 2 глав и >= 3 вхождений,
  единственный консистентный target в кумулятивном ledger-рекорде.
- **Несогласованность** → ledger `conflicts` (не промоутится).
- Ledger: `glossary_candidates.json` в out-base.
- Промоут — через `observations` + `promote` (B7).

### 3.1 Строгие свидетельства (решение владельца: Вариант B + строгие свидетельства)

Промоут кандидата требует строгих свидетельств (B9-F2/F3/F5/F6, review
RV2/RV4/RV5):

- **co-occurrence guard**: target, разделяемый >1 term-кандидатом в пределах
  главы, отбрасывается — оба кандидата лишаются target и уходят в `conflicts`
  (частотный контраст не отличает «перевод кандидата» от «слова, лишь
  совстречающегося с кандидатом в тех же pids», B9-F2); ложные пары не
  промоутятся.
- **Кумулятивный ledger-рекорд**: промоут ТОЛЬКО при единственном консистентном
  target в кумулятивном ledger-рекорде без cross-chapter конфликта (RV2 HIGH,
  B9-F3) — запись, чьи главы разрешили source в разные targets, навсегда имеет
  target None (слияние необратимо), считается `conflict` и никогда не
  промоутится, даже если текущая глава однозначна.
- **Quarantined fail-closed**: при `accepted_degraded` с quarantined-чанками
  генерация/промоут fail-closed, если `chunk_plan.json` не может авторитетно
  исключить ВСЁ quarantined-свидетельство (RV4/RV5 HIGH, B9-F5/F6):
  missing/corrupt/empty/incomplete план (source/translation PID без маппинга)
  или неоднозначный (duplicate PID/chunk ownership, malformed данные) →
  ноль кандидатов, ноль ledger-строк, ноль наблюдений, glossary не
  мутируется; warning в лог, run не падает. Quarantined-чанки исключаются ДО
  аккумуляции ledger и авто-промоута на уровне pids (B9-RV3): кандидат
  целиком из quarantined-чанка не имеет ledger-строки и не промоутится,
  смешанный кандидат учитывает только accepted-chunk occurrences.

### 3.2 Артефакты — семантика `book_run.json` candidates

Per-chapter блок `candidates` `{generated, proposed, committed, conflicts}`
(определения совпадают с docstring `v4_book_run.py`):

- `generated` — число выровненных кандидатов главы (генерация + консенсус-
  выравнивание, исключения применены); 0 для глав без принятого терминального
  статуса (complete/accepted_degraded).
- `proposed` — сколько кандидатов главы отправлено в `MemoryManager.
  add_observation` (v3-пороги + единственный консистентный target + нет
  established-конфликта) — это add_observation ДО B7-quarantined-фильтра.
- `committed` — сколько из `proposed` реально попало в `glossary.json` после
  `promote` (diff glossary до/после). В финальном коде (B9-F5/F6) quarantined-
  свидетельства исключаются ДО генерации на уровне pids (RV3), поэтому
  B9-сгенерированные observations несут только accepted `chunk_id` →
  B7-фильтр их не отбрасывает и committed == proposed для complete И
  accepted_degraded (валидный план); B7-фильтр остаётся defense-in-depth
  (например, ручные наблюдения с quarantined `chunk_id` могут дать
  committed < proposed).
- `conflicts` — выровненные записи, НЕ отправленные в add_observation:
  alignment-конфликт (несколько заметных вариантов, нет единственного target),
  кумулятивный ledger target-конфликт (cross-chapter расхождение) или
  established-конфликт (запись glossary с другим target) — не промоутятся.

### 4. Identity / кеши

- `observations.json` и ledger `glossary_candidates.json` — вне снапшота и
  identity (проверено B9-I1) → B9 сам по себе не инвалидирует cache.
- B9-промоут добавляет только категорию `glossary` через
  `MemoryManager.add_observation` -> `promote` (B7): после glossary-only
  промоута меняется ТОЛЬКО `glossary.json` (flat `{source: target}`),
  `book_memory.json` НЕ трогается → `book_memory_hash` (хеш только
  `book_memory.json`, `_book_memory_hash()` в `v4_book_run.py`) остаётся
  неизменным. Следующая глава читает обновлённый `glossary.json` (глава
  исключает его ключи из кандидатов), а `book_memory_hash`/identity не
  меняются. Утверждения о cache/resume ссылаются на фактически проверенный
  identity field (snapshot/config identity), НЕ на `book_memory_hash`.

### 5. Тесты

- Unit: генератор кандидатов (частотность, исключения allowlist/библии,
  формат записи `{source, kind, occurrences, chunk_ids, context}`);
  add_observation-вызов в book_run (fake текст главы); promote переносит
  кандидатов, quarantined-фильтрация работает по observation `chunk_id`
  (B7) и по pre-ledger pid-исключению (RV3/F5/F6).
- Integration: book-run двух глав — вторая видит обновлённый glossary после
  promote первой (авто-промоут V-финал); accepted_degraded с quarantined-
  чанками и битым/неоднозначным `chunk_plan.json` — fail-closed (ноль
  кандидатов/ledger/observations/glossary-мутаций).
- Полный `tests/pact_v4/` зелёный.

### 6. Обязательный шаг перед первым боевым book-run — офлайн-валидация авто-промоута

Перед включением авто-промоута в production book-run (решение владельца
2026-08-04): офлайн-валидация на артефактах главы 0001 (`run_002_remote`) —
прогнать генератор + промоут, выложить, что промоутнулось (proposed/committed/
conflicts), и только после этого включать в production book-run.

## Вне scope (другие карточки)

- B8 (валидационный прогон главы 0001) — не блокируется B9; кандидаты по главе
  0001 собираются офлайн из артефактов прогона.
- Перенос v3-глоссария/библии — сделано (2026-08-04, входы созданы).
- 6 provisional-записей v3 — не включаем (решение владельца).
- Phase 1/2, cascade, risk — нельзя менять.
- Поток D (D1/D2/D3) — параллельны, не зависят от B9.

## Gate / Acceptance

1. Генератор кандидатов детерминированный, без вызовов моделей.
2. `add_observation` вызывается в `v4_book_run` после каждой главы до promote.
3. Candidate несёт `chunk_ids`; observation payload `{target, type, chunk_id}`
   (ключ — `source`) фильтрует quarantined (B7) + pre-ledger pid-исключение
   (RV3/F5/F6); битый/неоднозначный `chunk_plan.json` при quarantined —
   fail-closed (ноль кандидатов/ledger/observations/glossary-мутаций).
4. Identity/кеши: B9 сам по себе не инвалидирует cache; glossary-only промоут
   оставляет `book_memory.json` и `book_memory_hash` неизменными, следующая
   глава читает обновлённый `glossary.json`.
5. DECISIONS.md — запись о политике наблюдений (V-финал) в том же коммите.
6. Перед первым боевым book-run — офлайн-валидация авто-промоута на артефактах
   главы 0001 (`run_002_remote`) с выложенным результатом (что промоутнулось).

## Роль-сплит

Обычная V4-фаза: «реализует, второй — adversarial review». Перед стартом
спросить, кто пишет код.

## Компактный промпт

```text
Реализуй v4 B9 (генератор кандидатов глоссария + add_observation в проде) из
docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md (Поток B+, B9).
Target: main. Draft PR. Детерминированный генератор кандидатов (частотный/regex,
без моделей) + вызов add_observation в v4_book_run после главы до promote.
Не меняй phase1/2, cascade, risk; v3 не трогай. Политика наблюдений — V-финал
(решение владельца 2026-08-04): без модельных вызовов; кандидаты — частотные
имена/термины source-текста главы, отсутствующие в glossary/book_memory; target —
консенсус-выравнивание по готовому переводу (pid→pid, >= 0.8); авто-промоут по
v3-порогам; несогласованность → ledger conflicts. Перед первым боевым book-run —
офлайн-валидация авто-промоута на артефактах главы 0001 (run_002_remote).
```
