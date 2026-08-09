# V4.1 — Архитектура whole-chapter перевода: план реализации

Дата: 2026-08-08
Статус: ЧЕРНОВИК для обсуждения (не карточки, не утверждённый план)
Ветка-цель: `dev/v4.1-reasoning-transport` (рабочая ветка 4.1, PR #145 draft → main не мержится до готовности)
Глава-валидатор: 0001 (Bonds 1.1)

---

## 0. Резюме

На основании серии слепых/контролируемых тестов (2026-08-07/08) установлено:

1. **Reasoning — главная потерянная переменная** у DeepSeek: `0 → High` даёт +0.9 балла (7.4 → 8.3) на direct full-chapter переводе. У Gemma эффект мал: `0 → 2048` даёт +0.05–0.1 (8.0 → 8.05–8.1).
2. **Translation-stage pipeline (промпт/контекст/формат)** стоит ≈ −0.4 балла при том же reasoning: Independent High ≈8.3 vs Pipeline Remote High ≈7.9 — **до** audit/repair.
3. **Glossary безопасен**: Independent №3 (High + locked glossary) ≈8.1–8.2 — падение ~0.1–0.2 в пределах вариативности, при этом даёт book-level консистентность.
4. **Audit + repair — 83% всех вызовов** текущего pipeline (246 из 297 на главу 0002) при том, что литературное качество создаёт генерация (6% вызовов). Repair/formatting способны ухудшать (A1c-ревью; ASCII-кавычки eff-a1a2; formatting transport_error).
5. **Локальная Gemma whole-chapter ≈8.0–8.1** без деградации на 16k-токенном контексте — конкурентоспособна с DeepSeek High (≈8.3), бесплатно и без лимитов.

Целевая архитектура 4.1: **whole-chapter генерация одним вызовом + детерминированный QA + консервативный семантический аудит (вторая модель) + селективный repair + детерминированный formatting**. Цель по вызовам: 3–7 вызовов на главу вместо 297; по качеству: ~8.2–8.3 без терминологического дрейфа.

---

## 0.1. Ключевые отличия от текущего v4 (сводно)

| Аспект | v4 (текущий) | v4.1 (цель) |
|---|---|---|
| **Генерация** | chunking (~200 слов/чанк, 16–18 вызовов на главу), A/B-кандидаты (balanced_literary + lazy fidelity_first) | **whole-chapter одним вызовом** (1 вызов), единственный кандидат; selection A/B вырождается и убирается |
| **Reasoning генератора** | `reasoning=0` принудительно (generation.py:103-117 — ValueError при ≠0) | **DeepSeek High** (remote) / **Gemma 2048** (local); уже реализовано в PR #145 (reasoningEffort через opencode serve 1.4.7, проверено: 117k reasoning-токенов на главу 0001) |
| **Промпт перевода** | короткий ролевой (balanced_literary: «prefer natural *provided meaning preserved*») + полный bible-дамп + glossary + строгий JSON по PID | **литературный промпт v3** (§4): «translate by effect», мат по силе, анти-калька, голоса; bible через **автоматический chapter_index** (§5), glossary **отфильтрован по главе** (§5); **строгий JSON `{pid: text}`** (PID-контракт §3.2) |
| **Bible** | глобальный дамп, капы «первые N» (20 персонажей/30 фактов/10 адресов), без привязки к главе | **автоматический детерминированный chapter_index** (скрипт, без моделей, без ручного утверждения): для каждой главы — персонажи/факты/адреса, встречающиеся в её source (+ обязательные), как B9-механизм |
| **Glossary** | A1.1-фильтр по чанку (owned+left+right) | тот же A1.1-механизм, вход = **текст всей главы** |
| **Аудит (Step 6)** | 2 детектора × 16 чанков = ~143 вызова (Qwen 55 + Gemma 88); категории omission/addition/referent/scene | **1 вызов Qwen whole-chapter** (`WholeChapterAuditEvaluator`); категории + invented_gender/changed_fact/negation (без scene); «when uncertain, PASS»; Gemma-детектор **выпилен** |
| **Reasoning аудитора** | 0 | **Qwen low (remote) / 2048 (local)** — отдельный параметр от генерации |
| **Repair (Step 7)** | генеративный, регионы по findings + Qwen-fidelity-gate + Gemma-recheck (~103 вызова, 2 раунда) | **селективный**: только confirmed major/high-confidence, «fix only this, preserve all», с полным контекстом; 0–N вызовов; Qwen-fidelity-gate/Gemma-recheck не нужны |
| **Пропуск repair** | никогда (всегда цикл по findings) | **0 findings → repair пропускается целиком** (TEaR) |
| **Re-audit** | полный повторный проход по чанкам | **1 вызов: вход = полный source + полная translation, scope ответа = changed PID + окно соседей** (не слепой PID-local) |
| **Formatting (Step 8)** | генеративный + model-fallback (15 вызовов; реальные transport_error/target_not_found в eff-a1a2) | **детерминированный** (B14-нормализация: `<em>`, кавычки, сущности), 0 вызовов модели; unresolved spans → debt, не тихая потеря |
| **Вызовы/глава** | **~297** (генерация 6%, audit 48%, repair 35%, formatting 5%) | **3–7** (генерация 1 + аудит 1 + repair 0–N + re-audit 0–1) |
| **Стоимость/глава** | ~$1.00 (eff-a1a2 ch.0002: $0.996) | ~$0.02–0.1 (оценка по составу вызовов) |
| **Контекст Qwen local** | 32k (`-c 32768`) | **49k** (`-c 49152`) — вход whole-chapter аудита ~28k + выход |
| **Промежуточные снапшоты** | только финальный translations.json (B13) | **translations_raw + translations_repaired + translations_final + translation_diffs.json** (§7) — атрибуция «кто что изменил»; atomic write, identity в каждом; translations.json остаётся финальным алиасом |
| **Вторая модель (Qwen)** | translator-кандидат + selector + auditor + repair-gate (везде) | **только аудитор** (никогда не переводит/не редактирует) — по Kocmi: аудитор ≠ генератор |
| **Local Gemma 2048** | недоступно: `validate_reasoning_backend()` fail-fast отклоняет reasoning>0 для local (runtime_config.py:616-645) | **поддержанный путь** (проверено владельцем 2026-08-08): reasoning передаётся через `--reasoning-budget 2048` в server-args llama-server, а не через request_options; меняется принцип validate_reasoning_backend |
| **max_output_tokens** | 8192, в identity, НЕ передаётся в POST (opencode_backend.py:740-779) | поднять конфиг (whole-chapter выход ~12k+); фактический серверный лимит — **проверяется в Gate 0** (§9.0) |

**Что сохраняется из v4:** PID-архитектура, детерминированный QA (полностью), glossary-хранилище + B9-наполнение (механизм не меняется), resume/кэш-логика (с новой identity), logging/monitoring, консервативный принцип «не переписывать хорошее».

**Что выпиливается:** chunking-генерация, selection A/B, Gemma-детектор, Qwen-fidelity-gate, Gemma-recheck, генеративный formatting, повторные полные ре-аудиты.

---

## 1. Доказательная база (документы и измерения)

### 1.1. Эксперименты (docs/audits, dev-клон `D:\pact\pact_translator_v4_1\docs\audits\`)

| Документ | Что показал | Ключевая цифра |
|---|---|---|
| `pact_translation_benchmark_report_v4_1.md` | Blind-сравнение 5 вариантов на ~110 PID + Independent №1 vs Pipeline Remote v4.1 | DeepSeek High full ≈8.3; Gemma 0 chunk ≈8.05; DeepSeek 0 full ≈7.4; Qwen 0 ≈6.7; T-lite ≈5.0; Pipeline Remote High ≈7.9 |
| `pact_gemma_reasoning_t2_t4_t5_report.md` | Gemma chunk, reasoning 0/2048/4096 | 0 ≈8.05, 2048 ≈8.1–8.15, 4096 ≈8.0 (4096 не дал выигрыша, больше вольностей) |
| `pact_t6_t7_vs_independent1_report.md` | Gemma whole-chapter, reasoning 0/2048 vs Independent №1 | T6 ≈8.0, T7 ≈8.05–8.1, Independent ≈8.3; **нет long-context деградации**; системные ошибки reasoning не лечит (Two past twelve, eye contact, Jesus fuck) |
| `pact_ch1_independent_vs_pipeline_report_v2.md` (в production repo) | Первый чистый контроль: direct full-chapter vs pipeline | Independent ≈8.1 vs pipeline ≈7.0; полная структура 402/402 блоков, `<em>` 101/101 у independent |
| Independent №3 (результат озвучен владельцем 2026-08-08) | DeepSeek High + locked glossary | ≈8.1–8.2; glossary безопасен, терминология лучше |

### 1.2. Самоанализ модели (техники перевода)

`D:\test folder\.hermes\desktop-attachments\0001_bonds-1-1.translation-report.md` — пост-хок self-report DeepSeek о технике литературного перевода:

- **Перевод по эффекту, а не по словарному соответствию**: мат подбирается по силе/регистру (cunt → сука, не пизда; fuck off → отъебись; drat/darn остаются смягчёнными).
- **Анти-калька**: перестройка под русский синтаксис (wannabe-architect → недоархитектор; two-theater podunk town → городишко с двумя кинотеатрами).
- **Голоса персонажей**: глава сама служит локальной character bible (модель выводит голоса из текста главы).
- **Точность деталей**: Two past twelve → две минуты первого (12:02) — на минутах держится сюжетный момент.
- Оговорка: self-report — пост-хок объяснение, не трассировка; но согласуется с наблюдаемым текстом.

### 1.3. Научная литература (reference `v4-a1c-literature-evidence.md`)

| Источник | Вывод | Применение в 4.1 |
|---|---|---|
| Karpinska & Iyyer (WMT'23) | Document-level контекст: 71.67% предпочтений, 31% меньше mistranslations, 15× меньше inconsistencies | Обоснование whole-chapter генерации |
| Wang et al. (EMNLP'23) | Document-промпты улучшают терминологию и discourse | Обоснование whole-chapter + glossary |
| **Kocmi & Federmann (GEMBA-MQM)** | «Avoid same-model audit → repair → self-approval without another signal» | **Аудитор ≠ генератор: Qwen как вторая модель** |
| Huang et al. (ICLR'24) | Intrinsic self-correction без внешнего фидбека деградирует | Repair только на confirmed findings, не свободный |
| Freitag et al. (TACL'21) | Оценка обязана идти с полным документным контекстом | Аудит whole-chapter, не по-PID окнам |
| Ki & Carpuat (NAACL'24) | Правка мест «No error» даёт metric drop | Аудит консервативный: «when uncertain, PASS» |
| Madaan (Self-Refine) | 33% wrong localization, 61% inappropriate fix | Хирургический repair с полным контекстом |
| TEaR (NAACL'25) | Селективное уточнение: 693/2037 refined vs 1417–1854 у неселективных | 0 findings → repair пропускается целиком |
| Raunak et al. | Прямое редактирование (без CoT) — единственное рабочее | Repair: «fix only this, preserve all» |

### 1.4. Измерения текущего pipeline (глава 0002, eff-a1a2, `usage.ndjson`)

| Фаза | Вызовы | Доля | Роль/модель |
|---|---|---|---|
| Генерация (phase2b) | 18 | 6% | DeepSeek (balanced_literary) |
| Selection (phase2c) | 18 | 6% | Qwen (fidelity gate) |
| Audit (phase3) | 143 | 48% | Qwen (55) + DeepSeek (88, gemma_russian_review) |
| Repair (phase4) | 103 | 35% | DeepSeek (75 region_repair) + Qwen (28 gate) |
| Formatting (phase5) | 15 | 5% | DeepSeek (formatting_align) |
| **Итого** | **297** | 100% | **$0.996** |

Прогон 4.1 (run_reasoning3_selection, глава 0001): 20 gen-вызовов, **117k reasoning-токенов**, $0.139 всего; 16/16 selected; balanced_literary выиграл 13/16. Consistency-ошибка `motorcycle → велосипед` (3 PID, от fidelity_first) — не поймана selection.

### 1.5. Классы ошибок, которые reasoning НЕ лечит (T6/T7, глава 0001)

- `Two past twelve → Две минуты двенадцатого` (правильно: 00:02) — семантическая неоднозначность.
- `I was making eye contact → Я смотрел прямо перед собой` — потерян foreshadowing.
- `namesake hill → холм, давший название поселению` — смысловая подмена.
- `Jesus fuck → Господи помилуй` — системное смягчение мата у Gemma.
- `motorcycle → велосипед` — consistency (pipeline 4.1).

**Вывод**: эти классы — работа детерминированных проверок (время/числа/имена/glossary) и консервативного семантического аудита, а не reasoning.

---

## 2. Диагноз текущего pipeline

```
8.3  Independent DeepSeek High (full chapter, литературный промпт)
 │   ▲
 │   │ reasoning effect: DeepSeek 0 → High = +0.9
7.9  Pipeline Remote High (DeepSeek High + v4-промпт/контекст/формат)
 │   ▲
 │   │ translation-stage penalty: −0.4 (промпт, bible-дамп, JSON, chunking)
 │   │ glossary: −0.1…−0.2 (безопасен, даёт консистентность)
7.4  DeepSeek 0 (direct)
7.0  Pipeline 0 (старый, reasoning=0)
```

Разложение разрыва «Independent High vs Pipeline 0» (≈1.3):
- ~0.9 — отсутствие reasoning у генератора (доказано A/B)
- ~0.4 — translation-stage контекст/промпт/формат (доказано: Pipeline Remote High = 7.9 при reasoning=High)
- repair/audit — пока не измерены отдельно (требуют A/B «raw vs raw+repair»)

Оставшийся gap после включения reasoning: **Independent ≈8.3 vs Pipeline High ≈7.9** — при этом audit/repair не участвовали (stop-after-selection). Значит деградация происходит в translation-stage: промпт, bible-дамп (нерелевантные персонажи), JSON-формат, chunking.

---

## 3. Целевая архитектура 4.1

```text
                      WHOLE CHAPTER (source ~13k токенов)
                                   │
                                   ▼
        ┌──────────────────────────────────────────────┐
        │ 1. ГЕНЕРАЦИЯ (1 вызов)                        │
        │    DeepSeek High  ИЛИ  Gemma 2048 (local)     │
        │    новый литературный промпт (см. §4)          │
        │    + chapter_index bible (§5) + locked         │
        │    glossary (A1.1-фильтр, вход = вся глава)    │
        │    строгий PID JSON-контракт (§3.2)            │
        │    → translations_raw.json (снапшот)           │
        └──────────────────────────────────────────────┘
                                   │
                                   ▼
        ┌──────────────────────────────────────────────┐
        │ 2. DETERMINISTIC QA (код, 0 вызовов)          │
        │    PID count/порядок (валидация контракта),   │
        │    <em>, кавычки, mixed_script, числа/время,  │
        │    имена, locked glossary, случайный          │
        │    английский                                 │
        └──────────────────────────────────────────────┘
                                   │
                                   ▼
        ┌──────────────────────────────────────────────┐
        │ 3. SEMANTIC AUDIT (Qwen, 1 вызов)             │
        │    WholeChapterAuditEvaluator, whole-chapter  │
        │    reasoning: low (remote) / 2048 (local)     │
        │    категории: omission/addition/referent/     │
        │    invented_gender/changed_fact/negation      │
        │    «when uncertain, PASS»                     │
        └──────────────────────────────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │ 0 findings                   │ findings
                    ▼                              ▼
          ┌──────────────────┐        ┌──────────────────────────────┐
          │ 4. SKIP repair    │        │ 4. SELECTIVE REPAIR (0-N)    │
          │ (TEaR)            │        │    DeepSeek/Gemma, «fix only │
          └──────────────────┘        │    this, preserve all»,      │
                                      │    с полным контекстом       │
                                      │    → translations_repaired.json│
                                      └──────────────────────────────┘
                                   │
                                   ▼
        ┌──────────────────────────────────────────────┐
        │ 5. RE-AUDIT (Qwen, 1 вызов)                   │
        │    вход = полный source + полная translation, │
        │    scope ответа = changed PID + окно соседей  │
        └──────────────────────────────────────────────┘
                                   │
                                   ▼
        ┌──────────────────────────────────────────────┐
        │ 6. FORMATTING (детерминированный, 0 вызовов)  │
        │    B14-нормализация: <em>, кавычки, сущности  │
        │    → translations_final.json + translation_diffs.json│
        └──────────────────────────────────────────────┘
```

**Бюджет вызовов: `2 + repair_calls + reaudit`** (генерация 1 + аудит 1 + repair 0–3 батч-вызова + re-audit 0–1) вместо 297. Оценка: 2–5 типично, до 6 при полном repair-цикле. Стоимость ≈ $0.02–0.05 remote вместо $1.

**Границы контекста/выхода** (вход whole-chapter ~16-17k с bible/glossary/prompt; выход = source×1.3 + reasoning): максимальная глава книги (кандидат 0077, ~59.5k bytes) + запас — измеряется в Gate 0 §9.0; `max_output_tokens` генератора ≥16384 (точное значение после Gate 0); контекст local 49k; remote — лимит провайдера (Gate 0).

### 3.2. PID-контракт whole-chapter генератора (обязательный)

Whole-chapter генератор сохраняет строгий PID-контракт (без него нельзя собрать, отремонтировать, проверить или возобновить):

- **Вход**: полный упорядоченный PID map (все PIDs главы, в порядке source) — вместо `owned_pids` чанка.
- **Выход**: строго JSON `{pid: russian_text}`, строго в исходном порядке, полный PID-set. **Prose-only вывод запрещён.**
- **Валидация** (реализуется в генераторе/детерминированном QA):
  - missing PID → retry;
  - extra/duplicate/reordered PID → retry;
  - empty/truncated/malformed JSON → retry (bounded, как B4/B10);
  - после лимита retry — честная ошибка/незавершённый run, НЕ «частичный успех».
- **Markup**: source HTML и inline `<em>` НЕ передаются модели как разрешение самой расставлять HTML; форматирование остаётся отдельным шагом (Step 8/Phase 5). Промпт требует только текст, без разметки.
- **Первый тест контракта — Gate 0** (§9.0): изолированный вызов полного source → строгий PID JSON, проверка полноты/порядка/размера/truncation до переделки runner.

### 3.3. Почему вторая модель остаётся (Qwen)

Литература прямо запрещает same-model self-approval (Kocmi, Huang). Qwen — идеальный аудитор: как translator слаба (6.7), как verifier сильна (T3-профиль). Компетенции не пересекаются с DeepSeek-генератором. **Qwen никогда не переводит и не редактирует — только находит.**

Для локальной версии: Qwen local получает контекст 49k (`-c 49152`) — вход whole-chapter аудита ~28k + выход заметок помещается с запасом.

---

## 3.4. Параметры локальных серверов v4.1 (актуальные, 2026-08-09)

### Gemma (генератор; sycl-edge build)

Актуальная команда запуска (проверено владельцем 2026-08-08/09; бинарь — `C:\src\llama-sycl-edge\build\bin\llama-server.exe`):

```powershell
cd C:\src\llama-sycl-edge\build\bin
$env:GGML_SYCL_FA_DECODE_KERNEL = "auto"

.\llama-server.exe `
  --model C:/llama-cpp/models/gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf `
  -ngl 99 `
  -ncmoe 18 `
  -dev SYCL0 `
  --load-mode mmap `
  --reasoning-budget 2048 `
  -np 1 `
  -c 49152 `
  -fa on `
  --jinja `
  -ctk q8_0 `
  -ctv q4_0 `
  --cache-ram 0 `
  --ctx-checkpoints 0 `
  --port 8094
```

Ключевые изменения vs v4 (`GEMMA_SERVER_ARGS` в v4_phase12_strict_run.py:73-87):

| Параметр | v4 | v4.1 |
|---|---|---|
| Бинарь | `C:\llama-cpp\llama-server.exe` | **`C:\src\llama-sycl-edge\build\bin\llama-server.exe`** (sycl-edge) |
| `GGML_SYCL_FA_DECODE_KERNEL` | — | **`auto`** (env, до запуска) |
| `-dev` | — | **`SYCL0`** |
| `--reasoning-budget` | `0` | **`2048`** (reasoning для whole-chapter генерации) |
| `-c` (контекст) | `32768` | **`49152`** (49k) |
| `-ctk` | (не задан) | **`q8_0`** |
| `-ctv` | (не задан) | **`q4_0`** |
| `-fa on`, `--jinja`, `--cache-ram 0`, `--ctx-checkpoints 0`, `-np 1`, `-ngl 99`, `-ncmoe 18`, `--load-mode mmap` | те же | без изменений |
| Порт | 8094 | 8094 (без изменений) |

**Qwen (аудитор):** параметры обновятся отдельно (владелец предоставит; текущее: `-c 49152`, `-ctk q8_0 -ctv q8_0`, `--reasoning-budget 0` для аудита — reasoning аудитора low/2048, см. §0.1/§6).

**Влияние на identity/resume:** смена `server_args` (бинарь, `-c`, `-ctk/-ctv`, `--reasoning-budget`) входит в identity → старые out-dir'ы не resumable; `validate_reasoning_backend()` больше НЕ должен блокировать `--reasoning>0` для local (проверено владельцем: reasoning-budget 2048 работает).

---

## 4. Промпт перевода (новый, versioned)

Заменяет `BALANCED_LITERARY_V1` (prompts.py). Основан на self-report (перевод по эффекту, анти-калька, мат по силе) + locked glossary/book_memory.

```text
You are a professional literary translator rendering an English fiction
chapter into natural, polished Russian. You have already read the whole
chapter: use that context to hold each character's voice, the emotional
register of every scene, and consistent decisions from the first to the
last paragraph.

BOOK CONTEXT (locked, authoritative — do not contradict):
{book_memory: персонажи/пол/отношения/факты, отфильтрованные по главе}

LOCKED GLOSSARY (use these translations consistently, do not vary):
Blake -> Блэйк
Paige -> Пэйдж
Other -> Иной
... (A1.1-фильтр: только термины главы + locked)

Translate by EFFECT, not by dictionary match:
- profanity: match the source's strength and register exactly. Never
  soften or intensify it. "Jesus fuck" -> "Господи блядь", "fuck off" ->
  "отъебись", "I don't give a flying fuck" -> "мне до одного хуя".
  Mild substitutes (drat, darn) stay mild ("чертовщина", "чёрт").
- sarcasm, humor, anger: preserve the character's voice, not the words.
- formal/archaic address stays archaic ("Master Blake" -> "мастер Блейк").

Avoid calques: rebuild the sentence under Russian syntax and intonation
("wannabe-architect" -> "недоархитектор", "two-theater podunk town" ->
"городишко с двумя кинотеатрами"). Do not keep English word order.

Preserve exact details: numbers, times, names, quantities ("Two past
twelve" = 00:02 -> "две минуты первого").

Do not omit, summarize, or add anything. Do not output any HTML or
markup — plain Russian text only.

Return STRICT JSON: an object mapping every PID from the SOURCE map to
its Russian translation, keys in exactly the same order as the source,
no missing keys, no extra keys, no duplicate keys. Do not wrap the JSON
in markdown fences or add commentary.
```

Правила версии: новый `PromptTemplate` с version `pact-v4-prompt-balanced-literary/v3`; версия входит в bundle identity (смена = инвалидация кэша, осознанно).

---

## 5. Bible/glossary по главе: автоматический chapter_index

### 5.1. Текущее состояние (проверено в коде)

- **Bible** (`render_bible_section`, bible_renderer.py:176): глобальный блок, НЕ фильтруется по главе; капы: 20 персонажей / 30 фактов / 10 адресных форм («первые N»). Нерелевантные главе персонажи (Aimon, Duncan…) занимают место, нужные могут отрезаться.
- **Glossary** (A1.1 `_glossary_entries_for_chunk`, _shared_runner_helpers.py:157): фильтр по чанку (source-термин в owned+left+right ИЛИ always_include: risk-категории/конфликты/narrator).

### 5.2. Решение: автоматический детерминированный chapter_index (без моделей, без ручного утверждения)

Владелец (2026-08-08): «не согласен утверждать что-либо руками, только автоматически и желательно без моделей» — и напомнил, что B9-промоут glossary уже автоматический.

**Механизм (аналог B9):**
1. **Скрипт `build_chapter_index.py`** (детерминированный, 0 модельных вызовов):
   - для каждой главы, отдаваемой на перевод, сканирует её source-текст;
   - для каждого персонажа/сущности/адресной формы из book_memory проверяет присутствие имени/термина в source главы (тот же `_term_present`, что в A1.1: `(?<!\w)...(?!\w)`, IGNORECASE, multi-word);
   - факты привязываются к персонажам/сущностям: факт включается, если в главе есть хотя бы один из его упоминаемых ключей (персонаж/место/термин) — детерминированный деривация из book_memory, без LLM;
   - narrator/обязательные (gender, locked) включаются всегда (fail-closed, как always_include в A1.1).
2. **Результат** — `chapter_index.json` в memory-dir: `{chapter_id: {characters: [...], facts: [...], address: [...]}}`, детерминированный, пересобирается скриптом, **без ручного утверждения** (владелец правит исходные book_memory/правила, не индекс).
3. **Промпт**: `render_bible_section(chapter_id)` берёт из chapter_index, а не «первые N».

**Правила деривации (зафиксированы, решение владельца 2026-08-08):**
- **Персонажи/сущности/адреса**: включаются, если их имя/термин присутствует в source-тексте главы (`_term_present`: `(?<!\w)...(?!\w)`, IGNORECASE, multi-word). Source — английский, book_memory — английский → прямых коллизий нет.
- **Факты**: факт включается, если в главе присутствует **хотя бы один** из его ключей (персонаж/место/термин из fact entry в book_memory). Связь «факт → ключи» — явная структура book_memory (факт ссылается на сущности), не свободный текст.
- **Обязательные (всегда)**: narrator_gender, narrator-имя, locked-термины, glossary-конфликты — fail-closed, как always_include в A1.1.
- **Политика при неявных упоминаниях**: персонаж/факт, критичный до первого прямого имени (неявная ссылка), НЕ включается индексом, если нет ключа в source. Для главы 0001 покрывается обязательными + присутствием имён; редкие неявные кейсы → future rule refinement (вносится в этот документ), не блокер. Если после прогона найдётся реальный кейс неявного упоминания — правило уточняется записью в DECISIONS.md.
- **Пороги частотности как в B9 (term_min_chapters и т.п.) НЕ применяются**: индекс ≠ glossary; для индекса достаточно присутствия в одной главе.

**Почему это НЕ конфликт с решением владельца «библия не режется» (DECISIONS 2026-08-06):** то решение запрещало *автоматическую эвристику релевантности на лету в рантайме* (bible budgeter). Здесь — *детерминированный предварительный индекс*, построенный скриптом по формальным правилам (присутствие термина), тот же принцип, что B9-глоссарий (автоматический promote с порогами). Никакая LLM-эвристика не участвует.

**Ограничение (осознанное):** персонаж/факт может быть критичен до первого прямого появления имени (напр. неявные упоминания). Для главы 0001 это покрывается обязательными (narrator/locked) + присутствием имён; редкие неявные кейсы — предмет будущих уточнений правил, не блокер.

### 5.3. Glossary: A1.1 с входом = вся глава

Механизм A1.1 сохраняется; вход меняется с чанка на **текст всей главы** → в промпт попадают только термины главы + locked. (Прогон 4.1 показал: 43 уникальных из 137 — фильтр уже работает.)

### 5.4. Механизм наполнения (B9) — НЕ меняется

B9 (`v4_book_run.py` → `glossary_candidates` → `_auto_promote_glossary`, стр. 450) читает `translations.json` (`{pid: text}`) + source; **полностью автоматический** (авто-промоут, без ручного утверждения). Зависит только от формата, не от chunking. В whole-chapter режиме translations.json пишется так же → кандидаты/promote между главами работают. Consensus-target считается по единственному варианту (упрощение, не поломка).

---

## 6. Промпт аудита Qwen (новый, versioned)

Заменяет `QWEN_AUDIT_V1` (prompts_runtime.py). Категории расширены, добавлена консервативность.

```text
You are a strict but conservative fidelity auditor for a Russian literary
translation of an English fiction chapter. You are given the full chapter
as two ordered PID maps: SOURCE (PID -> English) and TRANSLATION (PID ->
Russian). Judge fidelity ONLY on these classes of errors:

- omission: a source element (word, clause, name, number, idiom) is missing
- addition: content not present in the source was introduced
- referent: a pronoun/referent/named entity is wrong or ambiguous
- invented gender: the source does not specify gender but the translation
  assumes one (e.g. "a wannabe-architect" -> "архитекторша")
- changed_fact: a number, time, quantity, or factual detail changed
  ("Two past twelve" must stay 00:02, never "две минуты двенадцатого")
- negation: the scope of a negation changed

Do NOT report: a different but valid synonym; a free idiomatic rephrasing;
a construction that merely differs from the English syntax. A stylistically
imperfect but semantically faithful translation is PASS. When uncertain,
PASS — do not guess.

Return STRICT JSON, no markdown, no commentary:
  issues: array of objects, each with:
    pid: string
    category: exactly one of 'omission' | 'addition' | 'referent' |
              'invented_gender' | 'changed_fact' | 'negation'
    severity: 'major' | 'minor'
    confidence: 'high' | 'medium' | 'low'
    note: short string describing the problem
    excerpt: optional short quoted fragment from the translation
If the chapter is faithful, return {"issues": []}.
```

Механика:
- Парсер категорий расширяется (`audit.py` QWEN_AUDIT_CATEGORIES): omission/addition/referent/invented_gender/changed_fact/negation; **`scene` НЕ входит в автоматический repair contract — допускается только diagnostic-only observation в будущем** (решение 2026-08-08).
- **Schema дополнена `severity` (major/minor) + `confidence` (high/medium/low)** — данные для repair eligibility (B2): только major + high-confidence ремонтируются автоматически; minor/medium/low → diagnostic/debt.
- Reasoning для Qwen-аудита: **low (remote) / 2048 (local)** — отдельный параметр от генерации (проверочная задача, high даёт переинтерпретацию).
- Аудит whole-chapter одним вызовом: источник + перевод ~28k токенов → влезает в Qwen remote (1M) и Qwen local (49k после правки `-c`).

---

## 7. Снапшоты translations между этапами (новая правка)

Для атрибуции «кто косячит/улучшает» — три полных снапшота + diff-отчёт:

| Файл | Когда | Содержание |
|---|---|---|
| `translations_raw.json` | сразу после генерации | `{pid: text}` как выдала модель |
| `translations_repaired.json` | после всех repair-циклов | raw + фиксы |
| `translations_final.json` | после formatting | новый снапшот (в B13 утверждён `translations.json` как единый финальный source of truth; здесь — отдельный snapshot для атрибуции, не «уже существующий файл») |
| `translation_diffs.json` | конец прогона | `{этап: {pid: {before, after}}}` для изменённых PID |

Применение:
- Атрибуция регрессий: final < raw → видим по diff, какой этап и какие PID испортил (кейс «repair портит хороший перевод» — теперь доказуемо).
- `repair precision` = улучшившие фиксы / все фиксы — считается точно по diff + ревью.
- Сверка с Independent №3: сравнивается `translations_raw.json` (не final) — чистый эффект pipeline-обвязки.

---

## 8. Объём работ (для будущих карточек, сейчас НЕ создаются)

Последовательность согласована владельцем (2026-08-08): **Gate 0 → A1 → A2 → B → B2 → C**. Каждый блок — отдельная пара карточек I+RV на `dev/v4.1-reasoning-transport`; карточки создаются только после утверждения плана.

### Gate 0 — технический spike (обязателен, до блока A1; НЕ production-код, НЕ запуск pipeline)

Изолированная проверка контракта whole-chapter генерации:
1. полный source главы 0001 → строгий PID JSON (тот же литературный промпт + JSON-контракт §3.2);
2. проверка полного PID-set, порядка, JSON-парсинга, размера output;
3. зафиксировать фактическое число output/reasoning токенов и **наличие/отсутствие truncation**;
4. проверить, задаёт ли текущий OpenCode transport (serve 1.4.7) фактический output limit, и каков дефолт провайдера (max_output_tokens не уходит в POST — opencode_backend.py:740-779; надо понять реальный серверный лимит);
5. сравнить raw PID-aware whole-chapter output с Independent №3 — **проверить гипотезу ревью: PID JSON-обёртка сама может возвращать часть потерянного quality gap** (если JSON вредит литературности заметно — обсудить prose + пост-маппинг кодом);
6. подтвердить local Gemma 2048 механизм (`--reasoning-budget 2048` в server-args) — уже проверено владельцем, зафиксировать в контракте.

Только если Gate 0 проходит — переделываем runner. **Gate 0 ПРОЙДЕН 2026-08-08** (результаты: docs/plans/V4_1_GATE_0_CONTRACT_RU.md §8). Решения, влияющие на A1: транспорт = CLI serve 1.4.7 (прямой API имеет лимит completion 65536 — закрыт); reasoningEffort = medium (почти не уступает high: 74k vs 84k reasoning); max_output_tokens = 32768; bounded retry обязателен (2 из 5 вызовов обрывались); deterministic QA на кавычки подтверждён.

### A1 — whole-chapter generation contract (без bible-фильтра, без нового промпта)
1. `generation.py` + `strict_runner.py`: whole-chapter режим (1 вызов на главу, полный PID map, строгий JSON-контракт §3.2, bounded retry на malformed/missing/extra/reordered **и на обрыв сессии** — Gate 0: 2/5 вызовов обрывались с finish=other/error).
2. **`max_output_tokens` генератора: бамп с 8192 до 32768** (решение владельца 2026-08-08; whole-chapter выход главы 0001 ≈12-19k токенов, самая длинная глава 0077 ≈21k; значение в identity, не ограничение модели; `MAX_TOKENS_CEILING=24576` для Qwen-ролей не затрагивается). **Проверка `OPENCODE_PINNED_SERVER_VERSION=1.4.7`** + **`reasoningEffort` mapping (1→low, 2→medium, 3→high)** — уже работает (117k reasoning-токенов на 0001; Gate 0 подтвердил medium), зафиксировать в карточке.
3. Identity/resume: новая identity (out-dir обязателен новый); chunk_plan остаётся для PID-карты; смена `--reasoning-budget`/`max_output_tokens` инвалидирует resume (документировать).
4. **Whole-chapter journal/provenance contract (обязательно описать в карточке A1):**
   - один `whole_chapter` generation record вместо per-chunk records; `candidate_id` = `whole_chapter:<role>:<hash>` (уровень главы, не чанка);
   - `generation_outcomes.json`/`selection_results.json`/journal: при отсутствии selection A/B `selection_results.json` пишется **всегда** с явной versioned schema (не «пустой или omit» — одна определённая форма, чтобы resume/diagnostics/B9 не гадали, артефакт ли это потерян или A/B просто не выполнялся):
     ```json
     {
       "schema": "pact-v4-whole-chapter-selection/v1",
       "mode": "not_applicable",
       "candidate_count": 1,
       "selection_performed": false,
       "coverage": "full_pid_map",
       "generation_record_id": "whole_chapter:balanced_literary:<hash>"
     }
     ```
     journal фиксирует один generation event;
   - B9 продолжает читать исходный `chunk_plan.json` ТОЛЬКО как PID-provenance (какие PIDs есть в главе) — не как источник кандидатов;
   - resume различает `translations_raw.json` (снапшот генератора) и финальный `translations.json` (алиас final) — не смешивать при восстановлении;
   - `ChunkPlanArtifact` сохраняется в исходном многосегментном виде (16 чанков главы 0001) исключительно как authoritative PID ownership/provenance artifact для B9, formatting и legacy-compatible read paths. **Whole-chapter генерация НЕ создаёт новый one-chunk ChunkPlan и НЕ использует chunk word limits** (`ChunkPlan.MAX_WORDS=640` — жёсткий потолок, `__post_init__` бросает ValueError при превышении, models.py:353-372; планировщик отклоняет лист >640 ещё на этапе планирования): она получает **деривированный упорядоченный полный PID list** (все PIDs главы в порядке source, отдельный контракт `WholeChapterPidMap`), и генератор работает с ним, а не с `chunk_plan.chunk(chunk_id)`. `PromptBundle` для whole-chapter: `chunk_id="whole_chapter"`, `owned_pids=<все>`, `owned_source=<все>` (валидация owned_source==owned_pids остаётся).
4. `translations_raw.json` снапшот (валидированный выход генератора, до QA/repair).
5. CLI: `--stop-after` → `--stop-after-generation`.

### A2 — промпт v3 + chapter_index + glossary full-chapter + снапшоты/diffs
1. `prompts.py`: `BALANCED_LITERARY_V1` → v3 (литературный промпт §4 + JSON-контракт).
2. **chapter_index (реализуемая единица, решение владельца 2026-08-08):**
   - новый `build_chapter_index.py` (детерминированный, 0 модельных вызовов, `_term_present`, факты по деривации из book_memory; narrator/locked всегда);
   - schema `chapter_index.json` в memory-dir: `{chapter_id: {characters, facts, address}}`; загрузка в `ChapterMemory`;
   - `render_bible_section(chapter_id)` вместо `render_bible_section(book_memory)` (bible_renderer.py:176);
   - регрессионные тесты: mandatory narrator/locked entries всегда присутствуют; факт, связанный с сущностью главы, включается; персонаж вне главы не включается.
   - **Одновременно запись в DECISIONS.md** (датированная, при реализации): прежний запрет bible budgeting (2026-08-06) заменяется детерминированным chapter_index; почему формальный term-presence достаточен; какие entries включаются всегда; политика при неявном упоминании (обязательные + присутствие имён покрывают главу 0001; редкие неявные кейсы — future rule refinement, не блокер).
3. `_shared_runner_helpers.py`: `_glossary_entries_for_chunk` с входом = вся глава (§5.3).
   **Locked-политика (решение владельца 2026-08-08, вариант «а»):** все существующие записи glossary считаются authoritative (presence-фильтр + always_include: конфликты/narrator/risk); отдельный locked-artifact/metadata НЕ вводится в этой версии (признак locked — будущее улучшение, если дрейф на не-locked парах станет проблемой). Фраза «+ locked» в промпте означает «весь established glossary, отфильтрованный по главе».
4. Снапшоты `translations_repaired` + `translation_diffs.json` (§7): atomic write, identity в каждом, diff раздельно raw→repaired и repaired→final; `translations.json` остаётся финальным алиасом (не конкурентный источник).

### B — single Qwen whole-chapter audit (новый контракт)
1. `prompts_runtime.py`: `QWEN_AUDIT_V1` → v2 (§6; категории без scene; «when uncertain, PASS»).
2. `audit.py`: `WholeChapterAuditEvaluator` (не per-chunk), chapter-level cache/resume identity (source + raw translation + audit template/policy + backend + reasoning); строгая валидация returned PID; категории + invented_gender/changed_fact/negation.
3. **Fail-closed policy** (§3.2-аналогия): malformed/empty audit JSON или transport failure → debt/`accepted_degraded`, НИКОГДА не «0 findings»; Qwen finding = кандидат на repair, не доказательство.
4. Reasoning Qwen-аудита: low (remote) / 2048 (local) — отдельный параметр.
5. `runtime_local.example.yaml`: qwen `-c 49152` (+ **нагрузочный тест памяти ДО прогона**: запуск сервера вручную, проверка OOM/KV-cache — решение владельца 2026-08-08; иначе блокер на проде).
6. Выпил Gemma-детектора (gemma_russian_review) и Qwen-fidelity-gate.

### B2 — selective repair (batch) + контекстный re-audit
1. Repair: **один batch-вызов на все eligible findings** (не per-finding, не per-region как в v4): промпт получает полный source + полную translation + список findings (каждый с PID/category/severity/confidence), модель возвращает правки только для явно разрешённых target PID, каждый фикс «fix only this, preserve all». Экономия: findings обычно 0–5 → 1 вызов вместо N.
2. **Микробатчи при большом числе findings** (по Cheng et al., Batch Prompting: качество падает с ростом batch — явные `[index]`-идентификаторы обязательны): если eligible findings > 4 — разбить на батчи по 3–4; итого 1–3 repair-вызова на главу.
3. **Cap на findings, не на вызовы** (решение владельца 2026-08-08): max 10 eligible findings на главу ремонтируются; сверх лимита → debt с пометкой «policy_limit: repair_findings_cap_10» (аналог remote_budget); после прогона зафиксировать, сколько ушло в debt, для будущей калибровки.
4. **Единый re-audit в конце, если был хотя бы один committed repair** (НЕ per-правка, НЕ per-раунд; это отличие от v4, где convergence re-audit шёл после каждого раунда — `_reaudit_chunks` repair.py:1148, по чанкам, батчами): **один вызов Qwen в конце** — вход = полный source + полная translation, scope ответа = ВСЕ изменённые PID + окно соседей; при превышении порога изменённых регионов — один full re-audit. Fail-closed: failed re-audit → debt, никогда не «0 findings» (Phase 0/A1c-фикс уже реализован).
5. Пропуск repair при 0 findings (TEaR).
6. Lifecycle-политика (policy qualification, а не независимое доказательство): `changed_fact`/числовые/glossary findings требуют **детерминированного подтверждения кодом**; остальные категории eligible только при `severity=major AND confidence=high`; Qwen finding = кандидат, не доказательство; Qwen-only семантические находки ниже порога → debt/diagnostic, не авто-ремонт; malformed/empty → fail-closed; когда `complete`, когда `accepted_degraded`.

### C — детерминированный formatting (после replay существующих артефактов)
1. `formatting`: убрать только model-fallback; сохранить deterministic incident report; проверить на замороженных артефактах, сколько обязательных spans останется unresolved (ожидание: ~0 на главу 0001, т.к. whole-chapter перевод держит `<em>` 101/101); unresolved → debt, не тихая потеря; результат «0 model calls» ≠ успех, если chapter стал accepted_degraded из-за formatting debt.

---

## 9. План валидации (прогоны — только владелец, вне чата)

### 9.0. Gate 0 (до кода, изолированный вызов; **запускает только владелец вручную вне чата**; архитектор подготавливает контракт/критерии и анализирует предоставленные артефакты)

**Измерение границ контекста и выхода по всем главам (решение владельца 2026-08-08):**
1. **Найти самую длинную главу** в `D:\pact\pact_chapters` (по размеру source-текста после парсинга; кандидаты по bytes: 0077 ≈59.5k, 0035 ≈58k, 0001 ≈54.6k — финально по токенам);
2. **Tokenize вход этой главы** (source → токены модели);
3. **Выход = вход × 1.3** (запас на перевод, по статистике Gemma: русский перевод длиннее английского source ≈ ×1.3) **+ reasoning budget** (Gemma 2048 / DeepSeek High);
4. Итог: **максимальные размеры входа и выхода** для всех глав → точные границы контекста (49k local / лимит remote) и точный `max_output_tokens` генератора;
5. Проверка на главе 0001: полный source → строгий PID JSON; полнота/порядок/размер/truncation;
6. Фактический output лимит OpenCode serve 1.4.7 (передаётся ли/дефолт) — **определяет бамп `max_output_tokens` генератора (≥16384; 50k допустимо как верхняя граница — в identity, не ограничение модели; точное значение выбираем вместе по результатам Gate 0)**;
7. **Нагрузочный тест Qwen local 49k** (`-c 49152`): запуск сервера вручную, проверка OOM/KV-cache до любых прогонов;
8. raw PID-aware vs Independent №3 — влияние JSON-обёртки на качество (Δ фиксируется, **решается вместе** — не автоматический блокер);
9. local Gemma 2048 механизм (подтверждён владельцем).

**Pass/fail критерии Gate 0:**
- **Жёсткие (fail → A1 не стартует):** PID completeness = 402/402; порядок exact; JSON валиден (парсится, без prose-only); нет truncation (output < фактического серверного лимита); output укладывается в identity `max_output_tokens` после бампа; **самая длинная глава токенизируется и её выход ×1.3 + reasoning укладывается в контекст** (иначе whole-chapter не покрывает книгу → нужна сегментация).
- **Мягкие (фиксируются, обсуждаются с владельцем):** Δ литературности raw PID-aware vs Independent №3 (второй судья НЕ подключается — решение владельца 2026-08-08; один blind-судья на всех точках).

### 9.1. После A1
- whole-chapter + PID-контракт, `--stop-after-generation` → `translations_raw.json` валиден (полный PID-set, порядок, без truncation); сравнение с Independent №3.
### 9.2. После A2
- + промпт v3 + glossary full-chapter → raw ~8.1–8.3, терминология без дрейфа; снапшоты/diffs корректны.
### 9.3. После B
- + аудит v2 → precision findings (реальных ошибок vs ложных); находки класса invented_gender/changed_fact/negation; fail-closed на malformed JSON.
### 9.4. После B2
- repair A/B: raw vs raw+repair → repair precision (улучшившие/все); re-audit контекстный (changed + соседи).
### 9.5. После C
- formatting: только детерминированные изменения (кавычки/`<em>`), 0 регрессий смысла; unresolved spans → debt.
### 9.6. Gemma local A/B
- whole-chapter 2048 с новым промптом → подтвердить ≈8.1 без remote.

Метрики: blind-оценка (ревью-модель/владелец), repair precision, вызовы/глава, стоимость, время, классы ошибок по категориям.

---

## 10. Открытые вопросы (для ревью плана)

Закрыты владельцем (2026-08-08): bible-фильтрация → автоматический chapter_index (§5); local Gemma 2048 → поддержанный путь через `--reasoning-budget` (§0.1); max_output_tokens → проверка в Gate 0 (§9.0); последовательность Gate 0→A1→A2→B→B2→C (§8).

Открытые:
1. `scene`-категория в Qwen-аудите: убрана из blocking (канал false positives); оставить как diagnostic observation или полностью убрать? (Рекомендация ревью: diagnostic only.)
2. Repair в whole-chapter: роль DeepSeek (генератор же) — не нарушает ли Kocmi (same-model repair после Qwen-аудита — нет, аудит от Qwen, repair от генератора — ок). Подтвердить при реализации B2.
3. Re-audit: всегда один вызов после repair, или только при N изменённых ≥ порога? (Рекомендация: при малых N — контекстный re-audit; при больших — full re-audit; порог — эмпирически.)
4. Whole-chapter выход одного вызова: риск обрезки/неполноты — решается Gate 0 (фактический серверный лимит + размер выхода); если truncation неизбежен — обсудить chunked-whole hybrid (глава → 2-3 сегмента по сценам) или prose + пост-маппинг.
5. Совместимость resume/cache: whole-chapter = новая identity (out-dir обязателен новый); chunk_plan остаётся для PID-карты — подтвердить при A1.
6. chapter_index: какие факты считать «привязанными» к главе (правила деривации) — уточнить на главе 0001 при реализации скрипта; пороги как в B9 (term_min_chapters и т.п.) не требуются (индекс ≠ glossary).

---

## 11. Non-goals (осознанно НЕ делаем)

- НЕ добавляем reasoning к Qwen-генерации/переводу — Qwen больше не генерирует: в v4.1 она только аудитор (Kocmi: аудитор ≠ генератор).
- НЕ возвращаем Gemma-детектор (116 вызовов ради дублирования сигнала).
- НЕ переписываем транспорт (opencode serve 1.4.7 остаётся; reasoningEffort уже работает — проверено).
- НЕ увеличиваем бюджет вызовов — наоборот, сокращаем (не возвращаем прежний неограниченный 297-call цикл; целевой типичный бюджет 2–5, максимум 6 при полном repair-цикле).
- НЕ делаем литературный полиш-проход (отдельный generative rewrite без доказанной пользы — литература против).
- НЕ трогаем B9-механизм наполнения glossary (остаётся автоматическим).
- НЕ вводим ручное утверждение bible-индекса/glossary (только автоматические, без моделей — решение владельца 2026-08-08).
- НЕ выпиливаем детерминированный QA (остаётся полностью, как ловитель классов ошибок, которые reasoning не лечит).
- НЕ меняем PID-архитектуру и финальный контракт `translations.json` (остаётся алиасом `translations_final`).
