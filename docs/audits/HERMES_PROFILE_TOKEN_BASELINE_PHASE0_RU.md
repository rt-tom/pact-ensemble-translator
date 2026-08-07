# Hermes Profile Token Baseline — Phase 0 (scorecard)

> Отчёт-бейзлайн по Phase 0 плана `2026-08-06_221320-hermes-profile-token-efficiency.md`.
> Назначение: зафиксировать измеряемое состояние потребления токенов/вызовов по
> профилям `architect`, `developer`, `reviewer` **до** каких-либо изменений
> модели/reasoning/compression. Изменений конфигурации, prompts, toolsets,
> AGENTS.md, кода или Kanban-протокола отчёт не делает — только измеряет.

## 1. Вводные и метод

- Источник: `state.db` (таблицы `sessions`, `session_model_usage`, `messages`) и
  `config.yaml` трёх профилей Hermes; репозиторный `AGENTS.md`.
- Режим: строго read-only (`sqlite3` с `mode=ro`); никакие колонки с содержимым
  (prompts, messages.content, system_prompt, title, origin_json, base_url,
  креды) не читались и не записываются.
- Инструмент воспроизводимости: `tools/hermes_profile_token_baseline.py`
  (stdlib-only, read-only, redacted-JSON на stdout).
- Снимок данных: `generated_at_utc = 2026-08-07T05:28:48Z`
  (фиксированный JSON-эвиденс: `docs/audits/HERMES_PROFILE_TOKEN_BASELINE_PHASE0_evidence.json`).
- Хэши входов на момент снимка (sha256, только метаданные):

| Профиль | state.db sha256 (первые 16) | state.db байт | config.yaml sha256 (первые 16) |
|---|---:|---:|---:|
| architect | `9cf37dd20105e409` | 31 023 104 | `b2966389abb31afc` |
| developer | `50fd1806ca7d19f8` | 75 517 952 | `fa83b5736392d44a` |
| reviewer | `98bc199bb54e4cea` | 45 928 448 | `786c415b66a22a25` |

- Все числа ниже — **измеренные** из state.db на момент снимка. Оценки
  помечены явно словом «оценка». Повторный запуск инструмента даст чуть другие
  суммы (state.db живые, в него пишут текущие сессии), поэтому отчёт
  опирается на зафиксированный JSON-снимок.

## 2. Сводные агрегаты по профилям (все сессии)

Использованы агрегаты `sessions` (по сессии) и `session_model_usage`
(по модели/провайдеру). Медиана/p90 считаются по сессиям.

| Профиль | Сессий | Вызовов (sum) | Вызовов p50/p90 | Input sum | Input p50/p90 | Output sum | Reasoning sum | Reasoning p50/p90 | Cache-read sum | Cache-write sum |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| architect | 14 | 1 678 | 40 / 276 | 5 253 728 | 82 915 / 1 336 499 | 1 258 386 | 781 379 | 13 637 / 115 581 | 327 638 677 | 285 236 |
| developer | 50 | 3 724 | 66 / 142 | 5 935 096 | 105 913 / 192 401 | 4 474 341 | 3 342 721 | 41 484 / 191 985 | 597 738 112 | 0 |
| reviewer | 112 | 1 153 | 8 / 17 | 9 762 811 | 73 091 / 145 114 | 550 487 | 240 301 | 958 / 5 139 | 82 285 312 | 0 |

Ключевые производные (по суммам):

| Профиль | reasoning/input | cache-read/input | output/input | вызовов на сессию (avg) |
|---|---:|---:|---:|---:|
| architect | 14.9 % | ~62× | 24.0 % | 120 |
| developer | **56.3 %** | ~101× | 75.4 % | 74 |
| reviewer | 2.5 % | ~8× | 5.6 % | 10 |

## 3. Kanban vs non-kanban (по `sessions.source`)

| Профиль | source | Сессий | Вызовы | Input | Output | Reasoning | Cache-read |
|---|---:|---:|---:|---:|---:|---:|---:|
| architect | kanban | 5 | 80 | 439 104 | 97 558 | 68 657 | 6 717 568 |
| architect | desktop | 8 | 1 408 | 3 478 125 | 1 033 767 | 628 719 | 299 869 184 |
| architect | telegram | 1 | 190 | 1 336 499 | 127 061 | 84 003 | 21 051 925 |
| developer | kanban | 50 | 3 724 | 5 935 096 | 4 474 341 | 3 342 721 | 597 738 112 |
| reviewer | kanban | 110 | 1 150 | 9 722 661 | 550 336 | 240 239 | 82 266 368 |
| reviewer | desktop | 2 | 3 | 40 150 | 151 | 62 | 18 944 |

Вывод: у developer и reviewer почти весь объём — kanban-работа. У architect
kanban-сессии малы (5 сессий, 80 вызовов); основной объём architect — это
desktop/telegram-сессии (в т.ч. одна большая: 891 вызов / 2,78M input).

## 4. Модели / провайдеры / reasoning (из `session_model_usage` + `model_config`)

| Профиль | model | provider | Вызовов | Input | Output | Reasoning | Cache-read | Сессий |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| architect | deepseek-v4-flash | opencode-go | 1 489 | 2 904 053 | 1 150 114 | 734 963 | 295 188 224 | 14 |
| architect | deepseek-v4-flash | opencode-go (chat_completions) | 128 | 715 437 | 69 072 | 32 243 | 26 619 136 | 1 |
| architect | qwen3.7-plus | opencode-go | 17 | 614 812 | 3 896 | 0 | 1 482 389 | 1 |
| architect | gpt-5.6-terra | openai-codex | 28 | 454 208 | 22 457 | 5 962 | 1 969 664 | 1 |
| architect | mimo-v2.5 | opencode-go | 7 | 339 223 | 2 897 | 1 832 | 842 496 | 1 |
| architect | New | moa | 9 | 225 995 | 9 950 | 6 379 | 1 536 768 | 1 |
| architect | deepseek-v4-flash | auto | 109 | 139 525 | 80 359 | 58 963 | 25 472 | 19 |
| architect | gpt-5.6-terra | auto | 3 | 111 307 | 14 179 | 0 | 0 | 2 |
| architect | gemini-3.6-flash | — | 1 | 395 | 1 | 0 | 0 | 1 |
| developer | deepseek-v4-flash | opencode-go | 3 724 | 5 935 096 | 4 474 341 | 3 342 721 | 597 738 112 | 50 |
| developer | deepseek-v4-flash | auto | 463 | 152 454 | 79 288 | 70 703 | 120 320 | 50 |
| developer | gemini-3.6-flash | — | 1 | 374 | 0 | 0 | 0 | 1 |
| reviewer | gpt-5.6-terra | openai-codex | 896 | 8 323 360 | 460 141 | 199 315 | 62 553 600 | 98 |
| reviewer | gpt-5.6-luna | openai-codex | 242 | 1 398 861 | 80 048 | 34 563 | 19 198 464 | 13 |
| reviewer | gpt-5.6-terra | auto | 161 | 71 147 | 5 830 | 0 | 0 | 49 |
| reviewer | gpt-5.6-luna | auto | 68 | 26 129 | 2 494 | 0 | 0 | 7 |
| reviewer | deepseek-v4-flash | opencode-go | 15 | 40 590 | 10 298 | 6 423 | 533 248 | 1 |

Reasoning-effort, записанный в сессиях (`model_config.reasoning_config.effort`):

| Профиль | medium | high |
|---|---:|---:|
| architect | 4 | 10 |
| developer | 50 | 0 |
| reviewer | 1 | 111 |

(Настроенные дефолты из `config.yaml`: developer `reasoning_effort: medium`;
architect и reviewer `high`; `max_turns: 500` у всех; `disabled_toolsets: [bfl]`
у всех; `platform_toolsets.cli` — см. §6.)

## 5. Сигналы завершения / reclaim / cancellation (где доступно)

- `sessions.end_reason`: у architect 13× NULL + 1× `ws_orphan_reap`;
  developer 50× NULL; reviewer 110× NULL + 2× `ws_orphan_reap`.
  В state.db более детальных меток reclaim/cancellation для kanban-задач нет —
  они живут в `kanban.db` (вне скоупа этой карточки), см. ограничения.
- `messages.finish_reason` (по всем сообщениям):

| Профиль | stop | tool_calls | length | (null) |
|---|---:|---:|---:|---:|
| architect | 319 | 1 743 | 0 | 2 331 |
| developer | 49 | 3 781 | **1** | 4 319 |
| reviewer | 112 | 1 040 | 0 | 2 573 |

  Один `finish_reason='length'` у developer — единственный зафиксированный
  признак обрезанного ответа (внутри 50 сессий); систематического тренда нет.

## 6. Текущий resolved CLI-toolset и размер AGENTS.md

- `platform_toolsets.cli` из `config.yaml` (это и есть диспетчерский resolved
  toolset для worker'ов):
  - **developer**: 10 тулсетов — file, terminal, kanban, skills, web, todo,
    memory, session_search, code_execution, delegation;
  - **reviewer**: 10 тулсетов — тот же список;
  - **architect**: 14 тулсетов — browser, clarify, code_execution, cronjob,
    delegation, file, kanban, memory, session_search, skills, terminal, todo,
    vision, web.
- У всех: `delegation.max_iterations: 50`, filesystem checkpoints включены,
  `compression` идентичен (enabled, threshold 0.5, target_ratio 0.2,
  protect_last_n 20, proactive_prune_tokens 0), `prompt_caching.cache_ttl: 5m`.
- **AGENTS.md (измерено):** 19 073 байта / 15 507 UTF-8 символов / 288 строк;
  sha256 `0495d94b28dd10ccec3178bc2480017433164acd0c8a7d17f9dbedc8567686a8`.
  Оценка токенов (по 4/3 символа на токен): ≈ 3 876–5 169 токенов на сессию
  холодного старта — это константный вклад в static context каждого worker-раза.

## 7. Top-5 сессий по input и по reasoning (id ред. — sha256 префикс)

### architect
| метрика | id (ред.) | source | model | effort | вызовов | msgs | tools | input | reasoning |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|
| input | `593a124a2054` | desktop | gpt-5.6-terra | medium | 891 | 232 | 112 | 2 782 692 | 409 055 |
| input | `58df02b27639` | telegram | deepseek-v4-flash | medium | 190 | 409 | 188 | 1 336 499 | 84 003 |
| input | `d4840b0674be` | desktop | deepseek-v4-flash | medium | 120 | 271 | 135 | 228 891 | 49 562 |
| input | `005c62b0d947` | desktop | deepseek-v4-flash | high | 276 | 555 | 263 | 223 730 | 115 581 |
| input | `cb9a9389584a` | kanban | deepseek-v4-flash | high | 40 | 97 | 56 | 145 358 | 33 231 |
| reasoning | `593a124a2054` | desktop | gpt-5.6-terra | medium | 891 | 232 | 112 | 2 782 692 | 409 055 |
| reasoning | `005c62b0d947` | desktop | deepseek-v4-flash | high | 276 | 555 | 263 | 223 730 | 115 581 |
| reasoning | `58df02b27639` | telegram | deepseek-v4-flash | medium | 190 | 409 | 188 | 1 336 499 | 84 003 |
| reasoning | `d4840b0674be` | desktop | deepseek-v4-flash | medium | 120 | 271 | 135 | 228 891 | 49 562 |
| reasoning | `cb9a9389584a` | kanban | deepseek-v4-flash | high | 40 | 97 | 56 | 145 358 | 33 231 |

### developer (все — kanban)
| метрика | id (ред.) | model | effort | вызовов | msgs | tools | input | reasoning |
|---|---|---:|---|---:|---:|---:|---:|---:|
| input | `c6f8d9f3f41e` | deepseek-v4-flash | medium | 191 | 235 | 116 | 480 308 | 228 105 |
| input | `daead227a833` | deepseek-v4-flash | medium | 28 | 63 | 34 | 252 771 | 12 494 |
| input | `f83ecd9fd0ab` | deepseek-v4-flash | medium | 111 | 233 | 121 | 215 899 | 227 140 |
| input | `22852dba5dd9` | deepseek-v4-flash | medium | 169 | 345 | 175 | 205 670 | 231 941 |
| input | `1ff0305ac249` | deepseek-v4-flash | medium | 129 | 283 | 153 | 192 401 | 38 743 |
| reasoning | `21a16f144d54` | deepseek-v4-flash | medium | 137 | 291 | 153 | 168 618 | 278 196 |
| reasoning | `22852dba5dd9` | deepseek-v4-flash | medium | 169 | 345 | 175 | 205 670 | 231 941 |
| reasoning | `c6f8d9f3f41e` | deepseek-v4-flash | medium | 191 | 235 | 116 | 480 308 | 228 105 |
| reasoning | `f83ecd9fd0ab` | deepseek-v4-flash | medium | 111 | 233 | 121 | 215 899 | 227 140 |
| reasoning | `8b6b152f8627` | deepseek-v4-flash | medium | 125 | 253 | 127 | 176 453 | 191 985 |

### reviewer (все — kanban)
| метрика | id (ред.) | model | effort | вызовов | msgs | tools | input | reasoning |
|---|---|---:|---|---:|---:|---:|---:|---:|
| input | `fc111add0304` | gpt-5.6-terra | high | 14 | 48 | 33 | 269 814 | 6 437 |
| input | `169b5fc67904` | gpt-5.6-terra | high | 14 | 50 | 35 | 231 563 | 2 974 |
| input | `94404d816383` | gpt-5.6-luna | high | 42 | 93 | 50 | 208 991 | 6 933 |
| input | `e2cdcf50cd64` | gpt-5.6-luna | high | 18 | 54 | 35 | 200 314 | 3 967 |
| input | `b28084d3e685` | gpt-5.6-terra | high | 12 | 50 | 37 | 187 230 | 4 912 |
| reasoning | `a95bca0fd1d2` | gpt-5.6-terra | high | 14 | 47 | 32 | 120 405 | 8 351 |
| reasoning | `aff3995f3a75` | gpt-5.6-terra | high | 18 | 49 | 30 | 145 114 | 8 007 |
| reasoning | `94404d816383` | gpt-5.6-luna | high | 42 | 93 | 50 | 208 991 | 6 933 |
| reasoning | `fc111add0304` | gpt-5.6-terra | high | 14 | 48 | 33 | 269 814 | 6 437 |
| reasoning | `89179a1d1b3d` | deepseek-v4-flash | medium | 15 | 33 | 17 | 40 590 | 6 423 |

## 8. Что доминирует в расходах (драйверы)

- **developer**: reasoning — 56.3 % от input (3,34M из 5,94M), output — 75.4 % от
  input; длинные «нормальные» сессии: p50 66 / p90 142 вызовов, max 191
  (потолок `max_turns: 500` не достигается). Драйвер — глубина reasoning +
  tool-петли (tool_calls finish 3 781 против 49 stop; top-tool terminal 2 407).
- **reviewer**: наоборот — reasoning всего 2.5 % input; объём создаётся
  **повторяемым статическим контекстом** (короткие сессии: p50 8 / p90 17
  вызовов, но p50 input 73k): kanban worker-контекст + skills + AGENTS.md
  загружаются на каждый раз. Cache-read всего ~8× input — контекст в основном
  не кэширован между короткими сессиями.
- **architect**: смешанно; основную массу дают **не-kanban** длинные сессии
  (одна desktop-сессия 891 вызов / 2,78M input), kanban-часть мала. Reasoning
  14.9 % input.
- Дубли/реclaim-циклы в state.db не видны (см. ограничения); по видимым данным
  систематического вклада retry/reclaim нет (end_reason почти весь NULL, один
  `length`).

## 9. Ограничения

1. **Доллары не выводятся.** Все `estimated_cost_usd`/`actual_cost_usd` = 0,
   `cost_source='none'`/`cost_status=unknown|included|None`. Из нулевых полей
   стоимости фактический USD-биллинг **не выводится**; приведённые токены —
   метрика объёма, а не денег.
2. **Cache-read токены — не «бесплатные».** Они записаны как отдельная
   категория; тарификация cache-чтений зависит от провайдера и здесь не
   известна. Отчёт не утверждает, что cache-read ничего не стоят.
3. `messages.token_count` в базе не заполнен (0 строк > 0) — поминутная
   атрибуция токенов по сообщениям невозможна; надёжный слой — агрегаты
   сессий/моделей.
4. В `session_model_usage` есть «дубли» по провайдеру `auto` (например,
   developer: opencode-go 3 724 + auto 463) — одна сессия может быть записана
   под несколькими (model, provider); для итогов по сессиям корректна таблица
   `sessions`.
5. Reclaim/cancellation-метки kanban-задач живут в `kanban.db` — вне
   разрешённых входов этой карточки; по state.db видны только `end_reason` и
   `finish_reason`.
6. state.db живые: суммы дрейфуют со временем; зафиксирован JSON-снимок
   (`HERMES_PROFILE_TOKEN_BASELINE_PHASE0_evidence.json`) и хэши входов.
7. Идентификация сессий — ред. sha256-префиксы; сопоставление с конкретными
   задачами/чатами не выполнялось и в отчёте не записано.
8. Активный чат-рантайм (gpt-5.6-terra/openai-codex) — это текущая сессия
   владельца, а не воркер-использование; воркер-профили разбираются по своим
   state.db (см. §4).

## 10. Рекомендация: ОДИН обратимый пилот

**Пилот: Phase 1 — компактный authoritative project context (AGENTS.md).**

Обоснование по данным:
- Наибольший устойчивый вклад в input у **reviewer** — повторяемый статический
  контекст (97.5 % input не-reasoning; p50 input 73k при p50 8 вызовах).
  `AGENTS.md` ≈ 3 876–5 169 токенов загружается на каждую сессию всех трёх
  профилей; это измеряемый, полностью обратимый docs-only рычаг.
- У **developer** доминирует reasoning (56.3 % input) — это кандидат на
  Phase 2 (per-task reasoning policy), но Phase 2 меняет политику reasoning и
  требует базы из Phase 1 для сравнения; поэтому в качестве первого пилота
  выбирается только Phase 1.
- Phase 1 — docs-only: коммит в git, откат = revert; не трогает model/reasoning/
  compression-дефолты, toolsets, prompts, cache, Kanban-протокол.

Критерий перехода: после Phase 1 замерить тот же бейзлайн тем же инструментом и
сравнить input/p50/p90; если сжатие AGENTS.md без потери правил даст
материальное снижение повторяемого контекста — переходить к Phase 2
(reasoning) как второму пилоту, снова через I+RV-цикл.

## 11. Проверка (команды)

```text
# воспроизведение JSON-эвиденса (тот же снимок, откуда взят отчёт):
C:\Python314\python.exe tools\hermes_profile_token_baseline.py --json out.json
# хэш/размер AGENTS.md:
sha256sum AGENTS.md            # 0495d94b28dd10ccec3178bc2480017433164acd0c8a7d17f9dbedc8567686a8
wc -c AGENTS.md                # 19073
# итоги по профилям — см. docs/audits/HERMES_PROFILE_TOKEN_BASELINE_PHASE0_evidence.json
```
