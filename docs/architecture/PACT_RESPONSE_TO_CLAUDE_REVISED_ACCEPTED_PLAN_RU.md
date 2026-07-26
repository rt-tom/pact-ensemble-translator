# Pact Ensemble Translator — пересмотренный ответ Claude и согласованный план развития

**Статус документа:** согласованный рабочий план после независимого аудита Claude и повторного архитектурного анализа ChatGPT.  
**Адресат:** Claude, для независимой проверки решений и дальнейшего ревью кода.  
**Приоритет проекта:**  
1. качество перевода;  
2. автоматическая надёжность без участия человека;  
3. скорость выполнения.

---

# 1. Контекст проекта

Pact Ensemble Translator — полностью локальный автоматический конвейер художественного перевода книги **Pact** с английского на русский.

Ограничения:

- человек не участвует в runtime-цепочке;
- используется локальное железо пользователя;
- одновременно практично держать только одну крупную модель;
- модели переключаются через `llama-server`;
- основной translator и большинство русских ролей выполняет Gemma;
- независимую смысловую ось представляет Qwen;
- качество важнее скорости, но архитектура не должна тратить вычисления без измеримой пользы.

Текущая production-линия до исправлений:

```text
runner / v31 ensemble scripts: 3.1.1
core pact_translate_v3.py:     3.1.0
```

Текущий тестовый run главы 60 был остановлен во время:

```text
primary Gemma Russian audit
```

Остановка была сделана до merge/cross-verification/repair, поскольку аудит Claude обнаружил критические ошибки в последующих стадиях.

---

# 2. Приоритет источников истины

При проверке этого документа использовать следующий порядок:

1. фактически установленные active-файлы;
2. generated config и run artifacts;
3. patch packages, manifests и installers;
4. marker-файлы и backups;
5. этот документ;
6. предыдущие handoff и переписка.

Успешное сообщение установщика само по себе не является доказательством, что active path действительно изменён.

---

# 3. Стратегическое решение

Принято разделить развитие системы на два направления.

## v3.1.2 / v3.1.3

Цель:

- безопасно завершить текущий run главы 60;
- закрыть доказанные integrity-дефекты;
- получить надёжный baseline;
- не превращать v3 в бесконечно усложняемую платформу.

## v4.0

Цель:

- построить новую архитектуру вокруг качества генерации;
- использовать постоянную память книги;
- создавать дополнительные варианты только для рискованных мест;
- отбирать кандидаты каскадно;
- заменить фиксированный residual pass сходимостью;
- минимизировать число model reloads;
- проверять все решения на golden benchmark.

Главный принцип:

> v3 стабилизируется и измеряется.  
> v4 создаётся отдельной веткой и заменяет v3 только после доказанного выигрыша.

---

# 4. Что исправлено в v3.1.2 Emergency Resume

## 4.1. CLD-001 — опасный merge по пересечению `target_span`

### Проблема

Две разные issues могли объединяться, если их spans пересекались. После этого:

```text
Qwen finding + Gemma finding
→ detector families = {qwen, gemma}
→ independent_detector_agreement
→ decision=repair без judge
```

При этом:

- категории могли различаться;
- required invariant одной issue терялся;
- совпадение места ошибочно считалось совпадением проблемы.

### Принятое исправление

1. Разные категории не объединяются только из-за overlap.
2. Пересечение span — вспомогательный сигнал, а не критерий эквивалентности.
3. Independent agreement разрешён только при точном semantic fingerprint.
4. Исходные findings и requirements сохраняются.
5. Старый self-test, закреплявший опасное поведение, изменён.

Принцип:

```text
совпадение места ≠ совпадение проблемы
```

---

## 4.2. CLD-002 — `mixed_script` был объявлен blocking, но не работал

### Проблема

Категория присутствовала в hard/final lists, но active deterministic detector её не создавал.

Не ловились, например:

```text
Mary кивнула.
Mэри посмотрела на него.
J.P. Корвидэ
```

Это напрямую относилось к regression `p00091`.

### Принятое исправление

Добавлен deterministic detector для:

- одиночных латинских слов;
- латинских имён;
- смешанных Cyrillic/Latin tokens;
- латинских инициалов;
- одиночной латиницы, не попадающей в sentence-level `english_residue`.

Исключения:

- URL;
- email;
- explicit allowlist.

Категория остаётся blocking:

```text
detection
→ hard deterministic route
→ repair required
→ final deterministic verification
```

---

## 4.3. CLD-009 — hard deterministic precedence

### Проблема

Если hard deterministic issue также находила модель, merge мог понизить её до обычной model issue и отправить судье.

### Исправление

Категории:

```text
missing
mixed_script
```

сохраняют hard route независимо от model overlap.

Model judge не может отменить объективный deterministic invariant.

---

## 4.4. CLD-003 — `uncertain` создавал детерминированный тупик

### Проблема

Даже:

```text
repair/high + repair/medium
```

считалось disagreement и останавливало run.

При temperature 0 перезапуск воспроизводил тот же результат.

### Принятое исправление

Если решения совпадают:

```text
repair/high + repair/medium → repair/medium
keep/high   + keep/medium   → keep/medium
```

Если решения действительно различаются:

```text
repair vs keep
repair vs uncertain
keep vs uncertain
```

применяется:

```json
{
  "uncertain_policy": "repair"
}
```

Это не означает автоматическое принятие правки.

Issue:

1. маркируется как policy-resolved;
2. отправляется в repair;
3. candidate проходит:
   - Qwen semantic gate;
   - Gemma semantic gate;
   - Gemma Russian gate;
   - deterministic gate.

Не принято:

- искусственно повышать confidence;
- молча выбирать `keep`;
- считать простое большинство достаточным.

---

## 4.5. Дополнительная находка ChatGPT — numeric equivalence

### Проблема

Translation validation допускала:

```text
3 → три
```

Repair validation требовала буквального символа `3`.

Корректный PID мог стать неисправимым по посторонней issue.

### Исправление

Repair validation должна:

- принимать корректные словесные эквиваленты;
- блокировать потерю числа;
- блокировать `3 → 4`;
- блокировать добавление нового числа.

Claude отдельно предлагается проверить:

- compound numbers;
- повторяющиеся числа;
- склоняемые словесные формы;
- порядок нескольких значений.

---

# 5. Операционные hotfix после v3.1.2

## 5.1. v3.1.2a

Убрана зависимость от:

```powershell
ConvertFrom-Json -AsHashtable
```

JSON читается как `PSCustomObject` и рекурсивно конвертируется в Hashtable.

## 5.2. v3.1.2b

Исправлен faulty installer validation:

- v3.1.2a искал запрещённую строку обычным text search;
- строка находилась в комментарии;
- исправный runner ошибочно отклонялся.

Новая проверка использует PowerShell AST.

## 5.3. v3.1.2c

Обнаружена несовместимость UTF-8 BOM:

```text
config.full_pipeline.v31.json
→ JSONDecodeError: Unexpected UTF-8 BOM
```

Плановое исправление:

- BOM-free JSON writer;
- `utf-8-sig` reader.

Повторный traceback показал, что active core всё ещё использовал plain `utf-8`.

## 5.4. v3.1.2d

Подготовлен content-based repair hotfix:

- патчит фактически установленный core;
- меняет `read_json()` на `utf-8-sig`;
- переводит runner JSON writers на UTF-8 без BOM;
- снимает BOM с существующего generated config;
- сохраняет translation/audit caches;
- запускает Python compilation и реальный JSON parse.

**Статус должен подтверждаться по фактическому active-файлу и успешному resume, а не по installer message.**

---

# 6. Пересмотренный объём v3.1.3

Рабочее название:

```text
v3.1.3 Quality Integrity
```

v3.1.3 не должна становиться новой архитектурой. Это последний крупный integrity-релиз v3.

В неё включаются только изменения, необходимые для:

- корректного `complete`;
- безопасного resume;
- совместимых caches;
- воспроизводимого baseline;
- будущего аудита главы 60.

---

## 6.1. Blocking formatting integrity

Принято из CLD-006.

Требования:

- каждый unresolved inline span создаёт incident;
- полный failure formatting batch создаёт incidents для всех затронутых spans;
- `formatting.required=true`;
- production default:

```text
max_formatting_incidents = 0
```

- unresolved required span блокирует `complete`;
- regression:
  - `p00020`;
  - `p00058`.

---

## 6.2. Final post-residual verification

Принято из CLD-005, но не как третий полный residual pass.

После residual repair:

1. собрать `changed_pids`;
2. расширить на affected neighborhoods;
3. повторить:
   - semantic check изменённых PID;
   - Russian check изменённых PID;
   - discourse check пересекающихся windows;
   - deterministic check полного final текста;
4. записать отдельное coverage;
5. блокировать `complete`, если final text не проверен.

После targeted checks должен выполняться один **глобальный smoke audit неподвижной главы**:

- Qwen: смысловая целостность;
- Gemma: русская/discourse целостность;
- deterministic: структура и blocking invariants.

Это не новый repair cycle.

---

## 6.3. Dependency-aware redo/resume

Принято из CLD-004.

Нужен artifact dependency DAG.

Например:

```text
source analysis changed
→ translation invalid
→ audits invalid
→ merge invalid
→ verification invalid
→ repair invalid
→ final/output invalid
```

Runner:

- автоматически инвалидирует downstream;
- либо явно запрещает небезопасную комбинацию.

---

## 6.4. Cache identity

Принято из CLD-014.

Каждый authoritative cache должен содержать:

```text
schema_version
producer_version
stage
source hash
input translation hash
model/profile ID
prompt version
config subset hash
complete flag
```

Reuse допускается только при совпадении required identity.

При несовпадении:

```text
cache_miss_with_reason
```

---

## 6.5. Atomic authoritative writes

Принято из CLD-016.

Все authoritative JSON/HTML:

```text
temp
→ flush/fsync
→ atomic replace
```

Никаких прямых partial writes.

---

## 6.6. Единый chapter resolver

Принято из CLD-007.

- один canonical ordered chapter list;
- один manifest;
- единая интерпретация `Start/End`;
- source hashes в manifest;
- PowerShell и Python не выбирают главы независимо.

---

## 6.7. Monitor correctness

Принято из CLD-010 и отдельной фактической ошибки.

Исправить:

- incremental source-analysis progress по `batch_*.json`;
- stale `complete`;
- приоритет active worker/current stage;
- consistency:
  - state;
  - final gate;
  - output hash;
  - process status;
- mixed-version run provenance.

---

## 6.8. Safe glossary bridge

Полная память книги относится к v4.

В v3.1.3 нужен только append-only candidate ledger:

```text
chapter
PID
source phrase
proposed target
final target
model/source
confidence
conflicts
producer version
```

Он:

- сохраняет знания между главами;
- ничего автоматически не закрепляет;
- не изменяет production glossary молча;
- станет входом для v4 promotion system.

---

## 6.9. Robust patch/install framework

Добавлено ChatGPT после цепочки a/b/c/d.

Требования:

- declarative manifest;
- exact target discovery;
- active-path verification;
- transactional install;
- post-install runtime smoke test;
- rollback;
- marker ↔ installed hash validation;
- отсутствие двойной ZIP-вложенности;
- version lineage.

---

## 6.10. Что не включать в v3.1.3

Если не требуется для integrity baseline, не переносить:

- третью модель;
- adaptive candidates;
- сложный finding graph;
- convergence engine;
- full persistent translation memory;
- risk-based routing;
- новый model scheduler;
- сложную reliability calibration.

Это задачи v4.

---

# 7. Принятая архитектура v4.0

## 7.1. Основные цели

v4 создаётся вокруг принципа:

> Качество выгоднее создавать на этапе контекстной генерации и отбора, чем пытаться получить его повторением всё более сложных аудитов одного draft.

Приоритеты:

1. смысловая верность и полнота;
2. сквозная согласованность книги и сцены;
3. естественный литературный русский;
4. структура и форматирование;
5. скорость.

Порядок лексикографический: хороший стиль не компенсирует смысловую ошибку.

---

# 8. Полная v4 pipeline architecture

## 8.1. Immutable source manifest

В начале главы создаются:

```text
chapter ID
source hash
stable PID
source text
HTML structure
inline spans
scene/chunk mapping
```

Manifest не меняется в течение run.

---

## 8.2. Frozen book-memory snapshot

Перед переводом фиксируется snapshot:

```text
authoritative glossary
provisional glossary
characters/entities
facts/relationships
address register
character voice notes
translation memory
regression memory
false-positive memory
```

Все artifacts ссылаются на hash snapshot.

Изменения общей памяти во время главы не влияют на текущий run.

---

## 8.3. Source-only analysis and risk assessment

Qwen анализирует оригинал до перевода:

- plain meaning;
- subject/object;
- negation;
- modality;
- time/causality;
- idioms;
- referents;
- speaker/addressee;
- `ты/вы`;
- entities;
- numbers;
- forbidden additions;
- local risk features.

Детерминированный слой извлекает:

- numbers;
- known names/terms;
- inline spans;
- dialogue structure;
- structural invariants.

---

## 8.4. Scene/chunk generation

Кандидаты создаются не изолированными PID, а связанными сценами/chunks:

```text
8–20 связанных PID
+ предыдущий контекст
+ ограниченный следующий контекст
+ memory snapshot
+ source invariants
```

Модель возвращает PID-map.

Это сохраняет:

- voice;
- references;
- register;
- dialogue flow;
- scene rhythm;
- `ты/вы`.

---

## 8.5. Progressive role-based candidates

### Low risk

```text
1 candidate
```

### Medium/high risk

```text
Candidate A — balanced literary translation
Candidate B — fidelity-first translation
```

### При доказанном disagreement

```text
Candidate C — resolve exact disagreement between A and B
```

Candidate C — targeted resolution, а не ещё один случайный sample.

Не принято:

- три кандидата для всей главы;
- универсальная высокая temperature;
- diversity ради diversity.

Temperature и candidate count определяются benchmark.

---

## 8.6. Disagreement as risk signal

Сравниваются:

- semantic choices;
- terminology;
- referents;
- idiom interpretation;
- length;
- register;
- syntax.

Disagreement:

- повышает risk;
- запускает Candidate C или усиленный audit;
- не считается доказательством ошибки.

Одинаковые candidates тоже не считаются доказательством правильности.

---

## 8.7. Cascaded candidate selection

Нельзя использовать единую weighted score, где стиль компенсирует смысл.

Порядок:

### Stage 1 — semantic qualification

Qwen проверяет:

- completeness;
- additions;
- subject/object;
- negation;
- modality;
- referents;
- idioms;
- numbers;
- source invariants.

Кандидат с semantic failure исключается.

### Stage 2 — book consistency qualification

Проверяется:

- glossary;
- names;
- `ты/вы`;
- facts;
- character relations;
- voice memory;
- mixed script;
- structural invariants.

### Stage 3 — Russian literary selection

Gemma Russian evaluator не видит оригинал и выбирает среди прошедших:

- naturalness;
- grammar;
- collocations;
- dialogue;
- rhythm;
- voice;
- absence of calque.

Принцип:

```text
semantic validity — обязательна
book consistency — обязательна
из прошедших выбирается лучший русский
```

Если никто не прошёл, система не выбирает «наименее плохой» вариант, а запускает targeted synthesis/repair.

---

## 8.8. Full assembled-chapter audit

После выбора кандидатов собирается полный draft главы.

Выполняются:

### Qwen semantic audit

- source vs full translation;
- local meaning;
- cross-PID references;
- omissions/additions;
- scene semantics.

### Gemma Russian/discourse audit

Без оригинала:

- natural Russian;
- voice;
- register;
- repetition;
- dialogue flow;
- discourse;
- `ты/вы`.

### Deterministic audit

- PID coverage;
- missing;
- numbers;
- mixed script;
- glossary;
- names;
- formatting contracts;
- HTML structure.

Не каждый простой PID проходит одинаково дорогой набор до этого этапа.

---

## 8.9. Immutable findings and explicit clusters

Каждая raw finding неизменяема:

```json
{
  "finding_id": "...",
  "detector": "...",
  "pid": "...",
  "category": "...",
  "problem": "...",
  "required_invariant": "...",
  "target_span": "..."
}
```

Cluster:

- связывает предположительно эквивалентные findings;
- не уничтожает их;
- не перезаписывает requirements;
- не превращает span overlap в automatic agreement.

Independent agreement разрешён только при совпадении:

- category;
- semantic fingerprint;
- required invariant;
- scope.

---

## 8.10. Region-level repair

Все подтверждённые требования региона собираются вместе:

```text
исправить X
сохранить Y
не добавлять Z
сохранить register
не менять соседнюю реплику
```

Gemma Repair создаёт:

- minimal repair;
- optional full-sentence rewrite, если локальный repair невозможен.

`challenge_issue` сохраняется.

---

## 8.11. Simplified independent gates

Базовый repair gate:

1. Qwen semantic gate;
2. Gemma Russian gate;
3. deterministic gate.

Gemma semantic gate может использоваться для high-risk escalation, но не считается полноценной независимой model family.

Третья модель:

- optional;
- только disputed/high-risk;
- только после benchmark.

---

## 8.12. Convergence instead of fixed residual

После accepted repair:

1. определить changed PID;
2. добавить соседние PID и discourse windows;
3. повторить только релевантные проверки;
4. остановиться при отсутствии blocking findings;
5. максимум 2–3 repair rounds.

После лимита:

```text
quarantined
```

а не silent complete и не бесконечный цикл.

---

## 8.13. Final global smoke audit

После завершения convergence и до formatting:

- Qwen проверяет полную смысловую целостность;
- Gemma проверяет полную русскую/discourse целостность;
- deterministic layer проверяет всю главу.

Это проверка неподвижного результата, а не новый repair cycle.

---

## 8.14. Translation-time formatting contract

Во время generation каждый source inline span получает mapping:

```json
{
  "span_id": "em02",
  "translated_text": "...",
  "occurrence": 2
}
```

Код проверяет:

- substring существует;
- occurrence однозначен;
- spans не конфликтуют;
- все required spans mapped.

Основной путь детерминированный.

Fallback:

1. exact alignment;
2. occurrence-aware alignment;
3. fuzzy alignment;
4. model fallback.

Unresolved required span блокирует `complete`.

---

## 8.15. Final states

Приняты:

```text
complete
quarantined
failed
```

### complete

Все blocking инварианты пройдены.

### quarantined

Целостный текст создан, но автоматическая система не смогла его принять.

Quarantine может автоматически запустить:

- extra candidate;
- larger context;
- third-model arbitration;
- enhanced audit.

Глава не включается в production book, пока quarantine не разрешён автоматически.

### failed

Технически не создан целостный результат.

---

## 8.16. Publish memory only after accepted chapter

Общая book memory обновляется только после `complete`.

При `quarantined` observations могут сохраняться отдельно, но не становятся authoritative.

Состояния памяти:

```text
observed
provisional
cross_chapter_supported
established
locked
conflicted
deprecated
```

Automatic promotion консервативен.

Conflict никогда молча не заменяет established/locked.

---

# 9. Что сохраняется из v3

Без изменений по смыслу:

- source-only analysis;
- Russian-only audit без оригинала;
- `challenge_issue`;
- deterministic gates;
- stable PID;
- resumability;
- frozen memory during run;
- atomic artifacts;
- independent Qwen semantic role.

---

# 10. Что отклонено или пересмотрено из идей Claude

## 10.1. Немедленная замена v3 на v4

Отклонено.

Сначала:

```text
finish chapter 60
→ forensic audit
→ golden baseline
→ v4 shadow/A-B
```

---

## 10.2. Три candidates с temperature 0.7–0.9 для всех

Отклонено.

Используются:

- adaptive count;
- role-based prompts;
- benchmarked temperature;
- targeted Candidate C.

---

## 10.3. Одна загрузка каждой модели

Пересмотрено.

Causal dependencies не позволяют буквально одну загрузку.

Реалистичная цель:

```text
5–8 крупных model sessions
```

через model-centric batching.

---

## 10.4. Обязательная третья 12–14B модель

Пересмотрено.

Только optional arbiter для disputed/high-risk cases после доказанного выигрыша.

---

## 10.5. Обязательный человек в review queue

Отклонено как runtime requirement.

Ranked review queue может существовать как optional diagnostic output, но production остаётся полностью автоматическим.

---

## 10.6. Немедленно убрать post-generation audits

Отклонено до ablation tests.

Russian-only audit, source analysis, independent semantic checks и challenge остаются, пока измерения не докажут избыточность.

---

# 11. Новые идеи ChatGPT, которых не было в исходном предложении Claude

## 11.1. Cascaded qualification

```text
semantic
→ consistency
→ Russian quality
```

Вместо общей weighted score.

---

## 11.2. Role-based candidate generation

```text
balanced
fidelity-first
ambiguity-resolution
```

Вместо нескольких случайных samples.

---

## 11.3. Immutable findings

Никакой destructive merge.

---

## 11.4. Content-addressed artifacts

Каждый artifact связан с exact inputs and producer identity.

---

## 11.5. Artifact dependency DAG

Resume/invalidation строится по зависимостям, а не именам файлов.

---

## 11.6. `quarantined` вместо расплывчатого `complete_with_flags`

При отсутствии человека flagged result не должен считаться готовым.

---

## 11.7. Final global smoke audit

После convergence проверяется вся неподвижная глава.

---

## 11.8. Publish memory only after acceptance

Никаких authoritative updates от unfinished/quarantined run.

---

# 12. Minimal viable v4 prototype

Первый prototype должен содержать только:

```text
1. Immutable source manifest
2. Frozen persistent memory snapshot
3. Source analysis + risk score
4. Scene/chunk generation
5. Adaptive role-based 1/2/3 candidates
6. Cascaded semantic → consistency → Russian selection
7. One full assembled-chapter audit
8. Immutable findings + simple clusters
9. Region-level repair
10. Targeted convergence
11. Final global smoke audit
12. Deterministic formatting contract
13. complete / quarantined / failed
14. Publish memory only after complete
15. Golden benchmark
```

Не включать сразу:

- mandatory third model;
- complex general graph engine;
- vector database;
- three candidates everywhere;
- dozens of policy knobs;
- complex reliability calibration;
- full scheduler optimization.

---

# 13. Последовательность реализации

## Phase 0 — measurement

1. Завершить главу 60 на patched v3.
2. Провести forensic translation audit.
3. Создать golden set 50–100 PID.
4. Измерить v3 baseline:
   - semantic recall;
   - false positives;
   - bad repairs;
   - final residual errors;
   - Russian quality;
   - formatting;
   - time/tokens/model loads.

Без Phase 0 v4 не принимается.

---

## Phase 1 — memory foundation

1. Book glossary.
2. Facts/entities/address memory.
3. Translation memory.
4. Regression/false-positive memory.
5. Provenance/conflicts/rollback.
6. Frozen snapshot per chapter.

Сначала shadow/observation mode.

---

## Phase 2 — candidate generation and selection

1. Source risk features.
2. Scene/chunk generation.
3. Candidate A/B.
4. Disagreement analysis.
5. Candidate C when needed.
6. Cascaded qualification.
7. Benchmark against single-draft v3.

---

## Phase 3 — full audit and findings

1. Full assembled-chapter semantic audit.
2. Russian/discourse audit.
3. Deterministic audit.
4. Immutable raw findings.
5. Simple explicit clusters.

---

## Phase 4 — repair and convergence

1. Region requirements.
2. Minimal/full repair candidates.
3. Independent gates.
4. Changed-neighborhood re-audit.
5. Max rounds.
6. Quarantine.
7. Final global smoke audit.

---

## Phase 5 — formatting

1. Translation-time span mapping.
2. Exact/occurrence-aware alignment.
3. Fuzzy fallback.
4. Model fallback.
5. Blocking integrity.

---

## Phase 6 — operational optimization

1. Model-centric batching.
2. Fewer reloads.
3. Optional third-model arbitration.
4. Vulkan degradation monitoring.
5. Cost-aware scheduling.

Только после доказанного качества.

---

## Phase 7 — A/B release

v3 и v4 получают:

- одинаковый source;
- одинаковый memory snapshot;
- независимые outputs;
- одинаковый golden evaluation.

Production switch допускается только если:

- semantic residual errors ниже;
- bad-repair rate не выше;
- Russian quality не хуже;
- formatting integrity не хуже;
- стоимость приемлема;
- quarantine rate приемлем.

---

# 14. Что просим Claude проверить

## v3.1.2 / hotfix

1. Соответствует ли active code заявленным исправлениям?
2. Безопасен ли resume mixed-version run?
3. Не создаёт ли новый merge слишком много duplicate issues?
4. Достаточно ли exact fingerprint?
5. Безопасна ли `uncertain_policy=repair`?
6. Полон ли mixed-script detector?
7. Корректна ли numeric equivalence?
8. Реально ли active `read_json()` использует `utf-8-sig`?
9. Correspond ли installed hashes marker-файлам?

## v3.1.3

1. Достаточен ли ограниченный integrity scope?
2. Достаточны ли targeted final checks + global smoke audit?
3. Как формализовать cache identity?
4. Как безопасно реализовать artifact DAG?
5. Достаточен ли append-only glossary candidate ledger как bridge?
6. Какие tests обязательны перед full-book run?

## v4.0

1. Есть ли архитектурные противоречия?
2. Достаточен ли cascade semantic → consistency → Russian?
3. Как лучше измерять candidate disagreement?
4. Как строить risk score без self-confirming bias?
5. Когда Candidate C действительно полезен?
6. Достаточно ли одного full assembled-chapter audit?
7. Какие checks должны быть global, а какие targeted?
8. Нужна ли третья модель?
9. Какой минимальный prototype быстрее всего проверит главную гипотезу?
10. Какие элементы можно удалить, чтобы не повторить сложность v3?

---

# 15. Итоговая дорожная карта

```text
v3.1.2d
→ подтвердить active installation и resume

chapter 60
→ завершить
→ отдельный forensic audit
→ golden baseline

v3.1.3
→ последний integrity-релиз v3
→ затем freeze, кроме critical bugs

v4.0 branch
→ measurement-first prototype
→ shadow/A-B
→ production только после доказанного выигрыша
```

Главная гипотеза v4:

> Контекстная генерация нескольких осмысленных кандидатов, каскадный отбор, постоянная память книги и targeted convergence дадут более высокий итоговый уровень перевода, чем повторение всё более сложных аудитов единственного draft.
