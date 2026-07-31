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
  fidelity-first черновик и не «наименее плохой» fallback.
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

for chunk N:
  Gemma lease: generate A/B with committed_context[N-1]
  Qwen lease:  fidelity gate for candidates of N
  local:       deterministic + required-risk-category gates
  Gemma lease: optional Russian preference among passed candidates
  commit:      selected candidate, or explicit non-selection state
              -> committed_context[N]
```

После Gemma preference service уже может оставаться загруженным для
generation chunk `N+1`; перед его selection всё равно нужен переход к
Qwen. Поэтому реальная стоимость — не предположение «десятки reloads», а
измеряемая последовательность lease/reload на конкретном железе.

Driver обязан писать append-only decision/context journal. Минимальная
запись на chunk: `chunk_id`, `parent_chunk_id`,
`parent_context_state_hash`, `left_context_kind` (`selected` или
`empty_after_nonselection`), `candidate_ids`, gate trace,
`selected_candidate_id | terminal_nonselection_state` и hash фактически
поданного left-context. Это позволяет resume повторно использовать лишь
артефакт с тем же родителем, snapshot, plan, prompt/config identity и
left-context hash.

Model lifecycle — отдельный адаптер driver'а (`acquire(Gemma|Qwen)`,
`release`). Он управляет только service, запущенными самим run, и не
подключается к чужому `llama-server` и не останавливает его. Реализация
не должна считать HTTP-ответ доказательством освобождения VRAM: это
подтверждает сам lifecycle adapter/наблюдаемый runtime state.

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
synthesis или повторной генерации. Финал можно принять лишь после
сравнения со строгим oracle-run: совпадают selected/non-selected states,
selected candidate maps, decision traces, translations и context hashes.

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

Такую идею стоит вести как отдельный `REVIEW REQUIRED` эксперимент. Её
контроль — strict exact-RU context; варианты — пустой контекст и
детерминированная memory-derived выжимка. Решение возможно только если
слепая оценка стыков и integrity не хуже control, а не потому, что
right-context не изменил FP-candidate rate в Track A. Тот benchmark не
измерял discourse, анафору или left-context вовсе.

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
   строгого oracle. Зафиксировать число волн, invalidations, повторные
   tokens/reloads и полное равенство final artifacts. Несовпадение —
   дефект, не допустимое качество/скоростное trade-off.
5. **Экономика второго service.** Сопоставить measured wall-clock и
   надёжность варианта 1 с ценой/доступностью второго GPU или удалённого
   Qwen. Решение фиксирует явный бюджет, а не декларацию
   «перезагрузок слишком много».

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
- regression покрывает winner B, quarantine, needs_synthesis и
  lifecycle failure между Gemma/Qwen без least-bad fallback;
- offline tests и отдельный hardware benchmark pass; production pipeline
  не запускается этой задачей.

## Открытый вопрос для review

Какой измеренный budget (wall-clock на главу и допустимый number of
reloads) делает строгий один-GPU driver приемлемым по сравнению с
выносом Qwen на второй service? До этой цифры рекомендация определяет
инвариант и порядок выбора, но не утверждает конкретный operational
вариант.
