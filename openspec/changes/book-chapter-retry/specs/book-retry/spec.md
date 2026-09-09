## Purpose

Продолжение прерванных ранов: повтор главы должен возобновлять именно первый незавершённый шаг, а не повторять уже успешно завершённые стадии. Повтор книги должен переиспользовать готовые главы только при актуальных входах и общей memory-state. Identity-гарантии сохраняются: чужой конфиг никогда не переиспользуется молча.

## Definitions

- **Stage:** `generation/selection`, `audit`, `repair`, `formatting/finalization` главы; promotion книги является отдельной стадией.
- **Incomplete chunk:** journal-запись чанка с `outcome == "incomplete_generation"`.
- **Ready chapter:** папка `chapter_<id>` содержит валидные stage artifacts и парсящийся `strict_chapter_trial_record.json` с терминальным статусом `complete` или `accepted_degraded`.
- **Matching identity:** source/chunk-plan/config identity рекорда соответствуют текущему запуску, backend-хэш рекорда входит в `acceptable_identity_hashes()` текущего конфига, а stage artifacts проходят проверку целостности. Более поздние additive изменения общей memory сами по себе не запрещают stage reuse.

## ADDED Requirements

### Requirement: Stage-aware chapter resume

При `chapter --resume` система SHALL определить первый failed, incomplete, missing или stale stage по durable stage checkpoint/manifest. Для chapter, запущенной из book, retry SHALL использовать существующие артефакты главы и актуальную общую memory для вновь выполняемого шага; memory не откатывается. Валидные артефакты успешно завершённых стадий SHALL переиспользоваться. SHALL быть перезапущен только первый незавершённый stage и зависящие от него последующие stages. Ошибка на repair SHALL повторять repair, а не generation/audit; ошибка на formatting/finalization SHALL повторять formatting/finalization, а не generation/audit/repair. Book promotion является отдельной book-stage и SHALL повторяться book-run без model calls. Chapter CLI не выполняет book promotion.

#### Scenario: Repair failure resumes at repair

- **WHEN** generation/selection и audit завершены, а repair завершился ошибкой
- **THEN** `chapter --resume` переиспользует их валидные artifacts, перезапускает repair и затем необходимую finalization, без повторной генерации.

#### Scenario: Formatting failure resumes at formatting

- **WHEN** generation, audit и repair завершены, а formatting/finalization завершился ошибкой
- **THEN** `chapter --resume` переиспользует translation/repair artifacts и перезапускает только formatting/finalization.

#### Scenario: Missing artifact rolls back to its dependent stage

- **WHEN** checkpoint помечает audit завершённым, но его artifact отсутствует или не проходит integrity check
- **THEN** resume начинает с audit и не использует непроверенный artifact.

#### Scenario: Foreign identity is fail-closed

- **WHEN** journal/checkpoint/artifact записан под другим config или backend identity
- **THEN** chapter resume отказывается использовать его с ошибкой, а не смешивает данные и не делает молчаливый перегон; полный rerun требует нового out-dir.

#### Scenario: Resume does not require manual stage selection

- **WHEN** пользователь запускает `chapter --resume` после сбоя
- **THEN** система сама выбирает первый незавершённый stage по checkpoint/артефактам; отдельный ручной `--retry-step` не требуется.

#### Scenario: Later memory additions do not invalidate completed stage

- **WHEN** chapters 3–4 добавили новые glossary/book-memory observations после сохранения артефактов chapter 2
- **THEN** эти additions не делают валидные завершённые stage chapter 2 stale и не требуют отката memory.

### Requirement: Durable stage checkpoints

Каждая завершённая stage SHALL иметь атомарный checkpoint с attempt/revision, input hashes, exact artifact set и integrity hashes. Checkpoint не считается завершённым, пока весь его artifact set не прошёл integrity validation; неудачная попытка не должна скрывать последнюю завершённую.

#### Scenario: Retry uses existing chapter artifacts

- **WHEN** chapter 2 упала после генерации, а главы 3–4 успели выполнить и добавить observations в общую memory
- **THEN** `book --resume` использует сохранённые artifacts chapter 2, запускает failed stage на актуальной общей memory и не откатывает observations глав 3–4.

#### Scenario: Partial stage does not hide complete stage

- **WHEN** процесс падает во время новой repair attempt после полностью сохранённого audit artifact
- **THEN** stage checkpoint позволяет повторить repair, сохранив audit artifact для reuse.

### Requirement: Retry incomplete generation

При `chapter --retry-incomplete` после identity-валидации journal SHALL отмотать точку generation resume до первой записи `incomplete_generation`; этот чанк и зависимый хвост SHALL быть сгенерированы заново. `selected`, `quarantined` и `needs_synthesis` до точки перемотки SHALL реконструироваться как сейчас. Journal SHALL оставаться append-only. `quarantined` SHALL не ретраиться этой операцией.

#### Scenario: Incomplete chunk is retranslated

- **WHEN** journal содержит `selected(chunk_A), incomplete_generation(chunk_B)` и глава перезапускается с `--retry-incomplete` в тот же `--out-dir`
- **THEN** `chunk_A` переиспользуется без модельных вызовов, а `chunk_B` генерируется заново.

#### Scenario: Tail after incomplete is regenerated

- **WHEN** journal содержит `selected(chunk_A), incomplete_generation(chunk_B), selected(chunk_C)` и глава перезапускается с `--retry-incomplete`
- **THEN** `chunk_C` перегенерируется, поскольку его левый контекст зависел от результата `chunk_B`.

#### Scenario: Quarantined chunk is not retried

- **WHEN** journal содержит `quarantined(chunk_Q)` и глава перезапускается с `--retry-incomplete`
- **THEN** `chunk_Q` не получает отдельную новую generation attempt и остаётся quarantined, если он входит в переиспользуемый prefix.

### Requirement: Book resume respects shared memory state

`book --resume` SHALL для каждой главы выбирать stage-aware reuse при валидных source/chunk-plan/config/backend artifacts. Готовая глава SHALL пропускать model stages и выполнять только acceptance+promotion. Глава без валидного ready artifact или с failed/incomplete stage SHALL продолжаться с первого зависимого stage в своей папке. Поздние additive изменения общей memory от уже завершённых downstream-глав не делают их stale и не требуют их пересчёта. Несовпадение config/backend identity SHALL быть hard failure, а не автоматическим reuse или очисткой старых артефактов.

#### Scenario: Ready chapter is skipped

- **WHEN** `book --resume` видит ready chapter с matching current identity
- **THEN** strict model stages главы не запускаются, а выполняется только acceptance+promotion.

#### Scenario: Downstream chapters remain intact

- **WHEN** chapter 2 successfully retries after chapters 3–4 were already completed and promoted
- **THEN** chapters 3–4 and their observations remain intact; only new observations from chapter 2 are added.

#### Scenario: Failed chapter resumes its failed stage

- **WHEN** `book --resume` продолжает книгу после failure главы 2 на repair
- **THEN** глава 2 переиспользует generation/audit artifacts и перезапускает repair; готовые главы с валидными artifacts переиспользуются, а их additive observations остаются в общей memory.

#### Scenario: Config or backend mismatch prevents reuse

- **WHEN** ready chapter имеет несовпадающий config или backend identity
- **THEN** book resume завершается с hard failure и не использует старые artifacts; полный rerun выполняется в новом out-dir.

#### Scenario: Forced rerun wins over resume

- **WHEN** `book --resume --force-rerun-chapter 0001` запускается для ready chapter
- **THEN** глава `0001` не пропускается и запускается заново.

### Requirement: Append-only retry revisions

При retry journal SHALL оставаться append-only, но каждая повторная попытка SHALL иметь monotonic revision/attempt marker. Replay SHALL выбирать последнюю валидную запись для каждого logical chunk/stage и SHALL восстанавливать единое логическое состояние без дублирования model results. Stage checkpoint SHALL atomically identify the current attempt, so a crash cannot make a new partial attempt hide the last complete artifact.

#### Scenario: Retry does not get hidden by old journal length

- **WHEN** retry дописывает новую запись для ранее `incomplete_generation` chunk
- **THEN** следующий resume использует новую revision этого chunk, а не пропускает весь журнал по старому `len(prior_entries)`.

### Requirement: Idempotent book promotion

Повторная acceptance+promotion готовой главы SHALL быть идемпотентной: повторный запуск не должен дублировать строки glossary/book-memory ledgers или canonical memory/index entries. `book_run.json` SHALL перестраиваться целиком из результатов текущего запуска.

#### Scenario: Promotion does not duplicate state

- **WHEN** `book --resume` повторно выполняет promotion пропущенной ready chapter
- **THEN** `glossary.json`, `book_memory.json`, `chapter_index.json` и candidate ledgers не содержат дублей этой главы.

## Compatibility

Без `--resume` и без `--retry-incomplete` существующее позиционное journal-resume и поведение book-run SHALL оставаться неизменными. Стабильный `--out-base` является обязательным для продолжения book-run в той же папке; при `book --resume` отсутствие явного существующего `--out-base` SHALL быть ошибкой, а не поводом создать новый timestamp-каталог. CLI принимает `--chapters N` или диапазон `--chapters N-M` (например, `--chapters 1-4`).
