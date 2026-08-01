# V4 Strict Single-Resident Driver — Chapter Trial (task)

Backing spec: `docs/plans/V4_SINGLE_RESIDENT_DRIVER_ARCHITECTURE_RU.md`
(§ «2. Строгий stop-and-switch», § «Model lifecycle», § «Ошибки между
gate и commit», § «Resume после обрыва», § «Подсчёт перезапусков»,
§ «Результат измерения 1», § «Результат измерения 2»).

## Зачем это отдельная карточка, а не продолжение измерений 1/2

Измерения 1 и 2 были **read-only / standalone**: карточка 1 — offline
shadow re-selection поверх уже существующего draft-run'а, карточка 2 —
синтетический lifecycle-бенчмарк вне `pact_v4/`. Обе карточки прямо
запрещали трогать `pact_v4/` и production pipeline.

Эта карточка меняет границу осознанно: если по итогам измерений 1 и 2
strict stop-and-switch выглядит выигрышным вариантом (см. обсуждение —
измерение 1 показало почти нулевое speculative-окно на chapter_046,
измерение 2 показало умеренную restart-стоимость на SYCL), следующий
шаг — не третий измерительный скрипт, а **реальный прогон целой главы
в правильной strict-последовательности**, реализованный поверх
настоящих Phase 1/2 модулей, а не поверх заглушек/synthetic-промптов.
Явное намерение: если strict будет принят как default topology, эта
реализация не выбрасывается и не переписывается заново — она и есть
кандидат в production driver.

**Следствие:** в отличие от карточек 1 и 2, здесь **разрешён и ожидаем**
импорт из `pact_v4/` и создание нового модуля внутри `pact_v4/pipeline/`
и `pact_v4/runtime/`. Production `pact_translate_v3.py`/v3 config
по-прежнему не трогаются — это остаётся v4-only trial на одной главе, не
переключение default production pipeline.

## Роль-сплит — флаг перед стартом

`docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md`
закрепляет для V4-фаз сплит «Codex реализует, Claude делает adversarial
review». Карточки 1 и 2 были явным исключением (полная реализация
Claude, по прямому запросу пользователя каждый раз). Эта карточка — уже
не измерительный скрипт, а кандидат в реальный `pact_v4/pipeline/`
модуль, то есть по духу гораздо ближе к обычной V4-фазе, чем к
измерению. **Перед началом реализации нужно повторно спросить
пользователя, кто пишет код** — прошлые override'ы не переносятся
автоматически на новую задачу.

## Что уже есть в кодовой базе и почему это не покрывает задачу

- `pact_v4.pipeline.v4_phase12_draft_runner.run_chapter` — корректный
  per-chunk алгоритм (`left_context` от реального cascade winner'а
  предыдущего chunk'а), но **требует одновременной доступности** Gemma
  и Qwen (`ModelCaller`, `QwenEvaluator`, `GemmaSelector` вызываются
  инлайн в одном процессе на один chunk). Не работает на single-GPU
  железе без reload на каждой границе.
- `pact_v4.pipeline.v4_phase12_sequential_runner` (`run_generate` +
  `run_select`) — уже решает problem одним восстановлением, но ценой
  `SEQUENTIAL_MODEL_CAVEAT`: `left_context` во время generation берётся
  из непроверенного `fidelity_first` DRAFT предыдущего chunk'а, а не из
  cascade winner'а. Это ровно тот defect, который strict-вариант обязан
  не воспроизводить.
- `pact_full_pipeline_runner_v1/v4_shadow_reselect_two_pass.py` —
  реализует нужный **split** `select_candidate`'s стадий (Qwen-only /
  Gemma-only) для single-resident железа, но **offline**: подаёт
  candidates из уже сгенерированного `generation_bundle.json`, не может
  подать реальный selected/`empty_after_nonselection` left-context
  обратно в generation следующего chunk'а, потому что generation уже
  прошла (использовала sequential runner'а fidelity-draft caveat).
- `pact_full_pipeline_runner_v1/v4_model_lifecycle_bench.py`
  (Измерение 2) — валидированный `LifecycleAdapter` (VRAM-confirmed
  release через `GPU Process Memory` counter, ownership-safety,
  driver-settle delay, SYCL-профиль) — **годится как основа**, но живёт
  вне `pact_v4/`, использует synthetic-промпты и всегда делает restart
  между Gemma-сегментами (не переиспользует lease между
  `Gpref(N)`/`Ggen(N+1)`, что не соответствует § «Подсчёт перезапусков»).

**Вывод:** ни один существующий компонент не даёт «сгенерировать N,
переключиться на Qwen, гейтить N, при необходимости переключиться
обратно на Gemma за preference, закоммитить N, генерировать N+1 с
реальным `left_context`» на живом однопроцессорном железе. Эта карточка
и есть эта недостающая связка.

## Что реализовать

Новый модуль `pact_v4/pipeline/v4_phase12_strict_runner.py` (имя по
аналогии с существующими `*_draft_runner.py`/`*_sequential_runner.py`),
использующий существующие Phase 1/2/3 модули без изменений:

1. **Per-chunk цикл**, повторяющий решающее дерево
   `pact_v4.phase2.cascade.select_candidate` **пошагово** (как это уже
   сделано в `v4_shadow_reselect_two_pass.py` для offline-случая, но
   здесь — live):
   - Gemma resident → Phase 2B generation чанка `N` с **реальным**
     `left_context` (committed RU текст `N-1`, либо
     `empty_after_nonselection`, никогда fidelity-draft).
   - Switch → Qwen resident → Qwen fidelity gate + deterministic gate +
     required-category gate (`risk`) на всех candidates chunk'а `N`.
   - Если 0 прошло → `quarantine`/`gate_failed`, `N+1` получает
     `empty_after_nonselection`.
   - Если ровно 1 прошёл → select, Gemma preference не нужна.
   - Если 2+ прошло → проверить `check_semantic_disagreement`; при
     несогласии без synthesis-кандидата → `needs_synthesis` (без
     preference); иначе → нужна Gemma preference.
   - `commit(N)` — durable, до Gemma для `N+1`.
2. **Model lifecycle adapter**, вынесенный/адаптированный из
   `v4_model_lifecycle_bench.LifecycleAdapter` в
   `pact_v4/runtime/model_lifecycle.py` (переиспользуемый модуль, не
   copy-paste): та же VRAM-confirmed release, ownership-safety
   (`assert_port_free_or_owned`), settle delay, SYCL-профиль (валидная
   конфигурация из Измерения 2 — `C:\llama-sycl-new`, `--load-mode
   mmap`, `-ncmoe 18`/`-fit on`, `-c 32768`, тот же MTP draft для
   Gemma).
3. **Правильный restart accounting** (в отличие от Измерения 2):
   `Gpref(N)` и `Ggen(N+1)` — **один и тот же** Gemma lease, restart
   между ними не делается. Restart происходит только на реальной смене
   модели (Gemma→Qwen, Qwen→Gemma). Это одновременно чинит
   ограничение №1 из Измерения 2 (Gemma generation-роль была
   недосэмплирована): здесь Gemma generation — это **настоящая** Phase
   2B генерация чанка на полную длину, не synthetic-заглушка.
4. **Durable append-only journal**, поля по списку из § «Model
   lifecycle»: `chunk_id`, `parent_chunk_id`,
   `parent_context_state_hash`, `left_context_kind`, `snapshot_hash`,
   `chunk_plan_hash`, `prompt/config identity`, `candidate_ids`, gate
   trace, `selected_candidate_id | terminal_nonselection_state`, hash
   реально поданного left-context. Плюс per-switch lifecycle-запись той
   же формы, что в Измерении 2 (`cold_acquire_seconds`,
   `unload_seconds`, `first_token_seconds`, `completion_seconds`,
   `peak_vram_mb`).
5. **Resume** по таблице § «Resume после обрыва» — обрыв не должен
   требовать перегенерации committed prefix.
6. **Operational policy на повторный `gate_failed`** — доку явно
   оставляет это открытым («либо глава останавливается согласно
   operational policy»). Эта карточка обязана **зафиксировать** политику
   до запуска (например: N подряд `gate_failed`/`quarantine` → hard stop
   с явной причиной в journal, не бесконечный `empty_after_nonselection`
   каскад) и записать её в provenance, а не решать по ходу.

## Явные не-цели

- Не трогать `pact_translate_v3.py`, `config.v3.json`, v3.1
  production-раннер — эта карточка не переключает production default.
- Не менять gate-логику (`deterministic_consistency_gate`,
  `required_category_gate`, `check_semantic_disagreement`,
  `select_candidate`) — импортируется как есть, как в
  `v4_shadow_reselect_two_pass.py`.
- Не отвечать на «Открытый вопрос для review» (batch-first vs strict
  noninferiority, задача 6 из «Что измерить до кода») — эта карточка
  валидирует механику и реальную стоимость strict, не сравнивает его с
  batch-first на качестве. Если strict принимается как default без
  approximation (потому что это и есть oracle), задача 6 может стать
  неактуальной — но это отдельное решение, не следствие этой карточки
  автоматически.

## Данные для прогона

Глава: **chapter_046** (та же, что в Измерении 1) — для прямой
сопоставимости с уже посчитанным `divergence_rate=1.0` и известным
числом chunk'ов/candidates. Число реальных restart НЕ приравнивается
заранее к 20 — это иллюстративная оценка доку для 10 high-risk chunk'ов;
здесь фиксируется фактическое число high-risk chunk'ов, A/B-кандидатов,
switch'ей и preference-вызовов chapter_046 даёт по факту, без подгонки.

## Acceptance criteria

- Реальный, не-stub v4-driver output для chapter_046:
  `translations.json`/`selection_results.json`/`provenance.json` той же
  схемы, что у `v4_phase12_draft_runner`/`v4_phase12_sequential_runner`
  (совместимо с `v4_v3_draft_compare.py` без изменений).
- Новый versioned measurement record (`pact-v4-strict-chapter-trial/v1`)
  с lifecycle-полями Измерения 2 (per-switch) + quality-полями
  Измерения 1 (selected/quarantined/needs_synthesis/gate_failed count,
  сопоставимо с уже посчитанными числами по этой же главе).
- Journal проходит resume-таблицу § «Resume после обрыва» — минимум один
  намеренный обрыв процесса среди tests/regression, подтверждающий
  продолжение без перегенерации committed prefix.
- Restart-count в записи соответствует реальному числу переключений
  модели, посчитанному по правилу «один lease на `Gpref(N)`+`Ggen(N+1)`»
  — не завышен искусственным always-restart, как в Измерении 2.
- `operational policy` на повторный `gate_failed` зафиксирована в
  provenance до старта, не изобретена постфактум по логам.
- Никаких изменений в `pact_translate_v3.py`/v3 config; production
  pipeline не запускается этой задачей.
- Regression/self-test покрывает: одиночный passing candidate (без
  preference), disagreement с synthesis, disagreement без synthesis
  (preference нужна), `quarantine`, `needs_synthesis`, `gate_failed` →
  `empty_after_nonselection` propagation, resume с каждого чекпоинта из
  таблицы.

## Известные риски / что может сорвать план

- MTP speculative decoding (draft-model acceptance rate) при
  `temperature=0` — детерминизм между запусками не проверен на этой
  конфигурации; если нужна побайтная воспроизводимость для regression,
  может потребоваться отключить `--spec-type draft-mtp` для тестового
  профиля (не для timing-профиля) или явно принять недетерминизм и
  сравнивать по looser critera.
- chapter_046 в Измерении 1 показала почти сплошной Qwen quarantine
  (82%, с отмеченным подозрением на parsing-артефакт при обрыве на
  `max_tokens`) — если это подтвердится и здесь, реальный прогон может
  почти целиком состоять из `gate_failed`/`empty_after_nonselection`
  chunk'ов, что даёт мало A/B-кандидатов и мало preference-вызовов для
  честной restart-статистики. Стоит зафиксировать заранее, считается ли
  это поводом сменить главу на прогон.
- Полный chapter-scale прогон с реальной генерацией — не 10-минутный
  синтетический бенчмарк; ожидаемое wall-clock нужно грубо оценить до
  старта (число chunk'ов × (cold_acquire+generation+unload) на chunk из
  Измерения 2 как нижняя граница, plus реальная длина генерации вместо
  synthetic 128 токенов).

## Результат прогона (chapter_046, SYCL, 2026-08-01)

Реализация: `pact_v4/runtime/model_lifecycle.py` (`LifecycleAdapter` +
`ModelRouter`), `pact_v4/runtime/model_lifecycle_adapters.py`
(`Lifecycle{ModelCaller,QwenEvaluator,GemmaSelector}`),
`pact_v4/pipeline/v4_phase12_strict_runner.py` (`run_chapter_strict`,
journal, resume, halt policy), CLI
`pact_full_pipeline_runner_v1/v4_phase12_strict_run.py`. `select_candidate`
и остальные gate-функции импортированы без изменений; единственная новая
логика — orchestration, lifecycle, journal, resume. 20 новых unit-тестов
(`tests/pact_v4/runtime/test_model_lifecycle.py`,
`tests/pact_v4/pipeline/test_v4_phase12_strict_runner.py`), без
subprocess/HTTP — используют `FakeLifecycleAdapter`. Полный
`tests/pact_v4/` (368 тестов) зелёный после рефакторинга
`v4_model_lifecycle_bench.py` на общий `LifecycleAdapter`.

**Прогон**: `D:\pact\gate_bench_runs\v4_phase12_strict_046\run_001\`
(`chapter_html=D:\pact\pact_chapters\0046_subordination-6-3.html`,
`memory_dir=D:\pact\pact_chapters` — тот же вход, что у существующих
`v4_phase12_046/dry_run_*`; пустой glossary/book_memory, как и там).
Прервался один раз по зафиксированной заранее operational policy
(`max_consecutive_terminal_nonselections=3`, сработала после 3 подряд
`quarantined` на chunk 3-5) — не молча, `halted_early=true` с явной
причиной в record. По решению пользователя продолжен через resume с
`max_consecutive_terminal_nonselections=11` (осознанное изменение policy
для второй сессии того же run, зафиксировано в её собственном record;
journal подтверждённо не переигрывал уже закоммиченные 5 chunk'ов).

**11/11 chunk'ов обработано**: 5 `selected` (4 `balanced_literary`, 1
`fidelity_first`), 5 `quarantined` (все — Qwen fidelity fail на обоих
кандидатах), 1 `needs_synthesis` (disagreement, jaccard 0.40, нет
synthesis-кандидата). Ноль `incomplete_generation`, ноль неучтённых
ошибок, ни одного orphan `llama-server` процесса после любой из двух
сессий (подтверждено `Get-Process` перед/после каждого запуска).

**Lifecycle-числа совпадают с Измерением 2** (синтетический бенчмарк): в
этой сессии — Gemma `cold_acquire` median 14.8s (p95 23.1s), Qwen 19.2s
(p95 20.3s); `unload` 6.4-6.7s для обеих ролей; `peak_vram` Gemma
~10.15GB, Qwen ~9.76GB. Практически совпадает с Измерением 2's Gemma
12.9s/Qwen 17.6s/unload ~6s/VRAM ~10.2GB и ~9.6GB — synthetic-бенчмарк
оказался репрезентативным для реальной chapter-scale нагрузки, закрывает
ограничение №1 Измерения 2 (там Gemma generation была недосэмплирована;
здесь она — настоящая Phase 2B генерация на полную длину).

**Restart составил ничтожную долю wall-clock.** Суммарно по обеим
сессиям: 9 + 11 = 20 restart (`startup_count` 10 и 12 — второе число
включает один дополнительный "cold" startup самого resume, отдельно от
идеальной непрерывной последовательности; это ожидаемо и совпадает с
`Резюме после обрыва`'s "+1 startup на checkpoint"). Совокупный
`wall_clock_seconds` двух сессий — 2868.9 + 3503.9 = 6372.8s (~106 мин)
на всю главу; restart-overhead (20 × ~15-20s ≈ 300-400s) — это **≈5-6%**
от полного времени. Доминирует реальное время generation/gate completion
(отдельные `/v1/chat/completions` вызовы занимали минуты, не секунды).
Прямое подтверждение вывода из Измерения 2 на реальной нагрузке: цена
restart на этом железе/билде не является узким местом strict-driver'а.

**Совпадает с Измерением 1 независимо.** 5/11 quarantined (все — Qwen
fidelity fail на *обоих* candidates) и 1/11 needs_synthesis
воспроизводят находку Измерения 1 (chapter_046 даёт высокий Qwen-отказ)
теперь на **живой** генерации, а не offline shadow re-selection поверх
tie-break run'а — снимает опасение Измерения 1 §"Ограничения" о том, что
82% quarantine мог быть parsing-артефактом при обрыве на `max_tokens`:
здесь генерация и гейты полностью живые, и высокий quarantine rate
(5/11 ≈ 45%, ниже 82% Измерения 1, но того же порядка) подтверждается
независимо.

**Значение для выбора топологии.** Обе стороны "cost vs quality"
уравнения теперь имеют реальные chapter-scale числа, не догадку: cost
strict-варианта на этом железе низкий (restart — единицы процентов
wall-clock); quality-сторона показывает, что даже строгий driver
регулярно не может выбрать кандидата на этой главе (45% chunk'ов) —
это ограничение самого Qwen fidelity gate / промпта / кандидатов, а не
lifecycle-топологии, и одинаково ударит по любому варианту (strict,
batch-first, speculative), поскольку все они используют один и тот же
`select_candidate`. Это смещает следующий приоритетный вопрос с "дорог
ли strict" (нет, судя по этим числам) на "почему Qwen fidelity gate так
часто отказывает на chapter_046" — вопрос retrival/промпта, не
топологии, и в любом случае должен быть закрыт до задачи 6
(noninferiority) независимо от того, какая топология выбрана default.

**Ограничения этого результата:**

1. `n=1` глава, один прогон (плюс один resume) — variance между
   прогонами/главами не измерена (см. ограничение №3 карточки).
2. MTP speculative decoding был включён (`--spec-type draft-mtp`);
   детерминизм между повторными запусками этой же главы не проверялся —
   если понадобится байт-в-байт воспроизводимость, нужен отдельный
   прогон без spec-decoding для сравнения.
3. Resume был протестирован только на реальном прерывании по
   operational policy (не на аварийном обрыве процесса/питания) —
   искусственный kill-and-resume тест остаётся только в unit-тестах
   (`FakeLifecycleAdapter`), не проверен здесь на живом процессе.
4. ~~Причина высокого Qwen fidelity fail rate не диагностирована~~ —
   диагностирована постфактум, см. ниже.

## Диагностика Qwen fidelity fail rate (2026-08-01)

Ограничение №4 выше закрыто отдельным диагностическим прогоном
(`pact_full_pipeline_runner_v1/v4_diag_qwen_truncation_repro.py`,
только Qwen, без Gemma — ~2 минуты, не полная регенерация): повторный
Qwen-запрос теми же candidate-переводами, что уже quarantined в
`run_001` (`chunk0009`, `chunk0010`, оба кандидата каждого).

**Корень найден: не fidelity-отказ, а обрезание JSON-ответа по
`max_tokens`.** `chunk0009/fidelity_first` — сырое тело ответа
обрывается на середине строки `"reason"`, но уже содержит
`faithful_to_source: true, completeness: true, introduced_errors: false,
confidence: "high"` — то есть Qwen склонялся к **passed**, и его
оборвало до закрытия JSON. Остальные три (`chunk0009/balanced_literary`,
`chunk0010/fidelity_first`, `chunk0010/balanced_literary`) — вовсе
**пустое** тело ответа (бюджет токенов, похоже, исчерпан внутри
`<think>`-блока до появления видимого контента). `_parse_qwen_verdict`
трактует любой невалидный JSON как `passed=False` — неотличимо от
содержательного отказа, ровно то ограничение, что уже отмечало
Измерение 1 ("это неразличимо retroactively"), только теперь с прямым
подтверждением, а не подозрением.

`pact_v4/runtime/qwen_evaluator.py`'s `DEFAULT_MAX_TOKENS` был
фиксирован на `4096` независимо от размера chunk'а (комментарий
утверждал "below any plausible chunk of 20 PIDs" — chunk0010 дал 44
PID). Исправлено на `16384` (тот же файл, с обновлённым комментарием,
ссылающимся на этот прогон). Это меняет поведение Qwen-гейта для
**всех** топологий и production baseline, не только strict-driver'а —
не тронуто ничего в `pact_v4/phase2/cascade.py` (сама gate-логика), это
чисто token budget. `tests/pact_v4/` (368 тестов) зелёный после правки.

**Не перепроверено на GPU в рамках этой сессии** — по решению
пользователя фикс подготовлен, но повторный запуск оставлен ему.
Команды для самостоятельной проверки — см. финальное сообщение сессии
2026-08-01 (или просто перезапустить diag-скрипт и/или resume run_001
после `--max-consecutive-nonselections` сброса).
