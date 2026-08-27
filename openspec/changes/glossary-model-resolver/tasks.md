## 1. Основа и флаг

- [ ] 1.1 Ввести `glossary_resolver_mode = off | shadow | promote` (identity-bearing, в `config_identity`/`runtime_config`/`B3AuditRepairConfig`) с default `off`, прокинуть через `v4_phase12_strict_runner`; rollback `off` запрещает новые `glossary` observations, отката к `align` нет — верификация: `openspec validate --strict` и `config_identity` меняется при смене mode
- [ ] 1.2 Расширить `B1.2` промпт полем `glossary_worthy` (advisory) и `source_aliases[]`, bump `EXTRACTOR_VERSION`/`prompt_version`/`CACHE_SCHEMA`, код-валидация `title-case`, `RU_STOP`, поверхность в source — верификация: `b1.2_entity_raw.txt` для `0033` содержит `glossary_worthy` и `Knights of the Basement` целиком, старый кэш инвалидируется

## 2. Batched resolver в B3 (единый пост-процессинг путь)

- [ ] 2.1 Реализовать `GlossaryResolver` (backend-agnostic, `model_bindings.glossary_resolver` → `local qwen_audit / remote russian_selector / composite` fallback, `max_tokens 1536`, `temperature 0`, `reasoning 0`, `response_schema glossary_proposal/v1`) — верификация: юнит-тест схемы, `3` главы без truncation, `glosaary` опечатка отсутствует
- [ ] 2.2 Вызвать резолвер в `B3AuditRepair.run` после `repair/re-audit` по единому пост-процессингу пути (включая ранний `cache hit` `b3_audit_repair.py:3769`), до `release()`; детерм. `allowed_evidence_pids` из `entity`/`VERIFIED aliases`, quarantined исключение — верификация: `Roxanne→Бабуль` с evidence вне `allowed` отклоняется, quarantined evidence отклоняется
- [ ] 2.3 Атомарная запись `glossary_proposals.json` (`tmp+rename`, `regular non-symlink`) с identity `chapter_id, snapshot_hash, config_identity, resolver_version, prompt_version, response_schema, model_ref/backend identity, candidate_input_hash, translation_hash(in-memory repairs)` — верификация: `sha256sum` sidecar совпадает с `candidate_input_hash`/`translation_hash`
- [ ] 2.4 Строгая валидация чтения sidecar (тип файла, точная схема/ключи, размеры, `duplicate entries`, `duplicate ru` между сущностями, `proposed_ru` в `evidence` тексте, повторная `provenance` перед промоутом, `stale` при любом hash mismatch) — верификация: `symlink/dir/non-regular` и `extra fields` отклоняются, `TOCTOU` (замена после проверки) не промоутит

## 3. Интеграция в book-run и промоут

- [ ] 3.1 В `v4_book_run` после `terminal_status` (`complete/accepted_degraded`) читать `glossary_proposals.json`, валидировать identity/семантический `translation_hash` (с учётом `formatting` тегов), вызывать `MemoryManager.add_observation("glossary")` через существующий `quarantine/promotion` gate; `mode=shadow` только логирует — верификация: `book_run.json candidates.proposed/committed` отражает `Leanne→Лианн` через новый путь, `shadow` не меняет `glossary.json`
- [ ] 3.2 Поддержка многословных и алиасов: `Knights of the Basement→Рыцари Подвала` целиком, `Craig Dowght/C. Dowght/Dowght→Даут` — один canonical, алиасы не ключи (duplicate внутри группы разрешён) — верификация: `Craig Dowght` не становится отдельным `glossary` ключом

## 4. Депрекейт и линт

- [ ] 4.1 Депрекейт `align_candidates` ветки для `proper_name` в `glossary_observations_from_entity_context` (оставить для `term` телеметрии) — верификация: `Shotgun→Дробовик` резолвится, `door→дверь` не промоутится
- [ ] 4.2 Заменить `pact-fidelity-lint` suffix/translit hard checks на проверку пары `proposed_ru + evidence surface forms` с fixture (`Роксанна/Херб/Дионис` pass, `Кристоффа/Диониса/Бабуль` fail), `Бабуль`-блоклист — только regression — верификация: `Sandra→Сандра` и `Roxanne→Роксанна` не падают на `а`

## 5. Модель и наблюдаемость

- [ ] 5.1 `glossary_resolver` `usage.ndjson` (`label glossary_resolver`), `backend events`, `phase_progress` на каждый `attempt` (1 logical batch = ≤3 attempts), `failure → fail-closed`, глава не падает — верификация: `usage` содержит `glossary_resolver` запись

## 6. Тесты persistent-data boundary

- [ ] 6.1 Добавить тесты: `full B3 cache hit + valid/missing/stale/tampered sidecar`, `crash между B3 cache и sidecar`, `invalid/extra/duplicate/truncated JSON`, `no candidates → 0 вызовов`, `resolver failure → 0 promotion`, `evidence PID не содержит source`, `quarantined evidence`, `aliases общий RU`, `symlink/dir/non-regular + TOCTOU`, `deterministic ordering`, `повторный book-run не делает повторный commit` — верификация: `pytest` green

## 7. Проверка

- [ ] 7.1 `openspec validate --all --strict` проходит
- [ ] 7.2 Shadow прогон 5-10 глав (`0033-0035`) с `mode=shadow→promote` — сравнить `glossary_proposals` vs старый `align`, `precision 50%→>90%`, отсутствие рестарта `local Qwen`, `v4_phase_progress` не регрессирует
