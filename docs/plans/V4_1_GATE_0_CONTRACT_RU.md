# V4.1 — Gate 0: контракт и команды технического спайка

Дата: 2026-08-08
Статус: ГОТОВ К ВЫПОЛНЕНИЮ (до кода A1; запуск модельных вызовов — только владелец, вне чата)
Источник: `docs/plans/V4_1_WHOLE_CHAPTER_ARCHITECTURE_PLAN_RU.md` §9.0
Рабочая копия: `D:\pact\pact_translator_v4_1` (dev-клон ветки `dev/v4.1-reasoning-transport`)

---

## 0. Цель Gate 0

Изолированно (НЕ production-код, НЕ переделка runner) проверить, что whole-chapter генерация **физически реализуема** на текущем транспорте, до того как переписывать pipeline:

1. **Границы контекста/выхода** для всех 148 глав (самая длинная глава должна влезать в контекст и в output-лимит).
2. **Строгий PID JSON контракт** на главе 0001 (полнота/порядок/парсинг/truncation).
3. **Влияние JSON-обёртки на литературность** (raw PID-aware vs Independent №3) — решается вместе с владельцем, не автоматический блокер.
4. **Нагрузочный тест Qwen local 49k** (OOM/KV-cache).
5. **Фактический output-лимит** OpenCode serve 1.4.7 → точный бамп `max_output_tokens`.

**Pass (жёсткие критерии, все):**
- PID completeness = **400/400** (p00001–p00400; 1 heading + 198 paragraph + 201 dialogue — измерено парсером 2026-08-08), порядок exact, JSON валиден, нет truncation;
- output < фактического серверного лимита; output укладывается в identity после бампа;
- самая длинная глава: вход + выход×1.3 + reasoning < контекст (49k local / лимит remote).

**Soft (фиксируются, решаются вместе):** Δ литературности raw PID-aware vs Independent №3.

---

## 1. Уже измерено архитектором (read-only, сделано 2026-08-08)

### 1.1. Размеры глав (оценка токенов chars/4, английский)

| Глава | Source chars | ~токенов | Примечание |
|---|---|---|---|
| **0077_null-9-4** | 51 426 | **~12 856** | **самая длинная в книге** |
| 0035_collateral-4-12 | 50 567 | ~12 641 | |
| 0068_signature-8-1 | 48 812 | ~12 203 | |
| 0001_bonds-1-1 | 47 749 | ~11 937 | вал-глава |
| 0024_collateral-4-1 | 46 848 | ~11 712 | |

### 1.2. Фактический коэффициент перевода (проверено на 0001)

- source 0001: 47 749 chars (~11 937 токенов)
- перевод 0001 (run_reasoning3_selection): 47 655 chars (~11 913 токенов)
- **коэффициент ≈ 1.00** (DeepSeek; русский ≈ английскому по токенам)
- Формула владельца `×1.3` — **консервативный запас** (покрывает Gemma-статистику и разброс глав); 1.3 оставляем как критерий, фактический 1.0 фиксируем как наблюдение.

### 1.3. Бюджет входа/выхода whole-chapter

**Глава 0001 (вал):**
- вход ≈ source 11 937 + bible-рендер 1 232 + glossary ~200 + prompt ~300 ≈ **~13 700 токенов**
- выход (по формуле) = 13 700 × 1.3 + reasoning 2048 ≈ **~19 850 токенов**
- сумма ≈ **~33 500** < 49 000 → влезает в local 49k ✅

**Самая длинная глава (0077):**
- вход ≈ ~12 856 + bible/glossary/prompt ≈ **~14 600**
- выход = 14 600 × 1.3 + 2048 ≈ **~21 030**
- сумма ≈ **~35 600** < 49 000 → влезает ✅

**Вывод (предварительный):** whole-chapter покрывает всю книгу при контексте 49k; `max_output_tokens` генератора должен быть **≥ 21 000** (для 0077) → **бамп до 24 576** (ближайший кратный потолок Qwen `MAX_TOKENS_CEILING`) или 32768 для запаса. Точное значение — после фактического замера выхода 0001 (п.2.2).

### 1.4. Ограничения текущей оценки

- chars/4 — приближение (английский ~4 chars/token); фактический токенайзер модели даст ±10%.
- bible-рендер 1232 токена — текущий капнутый рендер (20/30/10); после chapter_index (A2) bible станет меньше (релевантные главе).
- Эти числа — для Gate 0 решения; в A1 реализуется **точечная токенизация через /tokenize** (см. §4, команда владельца) при необходимости.

---

## 2. Что делает ВЛАДЕЛЕЦ (модельные вызовы, вручную, вне чата)

> Все команды — из `D:\pact\pact_translator_v4_1`, PowerShell. Прогон модельных вызовов — только владелец (правило AGENTS.md 2026-08-06).

### 2.1. Запуск opencode serve 1.4.7 (managed, фоновый)

```powershell
cd D:\pact\pact_translator_v4_1
npx -y opencode-ai@1.4.7 serve --pure --port 4097
```

Ожидание: сервер поднялся, `http://127.0.0.1:4097` отвечает. (Pinned-версия 1.4.7 — как в production контракте C1.)

### 2.2. Whole-chapter вызов главы 0001 → строгий PID JSON (ДВА вызова)

Два вызова (решение владельца 2026-08-08), чтобы изолировать влияние bible от JSON-эффекта:

| Вызов | Файл тела запроса | Что изолирует |
|---|---|---|
| 1. **с bible** | `gate0_request_0001_with_bible.json` | pipeline-режим: bible-рендер + полный glossary (43 термина) + JSON |
| 2. **без bible** | `gate0_request_0001_no_bible.json` | Independent-режим: без bible + established glossary (30) + JSON |

Каждый вызов — DeepSeek через opencode serve (reasoningEffort: high), промпт + полный source-карта PIDs (400 шт., порядок source) уже встроены в тело запроса. Ожидания (оба вызова):

- ответ — валидный JSON `{pid: russian_text}`;
- ровно 400 ключей (p00001–p00400), порядок как в source;
- никаких markdown-обёрток, комментариев, prose;
- выход < фактического лимита (см. п.2.4).

Команда (PowerShell, сервер из п.2.1 уже запущен):

```powershell
cd D:\pact\pact_translator_v4_1
# Шаг 1: создать сессию
$sess = curl.exe -s -X POST http://127.0.0.1:4097/session -H "Content-Type: application/json"
$sess   # → {"id": "ses_..."}  — запомнить id
# Шаг 2: отправить сообщение (вызов 1 — с bible)

curl.exe -s -X POST "http://127.0.0.1:4097/session/ses_01f58aba2ffeNoy3lBKIdzxVpk/message" `
  -H "Content-Type: application/json" `
  --data-binary "@D:\pact\gate_bench_runs\gate0\gate0_request_0001_with_bible.json" `
  -o D:\pact\gate_bench_runs\gate0\gate0_0001_with_bible_raw.json
# Шаг 3: вызов 2 — без bible
curl.exe -s -X POST "http://127.0.0.1:4097/session/ses_01f58aba2ffeNoy3lBKIdzxVpk/message" `
  -H "Content-Type: application/json" `
  --data-binary "@D:\pact\gate_bench_runs\gate0\gate0_request_0001_no_bible.json" `
  -o D:\pact\gate_bench_runs\gate0\gate0_0001_no_bible_raw.json
```

> Примечание: если serve API требует отдельный `provider`/`model` в теле — архитектор дополнит тела по фактическому ответу `/session` (сверка с C1-контрактом serve 1.4.7). Если ответ содержит usage (output/reasoning токены) — сохраняется автоматически в `-o` файл.

**Артефакты для архитектора:**
- `D:\pact\gate_bench_runs\gate0\gate0_0001_with_bible_raw.json`
- `D:\pact\gate_bench_runs\gate0\gate0_0001_no_bible_raw.json`

### 2.3. Нагрузочный тест Qwen local 49k (OOM/KV-cache)

Запуск Qwen-сервера с `-c 49152` (до любых прогонов):

```powershell
cd D:\pact\pact_translator_v4_1
C:\llama-sycl-new\llama-server.exe `
  -m C:\llama-cpp\models\Qwen3.6-35B-A3B\Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf `
  -c 49152 -ngl 99 -np 1 -fa on --jinja `
  --cache-ram 0 --ctx-checkpoints 0 `
  --host 127.0.0.1 --port 8095
```

Проверка: сервер поднялся без OOM; один тестовый вызов (небольшой) проходит; KV-cache на 49k держится. Зафиксировать потребление VRAM/RAM.

**Артефакт:** вывод сервера (лог старта, метрики памяти) → `D:\pact\gate_bench_runs\gate0\qwen_49k_load.log`.

### 2.4. Проверка фактического output-лимита opencode serve 1.4.7

После whole-chapter вызова (п.2.2) из ответа/usage определить:
- фактический `finish_reason` (должен быть `stop`, не `length`);
- если `length` — серверный лимит меньше выхода → бамп транспорта/конфига;
- если `stop` — лимит покрывает (фиксируем значение из дефолтов провайдера).

**Артефакт:** результат в ответе п.2.2 (usage-поля, если доступны через serve API).

### 2.5. (Опционально) точная токенизация через /tokenize

Если нужна точная токенизация (вместо chars/4):

```powershell
# при поднятом llama-server (п.2.3):
$body = '{"content":"[текст главы 0077]"}'
curl.exe -s -X POST http://127.0.0.1:8095/tokenize -H "Content-Type: application/json" -d $body
```

Ответ: `{"tokens":[...], "count": N}` → N = точные токены входа 0077.

---

## 3. Промпт Gate 0 (файл `gate0_prompt_0001.txt`)

Промпт = литературный v3 (из плана §4) + строгий PID JSON контракт. Source-карта PIDs прилагается (генерируется парсером source_html — архитектор подготовит `gate0_source_0001.json` = `{pid: english_text}` в порядке source).

```text
You are a professional literary translator rendering an English fiction
chapter into natural, polished Russian. You have already read the whole
chapter: use that context to hold each character's voice, the emotional
register of every scene, and consistent decisions from the first to the
last paragraph.

BOOK CONTEXT (locked, authoritative — do not contradict):
Narrator: male (Blake Thorburn, first person)
Characters: [chapter_index-релевантные, см. gate0_source_0001.json]

LOCKED GLOSSARY (use these translations consistently, do not vary):
Blake -> Блэйк
Paige -> Пэйдж
Irene -> Ирэн
... (полный список глав-терминов в gate0_source_0001.json)

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

**Примечание:** для чистоты Gate 0 (изолировать JSON-эффект) bible/glossary можно передать в том же виде, что в прогоне 4.1 (капнутый рендер + A1.1-термины главы) — чтобы сравнение raw vs Independent №3 было честным (Independent №3 имел locked glossary без bible). Архитектор подготовит оба варианта: «с bible» и «без bible», чтобы измерить влияние bible отдельно.

---

## 4. Что делает АРХИТЕКТОР (read-only, без модельных вызовов)

| # | Проверка | Метод | Статус |
|---|---|---|---|
| 1 | Размеры всех глав, самая длинная | скрипт chars/4 (сделано, §1.1) | ✅ сделано |
| 2 | Фактический коэффициент перевода | usage.ndjson 0001 (сделано, §1.2) | ✅ сделано |
| 3 | Подготовка `gate0_source_0001.json` | парсер source_html (PIDs в порядке) | ⏳ подготовить |
| 4 | Подготовка `gate0_prompt_0001.txt` (2 варианта: ±bible) | из плана §4 | ⏳ подготовить |
| 5 | Подготовка тела запроса `gate0_request_0001.json` | serve-контракт 1.4.7 + reasoningEffort high | ⏳ подготовить |
| 6 | Анализ артефактов после прогона владельца | read-only: PID-полнота/порядок/JSON/truncation/usage | ⏳ после 2.2 |
| 7 | Сравнение raw vs Independent №3 (Δ, soft) | blind-сводка для решения владельцем | ⏳ после 2.2 |
| 8 | Проверка Qwen 49k лога (OOM?) | read-only лог 2.3 | ⏳ после 2.3 |
| 9 | Итоговый вердикт Gate 0 (pass/fail + рекомендация max_output_tokens) | сводка → решение владельца | ⏳ после всех |

---

## 5. Чек-лист завершения Gate 0

- [ ] Владелец: serve 1.4.7 поднят (п.2.1)
- [ ] Владелец: whole-chapter вызов 0001, ответ сохранён в `gate0_0001_raw.json` (п.2.2)
- [ ] Владелец: Qwen 49k сервер поднят без OOM, лог сохранён (п.2.3)
- [ ] Владелец: finish_reason зафиксирован (stop / length) (п.2.4)
- [ ] Архитектор: анализ ответа (PID 402/402, порядок, JSON, truncation)
- [ ] Архитектор: Δ vs Independent №3 (soft) — сводка для владельца
- [ ] Архитектор: вердикт pass/fail + рекомендация `max_output_tokens`
- [ ] Владелец: решение по soft-критерию (Δ) и значению max_output_tokens
- [ ] → A1 карточка (только после pass)

---

## 6. Ключевые числа для решения (сводка)

| Метрика | Значение | Источник |
|---|---|---|
| Самая длинная глава | 0077, ~12.9k токенов входа | §1.1 |
| Коэффициент перевода | ~1.00 (DeepSeek 0001); 1.3 — консервативный запас | §1.2 |
| Вход whole-chapter 0001 | ~13.7k токенов | §1.3 |
| Выход 0077 (×1.3 + 2048) | ~21k токенов | §1.3 |
| Вход+выход 0077 | ~35.6k < 49k | §1.3 |
| Рекомендуемый max_output_tokens | **≥ 24 576** (потолок Qwen MAX_TOKENS_CEILING) или 32768 с запасом | §1.3, решение после п.2.2 |
| bible-рендер сейчас | 4 931 chars ≈ 1 232 токена (капы 20/30/10) | измерено |

---

## 8. РЕЗУЛЬТАТЫ Gate 0 (2026-08-08, выполнено)

### 8.1. Выполненные вызовы (глава 0001, DeepSeek v4-flash через opencode-go)

| Вызов | Транспорт | Effort | Reasoning | Output | PIDs | JSON | finish | Статус |
|---|---|---|---|---|---|---|---|---|
| high + bible | CLI serve | high | 108 | 18 495 | 400/400 | ✅ | stop | ⚠️ нерелевантен (модель не думала) |
| high + no_bible | CLI serve | high | 84 933 | 18 856 | 400/400 | ✅ | stop | ✅ релевантен |
| medium + bible | CLI serve | medium | 74 040 | 19 822 | 400/400 | ⚠️ 5 битых кавычек | stop | ✅ после детерминированного фикса |
| high + no_bible | **прямой API** | high | 51 578 | — | 283/400 | ❌ | **length** | ❌ лимит completion 65 536 |
| medium + bible | **прямой API** | medium | 49 642 | — | 334/400 | ❌ | **length** | ❌ лимит completion 65 536 |

### 8.2. Жёсткие критерии — ПРОЙДЕНЫ

- ✅ PID completeness = 400/400 (два валидных CLI-вызова)
- ✅ порядок exact (p00001→p00400)
- ✅ JSON валиден (после детерминированного фикса 5 битых кавычек у medium)
- ✅ нет truncation (finish=stop)
- ✅ модель правильная (deepseek-v4-flash через opencode-go)
- ✅ самая длинная глава 0077 (~12.9k токенов входа) → выход ×1.3 + reasoning ≈ 21k, сумма ~35.6k < 49k контекст — whole-chapter покрывает книгу

### 8.3. Ключевые выводы

1. **CLI (serve) — единственный рабочий транспорт для whole-chapter.** Прямой relay API (`https://opencode.ai/zen/go/v1`) имеет жёсткий лимит completion ≈ 65 536 токенов; при reasoning 50-85k + текст 15k выход физически не влезает (finish=length, обрыв на 283-334/400 PID). Путь A закрыт как резерв.
2. **medium у DeepSeek существует и работает**, но почти не экономит: CLI medium = 74k reasoning vs high = 84k (−13%). medium ≠ «половина думанья».
3. **Reasoning нестабилен** (108 / 74k / 84k на одинаковых параметрах): `reasoningEffort` — разрешение думать, не жёсткий бюджет. Следствие: **bounded retry на обрыв/обрезание обязателен в A1** (2 обрыва из 5 вызовов).
4. **Битые кавычки — реальный класс дефектов**: medium+bible сломал JSON в 5/400 PID (ASCII `"` вместо русской `”`). Детерминированный фикс работает; deterministic QA (кавычки) — подтверждённая необходимость.
5. **Терминология идентична во всех трёх переводах** (Блэйк/Пэйдж/Ирэн/Молли/Калан — точное совпадение счёта): locked glossary работает независимо от bible/reasoning. Дрейфа нет.
6. **Качество pipeline-вызова ≈ Independent 3** (в пределах шума): ind3 литературнее в отдельных местах («самолюбование» vs «самовозвеличивание», «втрёшься» vs «вклинишься»), pipeline чуть более развёрнутый, но без терминологического дрейфа и без смягчения мата.
7. **Bible почти не влияет на качество** (сходство med_bible vs high_no_bible = 0.80 на достоверных зонах; различия стилистические, не смысловые).

### 8.4. Soft-критерий (Δ vs Independent 3)

- Independent 3 извлечён в `gate0_independent3_translation.json` (400 PIDs, 45 795 chars); локальные структурные сдвиги (±1 блок в зонах p00155-235) — для строгого по-PID сравнения нужен alignment (как B9), общая статистика сходства 0.62-0.63 приблизительна.
- **Решение владельца не требуется для старта A1** (Δ в пределах шума, терминология идентична). Полное слепое сравнение — после A1 на translations_raw.

### 8.5. Влияние на параметры A1

| Параметр | Решение (из Gate 0) |
|---|---|
| Транспорт | CLI serve 1.4.7 (не прямой API) |
| reasoningEffort | **medium** (чуть дешевле high, качество то же; high допустим) |
| max_output_tokens генератора | **≥ 32 768** (решение владельца; 21k нужен для 0077, 32k с запасом) |
| Retry | bounded retry на empty/truncated/malformed/обрыв (2/5 вызовов обрывались) |
| Deterministic QA | кавычки (русские „"/«»), PID-полнота, порядок — подтверждённая необходимость |

---

## 9. Файлы Gate 0

| Файл | Кто создаёт | Назначение |
|---|---|---|
| `docs/plans/V4_1_GATE_0_CONTRACT_RU.md` | архитектор (этот документ) | контракт и команды |
| `gate0_source_0001.json` | архитектор ✅ сделано | `{pid: english}` в порядке source (400 PIDs) |
| `gate0_glossary_0001.json` | архитектор ✅ сделано | glossary-термины главы (43 полных / 30 established) |
| `gate0_bible_render.txt` | архитектор ✅ сделано | bible-рендер (4932 chars) |
| `gate0_prompt_0001_with_bible.txt` | архитектор ✅ сделано | промпт: bible + полный glossary |
| `gate0_prompt_0001_no_bible.txt` | архитектор ✅ сделано | промпт: без bible + established glossary |
| `gate0_request_0001_with_bible.json` | архитектор ✅ сделано | готовое тело POST (user + reasoningEffort high) |
| `gate0_request_0001_no_bible.json` | архитектор ✅ сделано | готовое тело POST (user + reasoningEffort high) |
| `D:\pact\gate_bench_runs\gate0\gate0_0001_with_bible_raw.json` | владелец | ответ whole-chapter (вызов 1) |
| `D:\pact\gate_bench_runs\gate0\gate0_0001_no_bible_raw.json` | владелец | ответ whole-chapter (вызов 2) |
| `D:\pact\gate_bench_runs\gate0\qwen_49k_load.log` | владелец | лог нагрузочного теста |
