# Hermes Profile Token Baseline — финальный отчёт (Phase 0)

> Отчёт-бейзлайн Phase 0 плана `2026-08-06_221320-hermes-profile-token-efficiency.md`.
> **Финальная версия (2-я волна)**: объединяет черновой
> DB-анализ (RV APPROVE) с контекст-базлайном (AGENTS.md размер +
> resolved CLI-тулсеты). Заменяет scorecard первой волны (снапшот 06:08Z) —
> финальные числа на снапшоте **2026-08-07T14:23:56Z**.
> Назначение: зафиксировать измеряемое состояние потребления токенов/вызовов
> по профилям `architect`, `developer`, `reviewer` **до** каких-либо изменений
> модели/reasoning/compression. Изменений конфигурации, prompts, toolsets,
> AGENTS.md, кода или Kanban-протокола отчёт не делает — только измеряет.

## 0. Мета и fingerprint

- Источник: `state.db` (таблицы `sessions`, `session_model_usage`, `messages`) и
  `config.yaml` трёх профилей Hermes; репозиторный `AGENTS.md`; dispatcher-
  резолв CLI-тулсетов.
- Снапшот: `generated_at_utc = 2026-08-07T14:23:56+00:00` (фиксированный
  JSON-эвиденс: `docs/audits/HERMES_PROFILE_TOKEN_BASELINE_PHASE0_evidence.json`).
- Контекст-базлайн (AGENTS.md + CLI-тулсеты): `tools/context_baseline.json`
  (`measured_at_utc = 2026-08-07T17:33:46Z`, ветка — Phase 0 branch,
  HEAD `1141373b360841268e064ea16a7f9451b5cbcee9`).
- WAL-согласованный fingerprint (sha256/размер **backup-копии** state.db через
  SQLite backup API — main+WAL в одном согласованном состоянии, `journal_mode`):

| Профиль | snapshot sha256 (первые 16) | snapshot байт | config.yaml sha256 (первые 16) |
|---|---:|---:|---:|
| architect | `b81479d9b7ad00bb` | 37 937 152 | `cf33d8ab1adafc00` |
| developer | `0d2d23b4516a1b22` | 80 855 040 | `fa83b5736392d44a` |
| reviewer | `8d97db60b6957571` | 50 274 304 | `2bb5098e41759b50` |

- Все числа ниже — **измеренные** из state.db на момент снапшота. Оценки
  помечены явно словом «оценка». Повторный запуск инструмента даст чуть другие
  суммы (state.db живые), поэтому отчёт опирается на зафиксированный JSON-снимок.

## 1. Метод

- Режим: строго read-only (`sqlite3` с `mode=ro`); колонки с приватным
  содержимым (промпты, тексты сообщений, системные промпты, заголовки,
  идентификаторы пользователей/чатов, конфигурация биллинга, креды,
  ошибки handoff/compression) **не читаются вовсе** — только по явным
  per-table allowlist'ам (`_SESSIONS_ALLOW` / `_USAGE_ALLOW` в
  `tools/hermes_profile_token_baseline.py`), никакого deny-list /
  SELECT-all; `messages` опрашивается фиксированными агрегатными запросами
  по `role`/`finish_reason`/`tool_name`.
- Инструменты воспроизводимости: `tools/hermes_profile_token_baseline.py`
  (stdlib-only, read-only, redacted-JSON на stdout) + `tools/token_analysis_derived.py`
  (производные метрики из вывода репортёра) + `tools/context_baseline.json`
  (контекст-базлайн).
- Перцентили: **линейная интерполяция между ближайшими рангами (R-7)** — тот же
  метод, что `numpy.percentile` по умолчанию и `PERCENTILE.INC` в Excel.
  Поэтому `p50` — стандартная медиана (для чётного n — среднее двух центральных
  значений), `p90` — стандартный интерполированный 90-й перцентиль. Граничные
  случаи покрыты тестами (n=1, 2, 10).
- **Снимок и fingerprint (WAL-корректно).** state.db работает в режиме WAL, и
  хэш одного main-файла `state.db` **не** описывает то, что видит read-only
  подключение (агрегаты читают main + WAL). Репортёр сначала копирует живую БД
  в согласованный snapshot через SQLite backup API (backup с read-only
  подключения включает WAL в одном согласованном состоянии), затем хэширует
  **именно snapshot** и считает все агрегаты из него. `fingerprint` в evidence —
  sha256/размер этого snapshot + `journal_mode`, т.е. fingerprint и агрегаты
  относятся к одним и тем же байтам.
- Redaction: id сессий — только sha256-префиксы (12 hex); идентификация
  конкретных задач/чатов не выполнялась.

## 2. Сводные агрегаты по профилям (все сессии)

Использованы агрегаты `sessions` (по сессии) и `session_model_usage`
(по модели/провайдеру). Медиана/p90 считаются по сессиям (R-7, см. §1).

| Профиль | Сессий | Вызовов (sum) | Вызовов p50/p90 | Input sum | Input p50/p90 | Output sum | Reasoning sum | Reasoning p50/p90 | Cache-read sum | Cache-write sum |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| architect | 23 | 1 929 | 21 / 176 | 6 244 902 | 74 960 / 227 858.8 | 1 574 329 | 1 010 435 | 13 148 / 83 714.2 | 357 905 685 | 285 236 |
| developer | 55 | 3 950 | 65 / 135 | 6 310 914 | 105 902 / 185 250.2 | 4 797 403 | 3 561 184 | 40 264 / 143 410.2 | 630 367 488 | 0 |
| reviewer | 117 | 1 275 | 9 / 19.4 | 10 574 808 | 77 606 / 147 471.2 | 613 515 | 273 323 | 1 697 / 5 464 | 91 153 152 | 0 |

Max по сессиям (измерено): architect — вызовы 944, input 2 944 126, output 728 031,
reasoning 424 156; developer — вызовы 191, input 480 308, output 317 907,
reasoning 278 196; reviewer — вызовы 42, input 273 098, output 16 337,
reasoning 9 035. Потолок `max_turns: 500` нигде не достигается.

Ключевые производные (по суммам; арифметика над измеренными суммами):

| Профиль | reasoning/input | cache-read/input | output/input | вызовов на сессию (avg) |
|---|---:|---:|---:|---:|
| architect | 16.2 % | ~57× | 25.2 % | 84 |
| developer | 56.4 % | ~100× | 76.0 % | 72 |
| reviewer | 2.6 % | ~9× | 5.8 % | 11 |

## 3. Kanban vs non-kanban (по `sessions.source`)

| Профиль | source | Сессий | Вызовы | Input | Output | Reasoning | Cache-read |
|---|---:|---:|---:|---:|---:|---:|---:|
| architect | desktop | 8 | 1 461 | 3 639 559 | 1 066 506 | 643 820 | 308 215 296 |
| architect | kanban | 14 | 278 | 1 268 844 | 380 762 | 282 612 | 28 638 464 |
| architect | telegram | 1 | 190 | 1 336 499 | 127 061 | 84 003 | 21 051 925 |
| developer | kanban | 55 | 3 950 | 6 310 914 | 4 797 403 | 3 561 184 | 630 367 488 |
| reviewer | desktop | 2 | 3 | 40 150 | 151 | 62 | 18 944 |
| reviewer | kanban | 115 | 1 272 | 10 534 658 | 613 364 | 273 261 | 91 134 208 |

- **developer** — 100 % kanban (55/55 сессий). **reviewer** — 99.6 % input
  kanban (115/117 сессий).
- **architect** — kanban лишь 20.3 % input (14 сессий); основная масса —
  desktop (58.3 %) и одна telegram-сессия (21.4 %); каналы смешаны, как и
  ожидалось для профиля-«оркестратора».

## 4. Модели / провайдеры / reasoning (из `session_model_usage` + `model_config`)

| Профиль | model | provider | Вызовов | Input | Output | Reasoning | Cache-read | Сессий |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| architect | deepseek-v4-flash | opencode-go | 1 687 | 3 733 793 | 1 433 318 | 948 918 | 317 109 120 | 23 |
| architect | deepseek-v4-flash | opencode-go (chat_completions) | 181 | 876 871 | 101 811 | 47 344 | 34 965 248 | 1 |
| architect | qwen3.7-plus | opencode-go (anthropic_messages) | 17 | 614 812 | 3 896 | 0 | 1 482 389 | 1 |
| architect | gpt-5.6-terra | openai-codex (subscription_included) | 28 | 454 208 | 22 457 | 5 962 | 1 969 664 | 1 |
| architect | mimo-v2.5 | opencode-go (chat_completions) | 7 | 339 223 | 2 897 | 1 832 | 842 496 | 1 |
| architect | New | moa (chat_completions) | 9 | 225 995 | 9 950 | 6 379 | 1 536 768 | 1 |
| architect | deepseek-v4-flash | auto | 137 | 149 554 | 83 786 | 62 278 | 32 896 | 23 |
| architect | gpt-5.6-terra | auto | 3 | 111 307 | 14 179 | 0 | 0 | 2 |
| architect | gemini-3.6-flash | — | 1 | 395 | 1 | 0 | 0 | 1 |
| developer | deepseek-v4-flash | opencode-go | 3 950 | 6 310 914 | 4 797 403 | 3 561 184 | 630 367 488 | 53 |
| developer | deepseek-v4-flash | auto | 526 | 181 343 | 91 081 | 82 244 | 135 936 | 53 |
| developer | gemini-3.6-flash | — | 1 | 374 | 0 | 0 | 0 | 1 |
| developer | deepseek-v4-flash | — | 1 | 301 | 67 | 63 | 256 | 1 |
| reviewer | gpt-5.6-terra | openai-codex (subscription_included) | 896 | 8 323 360 | 460 141 | 199 315 | 62 553 600 | 98 |
| reviewer | gpt-5.6-luna | openai-codex (subscription_included) | 364 | 2 210 858 | 143 076 | 67 585 | 28 066 304 | 18 |
| reviewer | gpt-5.6-terra | auto | 161 | 71 147 | 5 830 | 0 | 0 | 49 |
| reviewer | gpt-5.6-luna | auto | 109 | 45 015 | 5 426 | 0 | 0 | 12 |
| reviewer | deepseek-v4-flash | opencode-go | 15 | 40 590 | 10 298 | 6 423 | 533 248 | 1 |

(Строки с provider `auto`/пустым mode — дубли-подписи сессий в
`session_model_usage`; для сумм по сессиям корректна таблица `sessions`, см.
§7 ограничения.)

Наблюдаемые модели: architect/developer — deepseek-v4-flash/opencode-go (рабочая
пара); reviewer — gpt-5.6-terra+gpt-5.6-luna/openai-codex. Внимание:
конфиг-дефолт reviewer — gpt-5.6-luna, а доминирующая наблюдаемая —
gpt-5.6-terra (не смешивать).

Reasoning-effort, записанный в сессиях (`model_config.reasoning_config.effort`):

| Профиль | medium | high |
|---|---:|---:|
| architect | 4 | 19 |
| developer | 55 | 0 |
| reviewer | 1 | 116 |

(Настроенные дефолты из `config.yaml`: developer `reasoning_effort: medium`;
architect и reviewer `high`; `max_turns: 500` у всех; `disabled_toolsets: [bfl]`
у всех.)

## 5. Сигналы завершения / reclaim / cancellation

- `sessions.end_reason`: architect — 22× NULL + 1× `ws_orphan_reap`; developer —
  55× NULL; reviewer — 114× NULL + 1× `agent_close` + 2× `ws_orphan_reap`.
  Детальных меток reclaim/cancellation для kanban-задач в state.db **нет** (они
  живут в `kanban.db` — вне разрешённых входов этого отчёта).
- `messages.finish_reason` (по всем сообщениям):

| Профиль | stop | tool_calls | length | (null) |
|---|---:|---:|---:|---:|
| architect | 334 | 1 979 | 0 | 2 647 |
| developer | 52 | 4 004 | 1 | 4 580 |
| reviewer | 117 | 1 155 | 0 | 2 793 |

  Один `finish_reason='length'` у developer — единственный признак обрезанного
  ответа (на 117+ сессий всех профилей). Систематического reclaim/обрезания по
  state.db **не видно**; вывод «нет систематического вклада» — отсутствие
  сигнала в разрешённом источнике, а не доказательство отсутствия (§7).
- Top-tools (counts, измерено): у всех доминирует `terminal` (arch 1 703,
  dev 2 570, rev 1 281); developer — инструментальная работа (`patch` 565,
  `read_file` 771); reviewer — чтение/kanban (`skill_view` 337, `kanban_show` 214,
  `kanban_comment` 101, `kanban_block` 70, `kanban_complete` 50); architect —
  тоже kanban-инструменты среди top-10 (`kanban_show` 42, `skill_view` 40).

## 6. Контекст-базлайн: resolved CLI-тулсеты и AGENTS.md

Измерено на HEAD ветки отчёта (Phase 0 branch, `git show HEAD:AGENTS.md` —
только чтение; источник — `tools/context_baseline.json`).

- **Resolved CLI-тулсеты** (путь разрешения тот же, что у диспетчера:
  `hermes_cli.kanban_db._resolve_worker_cli_toolsets(<profile_dir>)`; источник —
  `config.yaml` → `platform_toolsets.cli` (явный список) минус
  `agent.disabled_toolsets` = `[bfl]`):
  - **architect: 14** — browser, clarify, code_execution, cronjob, delegation,
    file, kanban, memory, session_search, skills, terminal, todo, vision, web;
  - **developer: 10** — code_execution, delegation, file, kanban, memory,
    session_search, skills, terminal, todo, web;
  - **reviewer: 10** — тот же список, что developer.
- Прочее из `config.yaml` (одинаково у всех): `delegation.max_iterations: 50`,
  filesystem checkpoints включены, `compression` идентичен (enabled,
  threshold 0.5, target_ratio 0.2, protect_last_n 20, proactive_prune_tokens 0),
  `prompt_caching.cache_ttl: 5m`.
- **AGENTS.md (измерено, HEAD `1141373b360841268e064ea16a7f9451b5cbcee9` == HEAD
  ветки отчёта; содержимое == origin/main — Phase-0 коммиты AGENTS.md не меняют):**
  - 21 824 байта / 17 227 UTF-8 символов / 311 строк (splitlines; `wc -l`
    согласуется; с хвостовым newline `len(split('\n'))` = 312);
  - sha256 `18262785f6b71e67002daac95b1120b09cd17414afb94450f8ff177e7840a4f3`;
  - оценка токенов (по 4/3 символа на токен, НЕ токенизатором): ≈ 4.3–5.7K
    токенов на сессию холодного старта — константный вклад в static context
    каждого worker-раза.

## 7. Top-5 сессий по input и по reasoning (id — sha256-префиксы)

### architect
| метрика | id (ред.) | source | model | effort | вызовов | msgs | tools | input | reasoning |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|
| input | `593a124a2054` | desktop | deepseek-v4-flash | medium | 944 | 339 | 158 | 2 944 126 | 424 156 |
| input | `58df02b27639` | telegram | deepseek-v4-flash | medium | 190 | 409 | 188 | 1 336 499 | 84 003 |
| input | `d4840b0674be` | desktop | deepseek-v4-flash | medium | 120 | 271 | 135 | 228 891 | 49 562 |
| input | `005c62b0d947` | desktop | deepseek-v4-flash | high | 276 | 555 | 263 | 223 730 | 115 581 |
| input | `67aaa81efd8e` | kanban | deepseek-v4-flash | high | 58 | 133 | 74 | 165 418 | 82 559 |
| reasoning | `593a124a2054` | desktop | deepseek-v4-flash | medium | 944 | 339 | 158 | 2 944 126 | 424 156 |
| reasoning | `005c62b0d947` | desktop | deepseek-v4-flash | high | 276 | 555 | 263 | 223 730 | 115 581 |
| reasoning | `58df02b27639` | telegram | deepseek-v4-flash | medium | 190 | 409 | 188 | 1 336 499 | 84 003 |
| reasoning | `67aaa81efd8e` | kanban | deepseek-v4-flash | high | 58 | 133 | 74 | 165 418 | 82 559 |
| reasoning | `49ba52d34a62` | kanban | deepseek-v4-flash | high | 31 | 68 | 36 | 147 597 | 55 956 |

### developer (все — kanban)
| метрика | id (ред.) | source | model | effort | вызовов | msgs | tools | input | reasoning |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|
| input | `c6f8d9f3f41e` | kanban | deepseek-v4-flash | medium | 191 | 235 | 116 | 480 308 | 228 105 |
| input | `daead227a833` | kanban | deepseek-v4-flash | medium | 28 | 63 | 34 | 252 771 | 12 494 |
| input | `f83ecd9fd0ab` | kanban | deepseek-v4-flash | medium | 111 | 233 | 121 | 215 899 | 227 140 |
| input | `22852dba5dd9` | kanban | deepseek-v4-flash | medium | 169 | 345 | 175 | 205 670 | 231 941 |
| input | `1ff0305ac249` | kanban | deepseek-v4-flash | medium | 129 | 283 | 153 | 192 401 | 38 743 |
| reasoning | `21a16f144d54` | kanban | deepseek-v4-flash | medium | 137 | 291 | 153 | 168 618 | 278 196 |
| reasoning | `22852dba5dd9` | kanban | deepseek-v4-flash | medium | 169 | 345 | 175 | 205 670 | 231 941 |
| reasoning | `c6f8d9f3f41e` | kanban | deepseek-v4-flash | medium | 191 | 235 | 116 | 480 308 | 228 105 |
| reasoning | `f83ecd9fd0ab` | kanban | deepseek-v4-flash | medium | 111 | 233 | 121 | 215 899 | 227 140 |
| reasoning | `8b6b152f8627` | kanban | deepseek-v4-flash | medium | 125 | 253 | 127 | 176 453 | 191 985 |

### reviewer (все — kanban)
| метрика | id (ред.) | source | model | effort | вызовов | msgs | tools | input | reasoning |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|
| input | `cd8e21a9de49` | kanban | gpt-5.6-luna | high | 29 | 85 | 55 | 273 098 | 6 446 |
| input | `fc111add0304` | kanban | gpt-5.6-terra | high | 14 | 48 | 33 | 269 814 | 6 437 |
| input | `169b5fc67904` | kanban | gpt-5.6-terra | high | 14 | 50 | 35 | 231 563 | 2 974 |
| input | `94404d816383` | kanban | gpt-5.6-luna | high | 42 | 93 | 50 | 208 991 | 6 933 |
| input | `e2cdcf50cd64` | kanban | gpt-5.6-luna | high | 18 | 54 | 35 | 200 314 | 3 967 |
| reasoning | `9a8cd2457056` | kanban | gpt-5.6-luna | high | 20 | 52 | 33 | 133 306 | 9 035 |
| reasoning | `a95bca0fd1d2` | kanban | gpt-5.6-terra | high | 14 | 47 | 32 | 120 405 | 8 351 |
| reasoning | `aff3995f3a75` | kanban | gpt-5.6-terra | high | 18 | 49 | 30 | 145 114 | 8 007 |
| reasoning | `2161ec3b77b2` | kanban | gpt-5.6-luna | high | 29 | 80 | 50 | 178 533 | 7 893 |
| reasoning | `94404d816383` | kanban | gpt-5.6-luna | high | 42 | 93 | 50 | 208 991 | 6 933 |

## 8. Дрейф между снапшотами (06:08Z первая волна → 14:23:56Z этот снапшот)

| Профиль | Сессий | Вызовов | Input | Δ вызовов | Δ input |
|---|---:|---:|---:|---:|---:|
| architect | 14 → 23 | 1 678 → 1 929 | 5.25M → 6.24M | +15.0 % | +18.9 % |
| developer | 51 → 55 | 3 807 → 3 950 | 6.08M → 6.31M | +3.8 % | +3.9 % |
| reviewer | 113 → 117 | 1 182 → 1 275 | 9.94M → 10.57M | +7.9 % | +6.4 % |

Рост за ~8 ч ожидаем (активные сессии пишутся). architect вырос сильнее всех —
за счёт длинных desktop/telegram-сессий (см. §3), а не kanban-части.

## 9. Доминирующие драйверы (ИНТЕРПРЕТАЦИЯ поверх измеренных данных)

Ниже — аналитические выводы. Числа — измеренные (§2–§7); причинно-следственная
привязка «что именно генерирует объём» — интерпретация, помечена как таковая.

1. **developer — reasoning + инструментальные циклы (нормальная длинная работа).**
   Reasoning = 56.4 % input (3.56M из 6.31M); output = 76 % input, но видимый
   текст — лишь 25.8 % output → ~3/4 output-токенов это reasoning. Tool-петли:
   `tool_calls` finish_reason 4 004 против 52 `stop` (≈98.7 %); top-tools —
   terminal/patch/read_file. Cache-read ÷ input = 99.9× — длинные сессии
   (p50 65 вызовов) активно переиспользуют кэшированный префикс. Потолок
   `max_turns: 500` не достигается (max 191) → это «нормальная длинная
   kanban-работа», а не обрыв/ретраи. Кандидат для Phase 2 (политика reasoning
   на задачу): снижение reasoning напрямую режет и output, и input.
2. **reviewer — повторяемый статический контекст на каждую сессию.**
   Короткие сессии (p50 9 вызовов, p90 19.4), но высокий p50 input 77 606 →
   ~8.6K токенов на вызов в среднем (оценка: p50 input ÷ p50 вызовов).
   Reasoning всего 2.6 % input — объём создаёт НЕ думание, а повторяемую
   загрузку контекста (AGENTS.md ≈ 4.3–5.7K токенов на сессию, skills —
   `skill_view` 337 вызовов, kanban-контекст — `kanban_show` 214). Cache-read ÷
   input всего 8.6× — между короткими сессиями кэш почти не переиспользуется
   (каждая ревью-сессия стартует холодно). Крупнейший рычаг Phase 1
   (компактный AGENTS.md): 115 kanban-сессий × повторяемый префикс.
3. **architect — смешанно; масса уходит в не-kanban длинные сессии.**
   Kanban-часть мала (20.3 % input, 14 сессий). Основной объём — desktop
   (58.3 %) и одна telegram-сессия (21.4 %); одна desktop-сессия
   `593a124a2054` = 2.94M input (47 % всего input профиля) при 944 вызовах —
   нормальная длинная интерактивная работа, не kanban-воркер. Reasoning
   16.2 % input. Для kanban-эффективности architect вторичен; его объём — это
   сессии владельца/оркестрации.
4. **Дубли/реclaim/retry-циклы — в state.db не видны.** По разрешённым данным
   систематического вклада нет: end_reason почти весь NULL, единственный
   `length`, один `agent_close`, три `ws_orphan_reap`. Если нужно
   подтвердить/исключить вклад reclaim-циклов — источник `kanban.db` (вне
   разрешённых входов этого отчёта).
5. **Cache-write почти нулевой** (285 236 только у architect, в telegram-сессии) —
   измеренный факт; интерпретация: провайдеры-рантаймы (opencode-go/codex) не
   сообщают cache_write на большинстве вызовов; это НЕ значит, что кэш не
   пишется.

## 10. Доступность стоимости

**USD-биллинг из нулевых cost-полей не выводится.** Все
`estimated_cost_usd`/`actual_cost_usd` = 0 (`cost_fields_nonzero_anywhere: false`),
`cost_source='none'`/`cost_status=unknown|included|None`. Приведённые токены —
метрика объёма, а не денег. **Cache-read токены не называются бесплатными**:
они записаны отдельной категорией, тарификация cache-чтений зависит от
провайдера и здесь не известна (у reviewer — `subscription_included`, но из
нулевых cost-полей стоимость не выводится).

## 11. Разделение measured / assumptions

**Measured (измерено):** все суммы/p50/p90/max токенов и вызовов, распределения
source/effort/end_reason/finish_reason, топ-инструменты, разбивки по моделям,
cache-соотношения (арифметика над измеренными суммами), дрейф между снапшотами,
размер/хэш AGENTS.md, resolved CLI-тулсеты.

**Assumptions (оценки/интерпретации):**
- «Видимый текст = output − reasoning» — по спецификации §9.10 (reasoning входит
  в output); арифметическое тождество, не прямое измерение видимых токенов.
- «~8.6K токенов на вызов у reviewer» — p50 input ÷ p50 вызовов (грубая оценка).
- AGENTS.md ≈ 4.3–5.7K токенов — по 4/3 символа на токен (не токенизатором).
- Причинная привязка драйверов (§9) — интерпретация паттернов, не измерение.
- «Cache-read ÷ input» — соотношение объёмов, НЕ утверждение о бесплатности.

## 12. Ограничения

1. **Доллары не выводятся.** Все cost-поля = 0 → фактический USD-биллинг **не
   выводится**; токены — метрика объёма, не денег.
2. **Cache-read токены — не «бесплатные».** Тарификация cache-чтений зависит от
   провайдера и здесь не известна.
3. `messages.token_count` в базе не заполнен (0 строк > 0) — поминутная/
   по-сообщённая атрибуция токенов невозможна; надёжный слой — агрегаты
   сессий/моделей.
4. В `session_model_usage` есть «дубли» по провайдеру `auto` (одна сессия может
   быть записана под несколькими (model, provider)); для итогов по сессиям
   корректна таблица `sessions`.
5. Reclaim/cancellation-метки kanban-задач живут в `kanban.db` (board) — вне
   разрешённых входов; по state.db видны только `end_reason` и `finish_reason`.
6. state.db живые: суммы дрейфуют со временем; зафиксирован JSON-снимок
   (fingerprint внутри, `generated_at_utc`).
7. **Fingerprint — это snapshot, а не сырой main-файл.** state.db работает в
   WAL; `fingerprint.snapshot_sha256` — sha256 согласованной backup-копии
   (main + WAL), из которой реально посчитаны агрегаты. Хэш одного `state.db`
   до чтения не был бы согласован с агрегатами и потому не используется;
   повторные запуски дают новый snapshot (дрейф данных).
8. Идентификация сессий — ред. sha256-префиксы; сопоставление с конкретными
   задачами/чатами не выполнялось и в отчёте не записано.
9. Конфиг-дефолт ≠ доминирующая наблюдаемая модель (reviewer: config
   gpt-5.6-luna, наблюдаемая gpt-5.6-terra) — не смешивать.
10. `reasoning_config.effort` отсутствует в части строк model_config
    (architect: 4 medium записаны явно, остальное high) — при отсутствии NULL,
    не подставлять.
11. Длительность: у живых сессий `ended_at` NULL → в duration-метрики не
    включались (в этой карточке длительность не считалась).
12. `output_tokens` включает reasoning (developer ~74 % output — reasoning);
    видимый текст ≈ output − reasoning.
13. Активный чат-рантайм (gpt-5.6-terra/openai-codex) — это текущая сессия
    владельца, а не воркер-использование; воркер-профили разбираются по своим
    state.db (см. §4).

## 13. Рекомендация: ОДИН обратимый пилот

**Пилот: Phase 1 — компактный authoritative project context (AGENTS.md).**

Обоснование по данным:
- Наибольший устойчивый вклад в input у **reviewer** — повторяемый статический
  контекст (97.4 % input не-reasoning; p50 input 77 606 при p50 9 вызовах;
  cache-read лишь 8.6× input — холодные старты). `AGENTS.md` ≈ 4.3–5.7K токенов
  загружается на каждую сессию всех трёх профилей; это измеряемый, полностью
  обратимый docs-only рычаг.
- У **developer** доминирует reasoning (56.4 % input) — кандидат на Phase 2
  (per-task reasoning policy), но Phase 2 меняет политику reasoning и требует
  базы из Phase 1 для сравнения; поэтому в качестве первого пилота выбирается
  только Phase 1.
- Phase 1 — docs-only: коммит в git, откат = revert; не трогает
  model/reasoning/compression-дефолты, toolsets, prompts, cache, Kanban-протокол.

Критерий перехода: после Phase 1 замерить тот же бейзлайн тем же инструментом и
сравнить input/p50/p90; если сжатие AGENTS.md без потери правил даст
материальное снижение повторяемого контекста — переходить к Phase 2 (reasoning)
как второму пилоту, снова через I+RV-цикл. Никаких других пилотов в этой фазе
не предлагается (ровно один).

## 14. Воспроизводимость (команды)

```text
# 1) свежий согласованный WAL-снапшот (суммы чуть дрейфуют — fingerprint в
#    evidence описывает именно тот snapshot, из которого посчитаны агрегаты):
python tools\hermes_profile_token_baseline.py --json live.json

# 2) производные метрики из вывода репортёра:
python tools\token_analysis_derived.py live.json

# 3) структурная сверка отчёта с evidence (таблицы, top-5, производные,
#    fingerprint; --self-test доказывает, что подмена числа из другого
#    профиля обнаруживается):
python tools\verify_baseline_report.py
python tools\verify_baseline_report.py --self-test

# 4) размер/хэш AGENTS.md (PowerShell):
Get-FileHash AGENTS.md -Algorithm SHA256
(Get-Item AGENTS.md).Length                        # 21824 байт
(Get-Content AGENTS.md -Raw).Length                # 17227 UTF-8 символов

# 5) redaction-check (встроен в verify_baseline_report.py): 0 совпадений с
#    запрещёнными паттернами (kanban task-ids, worktree-ветки, абсолютные
#    пути, имена приватных колонок/секретов) в report/evidence/context_baseline.json
```

Итоговые агрегаты — в `docs/audits/HERMES_PROFILE_TOKEN_BASELINE_PHASE0_evidence.json`;
контекст-базлайн — в `tools/context_baseline.json`.
