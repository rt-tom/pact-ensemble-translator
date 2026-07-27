# v4: обзор научной литературы и рекомендация по архитектуре

Дата: 2026-07-27. Автор материала — исследование по запросу RT.
Цель документа: сопоставить принятый план v4 (`PACT_RESPONSE_TO_CLAUDE_REVISED_ACCEPTED_PLAN_RU.md`)
с выводами свежих научных работ по литературному переводу через LLM и предложить
лучшую и по возможности быструю архитектуру v4.

---

## 1. Краткий вывод (TL;DR)

Принятый план v4 уже очень близок к фронтиру исследований 2024–2026: переход от
«один draft + всё более сложные аудиты» к «контекстная генерация нескольких
осмысленных кандидатов + каскадный отбор + постоянная память книги + targeted
convergence» — это ровно тот сдвиг парадигмы, который подтверждают работы по
литературному переводу (TransAgents/TACL, WMT-24 discourse-level, quality-aware
decoding). Главная гипотеза v4 научно обоснована.

Что литература добавляет к плану, и что даёт скорость:

1. **Risk-gated compute — главный рычаг скорости.** Длинный chain-of-thought и
   лишние кандидаты улучшают только трудные места (идиомы, метафора, культурный
   слой) и *ухудшают* лёгкие («overthinking»). Значит: большинство PID — один
   кандидат, без reasoning; 2 кандидата и reasoning-бюджет включаются только по
   risk score. План это предусматривает — нужно сделать это жёстким главным
   инвариантом, а не опцией.
2. **Мало кандидатов + хороший селектор бьёт много кандидатов.** Полный MBR
   (100–1000 сэмплов, O(M²)) непрактичен. Каскад semantic→consistency→Russian —
   это дешёвый структурный QE/MBR. Оставить 2 кандидата (A/B) + C по требованию.
3. **Минимальный region-repair, а не полный переписыв.** Post-editing литературы
   (CREAMT) показывает: точечная правка сохраняет творческий текст, полный
   rewrite его «сглаживает». План уже так делает — закрепить приоритет minimal
   repair.
4. **Метрики.** BLEU/COMET (и даже xCOMET/GEMBA) для литературы ненадёжны.
   Golden benchmark Phase 0 должен опираться на rubric/QA-оценку (в духе
   LiTransProQA) + LTCR (согласованность терминологии) + детерминированную
   integrity, а не на BLEU. Это критично, иначе A/B-сравнение v3 vs v4 будет
   мерить не то.
5. **Скорость на уровне операций — минимизировать reload'ы моделей.** Локальный
   llama.cpp с двумя моделями (Qwen/Gemma) — узкое место не в токенах, а в
   переключениях. Батчить по ролям на всю главу (все source-analysis Qwen → вся
   генерация → все аудиты), а не по PID.

---

## 2. Что говорит литература (основные выводы)

### 2.1. Мульти-агентная / ролевая коллаборация — TransAgents (TACL, 2025)

Ролевой конвейер (переводчик, редактор, localization specialist, корректор) с
двумя стадиями — **preparation** (сборка «команды» и составление translation
guideline: глоссарий, персонажи, стиль) и **execution** — даёт переводы
ультра-длинных литературных текстов, которые люди и LLM предпочитают и GPT-4, и
человеческим референсам. При этом **d-BLEU у системы низкий** — из-за
ограниченного разнообразия референсов: хороший творческий перевод *расходится* с
эталоном. Стоимость — примерно в 80 раз ниже человеческого перевода.

Значение для PACT: подтверждает (а) frozen book-memory / preparation-стадию,
(б) разделение ролей source-analysis / генерация / Russian-audit,
(в) что метрики на основе референса нельзя использовать как главный критерий.

### 2.2. WMT-24 Discourse-Level Literary Translation (в т.ч. Chinese→Russian)

Вторая итерация shared task; добавлено направление на русский. Два критерия
человеческой оценки: **general quality** (fluency/adequacy) и **discourse-aware
quality** (consistency, word choice, anaphora). Главный вывод: поверхностная
беглость маскирует критические **пропуски, дискурсивные искажения и культурные
ошибки**, которые сильно бьют по литературному смыслу. «Лёгкость» литературного
домена в крупных оценках обманчива — она игнорирует нарративную перспективу,
отношения персонажей, культурно-нагруженный смысл.

Значение для PACT: обосновывает full-assembled-chapter **discourse-audit**,
Russian-only аудит без оригинала, память согласованности (глоссарий, факты,
ты/вы, референты) и метрику согласованности терминологии.

### 2.3. Quality-aware decoding и MBR (кандидаты + селекция)

MBR-декодирование выбирает не самый вероятный, а самый «качественный» по
utility-метрике кандидат — и стабильно бьёт обычный MAP-декодинг. Но оно дорогое:
O(M²), в идеале M=100–1000 сэмплов. Практичная замена — **quality-aware модели**,
оценивающие качество собственного вывода, и QE-реранкинг небольшого числа
кандидатов.

Значение для PACT: каскадный отбор v4 — это по сути дешёвый структурный
QE/MBR-реранкинг на 2–3 кандидатах. Литература прямо поддерживает «adaptive
count + benchmarked temperature», а не «diversity ради diversity».

### 2.4. Self-reflection / DUAL-REFLECT / Reflective Translation

Самокритика и правка своих переводов повышают верность, но с **убывающей отдачей
и риском внести новые ошибки**; надёжность растёт, когда рефлексия заземлена
внешним сигналом (обратный перевод, dual feedback), а не «мнением» самой модели.

Значение для PACT: обосновывает region-repair + convergence и `challenge_issue`;
и объясняет, зачем нужны **детерминированные gate'ы** — как внешнее заземление,
которого требует литература по self-refine.

### 2.5. Reasoning-модели / длинный CoT (DRT, o1-подобные) и «overthinking»

Длинный chain-of-thought помогает на трудных идиоматических/метафорических/
культурных местах, но на простых — **деградирует**: модель «переубеждает» себя и
вносит ошибки; плюс латентность и стоимость кратно растут. Есть и обратная
крайность (underthinking). Вывод: reasoning нужно **дозировать по сложности**.

Значение для PACT: подтверждает решение v3 держать `--reasoning-budget 0` у
переводчика и даёт главный принцип скорости для v4 — reasoning-бюджет
включается только на high-risk сегментах.

### 2.6. Document-level контекст и размер чанка

Контекст-aware перевод (обычно ~3 предыдущих сегмента) заметно улучшает
согласованность; чанковый перевод (несколько предложений) бьёт по-предложенный
для дискурса. Метрика **LTCR** (Lexical Translation Consistency Ratio) измеряет
согласованность повторяющейся терминологии по документу.

Значение для PACT: обосновывает scene/chunk генерацию (8–20 связанных PID +
предыдущий контекст + ограниченный следующий) и даёт готовую метрику для golden
benchmark.

### 2.7. Пост-редактирование литературного MT (CREAMT-расширение)

LLM хорошо выполняют **точечное** пост-редактирование литературного MT, сохраняя
творческий регистр; агрессивный полный rewrite рискует «сглаживанием» стиля.

Значение для PACT: приоритет minimal repair над full-sentence rewrite (rewrite —
только когда локальная правка невозможна).

### 2.8. Ограничения автометрик и LiTransProQA

xCOMET-xl/xxl, GEMBA-MQM, Prometheus ограниченно работают на литературе.
LiTransProQA — метрика на основе профессиональных QA-вопросов — ближе к
литературным критериям (голос, регистр, образность, культурный слой).

Значение для PACT: дизайн golden set должен быть rubric/QA-ориентированным.

---

## 3. Сопоставление: v3 → план v4 → литература

| Аспект | v3 (сейчас) | План v4 | Вердикт литературы |
|---|---|---|---|
| Парадигма | один draft + детермин. QA + issue-only аудит + repair | контекстная генерация кандидатов + каскадный отбор + convergence | ✅ v4-сдвиг подтверждён (TransAgents, quality-aware) |
| Память книги | book_bible.json, provisional glossary | frozen snapshot (glossary/facts/voice/TM/regression) | ✅ соответствует preparation-стадии TransAgents |
| Единица генерации | сегмент ~900 слов / PID | scene/chunk 8–20 PID + prev + limited next | ✅ doc-level findings; добавить ~3-сегментный lookback как ориентир |
| Кандидаты | 1 | adaptive 1 / A+B / +C | ✅ мало кандидатов + селектор; не «3 везде» |
| Отбор | — (нет; сразу аудит) | каскад semantic→consistency→Russian | ✅ структурный QE/MBR |
| Reasoning | выключен у переводчика | не формализовано по риску | ⚠️ формализовать risk-gated reasoning (главный рычаг скорости) |
| Аудит | issue-only bilingual | 1 full assembled-chapter (Qwen sem + Gemma Rus + determ.) | ✅ discourse-aware; хватает одного полного прохода |
| Repair | targeted repair flagged PID | region-level minimal / optional rewrite | ✅ post-editing findings |
| Сходимость | фиксированный residual pass | convergence, max 2–3 rounds, затем quarantine | ✅ убывающая отдача self-refine → cap оправдан |
| Форматирование | семантическое восстановление после | translation-time span contract + fallback-каскад | ✅ раньше и детерминированнее — лучше |
| Метрики | benchmark recall/FP по PID | golden benchmark (план) | ⚠️ добавить rubric/QA + LTCR; не полагаться на BLEU/COMET |
| Модели | Qwen + Gemma, reload по фазам | optional 3-я модель | ✅ 3-я только high-risk; +нужна ops-оптимизация reload |

Вывод: **архитектурных противоречий в плане v4 нет** (это был вопрос №1 в разделе
14.v4.0 плана). Каскад semantic→consistency→Russian достаточен как основной
механизм отбора. Одного full assembled-chapter аудита достаточно при условии
targeted convergence на изменённых окрестностях.

---

## 4. Риски и уточнения к плану v4

1. **Risk score и self-confirming bias** (вопрос №4 плана). Строить риск из
   *внешних* сигналов: source-only признаки (идиомы, отрицание, модальность,
   референты, числа, смена speaker/ты-вы), детерминированные фичи и
   **disagreement между A и B** — а не из «уверенности» генератора. Литература по
   MBR/QE прямо трактует disagreement как сигнал риска, а согласие — не как
   доказательство правильности.
2. **Candidate C** (вопрос №5). Полезен только при *доказанном* semantic
   disagreement A vs B — как targeted synthesis, разрешающий конкретное
   расхождение, а не ещё один случайный сэмпл. План формулирует верно; держать
   строго так.
3. **Третья модель** (вопрос №8). Мульти-агентность помогает, но каждая роль —
   это латентность и (локально) reload. Держать 3-ю модель optional/high-risk и
   только после benchmark. Не делать её обязательной family в gate.
4. **Метрики A/B** (риск для Phase 7). Если сравнивать v3 vs v4 по BLEU/COMET —
   творческий v4 может «проиграть» из-за расхождения с референсом (эффект low
   d-BLEU у TransAgents). Критерии switch в плане (semantic residual, bad-repair
   rate, Russian quality, formatting, cost, quarantine rate) — правильные;
   инструментовать их rubric/QA + LTCR + детерминированной integrity.
5. **Overthinking-регресс.** Включив reasoning на high-risk, обязательно
   benchmark'ить, что он *не ухудшает* эти же места (короткий vs длинный CoT).

---

## 5. Рекомендуемая архитектура v4 (быстрая)

Принцип: **делать качество на генерации и отборе, а скорость — за счёт того, что
дорогие операции применяются только там, где риск это оправдывает, и за счёт
батчинга по моделям, а не по PID.**

### 5.1. Один проход на главу, разбитый по ролям (минимум reload'ов)

```text
Этап A — Qwen (загружен один раз на главу):
  source-only analysis + risk scoring для ВСЕХ chunk'ов
Этап B — генератор (Gemma/переводчик):
  scene/chunk генерация всех кандидатов
  reasoning-бюджет включён ТОЛЬКО для high-risk chunk'ов
Этап C — каскадный отбор:
  Stage 1 Qwen semantic qualification (batch)
  Stage 2 deterministic + memory consistency (модель-free)
  Stage 3 Gemma Russian selection без оригинала (batch)
Этап D — один full assembled-chapter аудит (Qwen sem + Gemma Rus + determ.)
Этап E — region-level minimal repair → convergence (≤2–3 rounds) → quarantine
Этап F — final global smoke audit (проверка неподвижного результата)
Этап G — translation-time formatting contract (детерминированный + fallback)
Публикация памяти — только после complete.
```

Ключ к скорости: этапы сгруппированы так, что каждая модель грузится в память
минимальное число раз за главу (batching по ролям). Это, а не число токенов,
доминирует на локальном llama.cpp.

### 5.2. Risk-gated бюджет (главный рычаг скорости и качества)

```text
low risk  (большинство PID):  1 кандидат, reasoning OFF, только Stage-1+2 отбор
med risk:                     A + B, reasoning OFF, полный каскад
high risk (идиома/метафора/   A + B (+C при disagreement),
  культурный слой/ты-вы/       reasoning ON (ограниченный бюджет),
  референт/число/negation):    полный каскад + усиленный audit
```

Порог риска и temperature/бюджет CoT калибруются на golden set, а не назначаются.

### 5.3. Отбор как дешёвый QE/MBR

Не единый weighted score (стиль не компенсирует смысл). Лексикографический
каскад: semantic (обязателен) → consistency (обязателен) → Russian (выбор
лучшего из прошедших). Если не прошёл никто — targeted synthesis/repair, а не
«наименее плохой».

### 5.4. Repair и сходимость

Minimal region repair как основной путь; full-sentence rewrite — fallback.
Детерминированные gate'ы как внешнее заземление рефлексии. Cap 2–3 раунда, затем
`quarantined` (не silent complete, не бесконечный цикл).

### 5.5. Golden benchmark (Phase 0 — без него v4 не принимать)

Мерить: semantic recall / false positives / bad-repair rate / final residual /
Russian quality — **rubric/QA-оценкой (LiTransProQA-стиль)**, плюс **LTCR**
(согласованность терминологии), плюс детерминированная integrity (PID coverage,
missing, mixed-script, numbers, formatting), плюс **time / tokens / model
reloads**. BLEU/COMET — максимум как вспомогательный сигнал.

### 5.6. Что НЕ включать в первый прототип (во избежание сложности v3)

Обязательную третью модель; общий graph-движок; векторную БД; «3 кандидата
везде»; десятки policy-ручек; сложную калибровку надёжности; полную оптимизацию
планировщика. Всё это — после доказанного качества (Phase 6+).

---

## 6. Ответы на вопросы раздела 14.v4.0 плана

1. Архитектурных противоречий нет.
2. Каскад semantic→consistency→Russian достаточен как основной отбор (структурный QE).
3. Disagreement измерять по: semantic choices, terminology, референты, идиомы, длина, регистр, синтаксис — и трактовать как **сигнал риска**, не как ошибку.
4. Risk score строить на внешних (source-only + детерминированных + disagreement) признаках, не на self-confidence.
5. Candidate C — только при доказанном A/B disagreement, как targeted resolution.
6. Одного full assembled-chapter аудита достаточно при targeted convergence.
7. Global: PID coverage, mixed-script, numbers, glossary/names, HTML/formatting, полная смысловая и русская целостность. Targeted: изменённые PID + соседние discourse-окна.
8. Третья модель — optional, high-risk, после benchmark; не обязательна.
9. Быстрее всего гипотезу проверит MVP из §5 на 1–2 главах против v3 на общем golden set.
10. Убрать: обязательную 3-ю модель, векторную БД, graph-движок, «3 кандидата везде», избыток policy-ручек.

---

## 7. Источники

- Wu et al., *(Perhaps) Beyond Human Translation: Multi-Agent Collaboration for Ultra-Long Literary Texts* (TransAgents), TACL 2025 — https://arxiv.org/abs/2405.11804
- *Findings of the WMT 2024 Shared Task on Discourse-Level Literary Translation* (вкл. zh→ru) — https://arxiv.org/abs/2412.11732 · https://aclanthology.org/2024.wmt-1.58/
- *Evaluating literary translation by LLMs: multidimensional QA of "Border Town"* — https://www.nature.com/articles/s41599-026-06868-y
- *LiTransProQA: LLM-based Literary Translation Evaluation with Professional QA* — https://arxiv.org/html/2505.05423
- *Extending CREAMT: LLMs for Literary Translation Post-Editing* — https://arxiv.org/html/2504.03045
- *Quality-Aware Translation Models: Efficient Generation and Quality Estimation in a Single Model* — https://arxiv.org/abs/2310.06707
- *DUAL-REFLECT: Enhancing LLMs for Reflective Translation via Dual Learning Feedback* — https://arxiv.org/pdf/2406.07232
- *DRT: Deep Reasoning Translation via Long Chain-of-Thought* — https://arxiv.org/pdf/2412.17498
- *Evaluating o1-Like LLMs: Unlocking Reasoning for Translation* — https://arxiv.org/pdf/2502.11544
- *Do NOT Think That Much... On the Overthinking of o1-Like LLMs* — https://arxiv.org/pdf/2412.21187
- *Multilingual Contextualization of LLMs for Document-Level MT* — https://arxiv.org/html/2504.12140
- *Chain-of-Thought Reasoning Improves Context-Aware Translation with LLMs* — https://arxiv.org/html/2510.18077
