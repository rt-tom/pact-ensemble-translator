# V4: финальная архитектура driver'а с одним резидентным model service

Статус: design-note, реализация отдельной задачей.

## Решение

Для одного GPU базовой финальной архитектурой должен стать **строгий
in-order single-resident driver**: для chunk `N` он завершает Phase 2B и
весь Phase 2C, фиксирует результат, и только затем начинает генерацию
`N+1`. В каждый момент driver владеет не более чем одним локальным model
service. Это сохраняет буквально контракт
`left_context = cascade-выбранный перевод предыдущего chunk'а` из
`V4_MVP_SPEC_RU.md` §3.3.

Это не означает, что данный driver уже следует реализовывать. Сначала
нужен короткий performance/quality gate из раздела «Что измерить». Если
переключения не укладываются в согласованный бюджет, предпочтительный
практический выход — вынести Qwen на второй GPU/машину и оставить
эталонный interleaved driver; не ослаблять `left_context`. Спекуляция
допустима только как последующая оптимизация, доказавшая совпадение со
строгим driver'ом.

`v4_phase12_draft_runner.py` остаётся эталоном корректности при двух
доступных service, а `v4_phase12_sequential_runner.py` остаётся
gate-bench-only вариантом с `SEQUENTIAL_MODEL_CAVEAT`. Ни один из них
этим решением не меняется.

## Неподвижные инварианты

- `select_candidate` остаётся локальным к одному chunk: статичный EN
  source + кандидаты данного chunk. Его порядок запуска не является
  источником межчанковой зависимости.
- Единственная межчанковая зависимость Phase 2B — явный `left_context`.
  Для chunk `N+1` он строится только из `selected_candidate_id` chunk `N`.
- При `quarantined`, `needs_synthesis` или incomplete generation у `N`
  нет established RU-текста. Следующий chunk получает `()` — не
  fidelity-first черновик и не «наименее плохой» fallback. Это
  зафиксированное policy-решение, а не эвристика, см. dated record в
  `DECISIONS.md`.
- Кандидат, сгенерированный с иным `left_context`, не может стать
  финальным reuse для текущей цепочки. Его можно хранить только как
  provisional artifact с явной зависимостью.
- Phase 1/2 контракты (`Candidate`, `generate_for_chunk`,
  `select_candidate`) не меняются. Меняются только порядок вызовов,
  lifecycle service и driver artifacts.

## Рекомендуемый driver: строгий stop-and-switch

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
| 1. Строгий stop-and-switch на одном GPU | Точное; идентично interleaved по входам | Средняя | Reload Qwen на каждой границе; Gemma переключается вокруг Qwen | Базовый correct architecture; принять или отвергнуть только по замеру |
| 2. Спекулятивные волны с откатом | Точное только после fixed-point re-run | Высокая | Дешёво при редких расхождениях, но может каскадно сжечь tokens/reloads | Эксперимент после измерений, не initial production path |
| 3. Ограниченное окно/батч `K` | Неточное без отката | Низкая без отката, высокая с ним | Уменьшает reloads, но внутри окна повторяет defect sequential-driver'а | Не принимать самостоятельным финальным вариантом |
| 4. Qwen на втором GPU/машине | Точное; существующий interleaved порядок | Низкая для driver'а, внешняя стоимость железа/ops | Не решает строгий запрет на глобальное одновременное резидентство, но решает лимит одного GPU | Предпочтительный operational fallback при дорогих reloads |

### 1. Строгий stop-and-switch

Это единственный однопроцессорный вариант, который не делает
непроверенный текст частью входа следующей генерации. При low risk
Gemma preference может быть не нужна, но Qwen fidelity и локальные gates
всё равно завершаются до commit. При high risk и нескольких прошедших
кандидатах Gemma вызывается как selector после Qwen; затем тот же Gemma
service можно использовать для генерации следующего chunk.

Не следует подменять preference детерминированным role-order tie-break
только ради меньшего числа reloads: это меняет выбор кандидата и должно
быть самостоятельной benchmark-policy, а не свойством lifecycle.

### 2. Спекулятивные волны с откатом

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

### 3. Батчинг с окном `K`

Сгенерировать `K` chunk'ов Gemma, затем выбрать их Qwen — допустимо как
измерительный или speculative-wave механизм, но не как финальный driver
сам по себе. Для `N+1..N+K-1` winner `N` на момент generation неизвестен;
подстановка fidelity draft есть именно `SEQUENTIAL_MODEL_CAVEAT`, только
локальная к окну.

Корректны лишь два частных случая: `K=1` (это вариант 1) либо последующий
каскадный откат до fixed point (это вариант 2). Фиксированное маленькое
`K` не создаёт третьего компромисса качества — оно лишь ограничивает
радиус уже недопустимого приближения.

### 4. Второй GPU или удалённый Qwen service

Если цель пользователя — «не держать обе модели на одном GPU», это
самый простой способ сохранить proven interleaved topology: Gemma и
Qwen закреплены за разными service, calls остаются логически
последовательными `generate(N) -> select(N) -> generate(N+1)`.

Если требование буквально запрещает их одновременное резидентство вообще,
вариант не подходит. Если же ограничение — память/операционность одного
узла, его надо сравнить с ценой reloads и сложностью speculative driver,
а не молча исключать. Сетевой отказ Qwen должен переводить текущий chunk
в существующий non-selection/quarantine путь; он не даёт права использовать
черновик как context.

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
5. **Экономика второго service.** Сопоставить measured wall-clock и
   надёжность варианта 1 с ценой/доступностью второго GPU или удалённого
   Qwen. Measurement record до implementation обязан задать численные
   `max_chapter_wall_clock`, `max_reload_count`, `p95_cold_acquire` и
   сравнимый budget второго service. До записи этих четырёх чисел действует
   предварительный threshold: implementation запрещена; декларация
   «перезагрузок слишком много» не считается решением. Числа не
   предзаполняются догадкой, потому что Phase 0C не измерял reloads.

## Условия перехода к реализации

Следующая карточка может реализовывать только вариант, для которого есть
versioned measurement record и выбранный бюджет. Минимальные acceptance
criteria strict driver:

- ни `generation_bundle.json`, ни финальный provenance не несут
  `SEQUENTIAL_MODEL_CAVEAT`;
- на generation `N+1` записанный parent context hash соответствует
  committed cascade result `N` либо явному empty-after-nonselection;
- resume не переиспользует candidate при несовпадающем parent context,
  snapshot, plan, prompt/config identity;
- отдельный negative regression доказывает invalidation при равном parent
  context hash, но ином `chunk_plan_hash`, и при изменённом frozen snapshot
  (например, glossary/memory patch);
- regression покрывает winner B, quarantine, needs_synthesis и
  lifecycle failure между Gemma/Qwen: Qwen failure не вызывает preference
  и не создаёт commit без gate или least-bad fallback;
- offline tests и отдельный hardware benchmark pass; production pipeline
  не запускается этой задачей.

## Открытый вопрос для review

Какой измеренный budget (wall-clock на главу и допустимый number of
reloads) делает строгий один-GPU driver приемлемым по сравнению с
выносом Qwen на второй service? До этой цифры рекомендация определяет
инвариант и порядок выбора, но не утверждает конкретный operational
вариант.
