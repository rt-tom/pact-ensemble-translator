**Goals**
- Повтор главы в тот же `--out-dir` реально переводит заново чанки, упавшие как `incomplete_generation`.
- Повтор книги в тот же `--out-base` не перегоняет уже готовые главы.
- Identity-гарантии не ослабить: чужой конфиг → перегон, а не тихое переиспользование.

## 1. Факты кодовой базы (2026-09-09)

- Journal: append-only, одна строка на чанк, исходы `selected | quarantined | needs_synthesis | incomplete_generation` (`v4_phase12_strict_runner.py: JournalEntry.outcome`).
- Resume главы: `resumed_from_index = len(prior_entries)`; `if index < resumed_from_index: continue` — пропуск позиционный. Записанный `incomplete_generation` никогда не ретраится.
- Identity journal: `chunk_plan_hash / config_identity / backend_identity_hash` остаются fail-closed для reuse. Сохранённый snapshot старой завершённой stage может отличаться от текущей общей memory: он описывает вход уже полученного результата, а не запрещает retry следующего шага; новый stage записывает текущий snapshot.
- Книга: `run_book()` идёт по всем `chapter_ids`, `_run_one_chapter()` безусловно вызывает `strict_main()`. Проверки готовых глав нет. `_PROMOTING_STATUSES = ("complete", "accepted_degraded")`.
- `book_run.json` перезаписывается каждый прогон целиком — при resume перестраивается из глав и содержит цепочку `book_memory_hash_before/after`. Отдельный pre-chapter memory snapshot не требуется: retry использует актуальную общую memory, а уже завершённые downstream-главы не откатываются.
- Леджеры `GlossaryCandidateLedger.append_chapter` / `BookMemoryCandidateLedger.append_chapter` уже идемпотентны (update in place по `chapter_id+key`) — повторный прогон главы не дублирует строки.

## 2. Stage-aware chapter resume

При `chapter --resume` после identity-валидации система читает durable stage checkpoints и выбирает первый незавершённый или failed stage. Успешные стадии и их валидные артефакты переиспользуются; зависимые последующие стадии выполняются заново. Минимальные стадии: `generation/selection`, `audit`, `repair`, `formatting/finalization`. Promotion книги является отдельной стадией book-run и не выполняется chapter CLI.

- Если failed stage — `generation/selection` с `incomplete_generation`, journal перематывается до первой такой записи; перегенерируется она и зависимый хвост. `selected` до точки реконструируется как сейчас, `quarantined` не ретраится как отдельная операция.
- Если failed stage — `audit`, валидные generation/selection artifacts переиспользуются; запускается audit, затем зависимые repair/finalization.
- Если failed stage — `repair`, валидные generation/selection/audit artifacts переиспользуются; запускается repair, затем finalization.
- Если failed stage — `formatting/finalization`, translation/repair artifacts переиспользуются; повторяется только formatting/finalization.
- Journal остаётся append-only, но retry не может использовать старую позиционную формулу `len(prior_entries)`: каждая новая попытка получает monotonic attempt/revision marker, а replay выбирает последнюю валидную запись для каждого chunk/stage.
- Если артефакт предыдущего шага отсутствует, повреждён или не проходит integrity check, resume откатывается к ближайшему зависимому шагу, а не использует непроверенный файл.
- Для retry главы книга использует её существующие stage artifacts и актуальную общую memory; memory не откатывается и не восстанавливается из pre-chapter snapshot.
- После успешного retry promotion добавляет новые glossary/book-memory observations главы в текущее состояние. Уже промоутированные downstream-главы не удаляются и не пересчитываются: их observations считаются валидным additive state.
- Для legacy out-dir checkpoint можно вывести из существующих status/artifact-файлов; отсутствие нового manifest не является причиной отказа, если нужные артефакты проходят проверки.
- Без `--resume` и без `--retry-incomplete` поведение существующего позиционного journal resume сохраняется.

Ключевой принцип: ошибка на одном шаге не является причиной повторять уже успешно завершённые шаги. Если артефакт предыдущего шага отсутствует, повреждён или не проходит integrity check, resume откатывается к ближайшему зависимому шагу, а не использует непроверенный файл.

Stage manifest SHALL bind each completed stage to its input hashes and exact artifact set:

| Stage | Reusable artifacts | Invalidates on retry |
|---|---|---|
| `generation/selection` | journal, generation outcomes, selection results/meta, selected translations | audit, repair, formatting/finalization |
| `audit` | audit cache, findings, B2 handoff | repair, formatting/finalization |
| `repair` | repair cache/report, repaired translation | formatting/finalization |
| `formatting/finalization` | formatting report and final translations | book promotion only |

Each stage checkpoint is written atomically only after its artifact set passes integrity validation. A failed stage never replaces the last completed checkpoint.

## 3. Book resume (`--resume`, `--force-rerun-chapter`)

В `run_book()` перед `_run_one_chapter()` для каждой главы, если задан `--resume`:

1. Прочитать durable stage checkpoints (или вывести их из существующих status/artifact-файлов для legacy out-dir) и `out_base/chapter_<id>/strict_chapter_trial_record.json`. Нет checkpoint/артефакта или он непарсящийся — выбрать первый безопасный незавершённый stage по имеющимся валидным артефактам.
2. Готовую главу (`complete`, `accepted_degraded`) можно полностью пропустить и выполнить только promotion, если её source/config/backend contract и stage artifacts валидны. Поздняя общая memory не делает такую главу stale.
3. Для retry главы использовать её существующие stage artifacts и актуальную общую memory; memory не откатывать.
4. После retry применять только новые observations этой главы к текущей общей memory. Уже промоутированные downstream-главы не удалять и не пересчитывать.
5. Глава с repair failure переиспользует generation/audit и перезапускает repair; глава с book-promotion failure повторяет только promotion без model calls.
6. Несовпадение backend/config identity — hard failure и не должно автоматически смешиваться с текущим out-dir; для осознанного полного rerun используется новый out-dir.
7. `--force-rerun-chapter <id>` (повторяемый) исключает главу из любого skip/reuse и создаёт новую revision/attempt внутри того же book out-dir.
8. Book SHALL NOT безусловно добавлять `--retry-incomplete` к каждой главе: automatic `--resume` выбирает первый незавершённый stage; journal rewind применяется только при явном retry-incomplete или когда stage checkpoint указывает на incomplete generation.

Без `--resume` — поведение побитово старое (все главы гонятся).

## 4. Operational examples

Initial book run:

```text
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 1-4 --out-base <book-dir> --local
```

Resume the same book after a failure:

```text
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 1-4 --out-base <book-dir> --resume --local
```

`<book-dir>` SHALL be the existing output directory from the first run; `--resume` without it SHALL fail instead of allocating a new timestamp directory. The profile/model flags SHALL remain the same. With a repair failure in chapter 2, resume reuses chapter 2 generation/audit artifacts, reruns repair/finalization, and leaves already completed downstream chapters and their additive memory observations untouched.

Direct chapter retry:

```text
python -m pact_full_pipeline_runner_v1.v4_run chapter --chapter-id 0002 --chapter-html <chapter.html> --memory-dir <memory-view> --out-dir <book-dir>/chapter_0002 --resume
```

No `...` or additional unspecified flags are implied: all flags needed to resolve the same source, memory, output and model profile must be supplied explicitly.

## 5. Что НЕ меняется

- Вокабуляр исходов journal и логическое состояние `book_run.json`; journal получает только совместимый attempt/revision marker для append-only retry, а stage manifest добавляется отдельным артефактом.
- Fail-closed identity-проверки (только добавляются точки их применения).
- Леджеры (уже идемпотентны).
- `quarantined` не ретраится нигде (осознанное решение селектора/аудита, а не техническая ошибка генерации).
- Автовыделение timestamp-каталогов: стабильный путь уже есть (`--out-base`), новый флаг имени не вводим.

## Migration / Risks

Аддитивные opt-in флаги и артефакты; дефолтные пути не меняются — старые раны воспроизводятся. Старый journal без revision marker читается как attempt 0, а новые записи получают marker; после включения retry логическое replay-состояние выбирает последнюю валидную попытку. Риск: точка перемотки меняет левый контекст хвоста главы → хвост перегенерируется (модельные вызовы), это ожидаемо и покрывается тестами. Promotion-блок книги для пропущенных глав выполняется заново на тех же артефактах — идемпотентность леджеров это покрывает.
