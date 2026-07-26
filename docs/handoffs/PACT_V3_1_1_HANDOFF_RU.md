# Pact Ensemble Translator — handoff для нового чата

**Актуальное состояние на 25 июля 2026 года**

Этот документ — рабочий источник контекста для продолжения проекта в новом чате.  
При расхождении с файлами из свежего bundle источником истины считаются **фактически установленные скрипты и результаты текущего run**.

---

## 1. Цель проекта

Создать полностью автоматизированный локальный конвейер перевода книги **Pact** с английского на русский.

Основные требования:

- участие человека не предполагается ни на одном этапе перевода;
- результат должен быть пригоден для автоматического перевода всей книги;
- система должна искать смысловые, языковые и сквозные ошибки, исправлять их и проверять собственные исправления;
- хорошая часть перевода не должна переписываться без необходимости;
- глава не должна считаться готовой при неполном покрытии обязательных проверок или unresolved issues.

Пользователь работает на Windows. Проект расположен здесь:

```text
D:\pact\pact_translator_v3
```

Текущий тест: **глава 60**.

---

## 2. Текущая версия

Установлена архитектура:

```text
Pact Ensemble Translator v3.1
```

Поверх неё установлены:

1. **Pact Pipeline v3.1.1 patch**
   - улучшенный Qwen source analysis;
   - performance preflight;
   - статистика source-analysis.

2. **Monitor speed patch**
   - live generation speed;
   - prompt speed;
   - generation speed;
   - число decoded tokens.

Текущий run:

```text
D:\pact\pact_translator_v3\pipeline_runs\chapter_60_to_60_v31
```

Команда полного запуска:

```powershell
cd D:\pact\pact_translator_v3\pact_full_pipeline_runner_v1

.\run_full_pipeline_v31.ps1 `
  -Start 60 `
  -End 60 `
  -Reset
```

Монитор:

```powershell
.\monitor_pipeline_v31.ps1 `
  -Start 60 `
  -End 60
```

Текущий прогон уже идёт. В новом чате пользователь сначала предоставит либо:

- финальный bundle этого прогона;
- либо ошибку и частичный bundle, если pipeline остановится.

---

## 3. Архитектура v3.1

Концептуальная цепочка:

```text
Gemma preflight
→ подготовка контекста / chapter data
→ Qwen source scene analysis
→ Gemma translation
→ deterministic QA
→ Qwen semantic audit
→ Gemma semantic audit
→ Gemma Russian audit
→ Gemma discourse audit
→ merge + deduplication
→ cross-verification
→ minimal repair
→ Qwen semantic post-gate
→ Gemma semantic post-gate
→ Gemma Russian post-gate
→ deterministic post-gate
→ repair retries
→ residual full-chapter audit
→ final quality gate
→ HTML finalization
```

Ключевой архитектурный принцип:

> Ни одна модель не должна быть единственным источником обнаружения ошибок и одновременно единственным судьёй собственного текста.

### Разделение ролей

**Qwen**

- source scene analysis;
- смысл относительно английского оригинала;
- пропуски и добавления;
- субъект/объект;
- отрицание и модальность;
- идиомы;
- референты;
- semantic audit;
- проверка замечаний, найденных Gemma;
- semantic post-repair gate.

**Gemma Translate**

- литературный перевод;
- текущий основной MTP-профиль;
- минимальные исправления выполняются отдельным стабильным repair-профилем.

**Gemma Verify / audits**

- независимый semantic audit;
- естественность русского;
- грамматика и сочетаемость;
- кальки;
- стиль;
- `ты/вы`;
- связность и discourse;
- проверка замечаний Qwen;
- semantic/Russian post-gates.

---

## 4. Зафиксированные профили моделей

### Qwen

```text
-c 32768
-fit on
-fitt 1280
-b 2048
-ub 512
-ctk q8_0
-ctv q8_0
-t 6
-tb 12
--no-mmap
--reasoning-budget 0
--device Vulkan0
```

Нормальная скорость после перезагрузки Windows:

```text
Prompt speed:      337.44 t/s
Generation speed:   33.83 t/s
Live generation:    33.84 t/s
```

### Gemma Translate

```text
Gemma 4 26B A4B Q4_K_XL
MTP Q8
--spec-type draft-mtp
--spec-draft-n-max 4
-ngl 99
-ncmoe 18
-c 32768
-np 1
-fa on
--no-mmap
--reasoning-budget 0
--device Vulkan0
--cache-ram 0
--ctx-checkpoints 0
```

Переменная окружения:

```powershell
$env:GGML_VK_DISABLE_COOPMAT = "1"
```

После перезагрузки на коротком реальном переводческом запросе:

```text
Prompt:      214.76 t/s
Generation:   34.69 t/s
```

### Gemma Verify

```text
без MTP
-fit on
-fitt 1536
-t 6
-tb 12
-c 32768
--no-mmap
--reasoning-budget 128
--device Vulkan0
```

### Gemma Repair

Отдельный стабильный профиль без MTP:

```text
-fit on
-fitt 1536
-t 6
-tb 12
-c 32768
--no-mmap
--reasoning-budget 0
--device Vulkan0
```

Причина отдельного профиля: ранее MTP давал HTTP 500 на структурированных repair-запросах.

---

## 5. Performance preflight

Перед длинным этапом Gemma выполняется короткий переводческий preflight.

Минимальные допустимые значения:

```text
prompt processing >= 100 t/s
generation        >= 20 t/s
```

Если порог не пройден, pipeline должен остановиться до дорогих этапов.

Результат:

```text
pipeline_runs\chapter_60_to_60_v31\preflight_performance.json
```

Диагностическое отключение:

```powershell
-SkipPreflight
```

В обычном запуске не использовать.

### Обнаруженная системная деградация

После множества запусков моделей за день и переключений профилей одновременно замедлились Gemma и Qwen:

- Gemma падала примерно до `60–85 t/s prompt` и `8–10 t/s generation`;
- после перезагрузки Windows вернулась к `214.76 / 34.69`;
- Qwen после перезагрузки вернулся к `337.44 / 33.83`.

Вывод:

- это было состояние Windows/Vulkan/Intel Arc B580, а не неправильные параметры;
- при неожиданном падении скорости сначала смотреть preflight;
- затем остановить `llama-server`;
- попробовать `Win + Ctrl + Shift + B`;
- при необходимости перезапустить Arc через `pnputil /restart-device` от администратора;
- надёжный вариант — перезагрузка Windows.

Не менять профили моделей только из-за такой деградации.

---

## 6. Изменения source-analysis в v3.1.1

Перед патчем Qwen на пакете из 6 PID несколько раз возвращал обрезанный JSON. Pipeline корректно делил пакет, но терял время. Также один необязательный неправильный `address_update` вызывал повтор всего запроса.

Исправлено:

- базовый batch уменьшен с **6 до 4 PID**;
- лимит ответа увеличен с **1800 до 2400 токенов**;
- JSON сделан компактнее;
- ограничения:
  - `idioms` — до 2;
  - `referents` — до 2;
  - `invariants` — до 3;
  - `forbidden_additions` — до 2;
- пустые необязательные поля не должны выводиться;
- каждый обязательный PID должен присутствовать;
- повреждение обязательного результата вызывает retry/split;
- повреждение необязательных данных не должно повторять весь запрос;
- неполные `address_updates`, включая:
  - `addressee = null`;
  - `expected_register = unknown`;
  - отсутствующий speaker/addressee;
  - неправильный evidence PID;
  просто отбрасываются.

В `source_scene_map.json` добавлена статистика:

```json
{
  "successful_batches": 0,
  "split_batches": 0,
  "model_attempts": 0,
  "failed_attempts": 0,
  "truncated_json": 0,
  "dropped_address_updates": 0,
  "dropped_optional_entries": 0,
  "unexpected_pid_entries": 0,
  "duplicate_pid_entries": 0
}
```

Эти данные также должны попадать в итоговые quality/report-файлы.

---

## 7. Что было исправлено до v3.1

Первоначальный pipeline полностью полагался на Qwen при обнаружении ошибок, а Gemma только проверяла найденное. Это создавало слепую зону: пропущенная Qwen ошибка никогда не попадала в repair.

Также были выявлены проблемы lifecycle:

- подтверждённый `minor` мог не попасть в repair;
- `keep` мог считаться успешным исправлением;
- `replace`, не изменивший текст, мог пройти;
- post-verifier мог отклонить repair и просто вернуть ошибочный draft;
- глава могла получить `complete` при unresolved issues;
- одна Gemma проверяла собственные исправления;
- post-repair verifier пропускал плохой русский.

v3.1 была создана как полноценный ensemble pipeline, а не точечный patch.

---

## 8. Regression PID из предыдущего запуска главы 60

При анализе следующего результата обязательно проверить как минимум:

| PID | Ожидаемая проверка |
|---|---|
| `p00026` | отклонённый repair должен запускать retry, а не возвращать ошибочный draft |
| `p00034` | `replace` без изменения текста запрещён |
| `p00062` | не должен оставаться вариант типа `трудно отложить` в значении `hard to put down/kill` |
| `p00088` | объективный `minor meaning` должен быть обнаружен и исправлен |
| `p00091` | остаток `Mary` должен быть исправлен на `Мэри`; `keep` недопустим |
| `p00152` | не принимать ухудшение вида `пока она подняла стекло` |
| `p00164` | `перевод верен / оставить как есть` не должен становиться repair issue |
| `p00250` | исправление параллелизма должно быть естественным по-русски |
| `p00254` | проверить обращение Блэйка к Дункану |
| `p00256` | проверить обращение Блэйка к Дункану |
| `p00273` | `draw it` о пистолете — естественное `достань`, не `вытяни` |
| `p00285–p00286` | `Barely. / If that.` — сохранить смысл `Едва. / И то едва.` |
| `p00343` | не добавлять отсутствующий в оригинале `грохот` |
| `p00398` | замечание `оставить как есть` не считать подтверждённой ошибкой |

Также проверить сквозную согласованность `ты/вы` по всей сцене, а не только отдельные PID.

---

## 9. Что анализировать после текущего прогона

В новом чате не начинать с нового redesign. Сначала разобрать фактический run.

### Проверка выполнения

- прошёл ли performance preflight;
- полное ли покрытие всех обязательных этапов;
- сколько source-analysis batches:
  - successful;
  - split;
  - failed;
  - truncated;
  - dropped address updates;
- были ли HTTP 500;
- были ли JSON retries;
- были ли singleton failures;
- были ли unresolved issues;
- разрешила ли final quality gate финализацию корректно;
- очищен ли `failures_latest.json` при успехе.

### Проверка ансамбля

- сколько issues нашёл каждый источник:
  - deterministic;
  - Qwen semantic;
  - Gemma semantic;
  - Gemma Russian;
  - Gemma discourse;
  - residual audits;
- пересечение и уникальные находки Qwen/Gemma;
- сколько issues подтвердил cross-verifier;
- сколько было оспорено;
- сколько repairs приняли/отклонили разные gates;
- сколько было retry rounds;
- не соглашались ли gates автоматически со всем.

### Проверка качества

- сравнить:
  - source;
  - draft;
  - repaired/final;
- отдельно просмотреть regression PID;
- найти принятые repair, которые ухудшили русский;
- найти исходные ошибки, пропущенные всеми аудитами;
- проверить:
  - идиомы;
  - субъект/объект;
  - модальность;
  - говорящего и адресата;
  - `ты/вы`;
  - имена;
  - глоссарий;
  - добавленные детали;
  - грамматику и сочетаемость;
  - связность между абзацами.

---

## 10. Monitor v3.1

Текущий монитор показывает:

```text
Active profile
Prompt speed
Generation speed
Live generation
Decoded tokens
```

Он читает актуальный `llama-server` stderr log.

Отдельный просмотр текущего лога:

```powershell
$logDir = "D:\pact\pact_translator_v3\pipeline_runs\chapter_60_to_60_v31\server_logs"

$log = Get-ChildItem $logDir -File -Filter "*_stderr.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

Get-Content -LiteralPath $log.FullName -Tail 100 -Wait
```

При переключении профиля создаётся новый лог. Старый `Get-Content -Wait` не переключается автоматически.

---

## 11. Важные правила работы над проектом

1. При большом количестве правок сначала составлять внутренний план из маленьких шагов.
2. После каждого шага выполнять проверку.
3. Не отправлять пустые промежуточные обещания о прогрессе.
4. Источником истины считать свежий bundle, а не воспоминания о patch.
5. Не менять уже протюнингованные параметры моделей без отдельного A/B-теста.
6. Не запускать всю книгу, пока глава 60 не пройдёт полный quality review.
7. Не полагаться только на формальный статус `complete`.
8. Не считать `accepted by model` доказательством качества без анализа текста.
9. ZIP-патчи упаковывать без двойной вложенности.

### ZIP-структура

Ранее архив распаковывался так:

```text
Downloads\pact_pipeline_v3_1_1_patch\
    pact_pipeline_v3_1_1_patch\
        install_patch.ps1
```

В будущих инструкциях:

- либо указывать две папки;
- либо упаковывать содержимое внутренней папки, чтобы установщик находился сразу на первом уровне.

---

## 12. Что пользователь даст в новом чате

Один из вариантов.

### Если run завершился

- ZIP, созданный `collect_v31_handoff_bundle.ps1`;
- этот handoff-файл;
- при необходимости скриншот финального монитора;
- субъективно замеченные странные места.

### Если run упал

- точный текст ошибки;
- последние 100–300 строк pipeline log;
- актуальный `llama-server` stderr;
- partial bundle из того же сборщика;
- этот handoff-файл.

Новый чат должен сначала прочитать handoff и bundle, затем сообщить:

1. какую фактическую версию кода видит;
2. на каком этапе остановился или завершился run;
3. какие данные полные, а какие отсутствуют;
4. план анализа небольшими шагами.

---

## 13. Начальная команда для нового чата

Готовый текст находится в отдельном файле `NEW_CHAT_STARTER_RU.txt`.
