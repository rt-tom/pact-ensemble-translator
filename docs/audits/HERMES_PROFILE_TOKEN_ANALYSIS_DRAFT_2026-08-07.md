# Hermes Profile Token Baseline — черновой анализ (DRAFT)

> Задача t_8954ee3d «Extract DB metrics and draft token analysis» (Phase 0 плана
> `2026-08-06_221320-hermes-profile-token-efficiency.md`).
> Статус: **DRAFT** — подлежит ревью; после approve финализируется.
> Измерено read-only утилитой `tools/hermes_profile_token_baseline.py` (переиспользована,
> ветка `vk/hermes-profile-token-baseline` @ bcd8444) + `tools/token_analysis_derived.py`
> (производные). Снапшот: `docs/audits/HERMES_PROFILE_TOKEN_ANALYSIS_DRAFT_2026-08-07_evidence.json`
> (`generated_at_utc = 2026-08-07T14:23:56+00:00`).
> Никакие config/production-файлы не изменены; отчёт только измеряет.

---

## 1. Метод

- Источники: `state.db` профилей `architect`, `developer`, `reviewer`
  (`C:\Users\ustom\AppData\Local\hermes\profiles\...`), таблицы `sessions` /
  `session_model_usage` / `messages` — только allowlist-колонки; строго `mode=ro`;
  согласованный WAL-снапшот через backup API; fingerprint снапшота в evidence.
- Перцентили: R-7 (линейная интерполяция), p50 = медиана, p90 — интерполированный
  90-й перцентиль (та же формула, что в скоpкарде Phase 0, §1).
- Redaction: id сессий — sha256-префиксы (12 hex); колонки с приватным содержимым
  не читаются вовсе (allowlist, не deny-list).
- Всё, кроме явно помеченных «оценка/assumption», — **измеренные** значения из
  снапшота. Повторный запуск даст дрейф (state.db живые): см. §7.

## 2. Агрегаты по профилям (все сессии; измерено)

Таблица A — суммы:

| Профиль | Сессий | Вызовов | Input | Output | Reasoning | Cache-read | Cache-write |
|---|---:|---:|---:|---:|---:|---:|---:|
| architect | 23 | 1 929 | 6 244 902 | 1 574 329 | 1 010 435 | 357 905 685 | 285 236 |
| developer | 55 | 3 950 | 6 310 914 | 4 797 403 | 3 561 184 | 630 367 488 | 0 |
| reviewer | 117 | 1 275 | 10 574 808 | 613 515 | 273 323 | 91 153 152 | 0 |

Таблица B — p50 / p90 / max по сессиям (R-7):

| Профиль | Вызовы p50/p90/max | Input p50/p90/max | Output p50/p90/max | Reasoning p50/p90/max | Cache-read p50/p90/max | Cache-write p50/p90/max |
|---|---:|---:|---:|---:|---:|---:|
| architect | 21 / 176 / 944 | 74 960 / 227 858.8 / 2 944 126 | 18 914 / 121 619.8 / 728 031 | 13 148 / 83 714.2 / 424 156 | 957 184 / 19 848 797.6 / 239 218 816 | 0 / 0 / 285 236 |
| developer | 65 / 135 / 191 | 105 902 / 185 250.2 / 480 308 | 58 752 / 190 059 / 317 907 | 40 264 / 143 410.2 / 278 196 | 7 045 632 / 27 715 635.2 / 56 036 608 | 0 / 0 / 0 |
| reviewer | 9 / 19.4 / 42 | 77 606 / 147 471.2 / 273 098 | 3 808 / 11 472.6 / 16 337 | 1 697 / 5 464 / 9 035 | 511 488 / 1 610 854.4 / 4 227 072 | 0 / 0 / 0 |

Производные (по суммам; измерено как арифметика над измеренными суммами):

| Профиль | Вызовов/сессию (avg) | Reasoning % input | Cache-read ÷ input | Output % input | Видимый текст (out−reasoning) | Видимый текст % output |
|---|---:|---:|---:|---:|---:|---:|
| architect | 83.9 | 16.2 % | 57.3× | 25.2 % | 563 894 | 35.8 % |
| developer | 71.8 | 56.4 % | 99.9× | 76.0 % | 1 236 219 | 25.8 % |
| reviewer | 10.9 | 2.6 % | 8.6× | 5.8 % | 340 192 | 55.4 % |

## 3. Kanban vs non-kanban (`sessions.source`; измерено)

| Профиль | source | Сессий | Вызовов | Input | % input | Output | Reasoning | Cache-read |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| architect | desktop | 8 | 1 461 | 3 639 559 | 58.3 % | 1 066 506 | 643 820 | 308 215 296 |
| architect | kanban | 14 | 278 | 1 268 844 | 20.3 % | 380 762 | 282 612 | 28 638 464 |
| architect | telegram | 1 | 190 | 1 336 499 | 21.4 % | 127 061 | 84 003 | 21 051 925 |
| developer | kanban | 55 | 3 950 | 6 310 914 | 100 % | 4 797 403 | 3 561 184 | 630 367 488 |
| reviewer | desktop | 2 | 3 | 40 150 | 0.4 % | 151 | 62 | 18 944 |
| reviewer | kanban | 115 | 1 272 | 10 534 658 | 99.6 % | 613 364 | 273 261 | 91 134 208 |

- **developer** — 100 % kanban (55/55 сессий). **reviewer** — 99.6 % kanban.
- **architect** — kanban лишь 20.3 % input; основная масса — desktop (58.3 %) и одна
  telegram-сессия (21.4 %); каналы смешаны, как и ожидалось для профиля-«оркестратора».

## 4. Модели / провайдеры / reasoning (из `session_model_usage` + `model_config`; измерено)

| Профиль | model | provider | mode | Вызовов | Input | Output | Reasoning | Cache-read |
|---|---|---:|---|---:|---:|---:|---:|---:|
| architect | deepseek-v4-flash | opencode-go | — | 1 687 | 3 733 793 | 1 433 318 | 948 918 | 317 109 120 |
| architect | deepseek-v4-flash | opencode-go | chat_completions | 181 | 876 871 | 101 811 | 47 344 | 34 965 248 |
| architect | deepseek-v4-flash | auto | — | 137 | 149 554 | 83 786 | 62 278 | 32 896 |
| architect | qwen3.7-plus | opencode-go | anthropic_messages | 17 | 614 812 | 3 896 | 0 | 1 482 389 |
| architect | gpt-5.6-terra | openai-codex | subscription_included | 28 | 454 208 | 22 457 | 5 962 | 1 969 664 |
| architect | mimo-v2.5 | opencode-go | chat_completions | 7 | 339 223 | 2 897 | 1 832 | 842 496 |
| architect | New | moa | chat_completions | 9 | 225 995 | 9 950 | 6 379 | 1 536 768 |
| architect | gpt-5.6-terra | auto | — | 3 | 111 307 | 14 179 | 0 | 0 |
| architect | gemini-3.6-flash | — | — | 1 | 395 | 1 | 0 | 0 |
| developer | deepseek-v4-flash | opencode-go | — | 3 950 | 6 310 914 | 4 797 403 | 3 561 184 | 630 367 488 |
| developer | deepseek-v4-flash | auto | — | 526 | 181 343 | 91 081 | 82 244 | 135 936 |
| developer | gemini-3.6-flash | — | — | 1 | 374 | 0 | 0 | 0 |
| developer | deepseek-v4-flash | — | — | 1 | 301 | 67 | 63 | 256 |
| reviewer | gpt-5.6-terra | openai-codex | subscription_included | 896 | 8 323 360 | 460 141 | 199 315 | 62 553 600 |
| reviewer | gpt-5.6-luna | openai-codex | subscription_included | 364 | 2 210 858 | 143 076 | 67 585 | 28 066 304 |
| reviewer | gpt-5.6-terra | auto | — | 161 | 71 147 | 5 830 | 0 | 0 |
| reviewer | gpt-5.6-luna | auto | — | 109 | 45 015 | 5 426 | 0 | 0 |
| reviewer | deepseek-v4-flash | opencode-go | — | 15 | 40 590 | 10 298 | 6 423 | 533 248 |

(Строки с provider `auto`/пустым mode — дубли-подписи сессий; для сумм по сессиям
корректна таблица `sessions`, см. ограничения.)

Reasoning-effort в сессиях (`model_config.reasoning_config.effort`; измерено):

| Профиль | medium | high |
|---|---:|---:|
| architect | 4 | 19 |
| developer | 55 | 0 |
| reviewer | 1 | 116 |

Наблюдаемые модели: architect/developer — deepseek-v4-flash/opencode-go (рабочая пара);
reviewer — gpt-5.6-terra+gpt-5.6-luna/openai-codex. Внимание: конфиг-дефолт reviewer —
gpt-5.6-luna, а доминирующая наблюдаемая — gpt-5.6-terra (см. ограничения).

## 5. Сигналы завершения / reclaim / cancellation (измерено)

- `sessions.end_reason`: architect — 22× NULL + 1× `ws_orphan_reap`; developer — 55× NULL;
  reviewer — 114× NULL + 1× `agent_close` + 2× `ws_orphan_reap`. Детальных меток
  reclaim/cancellation для kanban-задач в state.db **нет** (они живут в kanban.db).
- `messages.finish_reason`:

| Профиль | stop | tool_calls | length | (null) |
|---|---:|---:|---:|---:|
| architect | 334 | 1 979 | 0 | 2 647 |
| developer | 52 | 4 004 | **1** | 4 580 |
| reviewer | 117 | 1 155 | 0 | 2 793 |

  Один `finish_reason='length'` у developer — единственный признак обрезанного ответа
  (во всех профилях, на 117+ сессий). Систематического reclaim/обрезания по state.db
  **не видно**; вывод «нет систематического вклада» — отсутствие сигнала в разрешённом
  источнике, а не доказательство отсутствия (см. §8).
- Top-tools (counts, измерено): у всех доминирует `terminal` (arch 1 703, dev 2 570,
  rev 1 281); developer — инструментальная работа (`patch` 565, `read_file` 771);
  reviewer — чтение/kanban (`skill_view` 337, `kanban_show` 214, `kanban_comment` 101,
  `kanban_block` 70, `kanban_complete` 50); architect — тоже kanban-инструменты среди top-10
  (`kanban_show` 42, `skill_view` 40).

## 6. Top-5 сессий (id — sha256-префиксы; измерено)

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

### reviewer (все — kanban, кроме 2 desktop-строк в end_reason)
| метрика | id (ред.) | model | effort | вызовов | msgs | tools | input | reasoning |
|---|---|---:|---|---:|---:|---:|---:|---:|
| input | `cd8e21a9de49` | gpt-5.6-luna | high | 29 | 85 | 55 | 273 098 | 6 446 |
| input | `fc111add0304` | gpt-5.6-terra | high | 14 | 48 | 33 | 269 814 | 6 437 |
| input | `169b5fc67904` | gpt-5.6-terra | high | 14 | 50 | 35 | 231 563 | 2 974 |
| input | `94404d816383` | gpt-5.6-luna | high | 42 | 93 | 50 | 208 991 | 6 933 |
| input | `e2cdcf50cd64` | gpt-5.6-luna | high | 18 | 54 | 35 | 200 314 | 3 967 |
| reasoning | `9a8cd2457056` | gpt-5.6-luna | high | 20 | 52 | 33 | 133 306 | 9 035 |
| reasoning | `a95bca0fd1d2` | gpt-5.6-terra | high | 14 | 47 | 32 | 120 405 | 8 351 |
| reasoning | `aff3995f3a75` | gpt-5.6-terra | high | 18 | 49 | 30 | 145 114 | 8 007 |
| reasoning | `2161ec3b77b2` | gpt-5.6-luna | high | 29 | 80 | 50 | 178 533 | 7 893 |
| reasoning | `94404d816383` | gpt-5.6-luna | high | 42 | 93 | 50 | 208 991 | 6 933 |

## 7. Дрейф между снапшотами (06:08Z первая волна → 14:23Z этот снапшот; измерено)

| Профиль | Сессий | Вызовов | Input | Δ вызовов | Δ input |
|---|---:|---:|---:|---:|---:|
| architect | 14 → 23 | 1 678 → 1 929 | 5.25M → 6.24M | +15.0 % | +18.9 % |
| developer | 51 → 55 | 3 807 → 3 950 | 6.08M → 6.31M | +3.8 % | +3.9 % |
| reviewer | 113 → 117 | 1 182 → 1 275 | 9.94M → 10.57M | +7.9 % | +6.4 % |

Рост за ~8 ч ожидаем (активные сессии пишутся). architect вырос сильнее всех — за счёт
длинных desktop/telegram-сессий (см. §3), а не kanban-части.

## 8. Доминирующие драйверы (ИНТЕРПРЕТАЦИЯ поверх измеренных данных)

Ниже — аналитические выводы. Числа — измеренные (§2–§6); причинно-следственная
привязка «что именно генерирует объём» — интерпретация, помечена как таковая.

1. **developer — reasoning + инструментальные циклы (нормальная длинная работа).**
   Reasoning = 56.4 % input (3.56M из 6.31M); output = 76 % input, но видимый текст —
   лишь 25.8 % output → ~3/4 output-токенов это reasoning. Tool-петли: `tool_calls`
   finish_reason 4 004 против 52 `stop` (≈98.7 %); top-tools — terminal/patch/read_file.
   Cache-read ÷ input = 99.9× — длинные сессии (p50 65 вызовов) активно переиспользуют
   кэшированный префикс. Потолок `max_turns: 500` не достигается (max 191) → это
   «нормальная длинная kanban-работа», а не обрыв/ретраи. Драйвер-кандидат для Phase 2
   (политика reasoning на задачу): снижение reasoning напрямую режет и output, и input
   (reasoning-токены, как правило, входят в контекст следующих шагов).

2. **reviewer — повторяемый статический контекст на каждую сессию.**
   Короткие сессии (p50 9 вызовов, p90 19.4), но высокий p50 input 77 606 → ~8.6K
   токенов на вызов в среднем (оценка: 77 606 / ~9). Reasoning всего 2.6 % input —
   объём создаёт НЕ думание, а повторяемая загрузка контекста (AGENTS.md ≈ 3.9–5.2K
   токенов на сессию, skills — `skill_view` 337 вызовов, kanban-контекст —
   `kanban_show` 214). Cache-read ÷ input всего 8.6× — между короткими сессиями кэш
   почти не переиспользуется (каждая ревью-сессия стартует холодно). Это крупнейший
   рычаг Phase 1 (компактный AGENTS.md): 115 kanban-сессий × повторяемый префикс.

3. **architect — смешанно; масса уходит в не-kanban длинные сессии.**
   Kanban-часть мала (20.3 % input, 14 сессий). Основной объём — desktop (58.3 %) и
   одна telegram-сессия (21.4 %); одна desktop-сессия `593a124a2054` = 2.94M input
   (47 % всего input профиля) при 944 вызовах — нормальная длинная интерактивная
   работа, не kanban-воркер. Reasoning 16.2 % input. Для kanban-эффективности
   architect — вторичен; его объём — это сессии владельца/оркестрации.

4. **Дубли/реclaim/retry-циклы — в state.db не видны.** По разрешённым данным
   систематического вклада нет: end_reason почти весь NULL, единственный `length`,
   один `agent_close`, три `ws_orphan_reap`. Если нужно подтвердить/исключить вклад
   reclaim-циклов — источник kanban.db (вне разрешённых входов этой карточки).

5. **Cache-write почти нулевой** (285 236 только у architect, в telegram-сессии) —
   измеренный факт; интерпретация: провайдеры-рантаймы (opencode-go/codex) не
   сообщают cache_write на большинстве вызовов; это НЕ значит, что кэш не пишется.

## 9. Ограничения и разделение measured / assumptions

**Measured (измерено):** все суммы/p50/p90/max токенов и вызовов, распределения
source/effort/end_reason/finish_reason, топ-инструменты, разбивки по моделям,
cache-соотношения (арифметика над измеренными суммами), дрейф между снапшотами.

**Assumptions (оценки/интерпретации):**
- «Видимый текст = output − reasoning» — по спецификации §9.10 (reasoning входит в
  output); это арифметическое тождество, а не прямое измерение видимых токенов.
- «~8.6K токенов на вызов у reviewer» — p50 input ÷ p50 вызовов (грубая оценка).
- AGENTS.md ≈ 3.9–5.2K токенов — по 4/3 символа на токен (не токенизатором).
- Причинная привязка драйверов (§8) — интерпретация паттернов, не измерение.
- «Cache-read ÷ input» — соотношение объёмов, НЕ утверждение о бесплатности
  (тарификация cache-чтений зависит от провайдера и здесь неизвестна).

**Ограничения (перенесено из спецификации):**
1. Все `estimated/actual_cost_usd` = 0 → **USD-биллинг не выводится**; токены —
   метрика объёма, не денег.
2. Cache-read **не называются бесплатными** (провайдерская тарификация неизвестна;
   reviewer — `subscription_included`, но из нулевых cost-полей стоимость не выводится).
3. `messages.token_count` не заполнен → поминутная/по-сообщённая атрибуция недоступна.
4. `session_model_usage` имеет дубли по provider `auto` → итоги по сессиям считаются
   из `sessions`.
5. Reclaim/cancellation-метки kanban-задач живут в `kanban.db` — вне разрешённых входов;
   по state.db видны только `end_reason` и `finish_reason`.
6. state.db живые: суммы дрейфуют; зафиксирован снапшот (fingerprint в evidence).
7. Fingerprint = WAL-согласованный snapshot (backup API), не сырой main-файл.
8. Идентификация сессий — ред. sha256-префиксы; сопоставление с задачами не делалось.
9. Конфиг-дефолт ≠ доминирующая наблюдаемая модель (reviewer: config gpt-5.6-luna,
   наблюдаемая gpt-5.6-terra) — не смешивать.
10. `reasoning_config.effort` отсутствует в части строк → NULL (architect: 4 medium
    записаны явно, остальное high).
11. Длительность живых сессий (ended_at NULL) в этой карточке не считалась.
12. `output_tokens` включает reasoning (developer ~74 % output — reasoning).

## 10. Вывод для Phase 1/Phase 2 (в развитие скоpкарда)

- Данные подтверждают пилот **Phase 1 (компактный AGENTS.md)**: максимальный устойчивый
  вклад в input у reviewer — повторяемый статический контекст коротких холодных сессий
  (115 сессий × ~8.6K токенов/вызов; reasoning всего 2.6 %). Это docs-only, обратимый
  рычаг, не трогает model/reasoning/compression.
- **Phase 2 (политика reasoning на задачу)** — кандидат по developer (56.4 % input —
   reasoning), но менять политику reasoning можно только ПОСЛЕ замера Phase 1 тем же
   инструментом.
- Никаких изменений эта карточка не вносит: только измерение и анализ.

## 11. Воспроизводимость (команды)

```text
# 1) свежий снапшот (WAL-consistent; суммы дрейфуют — fingerprint в evidence):
C:\Python314\python.exe tools\hermes_profile_token_baseline.py --json live.json
# 2) производные (из live.json — вывода утилиты из шага 1):
C:\Python314\python.exe tools\token_analysis_derived.py live.json
# 3) redaction-check по evidence-файлу: 0 совпадений со списком запрещённых
#    паттернов из §5 спека (HERMES_TOKEN_REPORT_SPEC.md): на evidence JSON
#    даёт 0 hits; отчёт выше паттерны-имена не содержит вовсе.
```

Утилита (`tools/hermes_profile_token_baseline.py`, `tools/verify_baseline_report.py`,
23 теста) — переиспользована с ветки `vk/hermes-profile-token-baseline` @ bcd8444
(карточка t_f2af0dc3); новых реализаций нет.
