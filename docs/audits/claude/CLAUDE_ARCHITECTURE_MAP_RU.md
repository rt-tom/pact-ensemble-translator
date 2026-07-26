# CLAUDE_ARCHITECTURE_MAP_RU

Карта фактического pipeline v3.1.1, восстановленная из кода (не из handoff).
Источник истины: `run_full_pipeline_v31.ps1` + вызываемые им Python-модули.

Обозначения:
- **AUTH** — authoritative artifact стадии (то, что читают downstream-стадии);
- **SKIP** — условие переиспользования кеша (resume);
- **FATAL** — что приводит к остановке всего run.

---

## 0. Общие свойства оркестрации

| Свойство | Фактическое поведение | Файл |
|---|---|---|
| Failure propagation | `Invoke-PythonStage` проверяет `$LASTEXITCODE`; ненулевой → `throw`. `$ErrorActionPreference='Stop'`, `Set-StrictMode -Version Latest` | `run_full_pipeline_v31.ps1:312-320` |
| Server switching | `Start-LlamaServer` всегда сначала вызывает `Stop-LlamaServer` | `:172-197` |
| Process cleanup | `Stop-LlamaServer` убивает **все** `llama-server` в системе, не только дочерний | `:160-170` |
| Готовность сервера | опрос `/health` до 240 с; статусы `ok` / `no slot available` | `:190-195` |
| CommonArgs | `--project-root --config --start --end` для всех v31-стадий | `:322-324` |
| Рабочая директория | `Push-Location $ProjectRoot` на каждый Python-вызов | `:315` |
| Выбор глав (PS) | `Get-ChildItem *.html | Sort-Object Name` + `Skip/First` — **лексикографическая** сортировка | `:65-68` |
| Выбор глав (Python) | `select_files()` — **натуральная** сортировка (`natural_key`) | `pact_translate_v3.py:3363-3377` |
| Итоговый bundle | `Compress-Archive` из config + output + work + logs + server_logs | `:468-470` |

Конфиг генерируется на каждом запуске: базовый `config.v3.json` → `ConvertFrom-Json -AsHashtable` → runner перезаписывает секции → `config.full_pipeline.v31.json` в корне run. В Python дополнительно накладывается `merge(DEFAULTS, file)`, поэтому **всё, чего нет в snapshot, приходит из `DEFAULTS`** (`pact_translate_v3.py:30-205`).

---

## 1. Stage graph (фактический порядок вызовов)

```
Start-LlamaServer GemmaTranslate
  └─ Invoke-GemmaPreflight                     (если не -SkipPreflight)
1/11  prepare_pipeline_context.py
      [-RedoFormatting only] Remove-SelectedOutputs
      [-RedoTranslation] rm drafts/ meta/ draft_translations.json + Remove-QualityArtifacts
      [-RedoQuality]     Remove-QualityArtifacts
      [-RedoSourceAnalysis|-RedoTranslation] rm book_consistency_ledger.json
Start-LlamaServer Qwen
2/11  v31_source_analysis.py            [--force при Redo{SourceAnalysis,Translation}]
Start-LlamaServer GemmaTranslate
3/11  pact_translate_v3.py --phase translate

Run-AuditPass 'primary' draft_translations.json
Run-RepairPass 'primary' draft_translations.json
Run-AuditPass 'residual' v31_primary_translations.json
Run-RepairPass 'residual' v31_primary_translations.json

10/11  v31_finalize_quality.py
10b/11 v31_build_review.py
Start-LlamaServer GemmaTranslate
11/11  pact_translate_v3.py --phase finalize   [--redo-formatting при любом Redo*]
Stop-LlamaServer → Compress-Archive
```

`Run-AuditPass <pass> <file>`:
```
Qwen        → v31_audit.py --mode qwen_semantic
GemmaVerify → v31_audit.py --mode gemma_semantic
            → v31_audit.py --mode gemma_russian
            → v31_audit.py --mode gemma_discourse
(без сервера) → v31_merge_issues.py
GemmaVerify → v31_cross_verify.py --judge gemma
Qwen        → v31_cross_verify.py --judge qwen
(без сервера) → v31_finalize_verification.py
```

`Run-RepairPass <pass> <file>` — цикл `round = 1..max_repair_rounds (3)`:
```
GemmaRepair → v31_repair.py --round N [--retry-only при N>1]
Qwen        → v31_postcheck.py --judge qwen_semantic
GemmaVerify → v31_postcheck.py --judge gemma_semantic
            → v31_postcheck.py --judge gemma_russian
(без сервера) → v31_deterministic_gate.py
(без сервера) → v31_adjudicate.py
Get-RetryCount → 0 ? return : следующий round, currentFile ← v31_{primary|final}_translations.json
```

---

## 2. Таблица стадий

| # | Стадия | Скрипт | Профиль | Inputs | Outputs (AUTH жирным) | Resume / retry | Fatal |
|---|---|---|---|---|---|---|---|
| 0 | Preflight | inline PS | GemmaTranslate | фиксированный EN-абзац в скрипте | **`preflight_performance.json`** | нет кеша; всегда выполняется | prompt < 100 t/s или gen < 20 t/s; невозможность распарсить stderr |
| 1 | Подготовка | `prepare_pipeline_context.py` | GemmaTranslate (для генерации chapter bible) | `config`, `glossary/*`, HTML главы | `manifest.json`, **`chapter_bible.json`**, `chapter_bible.raw.json`, восстановленный `glossary/*` | `chapter_bible.json` существует → переиспользуется и повторно санируется | отсутствие `pact_translate_v3.py`; ошибка Runner.prepare_chapter |
| 2 | Source analysis | `v31_source_analysis.py` | Qwen | `manifest`, `chapter_bible`, глоссарий, `book_consistency_ledger.json` | **`source_scene_map.json`**, `v31_source_analysis/batch_*.json`, `book_consistency_ledger.json` | SKIP если `source_scene_map.json` есть и нет `--force`; batch-кеш `batch_NNNN.json`; при провале — split пополам, рекурсивно | одиночный PID не распарсился после 3 попыток; `set(by_pid) != set(pids)` |
| 3 | Перевод | `pact_translate_v3.py --phase translate` | GemmaTranslate | `manifest`, `chapter_bible`, `source_scene_map`, глоссарий | **`draft_translations.json`**, `drafts/*.json`, `meta/*.json` | SKIP по чанкам: `drafts/{chunk}.json` содержит все PID чанка | рекурсивное деление чанка не помогло; `fit_output_budget` → prompt too large |
| 4 | Аудиты ×4 | `v31_audit.py --mode …` | Qwen / GemmaVerify | `manifest`, translations-file, `chapter_bible`, `source_scene_map` | **`v31/{pass}/{mode}.json`**, `v31/{pass}/audits/{mode}/unit_*.json` | SKIP если консолидированный файл есть; per-unit кеш; split пополам при провале (кроме discourse) | неполное покрытие PID; провал discourse-окна; провал юнита из 1 PID |
| 5 | Merge/dedup | `v31_merge_issues.py` | — | 4 файла аудитов + `deterministic_issues()` | **`v31/{pass}/merged_issues.json`**, `verify_queue_{qwen,gemma}.json` | SKIP если `merged_issues.json` есть | `coverage.ok` любого детектора ≠ true |
| 6 | Cross-verify ×2 | `v31_cross_verify.py --judge …` | GemmaVerify / Qwen | соответствующая очередь | **`v31/{pass}/cross_verify_{judge}.json`**, `cross_verify/{judge}/{issue_id}.json` | SKIP по файлу и по per-issue кешу | 3 неудачных попытки на один issue |
| 7 | Finalize verification | `v31_finalize_verification.py` | — | `merged_issues.json`, оба `cross_verify_*.json` | **`v31/{pass}/verified_issues.json`**, `rejected_issues.json`, `uncertain_issues.json`, `verification_report.json`; для primary — `work/issues.json`, `work/verified_issues.json` | кеша нет, всегда пересчитывается | `expected != completed`; отсутствие решения судьи; **любой `uncertain` при `fail_on_uncertain=true`** |
| 8 | Repair | `v31_repair.py --round N` | GemmaRepair | `verified_issues.json`, translations-file, `retry_requests_round_{N-1}` | **`v31/{pass}/repair_candidates_round_NN.json`**, `repairs/round_NN/{pid}.json` | SKIP по round-файлу и per-PID кешу | ни одного валидного кандидата после 3 попыток |
| 9 | Post-gates ×3 | `v31_postcheck.py --judge …` | Qwen / GemmaVerify | round-файл кандидатов, translations-file | **`v31/{pass}/post_gate_{judge}_round_NN.json`**, `post_gates/{judge}/round_NN/{pid}_{cid}.json` | SKIP по round-файлу и per-candidate кешу | невалидный verdict/confidence/bool после 3 попыток |
| 10 | Deterministic gate | `v31_deterministic_gate.py` | — | round-файл кандидатов, translations-file | **`v31/{pass}/post_gate_deterministic_round_NN.json`** | кеша нет | — (пишет `passed=false`, не бросает) |
| 11 | Adjudication | `v31_adjudicate.py` | — | round-файл кандидатов + 4 gate-отчёта | **`work/v31_{primary|final}_translations.json`**, `retry_requests_round_NN.json`, `adjudication_round_NN.json`, `lifecycle.json`, **`v31/{pass}/status.json`** | кеша нет | `expected/completed ≠ total` любого gate; отсутствие решения для пары (pid, candidate_id) |
| 12 | Final quality gate | `v31_finalize_quality.py` | — | всё выше по обоим pass | **`work/v31_quality_gate.json`**, `issues.json`, `verified_issues.json`, `repaired_translations.json`, `repaired_translations.preverify.json`, `repair_records.json`, `issue_lifecycle.json`, `post_repair_report.json` | кеша нет | любое из ~10 условий `unresolved` |
| 13 | Review | `v31_build_review.py` | — | draft/primary/final translations, lifecycle, quality gate | `work/review_comparison_v31/{index.html,summary.json}` | кеша нет | KeyError по PID, отсутствующему в manifest |
| 14 | Finalize/HTML | `pact_translate_v3.py --phase finalize` | GemmaTranslate | `repaired_translations.json`, `issues.json`, `manifest`, normalized HTML | **`output/{chapter}.html`**, `quality_report.json`, `audit_report.html`, **`work/state.json`** | `formatting/batch_*.json` переиспользуется, если нет `--redo-formatting` | legacy post-repair gate (де-факто no-op); `final_integrity.ok == false` |

---

## 3. Профили моделей и где они реально применяются

| Профиль | Порт/флаги | Стадии |
|---|---|---|
| `GemmaTranslate` | MTP draft (`--spec-type draft-mtp`, `-n-max 4`), `-ngl 99 -ncmoe 18 -c 32768 -fa on --reasoning-budget 0` | preflight, prepare (chapter bible), перевод, финализация/formatting |
| `GemmaRepair` | без MTP, `-fit on -fitt 1536 -t 6 -tb 12 --reasoning-budget 0` | только `v31_repair.py` |
| `GemmaVerify` | без MTP, `-fit on -fitt 1536`, `--reasoning-budget 128` | gemma_semantic / gemma_russian / gemma_discourse audits, gemma cross-verify, gemma post-gates |
| `Qwen` | `-fit on -fitt 1280 -b 2048 -ub 512 -ctk q8_0 -ctv q8_0 --reasoning-budget 0` | source analysis, qwen_semantic audit, qwen cross-verify, qwen semantic post-gate |

`GGML_VK_DISABLE_COOPMAT=1` выставляется в `Start-LlamaServer` перед каждым запуском.

Важно: **API-секция выбирается по имени судьи, а не по профилю.** `reviewer_api` используется для всех Qwen-ролей, `translator_api` — для всех Gemma-ролей, но обе секции указывают на один и тот же `http://127.0.0.1:8080`. Разделение моделей обеспечивается исключительно последовательностью `Start-LlamaServer`, а не конфигом. Любая перестановка вызовов в PS немедленно отправит промпт не в ту модель, и ни один Python-модуль этого не заметит.

---

## 4. Маршрутизация issue (фактическая, `v31_merge_issues.py:91-143`)

| Условие (по `detected_by`) | `verification_route` | Кто судит |
|---|---|---|
| qwen **и** gemma, обе с `confidence=high` | `independent_detector_agreement` | **никто** — сразу `decision=repair` |
| qwen **и** gemma, но не обе high | `dual_cross_judge` | оба судьи, требуется точное совпадение |
| только `deterministic` **и** категория ∈ {`missing`, `mixed_script`} | `hard_deterministic` | **никто** — сразу `repair` |
| только qwen | `gemma_cross_judge` | Gemma |
| только gemma | `qwen_cross_judge` | Qwen |
| `deterministic` в наборе с другими | `{qwen|gemma}_deterministic_judge` по `QWEN_PREFERRED` | один судья |
| иначе | `gemma_cross_judge` | Gemma |

Консолидация (`v31_finalize_verification.py:138-143`):
- `repair` + confidence ∈ {`high`, `deterministic`} → **verified**;
- `keep` + `high` → **rejected**;
- всё остальное → **uncertain** → при `fail_on_uncertain=true` **весь run падает**.

---

## 5. Условия приёмки repair (`v31_adjudicate.py:19-42, 97-102`)

Кандидат принимается только если **все четыре** gate дали `passed`:
1. `qwen_semantic`: `verdict=accept`, `confidence=high`, `faithful_to_source=true`, `all_issues_fixed=true`, `introduced_new_semantic_error=false`;
2. `gemma_semantic`: то же;
3. `gemma_russian`: `verdict=accept`, `confidence=high`, `natural_russian=true`, `all_issues_fixed=true`, `introduced_new_russian_error=false`;
4. `deterministic`: `passed=true` (нет validation_errors, нет введённых hard-категорий, исходные deterministic-категории устранены).

Для `challenge_issue` — `accept_challenge` + `confidence=high` + `issue_valid=false` от всех трёх модельных gate.
Все булевы поля проходят `strict_bool` — строка `"true"` отклоняется.

Выбор среди принятых: минимальный `changed_ratio` (`v31_adjudicate.py:123`).

---

## 6. Условия `state.json = complete`

`Runner.finalize` (`pact_translate_v3.py:3210-3280`) пишет `complete` если:
1. legacy post-repair gate: `post_repair_report.unresolved_total == 0` — **это поле хардкодится нулём** в `v31_finalize_quality.py:244`, то есть условие всегда истинно;
2. `final_integrity.ok == true`, где `ok = not errors`.

`errors` возникают только при: утечке маркера `[[FMT_`, отсутствии PID в translations, несовпадении цифр при `strict_digits` (по умолчанию `true`).

`warnings` (не блокируют): английский остаток в финальном HTML, **любое количество невосстановленных inline-спанов**.
