# Инвентаризация фактического v4 book-пайплайна

**Статус:** исследование кода `main`; не является инструкцией запуска и не
заменяет владелецский запуск на RT.

**Область:** публичный workflow `python -m pact_full_pipeline_runner_v1.v4_run book ...`
в обычном (simple) режиме `--local` / `--remote`. Он отличается от прямого
`chapter`-вызова и от исторического chunked strict-path.

> ## Терминологическая оговорка
>
> `book` — это не отдельный «не-strict» движок. Это book-оркестратор поверх
> `v4_phase12_strict_run`. Однако диспетчер **всегда добавляет
> `--whole-chapter`**. Поэтому обычный book-запуск не использует chunked
> generation + A/B/cascade-selection path, несмотря на внутреннее имя
> strict runner. Он переводит всю главу одним вызовом со строгим контрактом
> `{PID: русский_текст}`, затем запускает B3 audit/repair. В этом документе
> «book path» означает именно этот whole-chapter путь.

---

## 1. Ваш дефолтный запуск (что реально происходит)

Вы запускаете ран только с главами и `--remote` / `--local`; всё остальное
берётся из профиля по умолчанию. Типичный вызов:

```bash
cd ~/projects/pact-ensemble-translator   # media
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 1-3 --remote
# либо
cd D:\pact\pact_translator_v4_1          # RT (PowerShell, python)
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 1-3 --local
```

В этом дефолтном сценарии применяется следующее (всё — из профиля, без явных
флагов):

- `--whole-chapter` **всегда** добавляется диспетчером (v4_run.py:1030-1031).
  Это центральный факт: генерация — один вызов на всю главу, а не per-chunk
  A/B/cascade.
- Профиль: remote → `configs/runtime_remote.example.yaml`; local →
  `configs/runtime_local.example.yaml`. `--translator` / `--reviewer` не
  заданы, поэтому модели берутся из `model_bindings` профиля.
- `reasoning` — из профиля (remote: 3 / high). `--reasoning` можно переопределить.
- Audit/repair (фаза B3) **включены по умолчанию** (`run_audit=True`,
  `russian_editor_enabled=True`). `--skip-audit` не задан.
- Entity context (source entity prepass) **включён по умолчанию**
  (`entity_context_enabled=True`).
- Media: в simple book mode диспетчер по умолчанию добавляет media book id /
  target / root, если они сконфигурированы для хоста; иначе синхронизация
  выключена. См. §6.4.

### Дефолтные привязки ролей к моделям (remote-профиль)

| Роль (model role) | Что делает | Дефолтная модель (remote) | Промпт (версия) |
|---|---|---|---|
| `generator` (`balanced_literary`) | Перевод целой главы одним вызовом | `opencode/muse-spark-1.2-contributor-free` | `pact-v4-prompt-balanced-literary/v5` (`pact_v4/phase2/prompts.py`) |
| `entity_extractor` | Source entity prepass до перевода | `openai/gpt-5.6-luna` | `pact-v4-entity-extractor-prompt/v3` (`pact_v4/audit/entity_extractor.py`) |
| `qwen_chapter_audit` | B3 chunked audit | `openai/gpt-5.6-luna` | `pact-v4-reviewer-qwen-audit/v4.3-lenses` (`pact_v4/runtime/prompts_runtime.py`, `QWEN_AUDIT_V4_1`) |
| `russian_editor` | B3 Russian-only editor (V4.2 R) | на audit backend (`qwen_audit` → `openai/gpt-5.6-luna`) | `pact-v4.2-russian-editor/v4` (`pact_v4/runtime/prompts_runtime.py`, `RUSSIAN_EDITOR_V4_2_R1`) |
| `selective_repair` (prompt role) / `generator` (model binding) | B3 selective repair | `opencode/muse-spark-1.2-contributor-free` — `repair_model_ref` выбирает `generator` первым (`("generator","default")`); в remote-профиле отдельного binding `selective_repair` нет, `repair` тоже указывает на ту же модель | `pact-v4-repair-as-verifier/v5` (`pact_v4/runtime/prompts_runtime.py`, `REPAIR_AS_VERIFIER_V1`) |
| `formatting` | Восстановление inline-тегов (book wrapper) | биндинг `generator` (по дизайну) | `pact_v4/phase5/formatting.py::formatting_messages` |
| `fidelity_reviewer` / `russian_selector` | (резерв chunked-path; в whole-chapter не используются) | `openai/gpt-5.6-luna` | — |

> Локальный профиль (`runtime_local.example.yaml`) биндит те же роли на
> локальные llama-server модели (qwen/gemma) через `model_paths`; логика
> этапов идентична. Явные `--translator` / `--reviewer` переопределяют только
> генераторную/ревьюверную модель, но **не** меняют тот факт, что book всегда
> whole-chapter.

---

## 2. Карта потока (whole-chapter book path)

```text
v4_run book --chapters N-M --remote|--local
  │
  ├─ A. CLI, host layout, profile, offline preflight
  ├─ B. source discovery и безопасное выделение book output
  ├─ C. [если включён media] fetch CURRENT state до MemoryManager
  └─ D. Последовательно для каждой главы
       │
       ├─ D1  source → frozen snapshot → PID map (структурная основа)
       ├─ D2  [entity context ВКЛ] source entity prepass — ВСЕГДА ДО перевода
       ├─ D3  Translation: одна модельная генерация целой главы
       ├─ D4  B3 Russian editor (V4.2 R)
       ├─ D5  B3 chunked audit
       ├─ D6  Hard filters → selective repair → re-audit
       ├─ D7  Step 6/7/8 status + артефакты
       ├─ D8  [book wrapper] inline formatting (italics)
       ├─ D9  [book wrapper] glossary / book-memory candidate handling
       ├─ D10 [book wrapper] transactional promotion shared state
       └─ D11 [если включён media] publish candidate revision
  │
  └─ E. book_run.json и общий exit verdict
```

Последовательность глав задаётся `--chapters`. Один `MemoryManager` используется
всей книгой; принятая глава делает изменённое состояние входом следующей.

---

## 3. До пайплайна: вход, layout, preflight

| Шаг | Что происходит | Защиты / отказ |
|---|---|---|
| Разбор режима | `_handle_book()` принимает только simple `--local` / `--remote [translator/reviewer]` либо advanced `--runtime-config` / `--profile`. Simple и advanced несовместимы. | `--chapters` обязателен; диапазон числовой, от 1, не более 500 глав (`parse_range`, v4_run.py:265). |
| Host layout | RT: source `D:/pact/pact_chapters`, state `D:/pact/book_state`, output `D:/pact/gate_bench_runs`; media: `/home/rt/pact_chapters`, `/home/rt/pact_runs/workers/media/book-1/state`, `/home/rt/pact_runs/outputs`. | Source/state/output не могут совпадать/вкладываться. |
| Поиск исходников | Для каждого номера ровно один `NNNN_*.html`; его stem → `chapter_id`. | Отклоняются zero/multiple matches, symlink, special/non-regular/unreadable file и symlink в цепочке предков. |
| Runtime profile | Загружает example-yaml профиля; резолвит backend, model bindings, reasoning, identity-bearing опции. | Некорректный profile/alias/topology не доходят до выполнения. |
| Offline preflight | `run_runtime_preflight()` — сеть/модели/SSH/state-sync не трогает, output не создаёт. `--preflight` только печатает sanitised report. | FAIL → exit 3; ничего не создаётся. |
| Output allocation | `book_<range>_<local|remote>_<timestamp>` (или `--out-base`). Диспетчер добавляет `--whole-chapter`; остальные strict-опции прокидываются дальше. | Перед созданием повторяется layout validation. |

**Код:** `pact_full_pipeline_runner_v1/v4_run.py` (`_handle_book`,
`_discover_chapter_sources`, `_validate_layout`, `_host_layout`).

---

## 4. Shared state и MemoryManager (до и между главами)

1. `run_book()` при заданном media book id вызывает `pre_init_fetch()` **до**
   создания `MemoryManager` (fetch authoritative media revision в рабочий
   `memory_dir`).
2. Инициализируются/проверяются ровно четыре canonical, regular, non-symlink
   JSON-файла: `glossary.json`, `book_memory.json`, `chapter_index.json`,
   `observations.json`. Недоступность media = stale fallback отсутствует:
   запуск прекращается до перевода.
3. Без media и при полном отсутствии всех четырёх файлов — они явно
   инициализируются. Частично чужой набор не считается «новым state» и fail
   closed на границе promotion.
4. `MemoryManager` создаётся один раз; открываются общекнижные ledgers
   `glossary_candidates.json` и `book_memory_candidates.json` в `out-base`.

| Файл | Назначение |
|---|---|
| `glossary.json` | Authoritative glossary для следующей главы. |
| `book_memory.json` | Authoritative book facts, characters, voice/address. |
| `chapter_index.json` | Causal per-chapter context/bible selection. |
| `observations.json` | Наблюдения текущего цикла; очищается при успешном promotion. |
| `chapter_memory.json` | Локальный snapshot glossary/book-memory перед главой; не входит в exact-four authoritative set. |

`ChapterMemory.from_directory()` и `build_snapshot()` связывают source hash,
содержимое памяти и index entry в `snapshot_hash` — изменённый source/state не
переиспользует journal/cache чужой идентичности.

**Код:** `pact_full_pipeline_runner_v1/v4_book_run.py::run_book`,
`pact_v4/phase1/memory.py::MemoryManager`,
`pact_v4/snapshot/run_hooks.py`.

---

## 5. Поэтапный разбор whole-chapter пути (главный раздел)

Ниже каждый этап описан так, чтобы человек понял логику работы: **зачем**,
**какая модельная роль**, **откуда промпт** (ссылка на файл, не сам текст),
**что собирается на вход** (включая «полная глава»), **какой ожидается выход**,
**как он проверяется**, **повторы/кэш**, **артефакты**.

### 5.1. Источник → snapshot → PID map (D1)

**Зачем.** Превратить HTML главы в детерминированную структуру, по которой
однозначно считается, что именно переводить.

**Роль/модель.** Без модели — чистый парсинг (`run_chapter_strict`).

**На входе.** Исходный `NNNN_*.html` главы.

**Что делается.**
1. Парсинг HTML; глава без блоков отвергается.
2. Строятся `SourceArtifact`, frozen `Snapshot`, `ConfigArtifact`,
   `ChunkPlanArtifact` (обычный chunk plan).
3. Сохраняется `chunk_plan.json`.

**В whole-chapter mode chunk plan — НЕ план generation work units.** Он
сохраняется как source-derived structural provenance. Поверх него строится
упорядоченный `WholeChapterPidMap` (каждый PID = один переводимый блок/абзац
в исходном порядке) и сохраняется `whole_chapter_pid_map.json`. Именно этот PID
map — контракт генерации: генератор обязан вернуть полное, упорядоченное,
строго валидное соответствие каждому PID — без пропусков, чужих PID, дублей и
неверного порядка.

**Проверка.** Source парсится; PID map конструируется детерминированно из
source. Резюме identity (`source_hash`, `snapshot_hash`, `chunk_plan_hash`)
записывается в record и используется для resume (чужая provenance → data-loss
failure, не silent regeneration).

**Артефакты.** `chunk_plan.json`, `whole_chapter_pid_map.json`.

---

### 5.2. Source entity prepass — ВСЕГДА ДО перевода (D2)

**Зачем.** Извлечь из оригинала проверенные факты о сущностях (персонажи,
места, предметы, gender, aliases) и отдать их в промпт генерации как
детерминированный, source-derived контекст. Это ключевой этап дефолтного пути:
в вашем сценарии (entity context ВКЛ, audit ВКЛ) он **всегда** выполняется
перед переводом.

**Роль/модель.** `entity_extractor` → `openai/gpt-5.6-luna` (по дефолтному
remote-профилю). Промпт: `pact_v4/audit/entity_extractor.py`
(`ENTITY_EXTRACTION_V1`, role `entity_extractor`,
version `pact-v4-entity-extractor-prompt/v3`; рендер через
`render_entity_extraction_prompt`). Вызов детерминированный: `temperature=0`,
JSON-object response schema, retry по `JsonRetryPolicy`.

**На входе.** Только **оригал** (source map целой главы) — никакого перевода
на этом этапе ещё нет. `BackendEntityExtractor.__call__` получает
`chapter_id` + `source` (full chapter text, ~16k токенов) и строит
source-only prompt. `max_tokens=20000` (достаточно для reasoning + entities JSON
большой главы).

**Что делается.**
- Модель возвращает JSON с сущностями: `memory_class` (named_character /
  named_place / named_group / named_artifact / named_creature / world_term /
  chapter_local), `memory_worthy`, aliases, evidence (anchor в source), gender.
- Каждый claim проходит **model gate** + **code gate** (`is_entity_glossary_candidate`):
  model-флаг `glossary_worthy`/`memory_worthy` и code-проверка (термин реально
  присутствует в source по word-boundary). Ложное вето модели не пропускает
  устаревший вывод.
- Результат классифицируется на **verified** (идут в промпт генерации) и
  **candidate** (гипотезы — идут ТОЛЬКО в audit, никогда не команды промпту).

**Ожидаемый выход.** `ChapterEntityContext` — проверенные и кандидат-факты с
evidence и валидацией.

**Проверка.** `entity_context_validation_report.json` фиксирует, что принято /
отклонено и почему. Cache identity включает `source_hash` + `extractor_version`;
чужой/tampered entry отвергается fail closed.

**Повторы/кэш.** Результат персистится в `entity_context_cache.json`. B3 audit
позже бьёт тот же cache (0 доп. model calls). Cache hit → resume без вызова.

**Артефакты.** `entity_context_cache.json`, `entity_context_validation_report.json`,
`b1.2_entity_reasoning.txt`.

**Гейт выполнения.** entity prepass запускается только при
`entity_context_enabled AND run_audit AND stop_after != "generation" AND
b3_audit_repair is not None`. В вашем дефолтном пути все четыре условия
выполняются → prepass идёт. Без audit/машины генерация идёт без entity-блока.

---

### 5.3. Translation — одна генерация целой главы (D3)

**Зачем.** Перевести всю главу за один модельный вызов, строго по PID map.

**Роль/модель.** `generator` (`balanced_literary`) →
`opencode/muse-spark-1.2-contributor-free`. Промпт: `pact_v4/phase2/prompts.py`
(`BALANCED_LITERARY_V4`, role `balanced_literary`,
version `pact-v4-prompt-balanced-literary/v5`; рендер через `render_prompt`).
(Роль `fidelity_first`/`pact-v4-prompt-fidelity-first/v3` в whole-chapter пути
не генерируется заранее — она reserve для lazy fallback; дефолтный whole-chapter
путь выдаёт ровно одного candidate `balanced_literary`.)

**На входе (собирается в `PromptBundle`).** Передаётся **полная глава**:
- `owned_source` = текст **всех** PID главы в исходном порядке (никакого
  left/right context — whole-chapter это одна «chunk» `chunk_id="whole_chapter"`).
- `glossary` — отфильтрован по тексту **всей** главы: только термины главы +
  always_include set (risk categories / conflicts / narrator) из `memory`.
- `bible_text` — book bible / chapter index (`render_bible_section` из memory),
  **плюс** блок `CHAPTER ENTITY FACTS - SOURCE-DERIVED`
  (`render_entity_context_block(..., verified_only=True)` из §5.2) и, если
  заданы, блок `АРКИ` из `deterministic_arc_names.json` (детерминированный
  перевод имён арок, напр. Bonds → Узы).
- `risk_band` + `required_risk_feature_codes` (из `_whole_chapter_risk` по
  source+glossary) — в промпт добавляются явные инструкции по нужным
  риск-категориям (tone/profanity и т.п.).
- `params`: `temperature`, `seed`, `max_tokens`, `reasoning` (из профиля/флага).

Полный `PromptBundle.bundle_hash` = identity кэша генерации (меняется при
изменении source/glossary/bible/entity/arc/risk/config).

**Ожидаемый выход.** Строгий JSON-объект: `{PID: "русский_текст"}` для
**каждого** PID в точном исходном порядке. Ровно одна запись `whole_chapter` в
`journal.ndjson`.

**Проверка.** `validate_whole_chapter_raw(raw, pid_map)` → `_parse_ordered_pid_pairs`
→ `_validate_pid_map`:
- Сначала строгий `json.loads`. При `JSONDecodeError` запускается tolerant
  `extract_pid_pairs`: он устойчив к дефектам длинного вывода (pid-colon,
  пропущенные запятые, обрезание, мусор) и **восстанавливает** полную чистую
  PID-map, если покрытие ≥ 90% (`min_coverage`) и все значения чистые. Только
  вывод с покрытием < 90% или подозрительным значением отвергается как
  `truncated`/`malformed` и попадает под bounded retry (`WholeChapterRetryPolicy`).
  То есть «обрезанный» вывод часто доходит до `_validate_pid_map` уже как
  восстановленная полная map, а не отбрасывается сразу.
- `_validate_pid_map`: нет missing/extra/reordered/duplicate PID; нет
  context-leakage (в whole-chapter не срабатывает — context PID отсутствуют);
  каждое значение — тип `str`, **но непустота и содержательная полнота текста
  не проверяются**: пустая строка пройдёт валидацию и будет принята как перевод
  (такой debt выявляется позже, на этапе B3 audit, а не на этапе генерации).
Сбой классифицируется как `malformed` / `missing_pid` / `truncated` / `abort`.
Adapter-level JSON retry **выключен** (чтобы попытки не умножались);
весь retry — в `WholeChapterRetryPolicy` (bounded, с backoff). После бюджета —
`status="incomplete"`, глава halted early; **частичная PID map никогда не
выдаётся как complete**.

**Повторы/кэш.** Cache hit по `bundle_hash` → мгновенный возврат (с
re-verification identity, защита от poisoned cache). Каждый attempt пишет
`whole_chapter_attempt<N>_raw.txt`; reasoning — в `whole_chapter_reasoning.txt`
/ `whole_chapter_retryN_reasoning.txt` (только если реально получен; это
diagnostics, не входит в identity).

**Артефакты.** `translations_raw.json` (immutable validated generator snapshot),
`generation_outcomes.json`, `journal.ndjson`, `glossary_budget_report.json`,
`whole_chapter_reasoning.txt`, `whole_chapter_attempt<N>_raw.txt`.

---

### 5.4. B3 Russian editor (V4.2 R) (D4)

**Зачем.** Russian-only лёгкая вычитка уже переведённой карты — безопасные
точечные правки (в основном inline-теги/форматирование и мелкие правки),
без переписывания смысла. Включён по умолчанию.

**Роль/модель.** `russian_editor` → на **audit backend** (`qwen_audit` →
`openai/gpt-5.6-luna`). Промпт: `pact_v4/runtime/prompts_runtime.py`
(`RUSSIAN_EDITOR_V4_2_R1`, role `russian_editor`,
version `pact-v4.2-russian-editor/v4`), используется в
`pact_v4/audit/russian_editor.py`.

**На входе.** Raw translation map (целая глава) из §5.3, разбитая на bounded
editor-chunks (`russian_editor_chunk_size` + `overlap_pairs`). Только safe
classes правок (`russian_editor_safe_classes`); ограничение
`max_edits_per_pid`.

**Ожидаемый выход.** Edited snapshot (RU) + `edit_candidates.json` (предложенные
правки) + `translations_edited.json`. Если stage реально сработал — это и есть
вход в audit/repair.

**Проверка.** Редактор Russian-only; не меняет видимый смысл вне safe classes.
Упавший editor-chunk = **debt** (журнал + outcome), как и упавший repair — не
fail-closed всей главы. Cache report editor (`r_editor`) повторно проигрывает
GOOD chunks без model calls.

**Повторы/кэш.** Per-chunk retry (`russian_editor_retry_max_retries`,
base_delay). Resume по `r_editor` report.

**Артефакты.** `translations_edited.json`, `edit_candidates.json`,
`audit_journal.ndjson` (события `r_editor_*`).

---

### 5.5. B3 chunked audit (D5)

**Зачем.** Найти расхождения EN↔RU и нарушения hard-фильтров по всей главе,
разбив её на управляемые audit units.

**Роль/модель.** `qwen_chapter_audit` → `openai/gpt-5.6-luna` (через
`audit_model_ref(self._audit_backend)`). Промпт: `pact_v4/runtime/prompts_runtime.py`
(`QWEN_AUDIT_V4_1`, role `qwen_chapter_audit`,
version `pact-v4-reviewer-qwen-audit/v4.3-lenses`), используется в
`pact_v4/audit/chunked_audit.py`.

**На входе.** `pairs_from_maps(source_map, translation_map)` — пары EN/RU по
всем PID. Разбиваются на bounded audit units (`audit_max_input_tokens`,
`overlap_tokens`). В пэйлоад попадают контекст пары + (опционально) entity
context как payload для hard-фильтров.

**Ожидаемый выход.** Классифицированные audit findings на каждую пару/PID.

**Проверка.**
- Audit journal (`audit_journal.ndjson`) пишет start/done/retry по каждой unit;
  поддерживается partial cache resume.
- Любая **незавершённая** audit unit делает `audit_complete=False`.
- Транспортные ошибки retry-shrink; исчерпание бюджета = неполный audit.

**Повторы/кэш.** Bounded transport retry (`audit_transport_max_retries`,
base_delay). Cache `audit_cache_b3.json` (identity = snapshot + raw hash +
config/backend + prompt/harness + entity context).

**Артефакты.** `audit_journal.ndjson`, `audit_cache_b3.json`.

---

### 5.6. Hard filters → selective repair → re-audit (D6)

**Зачем.** Отсечь ложные находки (hard filters), затем починить только то, что
действительно неверно, и перепроверить изменённое.

**Hard filters (B1.1).** `apply_hard_filters` (`pact_v4/audit/hard_filters.py`)
классифицирует findings в `CONFIRMED` / `REJECTED` / `TIER_B`. Entity-PID issues
принудительно идут в более строгую ветку; entity context передаётся как payload
(`render_entity_context_to_hard_filters`).

**Fail-closed gate.** При `audit_complete=False` repair **не запускается**, глава
не считается released as audited → `step8.status = fail_closed_audit_incomplete`.

**Selective repair.** Prompt role `selective_repair`; модель резолвится через
`repair_model_ref` → `generator` первым (`("generator","default")`), то есть
`opencode/muse-spark-1.2-contributor-free` (в remote-профиле отдельного binding
`selective_repair` нет). Промпт: `pact_v4/runtime/prompts_runtime.py`
(`REPAIR_AS_VERIFIER_V1`, role `selective_repair`,
version `pact-v4-repair-as-verifier/v5`), используется в
`pact_v4/repair/selective_repair.py`.
- Чинит **только** eligible regions/PIDs (CONFIRMED findings), возможно
  микробатчами (`audit_repair_findings_cap`, `microbatch_trigger/target`).
- После изменений **re-audit** охватывает изменённые PID + configured neighbour
  context (`repair_reaudit_neighbour_window`, delta-format `REPAIRED CHANGES`).
- Good batches / reaudit units пригодны для identity-validated partial resume.

**Ожидаемый выход.** Repaired PID map (те же PID, исправленные значения) +
отчёт о долге.

**Артефакты.** `translations_repaired.json`, `translation_diffs.json`,
обновлённые `audit_cache_b3.json` / `audit_journal.ndjson`.

---

### 5.7. Step 6/7/8 status и артефакты главы (D7)

`step6` описывает audit, `step7` repair/debt. `step8` (terminal) — целиком копируется из B3 (`_run_whole_chapter_strict_impl`
копирует `b3_audit_result.step8` дословно):
- `status="complete"` + `released_as_audited=true` — audit завершён **и** repair
  завершён (каждый batch GOOD и post-repair re-audit прошёл);
- `status="accepted_degraded"` + `released_as_audited=false` — audit завершён, но
  repair не завершён/упал (`repair_complete=False`, reason `repair_failed` /
  `repair_incomplete`). **Whole-chapter runner выставляет этот статус** (равно как
  и cache-replay той же ситуации); он не является исключительно chunked-path;
- `fail_closed_audit_incomplete` / `failed` — неполный/упавший B3 audit
  либо падение B3 machinery. Неполная генерация фиксируется иначе:
  `step8.status="skipped"` (reason `whole_chapter_generation_only`), поскольку
  при пустом generation result B3 не запускается.

Главный per-chapter record — `strict_chapter_trial_record.json`. Book wrapper
берёт terminal status оттуда (`record["step8"]["status"]`).

| Artifact | Роль |
|---|---|
| `strict_chapter_trial_record.json` | Главный record: identities, backend/runtime, policy, counters, step6-8, пути артефактов. **Источник terminal status для book wrapper.** |
| `translations_raw.json` | Validated output генерации до R-editor/audit/repair. Resume source. |
| `translations_repaired.json` | Repaired PID map с identity. |
| `translations.json` | Final alias (repaired map; позже book wrapper может восстановить inline formatting). Во время live execution не финален. |
| `journal.ndjson` | Generation journal; whole-chapter — одна запись. |
| `generation_outcomes.json`, `selection_results.json` | Provenance; `selection_results.json` имеет `mode: not_applicable` (cascade не выполнялась). |
| `audit_journal.ndjson`, `audit_cache_b3.json` | B3 audit events/cache. |
| `entity_context_cache.json`, `entity_context_validation_report.json` | Validated source-entity context. |
| `translations_edited.json`, `edit_candidates.json` | R-editor evidence (если работал). |
| `phase_progress.ndjson`, `usage.ndjson` | Append-only observability; не authority для resume/terminal. |

**Код:** `pact_v4/pipeline/v4_phase12_strict_runner.py`
(`run_chapter_strict`, `_run_whole_chapter_strict_impl`),
`pact_full_pipeline_runner_v1/v4_phase12_strict_run.py`,
`pact_v4/pipeline/b3_audit_repair.py`, `pact_v4/audit/russian_editor.py`,
`pact_v4/audit/chunked_audit.py`, `pact_v4/repair/selective_repair.py`,
`pact_v4/audit/entity_extractor.py`, `pact_v4/phase2/generation.py`,
`pact_v4/phase2/prompts.py`.

---

## 6. Book wrapper: post-strict шаги (после возврата strict CLI)

Book wrapper читает `step8.status`. Обёртка допускает `complete` и
`accepted_degraded` (`_PROMOTING_STATUSES`). Для whole-chapter path строгий
runner выставляет `step8.status` = `complete` (всегда с `released_as_audited=true`
— полный repair), `accepted_degraded` (audit done, repair incomplete/failed,
`released_as_audited=false`), `failed`, `fail_closed_audit_incomplete` либо
`skipped` (generation-only). Оба допустимых статуса (`complete` и
`accepted_degraded`) promote-ятся book wrapper-ом.

### 6.1. Inline formatting / italics (D8)

**Зачем.** Если в оригинале были inline-теги (`<em>`, `<strong>`, `<i>`, `<b>`,
`<a>`), восстановить их вокруг соответствующего русского текста.

**Роль/модель.** Book wrapper создаёт formatting backend/client (биндинг
`generator`). Model call: `pact_v4/phase5/formatting.py::resolve_format_mappings`
через `formatting_messages` (system-инструкция: «ты специалист по форматированию,
не меняй текст»). **Само обёртывание тегов — model-free** (`apply_span_mappings`
детерминированно находит текст и оборачивает исходными тегами).

**На входе.** Source HTML + `translations.json` (только блоки с `inline_spans`).
PID-ы без spans не вызывают модель. Single-call, если spans ≤ лимита и prompt
≤ 12000 токенов, иначе батчи.

**Ожидаемый выход.** `{(pid, span_id): (target_text, occurrence)}` — русские
target-теги. `run_formatting_align` перезаписывает `translations.json`
атомically и пишет `formatting_report.json`.

**Проверка.** Formatting **не переписывает видимый RU text**. Неразрешённый span
→ incident/debt. В strict path default incident limit блокирующий (0); book
wrapper использует отдельную lenient policy (`_DEFAULT_MAX_FORMATTING_INCIDENTS=999`).
Exception в post-strict formatting логируется и **не отменяет** уже прочитанный
terminal status.

**Артефакты.** `formatting_report.json`, `formatting_batch<N>_*.txt/json`,
обновлённый `translations.json`.

---

### 6.2. Glossary / book-memory candidate handling (D9)

**Зачем.** Из проверенного entity context + финального перевода сформировать
предложения в glossary/book-memory (они ещё не authoritative).

**Логика (без отдельного model call на этом шаге — опирается на §5.2).**
- glossary resolver mode: `off` / `shadow` / `promote`; stale/invalid sidecar =
  fail-closed для proposal.
- evidence PID из quarantined chunks исключается.
- book-memory policy: `promote_verified` / `observe` / `off`.
- candidates/reports получают source/snapshot/config policy provenance.

**Артефакты.** `glossary_candidates.json`, `book_memory_candidates.json`
(в `out-base`); proposals не попадают в authoritative state до promotion (§6.3).

---

### 6.3. Transactional promotion shared state (D10)

**Зачем.** Атомарно применить перевод и кандидатов к общекнижному state, чтобы
следующая глава увидела обновлённый контекст.

**Условие.** `terminal in _PROMOTING_STATUSES` и policy разрешает promotion →
`MemoryManager.promote()`.

**Что делается.**
1. `complete` → assert отсутствие quarantined chunks.
2. `accepted_degraded` → observations с quarantined `chunk_id` удаляются
   (в whole-chapter path quarantined chunks отсутствуют, поэтому удаление
   не затрагивает ни одной записи, но ветка применяется и здесь).
3. Existing established/locked records не перетираются автоматически.
4. Staged transaction заменяет **одновременно** все четыре canonical files
   (glossary, book memory, chapter index, observations).
5. До staging и перед move — exact file allowlist, отсутствие symlink/special,
   JSON validity.
6. Backups + `.pact_transaction_marker.json`, fsync/atomic replace, проверка
   raw hashes в конце.

**Проверка.** При crash следующий `MemoryManager` восстанавливает backups по
marker; corrupt/missing recovery material fail closed. `rollback()` —
**не** полный rollback четырёх файлов (только glossary/book_memory из
`chapter_memory.json` + очистка observations); для долговечной отмены — отдельная
owner-approved media rollback процедура.

---

### 6.4. Media publish (D11, если включён media)

**Зачем.** Сделать состояние главы authoritative между хостами (immutable media
store: `<root>/books/<book-id>/CURRENT.json` → `snapshots/<rev>/{manifest,state/}`).

**Логика.** После local promotion `post_promote_push()`:
1. проверяет локальный four-file boundary;
2. загружает candidate с manifest/hashes;
3. media проверяет allowlist, hashes, **заявленный в candidate manifest**
   terminal status, parent revision, lease; текущий `push_candidate()` пишет
   в manifest фиксированное `"terminal_status": "complete"` (remote_client.py),
   поэтому это **не независимая проверка** фактического `step8` /
   `released_as_audited`;
4. media atomically назначает revision, меняет `CURRENT.json` либо quarantines
   rejected candidate;
5. при `STALE_PARENT` локальная сторона сохраняет canonical bytes, fetches
   новый parent, восстанавливает bytes и повторяет публикацию **один раз**.

Transport/lease/hash rejection не стирают local state; фиксируются в per-chapter
record; финальный book CLI печатает `MEDIA PUBLISH: REJECTED` и возвращает
non-zero. «Главы завершились» ≠ «cross-host state опубликован».

---

## 7. Book result, resume и exit semantics

После цикла пишется `<out-base>/book_run.json`: terminal status, memory hashes
before/after, promotion flag/detail/error, output dir, glossary/book-memory
counts, media confirmation/error.

- Book CLI возвращает success только если каждая глава `complete` либо
  `accepted_degraded` (оба по `step8.status`, независимо от path) и, если media
  включён, все promoted
  chapters получили ACCEPTED confirmation.
- strict CLI возвращает non-zero для halted generation / B3 `failed` /
  `fail_closed_audit_incomplete` / not-released. Но `_run_one_chapter()`
  **игнорирует обычный возвращённый integer** strict CLI и продолжает по
  `step8.status`; только выброшенные `SystemExit`/exception → chapter `exit`/
  `error`. Поэтому в whole-chapter path даже degraded (not released as audited)
  глава имеет `step8.status="accepted_degraded"` и promote-ится как
  `accepted_degraded` (входит в `_PROMOTING_STATUSES`), хотя strict CLI вернул бы
  non-zero.
- `--promote-existing` не запускает модели: reuse готового chapter out-dir,
  только acceptance/promotion.
- Per-chapter generation/audit resume durable через journals/caches + identity
  checks. Но `book_run.json` пишется после полного цикла, не инкрементально:
  аварийный обрыв посередине может оставить продвинутые artifacts + shared state
  без aggregate book record. Отсутствие `book_run.json` ≠ отсутствие выполненных
  глав.

---

## 8. Что намеренно НЕ происходит в book whole-chapter path

| Не выполняется как основной этап | Почему это важно |
|---|---|
| Per-chunk generation | Генерация — один whole-chapter model call. |
| A/B candidates и Qwen → deterministic → Gemma cascade | `selection_results.json` явно `not_applicable`; не скрытый selection. |
| Chunk-level RU left-context handoff для generation | Для generation нет последовательного chunk loop; context из frozen book state/source. |
| Старый Phase 3/4 chunked strict audit/repair path | Его заменяет conditional B3 whole-chapter orchestrator. |
| Автосборка переведённых HTML в книгу | `v4_book_run` не собирает `book.html`; это отдельный `v4_book_html` шаг. |

---

## 9. Операционные риски (что проверять оператору/монитору)

1. **Mode confusion:** successful book generation ≠ исполнение canonical chunk
   cascade; это другой branch.
2. **Strict CLI exit vs book decision:** book wrapper игнорирует integer strict
   CLI и опирается на `step8.status`. Проверять `released_as_audited`: оба
   допустимых статуса (`complete` с `released_as_audited=true` и `accepted_degraded`
   с `released_as_audited=false`) promote-ятся, хотя strict CLI вернул бы non-zero
   для not-released (`accepted_degraded`) результата.
3. **Terminal vs final formatting:** проверять `step8`, `released_as_audited`,
   `formatting_report.json` и actual `translations.json` вместе.
4. **Publication vs local completion:** non-zero `MEDIA PUBLISH: REJECTED`
   оставляет local state, но cross-host authority не продвинута.
5. **Mid-book interruption:** inspect chapter records, four-file state и media
   `CURRENT` перед повтором; aggregate `book_run.json` может отсутствовать.
6. **Advanced mode:** historical compatibility path не даёт simple-mode гарантий
   layout collision/source discovery в том же объёме; явно проверять пути.
7. **Observability is diagnostic:** `phase_progress.ndjson` / `usage.ndjson`
   помогают мониторингу, но terminal/resume authority — в strict records,
   identity-bound artifacts и canonical state.

---

## 10. Основные исходники и тестовые доказательства

| Область | Код | Характеризующие тесты |
|---|---|---|
| Dispatcher, preflight, path isolation, media defaults | `pact_full_pipeline_runner_v1/v4_run.py` | `tests/pact_v4/test_v4_run_dispatcher.py` |
| Book sequencing, promotion, formatting, book result | `pact_full_pipeline_runner_v1/v4_book_run.py` | `tests/pact_v4/test_b9_book_run_integration.py`, `tests/pact_v4/test_book_formatting_remote_lifecycle.py` |
| Whole-chapter generation + provenance + resume | `pact_v4/phase2/generation.py`, `pact_v4/phase2/prompts.py`, `pact_v4/pipeline/v4_phase12_strict_runner.py`, `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py` | `tests/pact_v4/pipeline/test_v4_phase12_strict_runner_whole_chapter.py`, `test_v4_phase12_strict_runner_{characterization,repair,formatting,retry,translations_final,remote}.py` |
| Entity prepass (до перевода) | `pact_v4/audit/entity_extractor.py` | `tests/pact_v4/audit/` |
| B3 audit/editor/repair | `pact_v4/pipeline/b3_audit_repair.py`, `pact_v4/audit/russian_editor.py`, `pact_v4/audit/chunked_audit.py`, `pact_v4/repair/selective_repair.py`, `pact_v4/audit/hard_filters.py` | `tests/pact_v4/audit/`, `tests/pact_v4/repair/` |
| Inline formatting | `pact_v4/phase5/formatting.py` | `tests/pact_v4/phase5/` |
| Memory transaction и media CAS | `pact_v4/phase1/memory.py`, `pact_v4/snapshot/` | `tests/pact_v4/test_transaction_fault_matrix.py`, `tests/pact_v4/snapshot/` |

---

## 11. Связанные документы

- `README.md` — supported command surface и host/media contracts.
- `docs/architecture/V4_MVP_SPEC_RU.md` — целевая MVP архитектура.
- `docs/agent_operations/AGENTS_REFERENCE_RU.md` — owner-only launch/monitor commands.
- `configs/runtime_remote.example.yaml`, `configs/runtime_local.example.yaml` —
  дефолтные профили (model bindings, reasoning, policy).

Если целевая MVP spec и этот inventory расходятся, для текущего production
поведения источником истины является код и артефакты конкретного run. Разницу
следует оформлять как отдельное решение/изменение, а не незаметно устранять в
документации.
