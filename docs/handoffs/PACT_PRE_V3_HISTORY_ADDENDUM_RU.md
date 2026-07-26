# PACT_PRE_V3_HISTORY_ADDENDUM_RU

**Историческое дополнение к `PACT_V3_1_1_HANDOFF_RU.md`**  
**Срез старого контекста: до перехода на v3.1 / v3.1.1**

> Этот файл намеренно не повторяет актуальную архитектуру, команды запуска, текущие профили и обязательные проверки, уже описанные в `PACT_V3_1_1_HANDOFF_RU.md`.
>
> При любом расхождении приоритет имеют:
>
> 1. свежий bundle текущего run;
> 2. фактически установленные скрипты;
> 3. run-файлы и логи;
> 4. актуальный handoff;
> 5. это историческое дополнение;
> 6. старая переписка.

## Статусы

- **CONFIRMED** — подтверждено фактическим запуском, логом, result-файлом или установленным файлом.
- **DISCUSSED** — обсуждалось или было подготовлено, но установка либо полный тест не подтверждены.
- **OBSOLETE** — относится к старой архитектуре и не должно применяться напрямую к v3.1.
- **UNKNOWN** — точного подтверждения в доступном контексте нет.

---

# 1. Архитектурные решения v1–v3.0 и причины их принятия

## 1.1. Ранний сегментированный перевод с полной ревизией сегмента

**Статус: CONFIRMED / OBSOLETE**

В ветке v2.2 использовалась цепочка примерно такого вида:

```text
HTML chapter
→ нормализация
→ присвоение PID
→ chapter memory
→ перевод chunk
→ полная revision того же chunk
→ glossary update
→ HTML assembly
```

Важные решения:

- глава делилась по границам HTML-блоков;
- целевой размер chunk был около `900` слов;
- использовались предыдущие, последующие и релевантные ранние блоки;
- создавалась chapter memory с summary, POV, tone и character voices;
- модели передавался предыдущий русский контекст;
- revision возвращала целый исправленный сегмент, а не отдельные исправления;
- inline-теги защищались специальными маркерами;
- glossary мог автоматически продвигать часто подтверждавшиеся имена и термины.

Причина появления этой архитектуры:

- перевод длинной главы целиком был слишком рискован для покрытия и структурной целостности;
- требовались контекст между chunk, устойчивое возобновление и сохранение HTML;
- простой независимый перевод каждого абзаца давал бы разрыв голоса, имён и `ты/вы`.

### Исторические настройки v2.2

**Статус: CONFIRMED / OBSOLETE**

В `pact_translate_v2_2_2_0.py` были зафиксированы:

```text
context_size: 49152
chunk target/min/max: 900 / 450 / 1200 words
chapter_memory temperature: 0.2
translation temperature: 0.3
revision temperature: 0.3
translation retries: 3
revision retries: 2
```

Эти параметры не являются рекомендацией для v3.1.

## 1.2. Защита от разрушительной полной revision

**Статус: CONFIRMED / OBSOLETE**

В v2.2.2 появились консервативные guards:

```text
conservative_guard
fallback_suspicious_pid_to_draft
max_word_ratio_vs_draft
max_added_words_vs_draft
min_word_ratio_vs_draft
max_removed_words_vs_draft
detect_neighbor_containment
english_residue_fallback
```

Причина:

- revision могла переписывать хороший русский без необходимости;
- могла втянуть текст соседнего контекста;
- могла потерять или добавить значительный объём;
- могла оставить английский остаток;
- полный segment rewrite плохо позволял отличить полезную правку от регрессии.

Архитектурный вывод, приведший к v3:

> проверять и исправлять нужно локальные issues, а не разрешать модели заново переписывать весь удачный chunk.

## 1.3. Переход v3.0 к issue-only audit и targeted repair

**Статус: CONFIRMED / OBSOLETE как конкретная реализация; принцип сохранён**

`pact_translate_v3.py` внутренней версии `3.0.2` перешёл к цепочке:

```text
Gemma draft translation
→ deterministic QA
→ reviewer issue-only audit
→ targeted repair
→ отдельное восстановление formatting
→ final integrity
```

Причины перехода:

- полная revision v2 слишком часто меняла удачные фрагменты;
- требовалось хранить конкретную причину изменения;
- repair должен был работать только по выбранным PID;
- появилась возможность сравнивать `draft` и `repaired`;
- структурная проверка стала отделена от литературного исправления.

В v3.0 issue имел примерно следующие поля:

```json
{
  "pid": "p00001",
  "severity": "major",
  "category": "meaning",
  "problem": "...",
  "repair_instruction": "...",
  "suggested_text": "...",
  "source": "reviewer",
  "deterministic": false,
  "status": "open",
  "issue_id": "..."
}
```

## 1.4. Внешний full-pipeline runner и последовательное переключение моделей

**Статус: CONFIRMED / OBSOLETE как версия**

Поскольку обе крупные модели одновременно не держались в памяти, `run_full_pipeline.ps1` стал оркестратором:

```text
Gemma
→ Qwen
→ Gemma verifier
→ Gemma repair/finalize
```

Решение было принято не как логическое требование архитектуры, а как ограничение локального железа и памяти.

`run_full_pipeline_v1_0_1.ps1` уже выполнял шесть стадий:

1. prepare;
2. Gemma translation;
3. Qwen bilingual audit;
4. Gemma verifies Qwen candidates;
5. Gemma repairs;
6. formatting/finalization.

В `v1.0.3` серверные профили были разделены на:

```text
GemmaTranslate
GemmaVerify
Qwen
```

Причина:

- перевод и verifier нуждались в разных thinking/runtime-параметрах;
- исходный runner ошибочно запускал все Gemma-этапы одним профилем;
- repair/finalization не должны были оставаться на reviewer-профиле.

## 1.5. Production glossary из существующего любительского перевода

**Статус: CONFIRMED**

Был обработан EPUB любительского перевода до главы 7.04.

Созданы и использовались:

```text
parallel_translation_memory.jsonl
pact_production_glossary_v1\
```

Из параллельного корпуса извлекались:

- имена;
- прозвища;
- названия сущностей;
- обращения;
- повторяющиеся термины;
- варианты человеческого перевода.

После ручных решений glossary был установлен с подтверждённой валидацией:

```text
locked=21
established=90
provisional=6
unresolved=0
```

Зафиксированные решения того этапа включали:

```text
The Abyss   → Бездна
Green Eyes  → Зеленоглазая
```

Причина:

- автоматическая транслитерация и chapter bible уже показывали нестабильные варианты;
- человеческий перевод предыдущих глав давал лучший источник согласованности, чем генерация с нуля.

## 1.6. Переход от «Qwen обнаруживает, Gemma только судит» к ensemble

**Статус: CONFIRMED; причина уже частично отражена в актуальном handoff**

В pre-v3.1 pipeline Qwen был единственным модельным detector. Gemma verifier видел только его кандидаты.

Практический результат:

```text
ошибка, не найденная Qwen
→ не попадала в verifier
→ не попадала в repair
```

Это было одной из главных причин полного перехода к нескольким независимым audit-проходам в v3.1.

---

# 2. Подходы, которые тестировались и были отвергнуты

## 2.1. Полная self-revision каждого chunk

**Статус: CONFIRMED / OBSOLETE**

Отвергнуто как основная стратегия, потому что модель:

- переписывала хороший текст;
- могла втянуть соседний контекст;
- могла изменить объём;
- затрудняла attribution конкретной правки;
- требовала эвристических guard-ов, которые лечили симптомы, а не причину.

## 2.2. Один reviewer как единственный detector

**Статус: CONFIRMED / OBSOLETE**

Отвергнуто после benchmark и главы 60.

Даже более сильный Qwen batch-reviewer показал неполное покрытие:

```text
PID recall:             0.625
strict issue recall:    0.3958
false-positive rate:    0.2917
```

То есть он был полезнее Gemma как detector, но одновременно:

- пропустил значительную часть gold issues;
- создал заметное число ложных кандидатов.

## 2.3. Gemma как единственный reviewer

**Статус: CONFIRMED / OBSOLETE**

На старом benchmark из 48 gold PID и 24 controls:

### Один из batch=8 запусков

```text
recall:              0.2708
false-positive rate: 0.0833
```

### Другой вариант prompt/batching

```text
recall:              0.1458
false-positive rate: 0.0
```

Отвергнуто как detector полного покрытия.

## 2.4. Qwen переводит, Gemma проверяет

**Статус: DISCUSSED**

Обратное назначение ролей обсуждалось после reviewer benchmark.

Точного подтверждённого полного production-run с Qwen в роли основного переводчика в доступном контексте нет.

Не считать этот вариант проверенным или отвергнутым по качеству перевода.

## 2.5. Автоматически repair всех `minor`

**Статус: DISCUSSED как общий вариант; фактически не включалось**

Причина отказа от простого правила:

- среди `minor` было много субъективных `style`, `tone`, `register`;
- включение всей severity привело бы к ненужному переписыванию текста.

Выбранное позднее направление:

```text
minor
+ объективная category
+ подтверждение verifier
→ допустим для repair
```

Точная реализация текущей политики должна проверяться по свежему bundle.

## 2.6. MTP для Qwen

**Статус: CONFIRMED — не работает**

Попытка создать MTP context на основном Qwen GGUF завершилась:

```text
context type MTP requested but model doesn't contain MTP layers
failed to create MTP context
```

Не повторять без отдельной совместимой draft/MTP-модели.

## 2.7. MTP для структурированного Gemma repair

**Статус: CONFIRMED — нестабильно**

На repair batch сервер трижды вернул HTTP 500.

После этого был выделен стабильный GemmaRepair без MTP.

Не возвращать MTP в repair только на основании его скорости на обычной генерации.

## 2.8. Gemma `-ncmoe 6`

**Статус: CONFIRMED — плохой профиль**

На длинном запросе наблюдалось примерно:

```text
prompt к концу: ~43 t/s
generation:     ~7.6 t/s
```

Профиль значительно хуже `-ncmoe 18`.

## 2.9. Gemma MTP `n_max=2`

**Статус: CONFIRMED — проиграл baseline**

При acceptance около `0.86` реальная генерация была около:

```text
16.8 t/s
```

Draft overhead оказался выше выигрыша.

Поздний `n_max=4` работал заметно лучше.

## 2.10. Ручное размещение expert-тензоров Qwen

**Статус: CONFIRMED — не стало финальным профилем**

Тестировались:

- автоматический `llama-fit-params`;
- перенос почти всех MoE experts на CPU;
- перенос части experts на GPU;
- `-ngl 99` плюс ручной `-ot`.

Наблюдались варианты около:

```text
22–24 t/s
15–16 t/s
```

Они проиграли восстановленному auto-fit профилю около `30 t/s` на коротком benchmark и более ранним ~35 t/s измерениям.

Причина не использовать:

- низкая скорость;
- высокая чувствительность к точному tensor placement;
- ручной профиль оказался хуже уже найденного auto-fit.

## 2.11. Полагаться только на короткий 129-token benchmark

**Статус: CONFIRMED — недостаточно**

Короткий тест не отражал:

- длинный prompt;
- сотни последовательных запросов;
- retries;
- JSON stability;
- MTP stability;
- shared-memory pressure;
- деградацию Windows/Vulkan после длительной работы.

Профиль должен был подтверждаться и длинным реальным запросом.

## 2.12. Обязательный completion marker в HTML-ответе

**Статус: CONFIRMED / OBSOLETE**

В v2 marker стал необязательным:

```text
completion_marker_required = false
```

Причина:

- модель могла вернуть все PID и корректный HTML, но пропустить служебный marker;
- отклонять весь результат только из-за marker было слишком дорого.

Coverage проверялся по фактическому списку PID, а не по маркеру.

---

# 3. История найденных багов

## 3.1. Coverage

### Пропуск последнего или внутреннего PID моделью

**Статус: CONFIRMED**

В первом полном запуске главы 60 были реальные примеры:

```text
p00050 — отсутствовал в c0001
p00281 — отсутствовал в c0005
p00437 — пустой/отсутствовал в c0007
p00398 — отсутствовал после split c0007a
```

Pipeline обнаруживал:

```text
PID mismatch expected=[...], got=[...]
empty or missing
```

После retry крупный chunk мог делиться на `a/b`.

Исторический вывод:

- `finish_reason=stop` не доказывает coverage;
- JSON/HTML может быть синтаксически валидным при отсутствующем PID;
- exact PID coverage обязателен.

### Extra context PID в ответе

**Статус: CONFIRMED / OBSOLETE**

В v2.2.1 extra PID из контекста начали игнорироваться как warning:

```text
ignore_extra_pids = true
```

При этом обязательные target PID по-прежнему должны были совпасть в правильном порядке.

### Fail-open audit coverage

**Статус: CONFIRMED / OBSOLETE**

В старом `run_full_pipeline_v1_0_3.ps1` было:

```text
audit.fail_open = true
audit.minimum_success_rate = 0.90
```

То есть audit мог продолжить pipeline при покрытии ниже 100%, если проходил порог.

Не переносить эту логику как достаточную для v3.1 final quality.

## 3.2. Audit

### Обрезанный или внутренне противоречивый JSON

**Статус: CONFIRMED**

На `c0001_q004` Qwen начал писать issue о `three hour window`, затем внутри `problem` сам признал, что `трехчасовое окно` уже присутствует, продолжил рассуждение и ответ был обрезан.

Результат:

```text
Invalid JSON response
pipeline stage 3 failed
```

Это пример сразу двух проблем:

- JSON не завершён;
- даже до обрыва содержимое не являлось чистым итоговым решением.

### Детерминированный suspect мог подталкивать модель к ложной ошибке

**Статус: CONFIRMED**

В том же примере suspect утверждал, что numeric equivalent отсутствует, хотя `трехчасовое` было в русском тексте.

Qwen пытался рационализировать suspect вместо независимой проверки.

Исторический вывод:

- deterministic suspect не должен считаться доказательством;
- model audit должен иметь право явно отклонить suspect;
- плохой suspect не должен заставлять повторять корректный пакет.

### Лимит issues скрывал дальнейшие проблемы

**Статус: CONFIRMED / OBSOLETE**

Старые long-context тесты часто возвращали ровно около 10 issues, преимущественно повторяющиеся ошибки имён `Барбаторе/Барбер`.

При cap модель могла потратить весь лимит на один повторяющийся тип и не сообщить другие ошибки.

### Reviewer benchmark содержал слабый PID-only gold

**Статус: CONFIRMED / OBSOLETE**

Первый benchmark оценивал только:

```text
expected_issue: true/false
```

Он не проверял, что модель нашла именно правильную ошибку.

Пример:

- модель могла отметить правильный PID, но придумать другую проблему;
- это считалось true positive по PID.

Позже появился strict semantic gold с category/keywords/issue_id.


## 3.3. Merge и deduplication

### Дубли одного PID и одной проблемы

**Статус: CONFIRMED / OBSOLETE**

Старые audit-ответы могли содержать несколько issues одного PID, например два отдельных замечания к `p00337`.

Это не всегда ошибка: на PID действительно могли быть две проблемы. Но dedup по одному только PID был недопустим.

Нужен был ключ уровня:

```text
PID + normalized category + normalized problem
```

или устойчивый `issue_id`.

### Изменение смысла `issues.json`

**Статус: CONFIRMED / OBSOLETE**

На разных стадиях/версиях `issues.json` означал разное:

1. сырой результат Qwen;
2. после verifier — подтверждённый список;
3. в некоторых reports — merged issues.

Из-за этого:

- подтверждённые issues могли повторно приниматься за raw candidates;
- visual report мог неверно считать pending/confirmed;
- rerun verifier мог получить уже отфильтрованные данные вместо исходных кандидатов.

Позднее разделили:

```text
issues.qwen_raw.json
verifier_report.json
issues.json
```

### `60/58` в старом мониторе

**Статус: CONFIRMED / OBSOLETE**

Монитор считал два служебных JSON-файла в `audit` как Qwen units.

Это была ошибка отображения, не реальный merge pipeline.

## 3.4. Cross-verification

### Verifier не обнаруживал новые ошибки

**Статус: CONFIRMED / OBSOLETE**

Старый Gemma verifier получал только candidate issue и решал:

```text
confirm / reject / uncertain
```

Он не выполнял независимый полный audit.

### Ошибка интерпретации benchmark результатов

**Статус: CONFIRMED**

В `case_026_p00387`:

```text
expected_verdict: confirm
actual verdict:   reject
```

Причина модели:

```text
«вылитый» — нормальный идиоматический эквивалент every inch
```

В старом обсуждении результат сначала был ошибочно прочитан как принятая правка.

Правильное чтение benchmark:

```text
actual verdict != expected verdict
→ MISS по benchmark
```

При этом сам gold case мог быть спорным, потому что модель обоснованно защищала `вылитый`.

Статус точной gold-разметки этого case: **UNKNOWN**.

### Thinking 256 не устранял ложные подтверждения

**Статус: CONFIRMED**

В benchmark с thinking=256 Gemma подтвердила кандидат `p00060`, хотя expected verdict был `reject`.

Больший reasoning budget не гарантировал правильное решение и иногда помогал модели убедительнее обосновать ошибочный verdict.

## 3.5. Repair lifecycle

### Confirmed minor не попадал в repair

**Статус: CONFIRMED / OBSOLETE**

Старая политика:

```text
auto_repair_severities = critical, major
```

Поэтому объективные minor issues, включая `p00088` и `p00250`, оставались без исправления.

### `keep` считался успешным завершением issue

**Статус: CONFIRMED / OBSOLETE**

Repair мог вернуть:

```text
action = keep
```

или объяснение вида:

```text
перевод верен / оставить как есть
```

При этом issue уже был подтверждён verifier.

Pipeline мог считать lifecycle завершённым, хотя исходная ошибка оставалась.

### `replace`, не изменивший текст

**Статус: CONFIRMED / OBSOLETE**

Repair мог формально вернуть `replace`, но proposed text совпадал с draft либо не исправлял заявленную проблему.

Старый lifecycle не всегда блокировал это.

### Rejected post-repair возвращал ошибочный draft

**Статус: CONFIRMED / OBSOLETE**

Если post-verifier отклонял repair, pipeline просто откатывал PID к исходному draft.

Но исходный draft уже имел подтверждённую ошибку.

Правильный lifecycle должен был делать retry с feedback, а не считать откат решением.

### Несовместимость repair cache после изменения batch size

**Статус: CONFIRMED / OBSOLETE**

Старые repair-файлы создавались пакетами по 2 или 4 PID.

После перехода к `max_pids_per_call=1` старый cache нельзя было безопасно переиспользовать.

Требовался:

```text
-RedoRepair
```

## 3.6. Post-repair gates

### Та же Gemma проверяла собственный repair

**Статус: CONFIRMED / OBSOLETE**

В v1.1 post-repair safety stage был полезнее отсутствия gate, но всё ещё использовал Gemma, которая участвовала в repair.

На одном проходе по 40 изменениям:

```text
p00026 → reject/revert
остальные показанные PID → accept
```

При ручном анализе среди accepted оставались плохие repairs.

Следовательно, высокий accept rate не был доказательством качества.

### Gate принимал улучшение одной детали при общей деградации

**Статус: CONFIRMED / OBSOLETE**

Подтверждённые примеры плохих accepted repairs перечислены в разделе 5 и regression table.

## 3.7. Finalization

### Потеря inline formatting

**Статус: CONFIRMED / OBSOLETE**

В финальном HTML первого полного run:

```text
p00020 — не восстановлен курсив на are
p00058 — не восстановлен курсив на втором No
```

Причина для повторяющихся одинаковых span:

- старый mapping не различал occurrence одного и того же текста.

### Visual report не распознавал final HTML

**Статус: CONFIRMED / OBSOLETE**

Финальный HTML удалял `data-pid`.

Старый `compare_pipeline_review.py` пытался найти финальный текст по `data-pid`, поэтому писал:

```text
pipeline_stage = after_repair
```

даже после успешной finalization.

### Minor issues ошибочно показывались как pending repair

**Статус: CONFIRMED / OBSOLETE**

Report не различал:

- issue намеренно исключён из auto-repair policy;
- issue ещё ожидает repair.

Из-за этого показывались ложные pending counts.

## 3.8. Ошибочные статусы `complete`

### Старый final integrity проверял структуру, но не unresolved quality

**Статус: CONFIRMED / OBSOLETE**

`state.json` мог получить:

```json
{"status": "complete"}
```

если HTML, числа, PID и другие structural checks прошли, даже когда:

- confirmed issues оставались;
- repair был rejected;
- keep не исправил проблему;
- post-gate вернул draft;
- formatting incident сохранялся.

### Stale `state.json` при downstream rerun

**Статус: CONFIRMED / OBSOLETE**

При `-RedoRepair` старый `state.json` и final HTML могли оставаться до завершения нового прохода.

Monitor v1.1.1 показывал:

```text
PIPELINE COMPLETE
```

пока реально выполнялся stage 5b.

Исправление в monitor v1.1.2:

- активный Python worker стал authoritative;
- стадии `5a` и `5b` разделены;
- старые output/state не считались текущим завершением.

---

# 4. Regression PID: дополнительные исторические сведения

Эта таблица добавляет фактические старые outcomes, которых нет в актуальном handoff.

| PID | Статус | Исходная проблема в старом run | Ожидаемый результат | Где проверялось | Подтверждённый старый итог |
|---|---|---|---|---|---|
| `p00026` | CONFIRMED | repair был отклонён safety verifier | retry repair; draft не считать исправлением | v1.1 post-repair run главы 60 | gate сделал reject и возврат к draft; lifecycle остался незавершённым |
| `p00034` | CONFIRMED | proposed repair заменил формулировку на `ходить кругами`, но заявленную проблему надёжно не исправил; в другом lifecycle наблюдался replace без содержательного изменения | либо реальная минимальная правка, либо retry/reject | v1.0–v1.1 chapter 60 | формально прошёл repair, качество осталось спорным |
| `p00062` | CONFIRMED | `hard to put down` было понято как `трудно отложить`, а не трудно убить/добить | смысл смерти/уничтожения | старый chapter 60 review | ошибка оставалась после старого pipeline |
| `p00088` | CONFIRMED | `cut through its knees` → `перерезала ему колени`; verifier подтвердил awkward/meaning issue | естественная правка без выдуманной анатомии | v1.1 report | не repaired из-за severity `minor` |
| `p00091` | CONFIRMED | английский остаток `Mary`; chapter/book bible допускал `Mary → Mary` | `Мэри`; keep запрещён | v1.1 chapter 60 | verifier/repair не устранили остаток последовательно |
| `p00092` | CONFIRMED | `J.P. Corvidae`/`Corvidae` оставались частично по-английски | согласованная русская форма | v1.1 chapter 60 | предложенный repair был отклонён из-за остававшегося `J.P.` |
| `p00152` | CONFIRMED | repair дал ухудшение вида `пока она подняла стекло` | сохранить исходную причинно-временную связь | v1.1 post-repair | плохая замена была принята gate |
| `p00153` | CONFIRMED | `Volunteering me?` стало `Сама вызвалась?`, сменился субъект | смысл «Ты меня добровольцем назначила?» | v1.1 manual review | плохой repair принят |
| `p00164` | CONFIRMED | issue/repair содержал `перевод верен / оставить как есть` | такой candidate должен быть reject ещё до repair | v1.1 report | попал в lifecycle как issue |
| `p00229` | CONFIRMED | repair удалил смысл `promised`: осталось «забыла отправить» | сохранить обещание отправить | v1.1 manual review | ухудшение принято |
| `p00250` | CONFIRMED | нарушенный параллелизм в фразе о вреде гостю/хозяину | естественный параллельный русский | v1.1 report | confirmed minor grammar не попал в repair |
| `p00254` | UNKNOWN | обращение Блэйка к Дункану | согласованный register сцены | old chapter 60 | точный старый финальный verdict не восстановлен |
| `p00256` | UNKNOWN | обращение Блэйка к Дункану | согласованный register сцены | old chapter 60 | точный старый финальный verdict не восстановлен |
| `p00267` | CONFIRMED | `I’m not even with Laird` → `Мы с Лейрдом даже не в расчёте` | «Я даже не на стороне Лейрда» или эквивалент | v1.1 manual review | плохой repair принят |
| `p00273` | CONFIRMED | `draw it` о пистолете передавалось как `вытяни` | `достань/выхвати` по контексту | old chapter 60 | старый вариант признан неестественным |
| `p00285–p00286` | CONFIRMED | `Barely. / If that.` теряло усиление второго короткого ответа | `Едва. / И то едва.` или эквивалент | old chapter 60 | старый pipeline не дал надёжной пары |
| `p00343` | CONFIRMED | русский добавил отсутствующий `грохот` | убрать добавленную деталь | old chapter 60 | addition прошёл предыдущие стадии |
| `p00362` | CONFIRMED | repair создал сломанную фразу `больше сил, чем они могли бы реально ими манипулировать` | грамматически корректная сравнительная конструкция | v1.1 manual review | плохой repair принят |
| `p00398` | CONFIRMED | candidate фактически предлагал «оставить как есть» | reject candidate, не создавать repair issue | old chapter 60 | старый verifier/lifecycle не отфильтровал надёжно |
| `p00415` | CONFIRMED | repair исправил одно `ты` на `вы`, но оставил `Каковы твои приоритеты?` в том же PID | единый register внутри абзаца | v1.1 manual review | частичная правка принята |
| `p00020` | CONFIRMED | потерян курсив | восстановить source emphasis | v1.0/v1.1 finalization | formatting incident |
| `p00058` | CONFIRMED | второе одинаковое `No` потеряло курсив | occurrence-aware span mapping | v1.0/v1.1 finalization | formatting incident |

---

# 5. Подтверждённые неудачи моделей

## 5.1. Модель пропустила реальную ошибку

### Gemma reviewer benchmark

**Статус: CONFIRMED**

Gemma пропустила 35 из 48 gold PID в одном batch=8 запуске.

Примеры false negatives:

```text
p00022, p00032, p00036, p00066, p00070,
p00075, p00090, p00097, p00145, p00160,
p00271, p00324, p00387, p00391
```

### Qwen reviewer benchmark

**Статус: CONFIRMED**

Qwen пропустил 18 из 48 gold PID по PID-level scoring.

Примеры:

```text
p00066
p00082
p00097
p00098
p00160
p00165
p00170
p00171
p00222
p00232
p00236
p00271
p00276
p00308
p00324
p00338
p00371
```

Strict semantic recall был только `0.3958`.

## 5.2. Модель придумала ошибку

### Gemma: title `Bonds → Узы`

**Статус: CONFIRMED**

На control `p00001` Gemma заявила, что `Bonds` якобы означает финансовые securities и `Узы` неверно.

Сам текст ответа называл claim borderline и упоминал другую «более уверенную» ошибку, но JSON включал именно ложный issue.

### Qwen false-positive PID

**Статус: CONFIRMED**

На reviewer benchmark Qwen имел false-positive rate `0.2917`.

False-positive PID:

```text
p00015
p00060
p00174
p00224
p00269
p00285
p00370
```

Некоторые из них были спорными, а не очевидно ложными. Но по gold benchmark они считались FP.

### Qwen deterministic numeric suspect

**Статус: CONFIRMED**

`трехчасовое окно` было ошибочно интерпретировано как отсутствие numeric equivalent.

Модель начала создавать issue, затем сама заметила противоречие, но уже испортила JSON.

## 5.3. Модель ухудшила русский при repair

**Статус: CONFIRMED**

Подтверждённые examples:

```text
p00152 — «пока она подняла стекло»
p00153 — «Сама вызвалась?» вместо вопроса о назначении говорящего
p00229 — удалено promised
p00267 — «Мы с Лейрдом даже не в расчёте»
p00362 — сломанная сравнительная конструкция
p00415 — смешение вы/ты осталось внутри одного PID
```

Также спорные/неполные:

```text
p00034 — правка не устранила заявленную проблему
```

## 5.4. Модель приняла `keep` как исправление

**Статус: CONFIRMED**

Cases:

```text
p00091 — keep/неисправление при английском Mary
p00164 — «перевод верен / оставить как есть» попал в repair lifecycle
p00398 — аналогичный candidate не был отфильтрован до repair
```

Точный action для каждого из трёх в каждом повторном run может различаться; общий lifecycle bug подтверждён.

## 5.5. Формально корректный, но фактически плохой JSON

**Статус: CONFIRMED**

Примеры:

1. `{"issues":[...]}` синтаксически корректен, но issue `Bonds → securities` ложный.
2. Chapter bible мог записать:
   ```text
   Mary → Mary
   J.P. Corvidae → J.P. Corvidae
   ```
   JSON корректен, но target-language policy нарушена.
3. Repair JSON мог иметь корректные поля `action=replace`, `accepted=true`, но:
   - текст совпадал с draft;
   - не исправлял issue;
   - ухудшал русский.
4. Verifier JSON мог корректно вернуть:
   ```json
   {"verdict":"confirm","confidence":"high"}
   ```
   для кандидата, который benchmark ожидал отклонить.

## 5.6. Некорректный/обрезанный JSON

**Статус: CONFIRMED**

Qwen audit:

- начинал валидный объект;
- уходил во внутреннее рассуждение внутри string;
- обрывался по лимиту;
- `finish_reason`/content не позволяли parse.

Gemma/Qwen translation:

- могли вернуть все элементы, кроме одного PID;
- JSON/HTML оставался parseable;
- coverage был неполным.

---

# 6. История профилей Qwen и Gemma

Актуальные выбранные профили уже описаны в основном handoff. Ниже только история тестов и причины выбора.

## 6.1. Qwen

| Статус | Тест | Наблюдение | Вывод |
|---|---|---|---|
| CONFIRMED | 32K, default/F16 KV | короткая генерация около `16–17 t/s`; median около `14.3 s` | KV footprint/offload давал плохую скорость |
| CONFIRMED | 32K, `-ctk q8_0 -ctv q8_0` | короткий median около `6.15 s`; generation около `34.8–35 t/s` | Q8 KV обязателен для практического профиля |
| CONFIRMED | long context ~15,295 prompt, mmap | prompt около `206–208 t/s`; generation около `26–29 t/s`; total около `96–99 s` | рабочий, но prompt медленнее no-mmap |
| CONFIRMED | long context, `--no-mmap` | prompt около `291–292 t/s`; generation около `28 t/s`; total около `76–78 s` | no-mmap выиграл на длинном input |
| CONFIRMED | `-fitt 1536` без восстановленных `-b/-ub` | production audit падал до примерно `8–16 t/s` generation | runner потерял найденный профиль |
| CONFIRMED | восстановленный `-fitt 1280 -b 2048 -ub 512` | short median `5.835 s`; rounds `5.725–6.187 s`; около `30 t/s` после reboot | выбран как устойчивый профиль |
| CONFIRMED | `llama-fit-params.exe` generated CPU expert overrides | примерно `22–24 t/s`, малая VRAM загрузка | хуже auto-fit winner |
| CONFIRMED | частичный перенос experts на GPU | около `15–16 t/s` | отвергнуто |
| CONFIRMED | Qwen MTP against same model | model does not contain MTP layers | не поддерживается |
| CONFIRMED | старый build reference | build `9721`, commit `5fd2dc2c4` использовался в части sweep | сравнение между build требует осторожности |
| CONFIRMED | после множества load/switch | обе модели замедлялись; reboot возвращал скорость | не путать системную деградацию с плохим тюнингом |

Дополнительный факт:

`qwen36-restored-final-profile.json` содержал один перевод с остатком:

```text
«Блядь,Jesus»
```

То есть хороший performance profile не гарантировал отсутствие mixed-script output.

## 6.2. Gemma

| Статус | Тест | Наблюдение | Вывод |
|---|---|---|---|
| CONFIRMED | short 32K baseline без MTP | median около `5.277 s`; prompt `~119–120 t/s`; generation `~31.8–32.4 t/s` | стабильный baseline |
| CONFIRMED | short 32K Q8 KV | median около `5.257 s` | почти без выигрыша против baseline на коротком input |
| CONFIRMED | `-ncmoe 6`, long context | prompt деградировал до `~43 t/s`; generation `~7.6 t/s` | явно плохой placement |
| CONFIRMED | `-ncmoe 18`, long context без MTP | prompt около `291–293 t/s`; generation около `24 t/s` | хороший базовый long-context profile |
| CONFIRMED | MTP Q8, `n_max=2` | acceptance `~0.86`, но generation `~16.8 t/s` | speculative overhead хуже baseline |
| CONFIRMED | MTP Q8, `n_max=4` | prompt `350.43 t/s`; generation `44.69 t/s`; acceptance `0.884`; mean accepted length `4.54` | выбран для translate |
| CONFIRMED | MTP на structured repair | HTTP 500 три раза на одном batch | для repair выделен профиль без MTP |
| CONFIRMED | verifier thinking 256 | более длинные ответы, но сохранялись неверные verdict | budget 256 не дал гарантированного качества |
| CONFIRMED | verifier temperature `0.0` | использовался в выбранном candidate verifier | deterministic sampling выбран для adjudication |
| UNKNOWN | точные aggregate metrics thinking off vs 128 vs 256 | файлы существовали, но полный сравнительный итог в доступном контексте неполон | не восстанавливать рейтинг догадкой |

## 6.3. Память

**Статус: CONFIRMED**

Наблюдавшиеся профили:

```text
Qwen tuned:
  около 3.9 GiB dedicated, 0 shared в одном конце audit

Gemma verifier:
  около 9 GiB dedicated + 9 GiB shared

Gemma MTP/другие long runs:
  около 9.5–9.8 GiB dedicated + 8–9+ GiB shared
```

Высокий shared usage сам по себе не был доказательством ошибки, но делал систему чувствительнее к состоянию Windows/Vulkan.


---

# 7. Важные скрипты, патчи и result-файлы до v3.1

## Core / translation

```text
pact_translate_v2_2_0_4.py
pact_translate_v2_2_1_0.py
pact_translate_v2_2_2_0.py
pact_translate_v3_3_0_1.py
pact_translate_v3_3_0_2.py
pact_translate_v3.py
config.v3.json
arc_names.json
```

## Reviewer / verifier benchmark

```text
reviewer_benchmark.py
reviewer_benchmark_2_0_0.py
reviewer_benchmark_2_2_0.py
candidate_verifier_benchmark.py
reviewer_gemma_v3.json
reviewer_gemma_b8.json
reviewer_qwen36_b8_v2.json
reviewer_qwen36_b8_strict.json
gemma4_verifier_thinking256.json
```

## Performance benchmark

```text
benchmark_server.py
qwen36-clean-baseline.json
qwen36-clean-baseline-32k.json
qwen36-32k-kvq8.json
qwen36_long_context_32k.json
qwen36_long_context_32k_nommap.json
qwen36-restored-final-profile.json
gemma4-clean-baseline-32k.json
gemma4-clean-baseline-32k-kvq8.json
gemma4_long_context_32k_f16.json
qwen_fit_1536.txt
start_llama_v3.ps1
```

Некоторые точные long-context script names: **UNKNOWN**. Result-файлы подтверждены, но имя runner script не всегда сохранено в доступном контексте.

## Full pipeline

```text
pact_full_pipeline_runner_v1\
run_full_pipeline_v1_0_1.ps1
run_full_pipeline_v1_0_3.ps1
prepare_pipeline_context.py
verify_pipeline_issues.py
verify_repair_results.py
```

## Glossary / human translation bootstrap

```text
parallel_translation_memory.jsonl
pact_production_glossary_v1\
validate_glossary.py
install_production_glossary.ps1
```

## Reports / bundle / monitor

```text
compare_pipeline_review_v1_0_1.py
collect_pipeline_result_v1_0_1.ps1
monitor_pipeline_v1_0_0.ps1
monitor_pipeline_v1_0_1.ps1
monitor_pipeline_v1_0_2.ps1
monitor_pipeline_v1_0_3.ps1
monitor_pipeline_v1_1_1.ps1
monitor_pipeline_v1_1_2.ps1
```

## Patches

```text
pact_pipeline_patch_v1_1_0.zip
pact_pipeline_hotfix_v1_1_1.zip
```

---

# 8. Что было фактически установлено, а что только создано

Подробная таблица находится в разделе C.

Кратко:

- `pact_translate_v3.py` версии 3.0.2 — **CONFIRMED installed/run**;
- production glossary — **CONFIRMED installed**;
- `run_full_pipeline` 1.0.1 и 1.0.3 — **CONFIRMED run**;
- patch v1.1.0 — **CONFIRMED installed**, после него repair упал на HTTP 500;
- hotfix v1.1.1 — **CONFIRMED installed**, после него pipeline завершился;
- monitor 1.0.0–1.0.3 — **CONFIRMED installed/tested sequentially**;
- monitor 1.1.2 — создан, точное подтверждение установки в доступном контексте **UNKNOWN**;
- обратный Qwen-translator pipeline — только **DISCUSSED**.

---

# 9. Изменения JSON-форматов и несовместимости

## 9.1. v2 translation/revision

**Статус: CONFIRMED / OBSOLETE**

Ответ был HTML с:

```html
<p data-pid="p00001">...</p>
```

Coverage определялся через `data-pid`.

Completion marker:

```html
<!-- CHUNK_COMPLETE:c0001 -->
<!-- REVISION_COMPLETE:c0001 -->
```

стал необязательным.

## 9.2. v3 translation

**Статус: CONFIRMED / OBSOLETE как формат конкретной версии**

Перевод перешёл к structured JSON с PID/text, а formatting был вынесен отдельно.

Smoke-test ожидал:

```json
{
  "translations": [
    {"pid": "p1", "text": "..."}
  ]
}
```

Нельзя подавать cached HTML-ответы v2 как v3 translations без конвертации.

## 9.3. Audit issues

**Статус: CONFIRMED**

Старые варианты использовали:

```json
{"issues":[{"pid":"...","category":"...","problem":"..."}]}
```

v3 добавил:

```text
severity
repair_instruction
suggested_text
source
deterministic
status
issue_id
```

Старый benchmark category мог содержать:

```text
Mistranslation
Translation Error
Grammar/Logic Error
```

Поздний benchmark нормализовал aliases к:

```text
meaning
subject
gender
grammar
register
name
```

## 9.4. Verifier

**Статус: CONFIRMED**

Per-case result:

```json
{
  "case_id": "...",
  "pid": "...",
  "expected_verdict": "confirm",
  "status": "success",
  "verdict": "reject",
  "confidence": "high",
  "reason": "...",
  "attempts": [...]
}
```

Production verifier дополнительно создавал:

```text
issues.qwen_raw.json
verifier\<case>.json
verifier_report.json
issues.json
```

`issues.json` нельзя интерпретировать без знания стадии/версии.

## 9.5. Repair

**Статус: CONFIRMED**

Существовали:

```text
repairs\batch_*.json
repair_records.json
repaired_translations.json
```

После post-repair safety появились:

```text
repaired_translations.preverify.json
post_repair_verifier\*.json
post_repair_report.json
repaired_translations.json
```

Последний файл после этого означал уже результат с применёнными revert.

## 9.6. Cache incompatibilities

**Статус: CONFIRMED**

Нельзя безопасно смешивать:

- repair batch cache с разным `max_pids_per_call`;
- raw `issues.json` до verifier и confirmed `issues.json` после verifier;
- old formatting cache до occurrence-aware spans;
- old `state.json` с новым downstream rerun;
- old run directory при изменившейся структуре стадий без целевого `Redo...`.

---

# 10. История monitor-скриптов

## 10.1. Monitor v1.0.0

**Статус: CONFIRMED / OBSOLETE**

Источники прогресса:

| Стадия | Источник |
|---|---|
| Context | `chapter_bible.json` в chapter work dir |
| Translation | `manifest.json`, `drafts\*.json`, `draft_translations.json` |
| Qwen audit | `manifest.chunks`, `config.audit.batch_pids`, `audit\*.json`, `issues.qwen_raw.json`/`issues.json` |
| Verifier | `verifier\*.json`, `verifier_report.json` |
| Repair | `issues.json`, `repairs\batch_*.json`, `repaired_translations.json` |
| Finalize | `formatting\batch_*.json`, `state.json`, `output\*.html` |
| Speed | последний подходящий `server_logs\*_stderr.log` |

Проблемы:

- параметр `$Pid` конфликтовал с PowerShell automatic `$PID`;
- после первого исправления оставалась локальная `$pid`;
- `PSObject.Properties.Count` падал под `Set-StrictMode`;
- один объект мог unwrap вместо массива;
- служебные audit JSON давали `60/58`;
- old server speed мог показываться после переключения профиля.

## 10.2. Monitor v1.0.1

**Статус: CONFIRMED / OBSOLETE**

Исправил только первый `$Pid → $ProcessId`.

Второй `$pid` в repair stats остался, поэтому ошибка повторилась.

## 10.3. Monitor v1.0.2

**Статус: CONFIRMED / OBSOLETE**

Исправил оставшийся `$pid → $issuePid`.

Позже упал на:

```text
The property 'Count' cannot be found
```

## 10.4. Monitor v1.0.3

**Статус: CONFIRMED / OBSOLETE**

Исправлено:

```powershell
@($acceptedMap.PSObject.Properties).Count
$chapters = @(Get-ChapterDirectories ...)
```

Также добавлен stack trace для будущих ошибок.

## 10.5. Monitor v1.1.1

**Статус: CONFIRMED / OBSOLETE**

Показывал post-repair counters, но не отделял новый stage 5b от stale complete.

При активном `verify_repair_results.py` мог показать:

```text
PIPELINE COMPLETE
```

по старому `state.json`.

## 10.6. Monitor v1.1.2

**Статус: DISCUSSED/создан; установка UNKNOWN**

Изменения:

- активный Python worker authoritative;
- `5a Repair` и `5b Safety`;
- current-run post-verifier files по времени;
- old output скрывается до нового finalize;
- отдельные server PID и worker PID.

## 10.7. Monitor v3.1: Qwen source analysis

**Статус: CONFIRMED symptom; причина UNKNOWN**

В текущем v3.1 run наблюдалось:

```text
Active profile Qwen и speeds обновляются правильно
2. Qwen source analysis остаётся 0% 0/445
pipeline log показывает 16/445, 20/445 ... 128/445
```

Это отдельная известная задача для анализа после свежего bundle.

Не делать вывод о реальном отсутствии прогресса по строке монитора.

Точная причина — неправильный artifact path, parser или stage accounting — **UNKNOWN**, пока не проверены установленные `monitor_pipeline_v31.ps1` и run-файлы.

---

# 11. Диагностика, восстановление и сбор bundle

Ниже перечислены старые команды, которые были фактически использованы или предложены в ходе диагностики до v3.1 и не приведены полностью в актуальном handoff. Они не заменяют текущие v3.1-скрипты.

## 11.1. Принудительно остановить `llama-server`

**Статус: CONFIRMED**

```powershell
Stop-Process -Name llama-server -Force -ErrorAction SilentlyContinue
```

Использовалось перед ручным benchmark, сменой профиля и восстановлением после нештатного завершения runner.

## 11.2. Проверить фактическую команду запуска сервера

**Статус: CONFIRMED**

```powershell
Get-CimInstance Win32_Process |
Where-Object Name -eq 'llama-server.exe' |
Select-Object ProcessId, CommandLine
```

Это было важнее чтения предполагаемого профиля из runner: именно так обнаружилось, что Qwen был запущен не с ранее найденным набором `-fitt/-b/-ub`.

## 11.3. Проверить dedicated/shared GPU memory конкретного PID

**Статус: CONFIRMED**

```powershell
$llamaPid = (Get-Process llama-server).Id

Get-Counter `
  '\GPU Process Memory(*)\Dedicated Usage',
  '\GPU Process Memory(*)\Shared Usage' |
Select-Object -ExpandProperty CounterSamples |
Where-Object { $_.InstanceName -match "pid_$llamaPid" } |
Select-Object `
  InstanceName,
  @{Name='Counter'; Expression={$_.Path.Split('\')[-1]}},
  @{Name='GiB'; Expression={
      [math]::Round($_.CookedValue / 1GB, 2)
  }}
```

## 11.4. Проверить RAM и pagefile

**Статус: CONFIRMED**

```powershell
Get-Counter `
  '\Memory\Available MBytes',
  '\Paging File(_Total)\% Usage'
```

Эти данные использовались вместе со скоростью и GPU memory; отдельно высокий shared usage не считался доказательством сбоя.

## 11.5. Зафиксировать версию и hash `llama.cpp`

**Статус: CONFIRMED**

```powershell
cd C:\llama-cpp

.\llama-server.exe --version

Get-FileHash .\llama-server.exe -Algorithm SHA256

Get-Item .\llama-server.exe |
Select-Object Length, LastWriteTime,
  @{Name='FileVersion'; Expression={$_.VersionInfo.FileVersion}}
```

Причина: часть speed sweep выполнялась на конкретном build, и сравнение только CLI-аргументов без версии binary могло быть ложным.

## 11.6. Короткий server benchmark

**Статус: CONFIRMED**

```powershell
cd D:\pact\pact_translator_v3

py .\benchmark_server.py `
  --label diagnostic-profile `
  --model <MODEL_NAME.gguf> `
  --rounds 3
```

Короткий benchmark использовался как smoke/performance check, но позднее был признан недостаточным без длинного реального запроса.

## 11.7. Получить предлагаемое размещение от `llama-fit-params`

**Статус: CONFIRMED**

```powershell
cd C:\llama-cpp

.\llama-fit-params.exe `
  -m .\models\Qwen3.6-35B-A3B\Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf `
  --device Vulkan0 `
  -c 32768 `
  -fit on `
  -fitt 1536 `
  -ctk q8_0 `
  -ctv q8_0 `
  -np 1 `
  -fa on
```

Вывод `llama-fit-params` рассматривался как кандидат для benchmark, а не как автоматически доказанный лучший профиль.

## 11.8. Проверить фактически отправляемые sampling/thinking-настройки

**Статус: CONFIRMED**

```powershell
Select-String `
  .\verify_pipeline_issues.py `
  -Pattern '"temperature"|"top_p"|enable_thinking' `
  -Context 1,1

Select-String `
  .\run_full_pipeline.ps1 `
  -Pattern "\['temperature'\]|\['enable_thinking'\]"
```

Эта проверка подтвердила, что старый production verifier отправлял:

```text
temperature = 0.0
top_p = 1.0
enable_thinking = true
```

а runner задавал `temperature=0.0` для основных стадий. Конкретный server-side reasoning budget определялся профилем запуска.

## 11.9. Валидация установленного production glossary

**Статус: CONFIRMED**

```powershell
cd D:\pact\pact_translator_v3

py `
  .\pact_production_glossary_v1\validate_glossary.py `
  --dir D:\pact\pact_translator_v3\glossary
```

Подтверждённый результат старой установки:

```text
locked=21, established=90, provisional=6, unresolved=0
```

## 11.10. Собрать полный bundle старого pipeline

**Статус: CONFIRMED / OBSOLETE для v3.1**

```powershell
cd D:\pact\pact_translator_v3\pact_full_pipeline_runner_v1

.\collect_pipeline_result.ps1 `
  -Start 60 `
  -End 60
```

Старый collector включал:

- весь run directory;
- server logs;
- isolated config;
- core и runner scripts;
- glossary;
- environment inventory;
- visual comparison.

Для текущего v3.1 run следует использовать актуальный `collect_v31_handoff_bundle.ps1`, если он фактически установлен.

## 11.11. Быстро проверить артефакты старого chapter work

**Статус: CONFIRMED / OBSOLETE**

```powershell
$work = 'D:\pact\pact_translator_v3\pipeline_runs\chapter_60_to_60\work\0060_void-7-5'

Get-ChildItem $work -File |
Select-Object Name, Length, LastWriteTime

Get-Content "$work\state.json" -Raw
Get-Content "$work\quality_report.json" -Raw
Get-Content "$work\post_repair_report.json" -Raw
```

Наличие файла не доказывает, что он относится к последнему rerun. Нужно сопоставлять timestamps, run command и активные worker/server logs.

## 11.12. Точная универсальная команда проверки integrity старых форматов

**Статус: UNKNOWN**

В старом контексте использовались встроенные `self-test`, `final_integrity`, visual report и ручное чтение JSON, но единого подтверждённого standalone-скрипта, валидирующего все старые версии run directory, не установлено.

---

# 12. Ограничения Windows, `llama.cpp`, Vulkan и Intel Arc B580

## 12.1. `GGML_VK_DISABLE_COOPMAT=1`

**Статус: CONFIRMED**

Для рабочей конфигурации Gemma на Vulkan использовалось:

```powershell
$env:GGML_VK_DISABLE_COOPMAT = "1"
```

Не удалять эту переменную из текущего профиля без отдельного A/B-теста.

## 12.2. PowerShell variables регистронезависимы

**Статус: CONFIRMED**

```text
$Pid == $PID
```

`$PID` — read-only automatic variable. Поэтому даже локальная переменная `$pid` вызывает:

```text
Cannot overwrite variable PID because it is read-only or constant
```

## 12.3. `Set-StrictMode` и `.Count`

**Статус: CONFIRMED**

Некоторые PowerShell collections или единичные объекты не имеют надёжного `.Count` под StrictMode.

Безопасный исторический fix:

```powershell
@($value).Count
```

А результаты функций, которые могут вернуть один объект, принудительно оборачивались:

```powershell
$chapters = @(Get-ChapterDirectories ...)
```

## 12.4. MTP требует совместимых layers/model

**Статус: CONFIRMED**

Основной Qwen GGUF не мог использоваться как MTP draft context.

Gemma использовала отдельный файл:

```text
mtp-gemma-4-26B-A4B-it-Q8_0.gguf
```

Наличие основной модели и поддержка speculative decoding — разные свойства.

## 12.5. `--fit`, tensor overrides и mmap

**Статус: CONFIRMED**

При CPU tensor overrides `llama.cpp` выдавал предупреждение:

```text
tensor overrides to CPU are used with mmap enabled
consider using --no-mmap for better performance
```

В длинном Qwen-тесте `--no-mmap` действительно улучшил prompt processing.

## 12.6. Shared GPU memory в Windows

**Статус: CONFIRMED**

Для Arc B580 наблюдалось до примерно `8–13 GiB` shared usage у живого процесса.

Это не означает автоматически:

- критический spill;
- pagefile thrashing;
- неправильный offload;
- обязательный crash.

Оценивать нужно вместе с:

- prompt/generation speed;
- available RAM;
- pagefile usage;
- HTTP/server errors;
- фактической command line.

## 12.7. Деградация после частых model reload

**Статус: CONFIRMED**

Одновременное падение скорости Gemma и Qwen без изменения аргументов восстанавливалось после перезагрузки Windows.

Это доказывает, что часть деградации была связана с состоянием Windows/Vulkan/driver, а не с tuning profile.

## 12.8. Результаты зависят от build `llama.cpp`

**Статус: CONFIRMED**

Часть старых winner-тестов выполнялась на:

```text
build 9721
commit 5fd2dc2c4
```

Если binary текущего run другой, разницу в скорости нельзя автоматически приписывать только CLI-аргументам.

## 12.9. Новый server log создавался на каждый запуск профиля

**Статус: CONFIRMED**

Один `Get-Content -Wait` продолжает читать старый файл после переключения модели. Источник speed/status нужно повторно выбирать после каждого server launch.

## 12.10. Две крупные модели одновременно

**Статус: CONFIRMED для этой машины**

На конфигурации пользователя с Arc B580 12 GB и 32 GB RAM production runner проектировался для последовательной загрузки Gemma и Qwen.

Это локальное аппаратное ограничение старого runner, а не обязательное свойство общей архитектуры.

---

# A. Что обязательно передать новому чату

Максимум 15 пунктов:

1. **Свежий bundle текущего v3.1 run имеет приоритет над этим файлом и старой перепиской.**
2. Фактические версии `pact_translate`, runner, monitor и config нужно читать из bundle/установленной папки, а не угадывать по имени patch.
3. v2 использовал полную revision chunk; от неё отказались из-за разрушительных переписываний, context contamination и слабой наблюдаемости правок.
4. v3.0 использовал `Gemma draft → Qwen detector → Gemma verifier → repair`; это оставляло слепую зону для ошибок, пропущенных Qwen.
5. Reviewer benchmark доказал: Gemma имела низкий recall; Qwen был сильнее, но тоже пропускал ошибки и создавал false positives.
6. `issues.json` менял смысл между версиями; всегда проверять соседние `issues.qwen_raw.json`, `verifier_report.json`, timestamps и version metadata.
7. Старый repair lifecycle ошибочно допускал `keep`, `replace` без изменения и revert к заведомо ошибочному draft после rejected repair.
8. Старый Gemma post-repair gate принял несколько фактически плохих repairs; высокий accept rate не является доказательством качества.
9. `state.json = complete` в старых версиях мог означать только structural integrity, а stale state мог пережить downstream rerun.
10. Проверять regression PID из актуального handoff и подтверждённые старые outcomes из таблицы D этого файла.
11. Не менять текущие Qwen/Gemma profiles только из-за старых sweeps: системная деградация Windows/Vulkan восстанавливалась reboot.
12. MTP был полезен для Gemma Translate, нестабилен на structured repair и не поддерживался использованным Qwen GGUF.
13. Старые monitor percentages имели несколько parser/accounting bugs; текущий симптом Qwen source analysis `0/445` нужно проверять по свежим run-файлам.
14. Любительский EPUB до 7.04 использовался для `parallel_translation_memory.jsonl` и production glossary; это подтверждённый источник старой терминологии.
15. При rerun старые cache/state/output могут быть несовместимы или stale; проверять формат, batch size, timestamps и текущий worker.

---

# B. Что не следует переносить из старой версии

- `audit.fail_open=true` и `minimum_success_rate=0.90` как достаточное условие качества.
- Полную self-revision каждого chunk.
- Предположение, что один reviewer обеспечивает полное обнаружение ошибок.
- Предположение, что candidate verifier выполняет независимый audit.
- Простую политику `repair only critical/major` без учёта объективной category и confidence.
- Простую политику `repair every minor`.
- `keep` как успешное исправление confirmed issue.
- `replace` без проверки фактического изменения и устранения заявленной проблемы.
- Revert к ошибочному draft как финальное решение rejected repair.
- Gemma MTP profile для structured repair.
- Попытку включить Qwen MTP для GGUF без MTP layers.
- Ручные tensor overrides из старых speed sweep как готовый production profile.
- Старые temperature `0.3/0.2` из v2.
- Cached HTML-ответы v2 в JSON-стадиях v3.
- Старый `issues.json` без определения его стадии и schema.
- Старый `state.json`, final HTML или monitor `100%` как доказательство завершения текущего rerun.
- Старые monitor counters как источник истины для v3.1 source analysis.
- Восстановление patch по памяти вместо анализа фактически установленных файлов.

---

# C. Таблица известных файлов и патчей

| Имя | Версия | Назначение | Установлен / только создан | Статус подтверждения |
|---|---:|---|---|---|
| `pact_translate_v2_2_0_4.py` | filename 2.2.0.4 | ранний segmented translator/revision | файл подтверждён; точная активная установка UNKNOWN | UNKNOWN |
| `pact_translate_v2_2_1_0.py` | filename 2.2.1.0 | extra PID handling и inline protection | файл подтверждён; точная активная установка UNKNOWN | UNKNOWN |
| `pact_translate_v2_2_2_0.py` | internal `2.2.0` | conservative revision guards | рабочая итерация подтверждена; install path UNKNOWN | CONFIRMED file / UNKNOWN install |
| `pact_translate_v3_3_0_1.py` | internal `3.0.1` | первая issue-only v3 | создан и проверялся | CONFIRMED |
| `pact_translate_v3_3_0_2.py` | internal `3.0.2` | fixes v3 | установлен как `pact_translate_v3.py` и запускался | CONFIRMED |
| `config.v3.json` | schema v3 | production config base | установлен/копировался в run | CONFIRMED |
| `reviewer_benchmark.py` | early | простой PID-level reviewer benchmark | запускался | CONFIRMED |
| `reviewer_benchmark_2_0_0.py` | `2.0.0` | robust benchmark с retries/checkpoints | запускался | CONFIRMED |
| `reviewer_benchmark_2_2_0.py` | `2.2.0` | strict semantic issue scoring | создан и использовался | CONFIRMED |
| `candidate_verifier_benchmark.py` | `1.0.0` | benchmark `confirm/reject/uncertain` | запускался | CONFIRMED |
| `benchmark_server.py` | UNKNOWN | short speed benchmark | запускался многократно | CONFIRMED |
| `start_llama_v3.ps1` | UNKNOWN | launcher/tuned profile source | существовал и использовался | CONFIRMED |
| `parallel_translation_memory.jsonl` | n/a | EN/RU alignment из human EPUB | создан | CONFIRMED |
| `pact_production_glossary_v1\` | `v1` | production glossary package | установлен | CONFIRMED |
| `validate_glossary.py` | `v1 package` | schema/count validation glossary | запускался до и после install | CONFIRMED |
| `install_production_glossary.ps1` | `v1 package` | установка glossary с backup | запускался | CONFIRMED |
| `run_full_pipeline_v1_0_1.ps1` | `1.0.1` | ранняя 6-stage orchestration | установлен и запускался | CONFIRMED |
| `run_full_pipeline_v1_0_3.ps1` | `1.0.3` | split Gemma profiles, Qwen tuning restore | установлен и запускался | CONFIRMED |
| `prepare_pipeline_context.py` | pre-v3.1 | sanitization/chapter context | установлен в runner | CONFIRMED |
| `verify_pipeline_issues.py` | pre-v3.1 | Gemma candidate verifier | установлен и запускался | CONFIRMED |
| `verify_repair_results.py` | pre-v3.1 | post-repair Gemma safety check | установлен и запускался | CONFIRMED |
| `pact_pipeline_patch_v1_1_0.zip` | `1.1.0` | repair policy/lifecycle, post-repair/report changes | фактически установлен | CONFIRMED |
| `pact_pipeline_hotfix_v1_1_1.zip` | `1.1.1` | GemmaRepair без MTP, batch=1, HTTP diagnostics | фактически установлен | CONFIRMED |
| `monitor_pipeline_v1_0_0.ps1` | `1.0.0` | первый universal monitor | установлен; упал на `$PID` | CONFIRMED |
| `monitor_pipeline_v1_0_1.ps1` | `1.0.1` | первый `$PID` fix | установлен; второй collision остался | CONFIRMED |
| `monitor_pipeline_v1_0_2.ps1` | `1.0.2` | второй `$pid` fix | установлен; затем `.Count` error | CONFIRMED |
| `monitor_pipeline_v1_0_3.ps1` | `1.0.3` | Count/array/stack fixes | установлен и работал | CONFIRMED |
| `monitor_pipeline_v1_1_1.ps1` | `1.1.1` | post-repair counters | установлен; false complete на 5b | CONFIRMED |
| `monitor_pipeline_v1_1_2.ps1` | `1.1.2` | active worker, отдельные 5a/5b | файл создан; установка не подтверждена | UNKNOWN |
| `compare_pipeline_review_v1_0_1.py` | `1.0.1` | visual source/draft/verifier/repair comparison | установлен/использовался collector | CONFIRMED |
| `collect_pipeline_result_v1_0_1.ps1` | `1.0.1` | полный bundle старого pipeline | установлен и использовался | CONFIRMED |
| `reviewer_gemma_v3.json` | result | Gemma reviewer metrics | result-файл | CONFIRMED |
| `reviewer_gemma_b8.json` | result | Gemma batch=8 metrics | result-файл | CONFIRMED |
| `reviewer_qwen36_b8_v2.json` | result | Qwen PID-level metrics | result-файл | CONFIRMED |
| `reviewer_qwen36_b8_strict.json` | result | Qwen strict semantic metrics | result-файл | CONFIRMED |
| `gemma4_verifier_thinking256.json` | result | verifier run с thinking 256 | result-файл | CONFIRMED |
| `qwen36-restored-final-profile.json` | result | restored Qwen performance profile | result-файл | CONFIRMED |
| `qwen_fit_1536.txt` | diagnostic result | `llama-fit-params` tensor overrides | создан; не выбран production winner | CONFIRMED |
| `gemma 26.txt` | diagnostic log | Gemma MTP long-run timing | создан | CONFIRMED |

---

# D. Таблица regression cases

| PID | Исходная проблема | Ожидаемый результат | Версия, где проверялось | Подтверждённый итог |
|---|---|---|---|---|
| `p00020` | потерян курсив на `are` | восстановить source emphasis | v1.0/v1.1 finalization | formatting failure подтверждён |
| `p00026` | rejected repair откатывался к ошибочному draft | retry или unresolved failure; draft не считать исправлением | v1.1 | reject/revert подтверждён; lifecycle не завершал ошибку корректно |
| `p00034` | repair не устранял issue / replace мог быть бессодержательным | meaningful minimal change либо retry | v1.0–v1.1 | формально repaired; качество осталось спорным |
| `p00058` | второе одинаковое `No` потеряло курсив | occurrence-aware restoration | v1.0/v1.1 finalization | formatting failure подтверждён |
| `p00062` | `hard to put down` → `трудно отложить` | смысл `трудно убить/добить` | pre-v3.1 chapter 60 | ошибка оставалась |
| `p00088` | `cut through its knees` → `перерезала ему колени`; confirmed minor meaning | естественная корректировка без выдуманной анатомии | v1.1 | не попал в repair из-за severity policy |
| `p00091` | английский остаток `Mary`; `keep` не исправлял его | `Мэри`, no keep | v1.1 | не устранено последовательно |
| `p00092` | `J.P. Corvidae` частично оставался латиницей | единая русская форма | v1.1 | proposed repair отклонён из-за остававшегося `J.P.` |
| `p00152` | repair создал вариант `пока она подняла стекло` | сохранить исходную временную/причинную связь | v1.1 post-repair | плохой repair принят gate |
| `p00153` | `Volunteering me?` → `Сама вызвалась?`, сменился субъект | передать назначение говорящего добровольцем | v1.1 | плохой repair принят |
| `p00164` | «перевод верен / оставить как есть» оформлено как issue | reject candidate до repair | v1.1 | вошло в repair lifecycle |
| `p00229` | repair удалил `promised` | сохранить обещание отправить | v1.1 | ухудшение принято |
| `p00250` | нарушен параллелизм фразы о госте/хозяине | естественная параллельная конструкция | v1.1 | confirmed minor grammar не repaired |
| `p00254` | register Блэйк→Дункан | единый `ты/вы` по сцене | pre-v3.1 | UNKNOWN |
| `p00256` | register Блэйк→Дункан | единый `ты/вы` по сцене | pre-v3.1 | UNKNOWN |
| `p00267` | `I’m not even with Laird` → `Мы с Лейрдом даже не в расчёте` | «Я даже не на стороне Лейрда» или эквивалент | v1.1 | плохой repair принят |
| `p00273` | gun `draw it` → `вытяни` | `достань/выхвати` по контексту | pre-v3.1 | старый вариант признан неестественным |
| `p00285–p00286` | потерян смысл пары `Barely. / If that.` | `Едва. / И то едва.` или эквивалент | pre-v3.1 | надёжное исправление не подтверждено |
| `p00343` | добавлен отсутствующий `грохот` | убрать addition | pre-v3.1 | addition прошёл старые стадии |
| `p00362` | repair создал грамматически сломанную сравнительную конструкцию | корректный русский без лишнего местоимения | v1.1 | плохой repair принят |
| `p00398` | candidate фактически предлагал оставить текст как есть | reject candidate, не создавать repair issue | pre-v3.1 | надёжно не отфильтрован |
| `p00415` | исправлено одно `ты→вы`, но внутри PID осталось `твои` | единый register внутри всего PID | v1.1 | частичный repair принят |

---

## Заключительная оговорка

**Статус: CONFIRMED как правило работы**

Этот файл не подтверждает:

- фактическую версию кода текущего v3.1 run;
- текущее coverage каждой стадии;
- установку последнего созданного monitor;
- результат текущего перевода главы 60;
- совместимость старых caches с текущей schema;
- применимость старых sampling/runtime-параметров.

Все эти сведения должны устанавливаться по свежему bundle, фактически установленным скриптам и run-файлам.
