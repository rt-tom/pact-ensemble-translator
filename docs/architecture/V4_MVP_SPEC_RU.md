# Pact v4.0 — Converged MVP Spec

Дата: 2026-07-27. Статус: согласовано (RT + Claude + ChatGPT, 2 раунда ревью).
Backing-документ: `V4_LITERATURE_REVIEW_AND_RECOMMENDATION_RU.md` (обзор литературы и
обоснования). Исходный план: `PACT_RESPONSE_TO_CLAUDE_REVISED_ACCEPTED_PLAN_RU.md`.

Это канонический источник архитектуры для ветки v4.0. Всё, что не описано здесь,
в MVP не входит.

---

## 0. Смена парадигмы

```text
v3: «Переведи хорошо, потом всё сложнее ищи и чини ошибки в одном draft.»
v4: «Сначала создай хорошие варианты в контексте, выбери лучший каскадом,
     потом почини только остаточные проблемы.»
```

Приоритеты (лексикографические — стиль не компенсирует смысл):

1. смысловая верность и полнота;
2. сквозная согласованность книги и сцены;
3. естественный литературный русский;
4. структура и форматирование;
5. скорость.

Скорость достигается не урезанием проверок, а тем, что дорогие операции
(вторые кандидаты, глубокий Qwen-анализ, reasoning) применяются только там, где
их оправдывает risk score, и тем, что модели грузятся минимальное число раз за
главу (батчинг по ролям, а не по PID).

---

## 1. Ключевые архитектурные решения (что изменилось после ревью)

1. **PID — только единица контроля; генерация — на уровне scene/chunk.**
   Вход генерации — сцена (8–20 связанных PID) + контекст; выход — по-прежнему
   PID-map `{PID → русский текст}`. Контекст даёт discourse-когерентность,
   PID-map сохраняет coverage/formatting/resume/cache identity.
2. **Risk pre-screen — детерминированный, без модели, по всем chunk'ам.**
   Он гейтит и число кандидатов, и то, идёт ли chunk на глубокий Qwen-анализ.
   Qwen не является первым фильтром.
3. **Никакого blanket literary-refinement.** Хороший текст не трогаем; полировка
   русского делается только там, где Russian-only аудит пометил проблему — тем же
   targeted repair. Отдельного generative rewrite-прохода нет (риск semantic
   drift и «сглаживания», CREAMT).
4. **Selection — лексикографический каскад, без scoring-движка:**
   Qwen semantic pass/fail → детерминированный consistency-gate (model-free) →
   Gemma Russian preference.
5. **Один assembled-chapter аудит** (Step 6) + targeted convergence по
   изменённым регионам (Step 7). Step 8 (final integrity check) по умолчанию
   не является модельным аудитом — см. §2 Step 8 и §11. Никаких повторных
   полных тяжёлых аудитов обеими моделями по всей главе.
6. **Reasoning в MVP выключен** (Gemma `reasoning=0`), но архитектура
   reasoning-ready: risk score и per-chunk конфиг уже в плайпе, включение —
   флагом позже, отдельным benchmark'ом.
7. **Repair мельче генерации.** Генерим сценой, чиним регионом/PID.
8. **Без vector DB / RAG / graph memory / обязательной 3-й модели в MVP.**
   Память — plain JSON.

---

## 2. Pipeline (8 шагов)

```text
1. Book context preparation (frozen snapshot на главу)
   - glossary (+ lock)
   - characters / style / voice memory
   - previous events / facts
   - term & name memory (НЕ fuzzy sentence-level TM)

2. Deterministic risk pre-screen — ВСЕ chunks, без модели
   сигналы: числа, отрицания, модальность, имена, glossary conflicts,
            смена speaker / ты-вы, плотность диалога, длина/сложность,
            mixed-script, previous-failure memory
   → risk score на каждый chunk

3. Qwen source analysis — ТОЛЬКО для flagged chunks
   plain meaning, subject/object, negation, modality, referents,
   idioms, numbers, ты/вы, forbidden additions, local risk features

4. Generation (Gemma, reasoning=0)
   low risk : 1 candidate
   high risk: A / B candidates
   disagreement A≠B по смыслу: optional C (targeted resolution, не случайный sample)
   вход: scene/chunk + prev context + limited future context (read-only)
   выход: PID-map { PID → текст }

5. Selection (каскад, без scoring-движка)
   Qwen        : semantic pass/fail        (обязательно)
   deterministic: consistency gate          (glossary/имена/ты-вы/числа/mixed-script)
   Gemma       : Russian preference         (без оригинала, выбор из прошедших)
   если не прошёл никто → targeted synthesis/repair, НЕ «наименее плохой»

6. Assembled-chapter audit (один раз, по собранной главе)
   Qwen        : source ↔ translation (пропуски/добавления/референты/сцена)
   Gemma       : Russian-only review (кальки, регистр, повторы, диалог, ты/вы)
                 — коррелированный сигнал (та же модель, что генерировала
                 текст в Step 4/5), НЕ независимое доказательство качества;
                 добавочная ценность против отсутствия проверки должна быть
                 подтверждена ablation-бенчмарком (см. §10)
   deterministic: PID coverage / numbers / mixed-script / glossary / names /
                  formatting contract / HTML structure
   formatting контракт (§8.14) применяется ДО Step 8, не после — final smoke
   должен видеть тот же текст, что попадёт в `complete`

7. Targeted repair
   region-level minimal repair (semantic + Russian findings)
   optional full-sentence rewrite ТОЛЬКО если локальная правка невозможна
   convergence: re-audit только изменённых PID + discourse-окрестностей
     - Qwen: semantic re-check изменённого региона
     - Gemma: Russian re-check только если правка затронула
       диалог/регистр/ты-вы/имена (risk-triggered, не blanket)
   max 2 rounds → иначе quarantined

8. Final integrity check + memory promotion
   ОБЯЗАТЕЛЬНО, без модели:
     - неподвижность финального результата (frozen hash)
     - PID coverage / numbers / glossary / mixed-script / formatting / HTML —
       вся глава
     - подтверждение, что все findings Step 6/7 закрыты
   УСЛОВНО, одна модель (не обе):
     - narrow Qwen semantic smoke по изменённым/рисковым регионам, ТОЛЬКО
       если в Step 7 был ≥1 repair round или иной измеримый risk trigger
     - Gemma smoke по умолчанию отсутствует (уже проверяла тот же текст в
       Step 5 и Step 6 как коррелированный сигнал — третий проход не даёт
       независимого сигнала)
   memory promotion ТОЛЬКО после complete
```

---

## 3. Scene/chunk генерация и роль PID

### 3.1. Разделение единиц

| | Единица | Зачем |
|---|---|---|
| Генерация (вход) | scene/chunk 8–20 PID + контекст | discourse: голос, регистр, ты/вы, анафора, ритм |
| Генерация (выход) | PID-map `{PID → текст}` | coverage, formatting span contract |
| Учёт/контроль | PID | issue identity, resume, cache identity |
| Repair | region / PID | минимальная правка (CREAMT), дешёвая convergence |

### 3.2. Chunking — structure-aware, динамический, зажатый в диапазон

MVP не использует модельную семантическую сегментацию. Границы берутся из
детерминированной структуры источника:

```text
- разрывы абзацев / сцен (HTML, пустые строки, маркеры)
- границы реплик диалога (не резать один обмен пополам)
- целевое окно 8–20 PID, мягкий min/max:
    расти до ближайшей границы абзаца, но жёсткий верхний cap
```

Верхний cap диктуется не только контекстным окном, а:

- long-context деградацией («lost in the middle»);
- стоимостью repair: большой chunk → перегенерация большого блока при одной ошибке.

### 3.3. Контекст и владение PID

```text
left context  = уже зафиксированный перевод предыдущего chunk'а (read-only)
right context = ИСХОДНЫЕ несколько PID следующего chunk'а (read-only, не переводятся здесь)
владение      = каждый PID переводится ровно одним chunk'ом (без двойного перевода)
```

---

## 4. Risk-gating (главный рычаг скорости)

```text
low risk  (большинство): 1 candidate, без Qwen deep-analysis, каскад = semantic+determ.
med risk :               A/B, Qwen deep-analysis, полный каскад
high risk (идиома/метафора/культурный слой/ты-вы/референт/negation/число/
           glossary conflict/previous failure):
                         A/B (+C при disagreement), усиленный audit
```

Правила против self-confirming bias:

- risk строится на **внешних** сигналах (source-only + детерминированные +
  disagreement A/B), не на «уверенности» генератора;
- disagreement A≠B → повышает risk / запускает C / усиливает audit, но **не**
  считается доказательством ошибки; согласие A=B — не доказательство
  правильности.

Reasoning в MVP выключен. Risk-gated reasoning — отдельный эксперимент Phase 6+,
включается флагом без редизайна.

---

## 5. Что берём из v3 и что убираем

Сохранить (реальные преимущества Pact):

```text
✅ stable PID + PID coverage
✅ deterministic formatting checks (span contract)
✅ glossary lock
✅ book memory / frozen snapshot per chapter
✅ resume safety
✅ targeted repair
✅ final acceptance gate (complete / quarantined / failed)
✅ Russian-only audit без оригинала
✅ challenge_issue
```

Убрать (было нужно для стабилизации v3, не должно быть фундаментом v4):

```text
❌ сложная deployment machinery
❌ десятки schema/version checks
❌ многостадийный audit lifecycle (primary/residual/final repair)
❌ сложный issue graph / merge по span overlap
❌ фиксированный residual pass (заменён convergence)
❌ blanket literary-refinement pass
❌ обязательная 3-я модель, vector DB, RAG, graph memory
```

Итог: ≈ 60–70% сложности v3 при потенциально более высоком качестве.

---

## 6. Память (plain JSON, без embeddings)

```text
glossary.json        — термины, имена, lock-статусы
book_memory.json     — персонажи, факты, отношения, address register, voice notes
chapter_memory.json  — frozen snapshot на текущую главу (ссылается на hash)
```

Состояния записи: `observed → provisional → cross_chapter_supported → established
→ locked` (+ `conflicted`, `deprecated`). Promotion консервативен; conflict не
затирает established/locked молча. Обновление общей памяти — только после
`complete`; при `quarantined` observations хранятся отдельно, не authoritative.

Осторожно с term/fact memory: годится для терминов/имён/фактов; **fuzzy
sentence-level TM не использовать** — тащит устаревшие формулировки в художку.

---

## 7. Final states

```text
complete   — все blocking-инварианты пройдены; память promote'ится
quarantined — целостный текст есть, но система не смогла принять автоматически;
              может запустить extra candidate / larger context / 3-ю модель / усиленный audit;
              в production book не попадает до авторазрешения
failed      — целостный результат технически не создан
```

---

## 8. Phase 0 — measurement (без него v4 не принимается)

### 8.1. Golden set

50–100 PID из реальной главы (кандидат — глава 60 на patched v3). Разметка:
известные проблемные места + чистые контроли.

### 8.2. Метрики (НЕ BLEU/COMET как главный критерий — для литературы ненадёжны)

```text
- semantic recall / false positives            (детекция)
- bad-repair rate                               (repair не портит)
- final residual errors                         (что осталось)
- Russian quality                               (rubric/QA, LiTransProQA-стиль)
- LTCR                                          (согласованность терминологии)
- deterministic integrity                       (PID/numbers/mixed-script/formatting/HTML)
- time / tokens / model reloads                 (скорость)
```

### 8.3. Chunk-size benchmark (последняя большая развилка)

Benchmark'ить не одно число, а сетку, всегда со снапом к границе абзаца/сцены:

```text
~8–12 PID  vs  ~12–20 PID
× {с limited future context, без него}
```

Мерить: LTCR, корректность ты/вы и анафоры **на стыках**, естественность
русского, cost/latency/reloads. Размер выбирается эмпирически, не назначается.

### 8.4. A/B против v3

Один source, один memory snapshot, независимые outputs, одинаковая golden-оценка.
Production switch допустим только если: semantic residual ниже; bad-repair rate
не выше; Russian quality не хуже; formatting integrity не хуже; стоимость
приемлема; quarantine rate приемлем.

---

## 9. Порядок реализации

```text
Phase 0  measurement: завершить главу 60 (v3) → forensic audit → golden set → v3 baseline
Phase 1  memory foundation: glossary / book_memory / chapter snapshot (shadow mode)
Phase 2  risk pre-screen + scene/chunk generation + A/B + cascaded selection
         → benchmark против single-draft v3
Phase 3  assembled-chapter audit (Qwen sem / Gemma Rus / determ.) + immutable findings
Phase 4  region repair + convergence (max 2) + quarantine + final integrity check
         (deterministic default, conditional narrow Qwen smoke — см. §2 Step 8)
Phase 5  translation-time formatting contract (exact → occurrence → fuzzy → model fallback)
Phase 6+ ops: batching по ролям, меньше reloads; опц. risk-gated reasoning; опц. 3-я модель
Phase 7  A/B release; switch только после доказанного выигрыша
```

Всё в отдельной ветке v4.0; v3 замораживается (кроме critical bugs) и служит
измеряемым baseline.

---

## 10. Открытые вопросы для benchmark (не для дизайна)

1. Chunk size: 8–12 vs 12–20 PID (§8.3).
2. Temperature/seed для A/B — калибровать на golden set.
3. Порог risk score (low/med/high).
4. Даёт ли risk-gated reasoning выигрыш на high-risk EN→RU — отдельный эксперимент Phase 6+.
5. Ablation на golden set для Step 6 Gemma Russian review: сравнить recall
   реальных дефектов / false positives / latency / reload cost для (a) Gemma
   self-review, (b) Qwen Russian review, (c) третья модель, (d) отсутствие
   дополнительной model-review. Решает, остаётся ли Gemma review в Step 6
   обязательной или переходит в high-risk-only escalation (см. §1 п.5, §2
   Step 6/8).

Эти решения принимаются числами из Phase 0, а не заранее.
