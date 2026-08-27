## 1. Основа и флаг

- [ ] 1.1 Добавить флаг `glossary_model_resolver_enabled` в `B3AuditRepairConfig`/`runtime_config` (default `false`) и прокинуть через `v4_phase12_strict_runner` — верификация: `openspec validate --strict` и `--help` не ломаются, флаг виден в `config_identity`
- [ ] 1.2 Расширить `B1.2` промпт полем `glossary_worthy` и `source_aliases[]` (proper-noun фильтр на источнике) — верификация: `b1.2_entity_raw.txt` для `0033` содержит `glossary_worthy` и `Knights of the Basement` целиком

## 2. Batched resolver в B3

- [ ] 2.1 Реализовать `GlossaryResolver` (backend-agnostic, `model_bindings.glosaary_resolver` → reuse `russian_selector`/`entity_extractor`) с `response_schema glossary_proposal/v1` — верификация: юнит-тест схемы и `max_tokens` на 3 главах без truncation
- [ ] 2.2 Вызвать резолвер в `B3AuditRepair.run` после `repair/re-audit` до `release()`, написать `glossary_proposals.json` (identity `source_hash+entity_context_hash+translation_hash`) — верификация: `ls out_dir/glossary_proposals.json` после `0032` прогона и `phase_progress` не регрессирует
- [ ] 2.3 Детерм. валидация (`source` в VERIFIED, `evidence_pid` существует, кириллица, `RU_STOP`, `Бабуль`-блоклист, коллизия `VALUE` другого ключа, дубль `ru←[en]`) — верификация: `Roxanne→Бабуль` отклоняется, `Herb→Минни` vs `Minnie→Минни` ловится
- [ ] 2.4 Депрекейт `align_candidates` ветки для `proper_name` в `glossary_observations_from_entity_context` (оставить для `term` телеметрии) — верификация: `Shotgun→Дробовик` резолвится, `door→дверь` не промоутится

## 3. Интеграция в book-run и промоут

- [ ] 3.1 В `v4_book_run` после `terminal_status` читать `glossary_proposals.json`, валидировать identity и вызывать `MemoryManager.add_observation("glossary")` через существующий gate — верификация: `book_run.json candidates.proposed/committed` отражает `Leanne→Лианн`, `Pauz→Пауз` через новый путь, `glossary_candidates.json` ledger пополняется
- [ ] 3.2 Поддержка многословных (`Leonard Harlan`, `Knights of the Basement`) — `proposed_ru` фраза целиком, `evidence_windows` — верификация: `Knights of the Basement→Рыцари Подвала` proposal принят

## 4. Линт и телеметрия

- [ ] 4.1 Добавить `tools/pact_fidelity_lint` проверки: падеж `а/у/ом`, `Бабуль`-блоклист, транслит `H→Х` — верификация: `pact-fidelity-lint` падает на `Christoff→Кристоффа` и `Dionysus→Диониса`, проходит на `Роксанна/Херб/Дионис`
- [ ] 4.2 Shadow-mode прогон 5-10 глав (`0033-0035`) с флагом `false→true` — сравнить `glossary_proposals` vs старый `align`, измерить `precision` (ожидалось `50%→>90%`) — верификация: отчёт `precision` и отсутствие рестарта `local Qwen`

## 5. Проверка

- [ ] 5.1 `openspec validate --strict` проходит
- [ ] 5.2 Ручная проверка: `python -m pact_full_pipeline_runner_v1.v4_phase_progress --out-base <book_run>` не врёт, `glossary.json` после `0033` содержит `Teddy→Тедди` без `Бабуль`-регрессии
