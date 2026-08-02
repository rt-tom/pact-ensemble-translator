# V4 A1 — Provider boundary: `CompletionBackend` (task)

Backing spec:
- `docs/plans/V4_OPENCODE_REMOTE_MODELS_INTEGRATION_PLAN_RU.md`
  (§5 контракты, §6 адаптеры ролей, §13 файлы, §14.1 unit tests,
  §14.3 pipeline parity, §16 PR 1).
- `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` (§4, Поток A — A1).
- `DECISIONS.md` (2026-08-01: утверждён план порядка реализации v4).

Target: `v4.0`. Draft PR. Характер: runtime/provider boundary — чистый
рефакторинг **без изменения алгоритма перевода**.

## Зачем это отдельная карточка

A1 = PR 1 интеграционного плана — первый шаг и фундамент для всех
остальных потоков. Это чистый рефакторинг: вводится transport-граница
`CompletionBackend` под уже существующими контрактами Phase 1/2, чтобы:

- Phase 3B/4 (Поток B) и OpenCode backend (Поток C) писались сразу
  backend-neutral, а не ретрофитились;
- один и тот же pipeline работал на local / remote / composite backend'ах
  (план §1: удалённая модель — замена способа выполнения model call,
  а не новый вариант pipeline);
- runtime-контракты были готовы к переносу в v5
  (`docs/architecture/V5_UNIVERSAL_LITERARY_TRANSLATOR_ARCHITECTURE_RU.md` §5.6).

Явное намерение: эта работа не выбрасывается — она и есть переносимый в
v5 provider boundary.

## Что уже есть в кодовой базе и почему это не покрывает задачу

Три инъекционные границы на месте (`ModelCaller`, `QwenEvaluator`,
`GemmaSelector`), `generate_for_chunk`/`select_candidate` сами HTTP не
запускают. Но runtime-слой зацеплен:

1. `ApiClient` (`pact_v4/runtime/api_client.py`) рассчитан на один
   OpenAI-compatible `chat/completions` endpoint — нет общего запроса/
   ответа/дескриптора, отделяющего транспорт от роли.
2. `StrictBackendConfig` (`v4_phase12_strict_runner.py`) описывает только
   локальный `llama-server` (exe/device/GGUF/server_args) — нет общего
   `BackendDescriptor`/`identity_hash`, пригодного для remote/composite.
3. HTTP-вызов, prompt rendering и разбор результата частично объединены
   внутри `HttpModelCaller`/`HttpQwenEvaluator`/`HttpGemmaSelector` —
   смена transport потребует переписывать каждый ролевой адаптер.
4. `run_chapter_strict` жёстко типизирован `ModelRouter`'ом
   (`router.switches`/`router.current_model`/`router.release()`).

Пункты 2 и 4 — это PR 3 (Поток C). **A1 их не трогает**: строго
`backend_protocol.py` + `LocalOpenAIBackend` + backend-neutral ролевые
адаптеры + совместимость текущих `Http*`.

## Что реализовать

### Новые модули

`pact_v4/runtime/backend_protocol.py`:

- `CompletionRequest` (frozen): `model_ref`, `messages`,
  `max_output_tokens`, `temperature`, `response_schema`, `label`,
  `request_options` (allowlist, входят в backend/config identity);
  секреты в объект не помещаются; rendered prompt не меняется
  transport-слоем.
- `CompletionResponse` (frozen): `text` (обязательный compatibility
  field), `structured`, `provider`, `model`, `finish_reason`, `usage`,
  `wall_seconds`, `request_id`, `session_id`, `retry_count`,
  `raw_metadata`.
- `BackendCallRecord`.
- `BackendDescriptor` (frozen): `kind`, `transport_version`,
  `endpoint_family`, `public_endpoint`, `model_bindings`,
  `effective_options`, `identity_hash` (`init=False`).
  `identity_hash` включает всё, что меняет ответ модели (kind, provider/
  model, adapter version, endpoint family, structured-output mode + schema
  version, effective temperature/max output/reasoning/variant, retry
  policy); НЕ включает API key, Basic Auth password, локальный TCP port,
  log-пути, display labels (план §5.4).
- `CompletionBackend(Protocol)`: `descriptor`, `complete(request)`,
  `close()`, `call_records()`.

`pact_v4/runtime/local_openai_backend.py`:

- `LocalOpenAIBackend` — адаптер над текущим `ApiClient`:
  - строит payload из `CompletionRequest`, выполняет
    `ApiClient.complete(...)`, нормализует результат в
    `CompletionResponse`;
  - сохраняет существующее поведение: `response_format=json_object` +
    grammar-reject fallback (`peg-gemma4`), bounded retries;
  - `close()` идемпотентен, закрывает HTTP session и НЕ останавливает
    чужой `llama-server` (этим по-прежнему управляет только lifecycle
    adapter, который его запустил);
  - `descriptor.public_record()` не содержит credentials/query secrets.

`pact_v4/runtime/backend_role_adapters.py`:

- `BackendModelCaller`, `BackendQwenEvaluator`, `BackendGemmaSelector` —
  backend-neutral реализации поверх `CompletionBackend` с теми же
  prompts/parsers/validation: `render_prompt`,
  `render_qwen_review_prompt`, `render_gemma_preference_prompt`,
  `_parse_qwen_verdict`, `_parse_gemma_preference`, существующая
  validation в `generation.py`.
- Механическая замена (план §6):
  `wrapper -> ApiClient.complete(...)` →
  `wrapper -> backend.complete(request)`.

### Совместимость

- Текущие `Http*` классы остаются compatibility wrappers над
  `LocalOpenAIBackend`: публичные имена/поведение не меняются, их тесты
  проходят без правок. Публичные классы и тесты переименовывать сразу
  НЕ нужно.
- Экспорт новых контрактов в `pact_v4/runtime/__init__.py`.
- `pact_v4/__init__.py`, `phase1/`, `phase2/`, `phase3/`, prompts и
  cascade не меняются.

### Вне scope (другие карточки)

- strict runner / `RuntimeCoordinator` / journal v2 / resume identity
  (PR 3, Поток C);
- `OpenCodeServerBackend` (PR 2, Поток C);
- tagged backend configs / CLI `--runtime-config` (PR 3/4);
- prompt templates, Phase 1/2 algorithms, risk, cascade, chunking,
  repair policy, terminal states.

## Тесты

- Unit tests backend protocol (план §14.1): canonical identity; secrets
  отсутствуют в identity/public record; unknown request option
  отвергается; usage/finish_reason/request_id нормализуются; structured
  output канонически пересериализуется; `close()` идемпотентен.
- Pipeline parity tests (план §14.3): один и тот же canned model output
  через local fake backend и `LocalOpenAIBackend` (fake `ApiClient`) даёт
  одинаковые `PromptBundle.bundle_hash`, rendered prompt bytes, Candidate
  PID-map, Qwen `GateResult`, Gemma selected candidate,
  `SelectionResult`/gate trace, final translations. Backend identity в
  реальном run, разумеется, различается — parity проверяется на
  идентичных fixtures.
- Существующий suite `tests/pact_v4/` зелёный (особенно
  `runtime/test_model_caller.py`, `test_qwen_evaluator.py`,
  `test_gemma_selector.py`, `test_api_client.py`,
  `pipeline/test_v4_phase12_strict_runner.py`).

## Gate / Acceptance

1. Полный `tests/pact_v4/` зелёный.
2. Старый local strict command даёт прежний порядок model calls и
   lifecycle.
3. Rendered prompts не отличаются из-за backend.
4. `ChunkPlanArtifact.plan_hash` совпадает для одинакового
   source/snapshot и chunk config.
5. Ни одна правка не меняет risk/chunking/cascade/repair policy.
6. Нет новых credentials/секретов в artifacts или logs.

## Роль-сплит

A1 создаёт реальные модули в `pact_v4/runtime/` — по духу это обычная
V4-фаза: по конвенции «реализует, второй делает adversarial review».
Перед началом реализации нужно повторно спросить пользователя, кто
пишет код — прошлые override'ы не переносятся автоматически.

## Компактный промпт

```text
Реализуй v4 A1 (PR 1 интеграционного плана) из
docs/plans/V4_OPENCODE_REMOTE_MODELS_INTEGRATION_PLAN_RU.md и
docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md (Поток A).
Target: v4.0. Draft PR. Не трогай v3 и production; не меняй Phase 1/2,
prompts, cascade, risk, strict runner и journal.
```
