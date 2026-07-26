# Pact Ensemble Translator — handoff для отдельного чата аудита главы 60

**Назначение документа:** передать новому чату задачу по анализу фактического результата текущего прогона главы 60 после его полного завершения.

**Важно:** этот чат должен заниматься только run-аудитом и качеством перевода. Техническая доработка pipeline, исправление скриптов, monitor, glossary и архитектурные изменения выполняются в другом чате.

---

## 1. Общая цель проекта

Проект создаёт полностью автоматизированный локальный конвейер перевода книги **Pact** с английского на русский.

Основные требования:

- участие человека не предполагается ни на одном этапе production-перевода;
- глава не должна считаться готовой при неполном покрытии обязательных проверок;
- хорошие фрагменты не должны переписываться без необходимости;
- обнаруженные смысловые, языковые и сквозные ошибки должны проходить полный lifecycle:
  detection → verification → repair/challenge → post-repair gates → residual audit → final quality gate;
- формальный `complete` не считается достаточным доказательством качества;
- отдельно проверяется фактический финальный русский текст и финальный HTML.

Текущий тестовый объект:

```text
Глава 60
```

Путь проекта на компьютере пользователя:

```text
D:\pact\pact_translator_v3
```

Текущий run:

```text
D:\pact\pact_translator_v3\pipeline_runs\chapter_60_to_60_v31
```

---

## 2. Фактическая версия системы

Текущая установленная система:

```text
Pact Ensemble Translator v3.1
runner / ensemble scripts: 3.1.1
core pact_translate_v3.py: 3.1.0
```

Поверх v3.1 установлены:

1. **Pact Pipeline v3.1.1 patch**
   - улучшенный Qwen source analysis;
   - performance preflight;
   - source-analysis statistics.

2. **Monitor speed patch**
   - live generation speed;
   - prompt speed;
   - generation speed;
   - decoded tokens.

При расхождении между этим handoff и свежим bundle источником истины являются:

1. фактически установленные скрипты в bundle;
2. run-файлы и логи;
3. итоговый generated config;
4. этот handoff;
5. исторические документы и старая переписка.

---

## 3. Фактическая архитектура pipeline

Концептуальная последовательность:

```text
Gemma performance preflight
→ подготовка manifest / source / chapter bible
→ Qwen source-only scene analysis
→ Gemma draft translation
→ deterministic QA
→ Qwen semantic audit
→ Gemma semantic audit
→ Gemma Russian audit
→ Gemma discourse audit
→ merge + deduplication
→ cross-verification
→ Gemma repair candidates
→ Qwen semantic post-gate
→ Gemma semantic post-gate
→ Gemma Russian post-gate
→ deterministic post-gate
→ repair retries
→ residual full quality pass
→ final coverage / lifecycle / deterministic quality gate
→ formatting restoration
→ HTML finalization
```

Ключевой принцип:

> Ни одна модель не должна одновременно быть единственным detector и единственным судьёй собственного текста.

---

## 4. Роли моделей

### Qwen

Используется для:

- source scene analysis;
- semantic audit относительно английского оригинала;
- проверки смысловых issues, найденных Gemma;
- semantic post-repair gate;
- проверки пропусков, добавлений, субъекта/объекта, модальности, референтов, идиом и register.

### Gemma Translate

Используется для:

- chapter bible;
- draft translation;
- formatting restoration;
- HTML finalization.

### Gemma Verify

Используется для:

- независимого semantic audit;
- Russian audit;
- discourse audit;
- cross-verification issues Qwen;
- semantic/Russian post-gates.

### Gemma Repair

Используется только для создания минимальных repair-кандидатов.

---

## 5. Что пользователь приложит к новому чату

После полного завершения текущего run пользователь должен приложить:

1. финальный ZIP, созданный актуальным `collect_v31_handoff_bundle.ps1`;
2. этот handoff;
3. основной актуальный handoff `PACT_V3_1_1_HANDOFF_RU.md`;
4. при необходимости исторический `PACT_PRE_V3_HISTORY_ADDENDUM_RU.md`;
5. субъективно замеченные странные места, если они есть;
6. при падении — точную ошибку и partial bundle вместо финального.

Не использовать промежуточный bundle, снятый во время Qwen semantic audit, как итоговый источник качества.

---

## 6. Главная задача нового чата

Провести полный аудит фактического run главы 60, не перепроектируя pipeline и не меняя скрипты.

Сначала установить:

1. точную версию кода и generated config;
2. завершился ли run или остановился;
3. последнюю реально завершённую стадию;
4. какие данные полные, а какие отсутствуют;
5. полное ли PID coverage;
6. есть ли stale, incompatible или частичные artifacts;
7. корректно ли final quality gate разрешил или запретил финализацию.

Только после этого переходить к анализу качества перевода.

---

## 7. Обязательный порядок анализа

### Шаг 1. Инвентаризация bundle

Проверить:

- версии runner и Python-скриптов;
- generated config;
- список work/output/report файлов;
- timestamps;
- state и failure files;
- фактическую последовательность завершённых стадий.

Вывод шага:

```text
Фактическая версия:
Фактический статус:
Последняя завершённая стадия:
Полные данные:
Неполные/отсутствующие данные:
```

### Шаг 2. Coverage и целостность

Проверить exact PID coverage для:

- manifest/source;
- source analysis;
- draft translation;
- каждого primary audit;
- merge;
- cross-verification;
- repair lifecycle;
- каждого residual audit;
- final translations;
- final HTML.

Не считать синтаксически корректный JSON доказательством полного coverage.

### Шаг 3. Failures и retries

Проверить:

- HTTP 500;
- invalid/truncated JSON;
- batch retries;
- recursive splits;
- singleton failures;
- repair retries;
- unresolved issues;
- `failures_latest.json`;
- stale error files после успешного завершения.

### Шаг 4. Ensemble accounting

Подсчитать отдельно:

- deterministic issues;
- Qwen semantic findings;
- Gemma semantic findings;
- Gemma Russian findings;
- Gemma discourse findings;
- residual findings;
- уникальные находки каждой model family;
- пересечения;
- merged issues;
- preverified agreement;
- cross-verifier repair/keep/uncertain;
- challenge outcomes;
- accepted/rejected repair candidates;
- число retry rounds.

### Шаг 5. Lifecycle correctness

Для каждой verified issue проверить:

```text
detected
→ merged
→ verified
→ repair/challenge
→ gate decisions
→ retry при необходимости
→ resolved_repair или resolved_false_positive
```

Искать:

- `keep` как ложный успех;
- `replace` без реального изменения;
- repair, не устранивший заявленную проблему;
- rejected repair, после которого вернулся ошибочный draft;
- issue, исчезнувшую из accounting;
- issue, ошибочно считающуюся unresolved;
- false positive, ошибочно считающийся реальной исправленной ошибкой.

### Шаг 6. Текстовый аудит

Для relevant PID сравнивать:

```text
English source
→ draft translation
→ primary repaired text
→ residual/final text
→ final formatted HTML
```

Не полагаться на `accepted=true`, `verdict=accept` или `complete`.

Проверять:

- пропуски;
- добавления;
- субъект/объект;
- отрицание;
- модальность;
- время и причинность;
- идиомы;
- говорящего и адресата;
- референты;
- имена;
- glossary consistency;
- `ты/вы`;
- grammar;
- collocation;
- кальки;
- естественность диалогов;
- межабзацную связность;
- ухудшения, внесённые repair;
- formatting incidents.

### Шаг 7. Финальное заключение

Разделить выводы на:

- pipeline execution defects;
- accounting/lifecycle defects;
- detector misses;
- false positives;
- bad repairs;
- final-text defects;
- formatting defects;
- спорные, но не доказанные места.

---

## 8. Обязательные regression PID

Минимальный основной список:

| PID | Что проверить |
|---|---|
| `p00026` | отклонённый repair должен запускать retry, а не возвращать ошибочный draft |
| `p00034` | `replace` без реального содержательного исправления запрещён |
| `p00062` | `hard to put down` не должно остаться как `трудно отложить` |
| `p00088` | объективный minor meaning должен быть обнаружен и исправлен |
| `p00091` | `Mary` должно стать `Мэри`; `keep` недопустим |
| `p00152` | не принимать ухудшение типа `пока она подняла стекло` |
| `p00164` | `перевод верен / оставить как есть` не должен становиться repair issue |
| `p00250` | параллелизм должен быть исправлен естественно по-русски |
| `p00254` | проверить обращение Блэйка к Дункану |
| `p00256` | проверить обращение Блэйка к Дункану |
| `p00273` | `draw it` о пистолете — `достань/выхвати`, не `вытяни` |
| `p00285–p00286` | `Barely. / If that.` — сохранить усиление второго ответа |
| `p00343` | не добавлять отсутствующий в оригинале `грохот` |
| `p00398` | `оставить как есть` не считать подтверждённой ошибкой |

Расширенный исторический список:

| PID | Что проверить |
|---|---|
| `p00020` | восстановлен ли курсив на соответствующем span |
| `p00058` | корректно ли восстановлен второй одинаковый курсивный `No` |
| `p00092` | единая русская форма `J.P. Corvidae` |
| `p00153` | `Volunteering me?` — не менять субъект |
| `p00229` | не потерян смысл `promised` |
| `p00267` | `I’m not even with Laird` — корректно передана сторона/принадлежность |
| `p00362` | не принята сломанная сравнительная конструкция |
| `p00415` | единый `ты/вы` внутри PID и сцены |

Кроме отдельных PID обязательно проверить сквозную согласованность `ты/вы` по сцене.

---

## 9. Известные исторические риски

Новый чат должен помнить, но не предполагать автоматически, что они повторились:

- один reviewer пропускал реальные ошибки;
- reviewer мог придумывать ошибку;
- confirmed minor не попадал в repair;
- `keep` считался успешным решением;
- `replace` мог не менять текст;
- post-repair gate мог принять ухудшение;
- rejected repair мог вернуть ошибочный draft;
- `state.json=complete` мог раньше означать только structural integrity;
- final HTML мог терять inline formatting;
- stale state/output могли пережить rerun;
- старый `issues.json` менял значение между стадиями.

Каждый такой риск нужно подтверждать фактическими файлами текущего run.

---

## 10. Что не делать в новом чате

- Не проектировать новую архитектуру pipeline.
- Не писать patches и не менять скрипты.
- Не менять параметры Qwen/Gemma.
- Не предлагать новый glossary workflow.
- Не исправлять monitor.
- Не считать историческую переписку источником истины.
- Не анализировать только aggregate JSON без чтения текста.
- Не считать высокий gate accept rate доказательством качества.
- Не считать `complete` достаточным доказательством готовности.
- Не запускать перевод заново без установленной точной причины.
- Не смешивать результаты текущего финального bundle с промежуточным snapshot.

Технические исправления после аудита будут реализованы в другом чате.

---

## 11. Формат первого ответа нового чата

После чтения финального bundle сначала ответить только:

```text
1. Фактическая версия кода.
2. Статус run и последняя завершённая стадия.
3. Какие данные полные.
4. Какие данные отсутствуют, частичны или потенциально stale.
5. Есть ли немедленная причина не доверять статусу complete.
6. Небольшой пошаговый план дальнейшего анализа.
```

Не начинать сразу длинный литературный разбор до завершения технической инвентаризации.

---

## 12. Ожидаемый итоговый результат аудита

Новый чат должен подготовить:

1. **Execution report**
   - версии;
   - стадии;
   - coverage;
   - retries/failures;
   - artifact integrity.

2. **Ensemble report**
   - findings по источникам;
   - merge;
   - cross-verification;
   - repairs;
   - gates;
   - residual pass;
   - lifecycle.

3. **Regression report**
   - таблица всех обязательных PID;
   - source/draft/final;
   - ожидаемый результат;
   - фактический результат;
   - pass/fail/uncertain.

4. **Translation quality report**
   - доказанные пропуски;
   - false positives;
   - bad repairs;
   - финальные языковые дефекты;
   - `ты/вы`;
   - formatting.

5. **Final verdict**
   - можно ли считать главу 60 принятой;
   - требуется ли targeted rerun;
   - какие defects должны быть переданы техническому чату.

---

## 13. Разделение ответственности между чатами

### Этот будущий чат

```text
Аудит run главы 60
Анализ translation quality
Regression PID
Final HTML
Execution/accounting defects
```

### Технический чат

```text
Аудит исходного кода
Сопоставление ревью ChatGPT и Claude
Исправление monitor
Исправление glossary accumulation
Исправление validators / lifecycle / gates
Создание patches и regression tests
```

Не смешивать эти две задачи.
