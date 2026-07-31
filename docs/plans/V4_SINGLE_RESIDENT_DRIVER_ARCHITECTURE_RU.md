# V4: финальная архитектура driver'а с одним резидентным model service

Статус: design-note, реализация отдельной задачей.

Scope: одна локальная машина; в каждый момент запущен ровно один
`llama-server` с Gemma **или** Qwen. Второй GPU, удалённая модель и второй
одновременный service не рассматриваются.

## Решение

Для одного GPU предпочтительным **экспериментальным** кандидатом становится
**batch-first discourse plan + targeted boundary convergence**. Он делает source-side preparation,
generation, semantic admission и Russian-only selection большими model-role
батчами, а exact selected RU left-context использует только для небольшого
числа boundary repairs. Это даёт 3 restart до repair и ещё 2 на фактически
нужный repair-round вместо 20 strict restart для десяти high-risk chunk'ов.

Строгий in-order driver остаётся quality oracle и benchmark-control: только
с ним можно доказать, что новая политика не ухудшает дискурс. Batch-first
нельзя назначать production default, пока он не покажет не худший результат
по заранее зафиксированной boundary-rubric, semantic residual и integrity
против strict control на одном golden set. Его преимущество в restart не
заменяет это доказательство.

`v4_phase12_draft_runner.py` остаётся эталоном корректности при двух
доступных service, а `v4_phase12_sequential_runner.py` остаётся
gate-bench-only вариантом с `SEQUENTIAL_MODEL_CAVEAT`. Ни один из них
этим решением не меняется.

## Неподвижные инварианты

- `select_candidate` остаётся локальным к одному chunk: статичный EN
  source + кандидаты данного chunk. Его порядок запуска не является
  источником межчанковой зависимости.
- Primary generation не получает непроверенный RU draft. Она получает
  frozen source-side discourse plan: glossary/имена, speaker/addressee,
  ты/вы, референты, время, voice notes и риск границ.
- Exact selected RU left-context обязателен для boundary repair. Candidate,
  созданный с другим committed left-context, не может быть reuse этого
  repair; journal хранит parent context hash.
- При отсутствии выбранного кандидата автоматический fallback обязан
  сохранить полный структурно валидный PID-map и trace. Он может быть выдан
  пользователю как `accepted_degraded` после исчерпания repair budget, но это
  terminal availability state, не canonical quality acceptance и не тихий
  `complete`.
- Базовые identity/validation контракты `Candidate` и PID-map сохраняются,
  но понадобятся новые artifacts: source-side discourse plan, RU boundary
  window, fallback debt trace и terminal state `accepted_degraded`.

## Рекомендуемый driver: batch-first discourse plan

```text
Qwen:  source-side discourse plan + boundary-risk map (один батч)
Gemma: A/B generation всех chunk'ов по frozen plan (один батч)
Qwen:  semantic admission кандидатов + deterministic/required-risk gates (один батч)
Gemma: Russian-only global selection и findings слабых стыков (один батч)
Gemma: targeted boundary repair с exact selected RU left-context
Qwen:  re-gate только изменённых region'ов
Gemma: Russian re-check/commit repaired region'ов
```

«Один батч» здесь означает **один непрерывный lease модели**, а не один
giant prompt: driver последовательно исполняет versioned bounded work units
(`chunk_id` / `boundary_id`, prompt/context hashes), не выгружая модель между
ними. Russian-only «global selection» охватывает все chunk'и главы в одном
Gemma lease, но каждая selection unit содержит только кандидатов central
chunk и budgeted соседнюю boundary-окрестность; aggregate decision trace
собирается детерминированно. Размер unit и context budget — benchmark-config,
а не неявное свойство длины главы.

Phase 1–2 — admission, не финальный аудит. Там обязательны только valid
PID-map/identity, coverage, hard deterministic constraints, Qwen semantic
admission, required-risk categories и выбор среди уже допустимых RU
кандидатов. Полная литературная и межчанковая проверка находится в Phase 3.

Phase 3 покрывает **всю главу** серией перекрывающихся audit units: каждый
chunk один раз является full central chunk, плюс получает budgeted tail
предыдущего и head следующего RU chunk, явно помеченные как read-only context.
Глобальные deterministic checks выполняются по полной собранной главе. Три
full chunk в одном model prompt допускаются только при risk trigger (диалог,
referent, ты/вы, scene transition) и после отдельного context-size benchmark.
Findings всегда принадлежат central chunk.

Finding store остаётся append-only. Если repair меняет текст chunk `N`, все
audit units, где `N` входил как central или read-only excerpt, обнаруживаются
по `audit_context_hash`. Их исходные findings получают событие
`context_stale_by_repair` (не удаляются и не merge'ятся) и не могут быть
закрыты/использованы как свежие evidence до revalidation. Driver повторяет
только затронутые central units и boundaries; подтверждённый finding получает
новую запись с `revalidated_from`, неподтверждённый — явное supersession
reason. Это исключает решение по finding, чьё соседнее evidence уже устарело.

Phase 4 выполняет один обязательный targeted boundary repair-round. Второй
разрешён только если re-gate всё ещё находит blocking finding либо первая
правка изменила boundary/context соседнего region. После лимита система может
выдать structurally-valid fallback как `accepted_degraded` с явным debt trace,
не продвигая память и не объявляя его `complete`; `failed` остаётся для
отсутствия какого-либо валидного PID-map после automatic retries.

### Ошибки и resume batch-first стадий

Batch не является атомарным: каждый generation/gate/selection/re-check work
unit сначала durable записывается с model/prompt/context identity, а затем
меняет lifecycle. При model failure driver повторяет только incomplete unit;
остальные подтверждённые units reuse'ятся лишь при совпадении всех identity.

- Qwen admission error даёт `gate_failed` только затронутым кандидатам:
  Gemma selection и commit для них запрещены. Другие chunk'и могут закончить
  свои work units, но глава не становится `complete`; после retry budget без
  semantically admitted PID-map это `failed`, не fallback без fidelity gate.
- Gemma selection error после прошедших Qwen/deterministic gates не меняет
  admission trace. После retry budget разрешён только записанный
  deterministic fallback **среди уже admitted candidates**; он несёт debt и
  может дать `accepted_degraded`, но не выбирает fidelity draft или Qwen-failed
  candidate.
- Если Qwen re-gate прошёл, а Gemma re-check упал, repair не закрывает
  исходный Russian finding. Driver сохраняет repair как candidate, возвращает
  последний admission-passed selected text либо после retry фиксирует
  `accepted_degraded` с открытым debt; без такого валидного текста — `failed`.

После любого retry, меняющего text/hash, применяются `context_stale_by_repair`
и ограниченный re-audit соседей выше; HTTP success сам по себе не является
checkpoint или commit.

### Межглавная continuity после `accepted_degraded`

`accepted_degraded` не меняет `book_memory.json` или locked glossary, но не
должен тихо обрывать continuity следующей главы. Driver создаёт отдельный
append-only `degraded_continuity_overlay`: только source-grounded facts и
детерминированные entities (имя, число, source term, chapter/PID provenance),
без свободной русской формулировки и без promotion. Следующая глава читает
overlay как advisory `requires_revalidation`, а её Qwen source-plan и
deterministic gates обязаны подтвердить или отвергнуть запись; overlay не
может сам стать authoritative memory.

Journal ведёт `consecutive_accepted_degraded` и debt chain. Достигнутый
versioned threshold не останавливает автоматический выпуск, но переводит
следующую главу в усиленный risk/audit profile и создаёт операционное
предупреждение. Значение threshold калибруется и pin'ится в benchmark-config;
неограниченная немаркированная цепочка degraded-глав запрещена.

## Reference oracle: строгий stop-and-switch

До первого model call driver детерминированно создаёт `ChunkPlan`, frozen
snapshot и source-side risk для всей главы. Затем он идёт только слева
направо. Условная Gemma preference выполняется лишь для кандидатов,
прошедших Qwen, deterministic и required-risk-category gates.

```text
preflight (model-free): plan + snapshot + risk

G(N):        Gemma generation with committed_context[N-1]
Q(N):        Qwen fidelity for candidates of N
local(N):    deterministic + required-risk-category gates
Gpref(N):    optional Gemma preference among passed candidates
commit(N):   durable selected candidate, or explicit non-selection
             -> committed_context[N]
G(N+1):      Gemma generation only after commit(N)
```

Граница lease определена явно. Generation `N` заканчивает Gemma lease до
`Q(N)`. После `Q(N)` вызов `Gpref(N)` и generation `N+1` **могут** быть
одним следующим Gemma lease, но это два раздельных вызова, между которыми
лежит durable `commit(N)`. Следовательно preference никогда не
«сливается» с generation следующего chunk и не может быть молча заменена
reload-оптимизацией. Если preference не нужна, `commit(N)` выполняется
после Qwen/local gates до acquire Gemma для `G(N+1)`. Перед selection
`N+1` всё равно нужен переход к Qwen. Поэтому реальная стоимость — не
предположение «десятки reloads», а измеряемая последовательность
lease/reload на конкретном железе.

Driver обязан писать append-only decision/context journal. Минимальная
запись на chunk: `chunk_id`, `parent_chunk_id`,
`parent_context_state_hash`, `left_context_kind` (`selected` или
`empty_after_nonselection`), `snapshot_hash`, `chunk_plan_hash`,
`prompt/config identity`, `candidate_ids`, gate trace,
`selected_candidate_id | terminal_nonselection_state` и hash фактически
поданного left-context. Это позволяет resume повторно использовать лишь
артефакт с тем же родителем, snapshot, plan, prompt/config identity и
left-context hash. Равный parent hash сам по себе не является reuse key:
смена plan или frozen snapshot (включая glossary/memory patch) инвалидирует
candidate и все зависимые downstream artifacts.

Model lifecycle — отдельный адаптер driver'а (`acquire(Gemma|Qwen)`,
`release`). Он управляет только service, запущенными самим run, и не
подключается к чужому `llama-server` и не останавливает его. Реализация
не должна считать HTTP-ответ доказательством освобождения VRAM: это
подтверждает сам lifecycle adapter/наблюдаемый runtime state.

### Ошибки между gate и commit

Порядок выше исключает ситуацию «preference есть, а Qwen gate того же
chunk ещё не был вызван»: `Gpref(N)` возможен только после успешного
`Q(N)`. Если Qwen недоступен/даёт неучтённую ошибку для `N`, Gemma
preference не вызывается, `N` получает явное `gate_failed` non-selection,
а продолжение возможно только с `empty_after_nonselection` (либо глава
останавливается согласно operational policy). Черновик никогда не
попадает в context.

Если Qwen упал на `N+1`, commit `N` уже валиден и не откатывается;
`N+1` не получает selected translation. Последующий retry, который меняет
это состояние, инвалидирует `N+1` и весь его context-dependent suffix по
journal. Так ошибка service не создаёт ни commit без gate, ни скрытый
least-bad fallback.

Качество: эквивалентно текущему interleaved driver'у при одинаковых
моделях, prompts и детерминизме, потому что вход Phase 2B каждого chunk
тот же. Сложность: средняя; основная новая часть — journal, resume и
владение service. Стоимость: максимальна по cold reloads, пока не снят
замер.

## Сравнение вариантов

| Вариант | Качество left_context | Сложность | Стоимость/ограничение | Вердикт |
|---|---|---|---|---|
| 1. Batch-first discourse plan + boundary convergence | Контекст проверяется и чинится на рискованных границах | Средняя | 3 restart до repair; +2 за round | Предпочтительный кандидат, сравнить со strict oracle |
| 2. Строгий stop-and-switch на одном GPU | Точное на каждом generation input | Средняя | 20 restart на 10 high-risk chunk'ов | Oracle/control, не default до Vulkan benchmark |
| 3. Спекулятивные волны с откатом | Точное только после fixed-point re-run | Высокая | 2…20 restart и возможный token waste | Второй performance experiment |
| 4. Ограниченное окно/батч `K` без discourse plan | Неточное без отката | Низкая без отката, высокая с ним | Повторяет defect sequential-driver'а внутри окна | Не принимать самостоятельно |

### 1. Batch-first discourse plan + boundary convergence

Этот вариант сохраняет научно обоснованные части v4: preparation/frozen
memory, небольшой набор A/B, Qwen semantic admission, Russian-only
selection и minimal convergence. Он не утверждает, что source-derived plan
эквивалентен raw selected RU: это проверяется против strict oracle на
boundary golden set. Его преимущество — роль модели меняется редко, а
русская связность проверяется после сборки текста там, где она наблюдаема.

### 2. Строгий stop-and-switch

Это единственный однопроцессорный вариант, который не делает
непроверенный текст частью входа следующей генерации. При low risk
Gemma preference может быть не нужна, но Qwen fidelity и локальные gates
всё равно завершаются до commit. При high risk и нескольких прошедших
кандидатах Gemma вызывается как selector после Qwen; затем тот же Gemma
service можно использовать для генерации следующего chunk.

Не следует подменять preference детерминированным role-order tie-break
только ради меньшего числа reloads: это меняет выбор кандидата и должно
быть самостоятельной benchmark-policy, а не свойством lifecycle.

### 3. Спекулятивные волны с откатом

Gemma может предварительно сгенерировать цепочку, используя provisional
`fidelity_first` left-context, после чего Qwen/Gemma выбирают результаты.
Но первый chunk, где final selected map отличается от provisional parent
(включая quarantine/needs_synthesis), инвалидирует generation всех
зависимых потомков. Driver обязан восстановить суффикс от первого
расхождения до fixed point, каждый раз заново выполняя каскад.

Здесь критерий правильности не «победившая роль отлична от
fidelity_first», а равенство PID-map/context hash. Даже выбор роли
`fidelity_first` не разрешает reuse, если карта текста изменилась из-за
synthesis или повторной генерации. Финал нельзя принять по одному
совпадению роли. На детерминированном fixture с теми же pinned
model/prompt/config identities он обязан побайтно совпасть со строгим
oracle-run по final artifacts. На живой главе с той же identity
сравниваются selected/non-selected states, decision traces и context
hashes; любые отличающиеся identity сначала делают сравнение невалидным,
а не «допустимым дрейфом». Переводы дополнительно проходят integrity
checks, но не подменяют ими oracle equality.

Это может быть полезно, если расхождения действительно редки, но
добавляет dependency DAG, invalidation, bounded retry/fixed-point policy
и сложную resume-семантику. До такого доказательства нельзя называть
вариант экономией: одно раннее расхождение делает его близким к строгому
per-chunk переключению и добавляет выброшенные generation calls.

### 4. Батчинг с окном `K`

Сгенерировать `K` chunk'ов Gemma, затем выбрать их Qwen — допустимо как
измерительный или speculative-wave механизм, но не как финальный driver
сам по себе. Для `N+1..N+K-1` winner `N` на момент generation неизвестен;
подстановка fidelity draft есть именно `SEQUENTIAL_MODEL_CAVEAT`, только
локальная к окну.

Корректны лишь два частных случая: `K=1` (это вариант 1) либо последующий
каскадный откат до fixed point (это вариант 2). Фиксированное маленькое
`K` не создаёт третьего компромисса качества — оно лишь ограничивает
радиус уже недопустимого приближения.

## Подсчёт перезапусков: 10 high-risk chunk'ов

Под «перезапуском» здесь понимается смена запущенной модели:
`Gemma -> Qwen` или `Qwen -> Gemma`. Начальный запуск Gemma считается
отдельно как startup, не как restart. Расчёт предполагает десять
high-risk chunk'ов, A/B для каждого, обе кандидатуры дошли до optional
Gemma preference и не было технических сбоев. Если один кандидат не
прошёл gate, соответствующий preference может не потребоваться — это
уменьшает count, но не является планом экономии.

| Вариант | Последовательность | Перезапуски llama | Всего startup llama | Статус качества |
|---|---|---:|---:|---|
| Строгий stop-and-switch | `Ggen1 → Q1 → Gpref1/Ggen2 → … → Q10 → Gpref10` | **20** | 21 | Точный `left_context` |
| Текущий sequential gate-bench | `Ggen1..10 → Qselect1..10` | **1** | 2 | Непригоден: fidelity draft в context; preference заменяется tie-break |
| Sequential с отдельной Gemma preference-фазой | `Ggen1..10 → Qgate1..10 → Gpref1..10` | **2** | 3 | Всё ещё непригоден: fidelity draft в context |
| Speculative fixed point, одна глава как окно | `Gwave → Qwave → Gpref-wave`, до convergence | **2…20** | 3…21 | Точный только после fixed point |
| Windowed speculation с rollback, окно `K` | По 1…`K` волн на окно | **2×ceil(10/K)…20** | 3…21 | Точный только после fixed point |

Для windowed варианта нижняя граница: `K=2` — 10 restart, `K=5` — 4,
`K=10` — 2. Верхняя граница для любого `K` — 20: в худшем случае в каждом
окне каждый следующий chunk меняет provisional parent, поэтому каждая
волна фиксирует только один дополнительный chunk. Это не хуже strict
варианта по перезапускам, но может существенно хуже по выброшенным
Gemma tokens.

У speculative fixed point число волн `W` ограничено `1 ≤ W ≤ 10`:
каждая волна должна durable-commit хотя бы один следующий chunk, иначе
driver останавливает run как non-convergent, а не крутится бесконечно.
Итого `restart_count = 2W`, `startup_count = 1 + 2W`. Оптимистичный
случай — все provisional parent совпали с final selected context; худший
— расхождение на каждой следующей границе.

Таким образом, при заданном ограничении только speculation может снизить
число restart ниже 20, сохранив exact context, но это вероятностная
экономия, а не гарантия. Чистый batching и sequential-driver дают малые
числа только ценой уже запрещённого приближения.

## Resume после обрыва

Journal обязан быть durable после каждого перечисленного checkpoint. При
обрыве процесс продолжает с последнего валидного checkpoint; уже
committed prefix не перегенерируется.

| Последний durable checkpoint | Продолжение | Дополнительный startup после остановки llama |
|---|---|---:|
| `generation_outcome(N)` записан | Запустить Qwen и завершить gates `N` | 1 |
| Qwen/local gate trace `N` записан, preference/commit нет | Запустить Gemma, preference (если нужна), затем commit `N` | 1 |
| `commit(N)` записан | Запустить Gemma и генерировать `N+1` | 1 |
| Обрыв внутри неперсистированного model call | Повторить только этот call с последнего checkpoint | 1 |

Если `llama-server` пережил обрыв driver'а и его identity/health доказаны,
этот дополнительный startup равен нулю. Если retry меняет committed state
раннего chunk, journal инвалидирует только его context-dependent suffix;
это правило одинаково для strict и speculative driver. Полный reboot
машины остаётся внешним восстановлением и не должен стирать journal или
cache artifacts.

## Как использовать текущий двухфазный sequential run

Текущий `v4_phase12_sequential_run` полезен как **read-only first-wave
measurement**, даже несмотря на его явный `SEQUENTIAL_MODEL_CAVEAT`.
После завершения run следует сохранить без перезаписи
`generation_bundle.json`, `selection_results.json`, `translations.json`,
`provenance.json` и замер одного ручного перехода Gemma → Qwen.

По этим artifacts можно посчитать:

- число high-risk chunk'ов и A/B-кандидатов;
- долю `fidelity_first`, не прошедших Qwen/deterministic/required-risk
  gates, а также quarantine и `needs_synthesis` rate;
- первый context-impacting mismatch и длину suffix, который speculative
  driver должен был бы инвалидировать в первой волне;
- объём candidate generation, который был бы выброшен при таком первом
  откате.

Ограничение существенно: при одном запущенном llama select-проход не
может одновременно вызвать Qwen и Gemma preference. Без
`--use-gemma-selector` существующий runner при нескольких прошедших
кандидатах использует deterministic role-order tie-break. Поэтому его
`selected_role` не является полным cascade winner и обычно занижает
расхождения с `fidelity_first`; этот run нельзя использовать как прямое
доказательство качества speculative или strict driver.

Для полного measurement без повторной generation допустим отдельный
shadow re-selection, не меняющий исходные artifacts: Qwen повторно
гейтит кандидаты из `generation_bundle.json` с per-candidate записью,
затем после одной смены Qwen → Gemma preference выбирает только среди
прошедших. Сравнение этого winner с fidelity draft даёт корректную оценку
первой speculative-волны. Оно не предсказывает последующие волны: при
регенерации suffix кандидаты могут измениться из-за правильного
left-context.

Наконец, время текущего Gemma → Qwen swap — лишь один lifecycle sample.
Оно не измеряет деградацию Vulkan driver от strict-driver 20 restart;
для этого нужен отдельный короткий `Gemma → Qwen → Gemma` benchmark без
production pipeline.

`v4_phase12_sequential_runner.py` остаётся **test-only measurement harness**:
ему запрещены production import, terminal commit и участие в runtime driver.
Он хранится до решения по default topology как воспроизводимый first-wave /
gate-bench control; после принятия topology отдельная review-card решает
оставить его regression fixture или архивировать. Его нельзя незаметно
превратить в fallback production path.

## Почему не надо сейчас заменять RU left_context памятью/выжимкой

Frozen glossary и book/chapter memory уже необходимы, но они не доказано
эквивалентны предыдущему RU-тексту: exact left-context несёт локальную
анафору, speaker continuity, ритм и формулировку перехода. Заменить его
на running glossary или model-generated summary означает изменить
translation semantics и prompt policy, а не только оркестрацию.

Это не закрытая дверь, а отдельная `REVIEW REQUIRED` карточка после
strict-driver oracle. Её контроль — strict exact-RU context; варианты —
пустой контекст и детерминированная memory-derived выжимка. Карточка
обязана включить заранее выбранные boundary cases и слепую оценку стыков.
Решение возможно только если они и integrity не хуже control, а не потому,
что right-context не изменил FP-candidate rate в Track A. Тот benchmark
не измерял discourse, анафору или left-context вовсе.

## Что измерить до кода

Ниже — маленькие измерительные задачи; они не запускают production
pipeline и не меняют run artifacts/caches.

1. **Расхождение draft ↔ winner на реальных artifacts.** Offline-анализатор
   читает `generation_bundle.json` и `selection_results.json` gate-bench
   run 046. Для каждого complete chunk считает: наличие fidelity draft,
   winner/non-selection, role mismatch и, главное, PID-map mismatch
   fidelity draft vs selected map. Отдельно: позиция первого расхождения,
   длина инвалидируемого суффикса, частоты quarantine/needs_synthesis.
   Выход — только новый measurement record, не перезапись run.
2. **Стоимость lifecycle.** На том же hardware, model files,
   quantization, context-size и server flags измерить warm/cold
   `acquire`, unload/освобождение VRAM, first-token и completion для
   Gemma generation, Qwen gate и Gemma preference. Замерять полную
   последовательность `G -> Q -> G`, а не одиночный startup. Зафиксировать
   число chunk'ов, preference calls, peak VRAM, wall-clock и errors.
3. **Чувствительность к left-context.** На frozen source/snapshot и
   заранее выбранных boundary cases сравнить strict selected RU context,
   empty context и fidelity draft. Оценка должна быть blind и
   boundary-focused: referent/anaphora, speaker/address register,
   continuity терминов, narration transition, Russian quality и
   deterministic integrity. Track A FP rate недостаточен.
4. **Равенство speculative fixed point.** Если вариант 2 остаётся
   кандидатом, прогнать deterministic fixture и реальную главу против
   строгого oracle с теми же pinned identities. Fixture требует полного
   равенства final artifacts. Реальная глава требует равенства
   selected/non-selection states, decision traces и context hashes на
   каждом chunk; translations дополнительно проходят integrity checks.
   Зафиксировать число волн, invalidations и повторные tokens/reloads.
   Несовпадение при одинаковых identity — дефект, не допустимое
   качество/скоростное trade-off.
5. **Restart budget и convergence.** На одной машине измерить real
   `max_chapter_wall_clock`, `max_reload_count`, `p95_cold_acquire`, число
   speculative волн, invalidated suffix и повторные tokens. До записи этих
   чисел implementation запрещена; декларация «перезагрузок слишком много»
   не считается решением. Числа не предзаполняются догадкой, потому что
   Phase 0C не измерял reloads.
6. **Batch-first против strict oracle.** На одном frozen source/snapshot
   сравнить выбранную русскую цепочку и Phase-3 findings: semantic residual,
   rubric по анафоре/ты-вы/voice, LTCR, deterministic integrity, quarantine /
   `accepted_degraded` rate, wall-clock, tokens и restart. Три audit profile:
   central-only, central + bounded neighbour excerpts, три full chunk только
   для рискованных границ. До запуска создаётся immutable
   `noninferiority_policy`: `n_boundary_units`, blind-rubric protocol,
   one-sided confidence level, margins для semantic/boundary defect rate,
   Russian rubric, LTCR и `accepted_degraded` rate. Integrity и PID/format
   contracts имеют margin=0; ни одно преимущество по скорости не компенсирует
   hard regression. Batch-first проходит, только если верхняя граница
   one-sided interval разницы с strict не превышает каждый соответствующий
   margin и sample size достигнут. Неопределённый/недостаточный результат
   оставляет topology experimental; числа margins и threshold не выдумываются
   post hoc, а pin'ятся в benchmark-config до первого сравнения.

## Результат измерения 1: расхождение draft ↔ winner (chapter_046, 2026-07-31)

Задача 1 из «Что измерить до кода» выполнена методом shadow re-selection,
описанным выше («Для полного measurement без повторной generation допустим
отдельный shadow re-selection…»). Источник: `draft_001` (sequential
generate+select run, tie-break, `SEQUENTIAL_MODEL_CAVEAT` записан) для
`chapter_046`, 11 chunk'ов. `draft_001/*` не менялся (verified по sha256 до
и после).

**Обнаруженное ограничение исходного инструмента.** `v4_phase12_sequential_run.py
--phase select --use-gemma-selector` не разделяет Qwen fidelity gate и Gemma
preference на два прохода: `select_candidate` зовёт Gemma **внутри** обработки
одного chunk, сразу после его Qwen-гейта, до перехода к следующему chunk'у.
Значит на железе с одной резидентной моделью этот флаг неисполним в одном
запуске. Вместо правки `pact_v4/` (что нарушило бы read-only-периметр этой
измерительной задачи) собран отдельный two-pass инструмент вне `pact_v4/`:
`pact_full_pipeline_runner_v1/v4_shadow_reselect_two_pass.py` — `--stage qwen`
прогоняет Qwen fidelity + deterministic + required-risk-category гейты по
всем chunk'ам и копит `pending_gemma` кандидатов в промежуточный
`qwen_pass_state.json`; `--stage gemma` (после ручной смены модели на
сервере) вызывает Gemma preference только для реально накопленных
`pending_gemma` chunk'ов и пишет `selection_results.json` /
`translations.json` / `provenance.json` в той же схеме, что и оригинальный
`--phase select`. Оркестрация повторяет ветвление `select_candidate`
построчно, но сами гейты (`deterministic_consistency_gate`,
`required_category_gate`, `check_semantic_disagreement`) импортированы из
`pact_v4.phase2.cascade` без изменений. Regression-проверка (replay
сохранённых Qwen-вердиктов `draft_001` через новый инструмент) побайтно
воспроизвела исходные `selected_role_counts`. Сравнение вынесено в
`pact_full_pipeline_runner_v1/v4_shadow_reselect_compare.py` (read-only,
берёт `fidelity_first` PID-map из `generation_bundle.json` и selected map из
`selection_results.json`).

**Результат живого прогона** (`shadow_reselect_001`, реальный Qwen,
`Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf`, `reasoning-budget=0` подтверждён в логе
сервера):

- `pending_gemma_chunks = 0` — ни разу не набралось 2+ прошедших кандидата;
  Gemma preference не вызывалась ни разу. Совпадает с исходным tie-break
  run'ом побайтно: `selected=2` (`chunk0001`, `chunk0008`, оба —
  `balanced_literary`), `quarantined=9`, `needs_synthesis=0`.
- `fidelity_first` совпал с cascade winner **0 раз из 11**: в 9 случаях сам
  `fidelity_first` не прошёл Qwen (вместе со вторым кандидатом), в 2 —
  прошёл только `balanced_literary`.
- Первое context-impacting расхождение — `chunk_index=0` (самый первый
  chunk главы). Инвалидируемый suffix первой спекулятивной волны —
  **10 из 11** chunk'ов.
- Итог по всем 11 chunk'ам: `context_impacting_mismatches=11`,
  `divergence_rate=1.0`.

Полная запись: `D:\pact\gate_bench_runs\v4_phase12_046_seq\shadow_reselect_001\measurement_record.json`
(таблица по chunk'ам, hash'и identity, ссылки на оба run-каталога).

**Значение для выбора топологии.** На этой главе у варианта 3 (спекулятивные
волны) буквально нет окна экономии: расхождение с provisional-контекстом
наступает на первом же chunk'е, поэтому первая волна инвалидирует
практически всю главу — restart-экономия speculative варианта против
strict stop-and-switch (раздел «Подсчёт перезапусков») здесь не реализуется.
Это согласуется с уже записанным анализом варианта 3, но не заменяет его:
здесь измерена ровно первая волна на одной главе, не fixed-point equality.

**Ограничения этого измерения, важные для интерпретации:**

1. `quarantine_rate=9/11` объясняется почти целиком отказом на этапе Qwen
   fidelity, а не сравнением ролей. При этом почти каждый Qwen-вызов
   доходил до `max_tokens=4096`, не останавливаясь естественно (видно по
   `n_decoded`/`eval time` в логе `llama-server`), несмотря на промпт,
   требующий `reason` в одно-два предложения. `response_format=json_object`
   гарантирует только синтаксическую валидность, а не длину; при обрыве
   генерации на границе `max_tokens` `json.loads` в `_parse_qwen_verdict`
   падает и трактуется как провал гейта — неотличимо от содержательного
   отказа в уже сохранённых артефактах, потому что `quarantine_reason`
   строит текст только из `traces`, куда провалившийся у Qwen кандидат не
   попадает (raw `detail` для таких случаев не сохраняется). Поэтому
   текущие 82% карантина — верхняя граница, не чистая оценка content-level
   fidelity; часть могла быть парсинг-артефактом. До отдельной доработки
   (сохранять `gate.detail` и для непройденных кандидатов) это неразличимо
   retroactively.
2. `n=1` глава, 11 chunk'ов — недостаточно для решения по топологии.
   `chapter_046` может быть нерепрезентативна (high-risk band у
   большинства chunk'ов). Повтор на нескольких главах golden-набора не
   выполнялся.

Следующие шаги при желании закрыть это измерение полнее: (a) починить
observability Qwen-гейта для непройденных кандидатов, (b) повторить
shadow re-selection на 2-3 других главах, прежде чем использовать
`divergence_rate=1.0` как аргумент против варианта 3/4 за пределами этой
одной главы.

## Условия перехода к реализации

Следующая карточка может реализовывать только вариант, для которого есть
versioned measurement record и выбранный бюджет. Минимальные acceptance
criteria batch-first driver:

- primary generation получает только frozen source-side plan, никогда
  fidelity-first RU draft;
- Phase 2 пропускает только structurally valid PID-map и candidate с
  записанным Qwen/deterministic trace; fallback имеет явный debt trace;
- Russian-only Phase 3 prompt содержит full central chunk и маркированные
  bounded neighbour excerpts; finding не может быть привязан к чужому PID;
- repair делает evidence всех audit units с изменённым context stale и
  повторно валидирует только затронутые central/boundary units;
- batch — один model lease из bounded work units, не giant prompt; Qwen
  failure не допускает Gemma selection/commit, а Gemma fallback разрешён
  только среди Qwen/deterministic-admitted candidates;
- `accepted_degraded` создаёт только non-authoritative source-grounded
  continuity overlay и ведёт versioned degraded-chain threshold;
- `noninferiority_policy` записан до comparison: sufficient sample, zero
  integrity regression и one-sided margins для quality/degraded metrics;
- один repair-round обязателен, второй требует recorded trigger; repair с
  changed boundary получает exact committed RU left-context и re-gate;
- resume не переиспользует repair candidate при несовпадающих parent context,
  snapshot, plan или prompt/config identity;
- regression покрывает winner B, отсутствие допустимого кандидата,
  `accepted_degraded`, Qwen/Gemma failure и восстановление после каждого
  durable checkpoint;
- offline tests и отдельный hardware benchmark pass; production pipeline
  не запускается этой задачей.

## Открытый вопрос для review

Покажет ли batch-first policy на boundary golden set не худший semantic /
Russian discourse результат против strict oracle при существенно меньшем
числе restart? Если нет, strict остаётся quality control, а batch-first
не принимается только потому, что он быстрее.
