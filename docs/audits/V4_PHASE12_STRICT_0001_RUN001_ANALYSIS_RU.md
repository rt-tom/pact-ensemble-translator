# V4 Phase 12 strict — анализ прогона `v4_phase12_strict_0001/run_001`

Дата анализа: 2026-08-03. Прогон выполнен на коде до PR #116 (Phase 4 repair call optimization L1/L2b/L3) и PR #117 (Phase 12 run-progress tracker).

## 1. Идентификация прогона

- Артефакт: `pact-v4-strict-chapter-trial/v2`, run_label `v4-phase12-strict-chapter-trial`.
- Глава: `0001_bonds-1-1` (книга Bonds, глава 1-1), 400 параграфов, 16 чанков.
- Период: 08/02/2026 21:29:55 → 08/03/2026 16:05:35. Wall clock **66 937 c ≈ 18,6 часа**.
- Возобновление: `resumed_from_index: 0`, `halted_early: False`; max подряд идущих nonselection = 2 при политике `max_consecutive_terminal_nonselections: 3`.
- Identity: source `2cef6f3a…`, snapshot `5cc3a9f2…`, chunk_plan `b99f1623…`, config `77bd1595…`, backend `c29c9461…`. Цепочка консистентна во всех артефактах.

## 2. Конфигурация бэкенда и операционные показатели

- `kind: local_llama`, `local-llama/v1`, `openai_chat_completions`, endpoint `http://127.0.0.1:8094`, device SYCL0 (`C:\llama-sycl-new\llama-server.exe`).
- Модели: `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` (fidelity reviewer, qwen audit; ~9,9 ГБ VRAM) и `gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf` (generator, gemma audit, russian selector; ~10,15 ГБ VRAM). У gemma — MTP-draft; structured output `json_object` (`pact-json-object/v1`); контекст 32768.
- **463 запуска llama-server / 462 перезапуска** (по одному на смену модели), `load_retries: 0` — ошибок загрузки нет. Server stderr — только штатные предупреждения MTP/draft memory fitting.
- Смена моделей: cold-acquire median gemma ~14,5 c / qwen ~17,6 c, unload ~6,4 c. Суммарно ~2,9 часа (~16% wall clock) ушло на переключения.

## 3. Результаты по этапам

### 3.1 Генерация (Phase 2B) — полная, 32/32

Все 16 чанков × 2 роли (`fidelity_first`, `balanced_literary`) сгенерированы: `status: complete`, `errors: {}`. Журнал (`journal.ndjson`, 16 записей) ведёт контекстную цепочку корректно (`left_context_kind`: none_first_chunk / selected / empty_after_nonselection).

### 3.2 Отбор (Phase 2C) — 12 selected / 4 quarantined

- **selected 12**: 4 × `fidelity_first` (chunk0002, 0004, 0007, 0016), 8 × `balanced_literary`. В чанках, где оба кандидата прошли, выбирался `balanced_literary`; где прошёл один — он.
- **quarantined 4** (103 параграфа; в каждом оба кандидата упали одинаково, disagreements не зафиксированы):
  - `chunk0001`: детерминированный gate `mixed_script` — **p00013 содержит латинские инициалы «R.D.T.»** (источник сам их содержит; оба кандидата не прошли `deterministic_consistency`).
  - `chunk0005`: qwen_fidelity — пропуск целого предложения в p00099 + ошибка родового согласования в p00095 (`позволила себе` у мужского нарратора).
  - `chunk0009`: qwen_fidelity — p00193 `grandchild` → `внук` (навязан род вопреки gender-neutral источнику и последующему уточнению).
  - `chunk0010`: qwen_fidelity — p00239 «well after dark» → «далеко за полночь», прямо противоречит следующей строке.
- `candidates_evaluated: 2`, `candidates_passed`: 1–2 у selected, 0 у quarantined. `needs_synthesis: 0`, `incomplete_generation: 0`.

### 3.3 Аудит (step6, B1) — status: **incomplete**

- Причина неполноты: `chunk0011 qwen_chapter_audit Reject partial or invalid JSON: Expecting value: line 1 column 1 (char 0)` — пустой/невалидный ответ qwen-аудита для chunk0011. При этом `covered_chunks: 16`, `uncovered: 0` (gemma-аудит покрыл все чанки; упал только qwen-аудит уровня главы для chunk0011).
- Findings: **114** = 58 `gemma_russian_review` + 55 `qwen_chapter_audit` + 1 `deterministic_integrity`; 95 регионов.
- Категории: calque 30, scene 25, addition 12, dialogue 11, omission 10, referent 8, register 6, ty_vy 6, repetition 5, mixed_script 1.
- Плотность по чанкам: quarantined — chunk0009: 16, chunk0005: 13; у прошедших 3–8.

### 3.4 Ремонт (step7, B2) — 2 раунда, terminal **accepted_degraded**

- 187 repair-записей, **93 закоммичено, 82 долга** (`debt_trace`). Полная сборка главы (400 pid) выполнена, включая quarantined-чанки через best-вариант (`best_variant_rule: max_gates_passed > role(fidelity_first > balanced_literary > synthesis) > candidate_id`).
- Долг по причинам:
  - `chunk0001` (8 из 8): все правки блокированы тем же `deterministic_consistency mixed_script p00013` — инициалы «R.D.T.» не поддаются ремонту при текущей политике.
  - `chunk0005` (20): провалы qwen_fidelity re-gate (пропуски, род, идиомы).
  - `chunk0009` (3), `chunk0010` (5): провалы qwen_fidelity re-gate.
  - Прочие: «Gemma re-check failed: the Russian finding remains open» и ≥1 transport/invalid-JSON отказ (`ValueError: Reject partial or invalid JSON: Unterminated string starting at line 5 column 13`) — записан как долг, не семантический терминальный статус (transport failure ≠ semantic gate failure).
- Распределение долга по чанкам: 0001:8, 0002:3, 0003:3, 0004:6, 0005:20, 0006:2, 0007:2, 0008:3, 0009:3, 0010:5, 0011:4, 0012:7, 0013:3, 0014:3, 0015:3, 0016:6.

### 3.5 Целостность (step7/step8) — **failed**

- `missing_pids: []`, `numeric_missing: []`, `glossary_missing: []` — покрытие и числовые инварианты в порядке.
- Единственный провал: **`mixed_script: ["p00013"]`** — в финальной главе остаются латинские «R.D.T.». Это же единственный mixed_script finding аудита и причина карантина chunk0001.
- `qwen_smoke: false` — это НЕ провал: узкий Qwen-smoke не требовался (текст менялся только в ре-аудируемом объёме). Диагностический флаг, не статус.
- `frozen_hash: d7d26b72…` — консистентно между repair_report и strict record.

### 3.6 Форматирование (step7/step8) — blocking

- 101 спан решён, **1 блокирующий инцидент** — `p00207` span `em01`, `target_not_found` («model fallback reported no corresponding fragment»); p00207 относится к quarantined chunk0009. **78** спанов решены через model fallback.
- `max_formatting_incidents: 0` → `blocking: true`; инцидент не устранён.

### 3.7 Терминал

- step7 = step8 = `accepted_degraded` (структурно валидный PID-map 400/400 + открытый долг), без memory promotion. `complete` недостижим из-за integrity failure (mixed_script p00013) и форматирующего инцидента.

## 4. Итоговые артефакты

| Артефакт | Покрытие | Содержимое |
|---|---|---|
| `translations.json` | **297/400 pid (74,25%)**, только 12 committed чанков | сырой текст выбранных кандидатов, **без ремонта и форматирования** (HTML-тегов нет) |
| `repair_report.json` → `final_translation` | 400/400 | полная глава после ремонта + форматирования (теги `<em>/<strong>`) |

`translations.json` сравнивался с `final_translation` по отдельным pid (p00016, p00054, p00183, p00328): тексты расходятся — в `translations.json` лежат исходные кандидаты, в `repair_report` — отремонтированные.

## 5. Корневые проблемы

1. **`p00013` mixed_script — структурный блокер прогона.** Источник легитимно содержит латинские инициалы «R.D.T.»; ни один кандидат, ни одна из 8 правок chunk0001 не смогли пройти mixed_script-gate. Это единственная причина карантина chunk0001, 8 долгов, финального `integrity: failed` и отсутствия `complete`. Это политическое/схемное противоречие (нужен allowlist/исключение для легитимных латинских инициалов или транслитерационная политика), а не дефект перевода.
2. **Надёжность бэкенда**: 1 пустой ответ qwen-audit (chunk0011) → `step6: incomplete`; ≥1 обрезанный JSON при repair → долг. Оба корректно не стали семантическими терминальными статусами, но риск повторяется (правило «never accept truncated JSON»).
3. **Quarantined чанки остались недовыпущенными**: 4 чанка (103 параграфа) не вошли в `translations.json`; repair-попытки по chunk0005/0009/0010 не прошли re-gate и остались долгом.
4. **Квалитет**: 114 аудит-находок, доминируют calque (30) и scene (25) — стилистические/структурные проблемы русской прозы; часть repair-долга — непройденные Gemma re-check.

## 6. Выводы и рекомендации

- Прогон **дошёл до терминала** (`accepted_degraded`) без ранней остановки и без потери данных; identity-цепочка консистентна. Это ожидаемый путь для прогонов с открытыми долгами.
- **Результат — degraded**: полный 400-pid текст существует только в `repair_report`; доставляемый `translations.json` покрывает 74,25% главы.
- **Единственный семантический блокер** — mixed_script «R.D.T.»; остальное — операционные дефекты бэкенда и ожидаемые repair-долги.
- Стоимость: ~18,6 ч, из которых ~2,9 ч — переключение моделей; 463 старта сервера при нуле ошибок загрузки.
- Рекомендации:
  (а) решить политику mixed_script для легитимных латинских инициалов/имён (allowlist или транслитерация) — разблокирует chunk0001 и integrity;
  (б) повысить устойчивость к пустому/обрезанному JSON на этапах qwen-audit и repair (retry);
  (в) рассмотреть отдельный цикл ремонта quarantined-чанков, т.к. текущие re-gate для chunk0005/0009/0010 систематически не проходят;
  (г) сохранить строгий статусный смысл: `accepted_degraded` ≠ `complete` ≠ authoritative memory.
