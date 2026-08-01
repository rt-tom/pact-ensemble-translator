# Pact v4 — минимальная интеграция внешних моделей через OpenCode

Дата: 2026-08-01
Статус: proposed implementation plan
Целевая ветка: `v4.0`
Характер изменения: runtime/provider integration без изменения алгоритма перевода

## 1. Решение

В v4 добавить возможность назначать внешние модели OpenCode на уже существующие
роли генератора, semantic fidelity reviewer и Russian-only selector, не меняя:

- структуру `SourceArtifact`, `Snapshot`, `ChunkPlanArtifact` и `Candidate`;
- word-based chunk profile `280/450/640`;
- порядок обработки chunk'ов strict-драйвером;
- exact committed RU `left_context`;
- набор A/B-кандидатов по risk band;
- prompt templates и их версии;
- PID-map output contract;
- `select_candidate` и deterministic gates;
- правила quarantine/needs_synthesis;
- journal/resume-семантику;
- текущие правила качества и terminal states.

Удалённая модель должна быть заменой способа выполнения model call, а не новым
вариантом pipeline. Для одинакового входа локальный и удалённый профили получают
одинаковые work units и одинаковые rendered prompts. Различаются только backend,
model identity, transport options и результаты модели.

Основной OpenCode transport для v4 — `opencode serve` и его HTTP/OpenAPI
интерфейс. `opencode run` допускается только как диагностический proof of concept,
но не как production backend.

## 2. Почему изменение можно сделать относительно изолированно

Текущая Phase 2 уже имеет три инъекционные границы:

```text
ModelCaller    : PromptBundle -> raw JSON text
QwenEvaluator : source + translation -> GateResult
GemmaSelector : candidate translations -> GateResult
```

`generate_for_chunk` и `select_candidate` не запускают HTTP или `llama-server`
самостоятельно. Реальный transport находится в `pact_v4/runtime`. Это позволяет
добавить OpenCode под существующими контрактами, не переписывая Phase 1/2.

Основные сцепления, которые всё же нужно устранить:

1. `ApiClient` рассчитан на один OpenAI-compatible `chat/completions` endpoint.
2. `StrictBackendConfig` описывает только локальный `llama-server`: executable,
   device, GGUF paths и server args.
3. `run_chapter_strict` типизирован конкретным `ModelRouter` и напрямую читает
   `router.switches`, `router.current_model`, вызывает `router.release()`.
4. Trial record всегда сериализует локальные поля backend и lifecycle.
5. HTTP-вызов, prompt rendering и разбор результата частично объединены внутри
   `HttpModelCaller`, `HttpQwenEvaluator` и `HttpGemmaSelector`.

Это runtime debt, который всё равно пришлось бы устранять в v5. Его выгодно
исправить сейчас, но узко: без переименования Qwen/Gemma-ролей и без универсализации
языков, форматов книги или chunk topology.

## 3. Цели и не-цели

### 3.1. Цели

- Использовать модели `provider/model`, доступные через OpenCode.
- Разрешить локальный, полностью удалённый и смешанный профиль ролей.
- Сохранить один и тот же pipeline для всех backend'ов.
- Не смешивать cache/resume между разными моделями или transport settings.
- Сохранять usage, latency, request/session ID и ошибки удалённых вызовов.
- Не записывать API keys и пароли в config artifacts или run artifacts.
- Сохранить текущий local strict mode без изменения поведения.
- Подготовить минимальные runtime-контракты, которые можно перенести в v5.

### 3.2. Не-цели v4

- Не менять размер или способ формирования chunk'ов.
- Не отправлять целую главу вместо текущего chunk prompt.
- Не добавлять EPUB или другие входные форматы.
- Не добавлять выбор языковой пары.
- Не добавлять интернет-исследование книги или автора.
- Не вводить adaptive execution planner.
- Не менять роли Qwen/Gemma на нейтральные по всему коду.
- Не добавлять новые model stages или третью обязательную модель.
- Не подключать OpenCode tools, shell, file access или агентную работу.
- Не считать внешний structured output заменой собственной валидации Pact.

## 4. Целевая runtime-схема v4

```text
run_chapter_strict
  |
  +-- generate_for_chunk
  |     -> BackendModelCaller
  |          -> CompletionBackend.complete()
  |
  +-- select_candidate
        +-- BackendQwenEvaluator
        |     -> CompletionBackend.complete()
        +-- deterministic gates (без изменений)
        +-- BackendGemmaSelector
              -> CompletionBackend.complete()

CompletionBackend implementations:
  - LocalOpenAIBackend       (нынешний llama-server)
  - OpenCodeServerBackend    (новый)
```

`CompletionBackend` — transport boundary. Логика prompts, parsing, PID validation,
candidate construction и cascade остаётся выше него.

## 5. Минимальные новые контракты

### 5.1. `CompletionRequest`

```python
@dataclass(frozen=True)
class CompletionRequest:
    model_ref: str
    messages: tuple[Message, ...]
    max_output_tokens: int
    temperature: float
    response_schema: Mapping[str, Any] | None
    label: str
    request_options: Mapping[str, Any] = field(default_factory=dict)
```

Требования:

- `model_ref` для OpenCode хранится как `provider/model`;
- rendered prompt не изменяется transport-слоем;
- `response_schema` описывает тот же JSON, который уже ожидает Pact;
- `request_options` проходят allowlist и входят в backend/config identity;
- секреты в объект не помещаются.

### 5.2. `CompletionResponse`

```python
@dataclass(frozen=True)
class CompletionResponse:
    text: str
    structured: Mapping[str, Any] | None
    provider: str
    model: str
    finish_reason: str | None
    usage: Mapping[str, Any]
    wall_seconds: float
    request_id: str | None
    session_id: str | None
    retry_count: int
    raw_metadata: Mapping[str, Any]
```

`text` остаётся обязательным compatibility field. Если OpenCode вернул
`structured_output`, backend сериализует его в канонический JSON и одновременно
кладёт исходный объект в `structured`. Существующие Pact parsers всё равно
повторно валидируют JSON и PID/schema invariants.

### 5.3. `CompletionBackend`

```python
class CompletionBackend(Protocol):
    @property
    def descriptor(self) -> BackendDescriptor: ...

    def complete(self, request: CompletionRequest) -> CompletionResponse: ...

    def close(self) -> None: ...

    def call_records(self) -> Sequence[BackendCallRecord]: ...
```

Для локального backend `close()` не обязан останавливать чужой server: этим
по-прежнему управляет только lifecycle adapter, который его запустил. Для
OpenCode backend `close()` закрывает HTTP session и очищает только те временные
OpenCode sessions, которые создал данный backend и которые разрешено удалить
session policy.

### 5.4. `BackendDescriptor`

```python
@dataclass(frozen=True)
class BackendDescriptor:
    kind: str                 # local_llama | opencode_server
    transport_version: str
    endpoint_family: str
    public_endpoint: str
    model_bindings: Mapping[str, str]
    effective_options: Mapping[str, Any]
    identity_hash: str = field(init=False)
```

`identity_hash` включает всё, что способно изменить ответ модели:

- backend kind;
- provider/model для каждой роли;
- OpenCode/server adapter version;
- endpoint family;
- system/agent profile identity;
- structured-output mode и schema version;
- effective temperature/max output/reasoning/variant;
- retry policy, если повтор может вызвать новый model output.

Не включаются:

- API key;
- Basic Auth password;
- локальный TCP port, если он не меняет model behaviour;
- пути к log directory;
- display labels.

## 6. Адаптеры существующих ролей

Создать backend-neutral реализации:

```text
BackendModelCaller
BackendQwenEvaluator
BackendGemmaSelector
```

Они используют текущие функции:

- `render_prompt`;
- `render_qwen_review_prompt`;
- `render_gemma_preference_prompt`;
- `_parse_qwen_verdict`;
- `_parse_gemma_preference`;
- существующую validation logic в `generation.py`.

Изменение должно быть механическим:

```text
раньше: wrapper -> ApiClient.complete()
после : wrapper -> CompletionBackend.complete()
```

Текущие `Http*` классы можно оставить как compatibility wrappers над
`LocalOpenAIBackend`. Не нужно сразу переименовывать публичные классы и все tests.

Это лёгкая правка, полезная для v5: model role больше не зависит от конкретного
HTTP protocol.

## 7. `OpenCodeServerBackend`

### 7.1. Выбранный интерфейс

Backend работает с заранее запущенным:

```text
opencode serve --hostname 127.0.0.1 --port 4096
```

Python-код обращается к OpenCode REST/OpenAPI напрямую. TypeScript sidecar и
Node SDK для v4 не нужны: они добавили бы второй runtime только ради нескольких
HTTP endpoints.

Перед первым model call backend выполняет read-only проверки:

1. `GET /global/health` — server доступен, версия поддерживается;
2. provider/config endpoint — нужный provider подключён;
3. нужная модель существует;
4. server version попадает в поддерживаемый диапазон адаптера.

Конкретные request/response поля сверяются с `/doc` установленной версии и
фиксируются contract tests. Нельзя полагаться только на текущую веб-документацию
без version pin.

### 7.2. Session policy

Default для Pact v4:

```text
session_scope: per_request
context_reuse: false
tools: disabled
project_access: disabled
retain_success_sessions: false
retain_failed_sessions: true
```

Каждый Pact work unit должен быть независимым. Нельзя продолжать общую OpenCode
session между chunk'ами: скрытая история изменила бы prompt identity и могла бы
передать незафиксированный контекст следующему вызову.

Backend:

1. создаёт session с Pact label/work-unit identity;
2. отправляет один prompt с явно выбранным provider/model;
3. отключает все tools;
4. получает text или structured output;
5. сохраняет session/request ID в `BackendCallRecord`;
6. удаляет только созданную им успешную session, если это разрешено policy;
7. при ошибке сохраняет session для диагностики, если `retain_failed_sessions`.

### 7.3. Agent isolation

Для перевода не нужен coding agent. Использовать dedicated OpenCode agent/profile:

- без `bash`, read/edit/glob/grep/web/task и других tools;
- без автоматического чтения repository instructions;
- без project initialization;
- с коротким нейтральным system prompt;
- без `--auto`;
- с versioned agent/system identity в provenance.

Prompt Pact остаётся главным содержательным prompt. OpenCode не должен добавлять
скрытую задачу «редактировать код» или искать файлы.

### 7.4. Structured output

Поддержать режимы:

```text
prompt_only
json_schema
```

Для первого parity smoke использовать `prompt_only`: модель получает ровно
текущую инструкцию вернуть strict JSON, а Pact применяет нынешний parser.

После parity smoke можно включить `json_schema` для OpenCode profiles. Schema
должна соответствовать существующему contract и иметь bounded `retryCount`.
Даже при validated structured output Pact повторно проверяет:

- полный PID set;
- порядок PID;
- отсутствие foreign/duplicate PID;
- allowed keys/categories;
- candidate IDs;
- semantic identity.

Нельзя одновременно включать многоуровневые непрозрачные retries. Выбирается
одна политика:

```text
OpenCode structured retryCount=N, Pact transport retries только на network/5xx/429
```

Schema failure не превращается в semantic gate failure. Это отдельный статус
`invalid_model_output`/`incomplete_generation`.

## 8. Конфигурация v4

Пример полностью удалённого профиля:

```yaml
runtime:
  kind: opencode_server
  base_url: http://127.0.0.1:4096
  server_version_policy: compatible_minor
  auth:
    type: basic_env
    username_env: OPENCODE_SERVER_USERNAME
    password_env: OPENCODE_SERVER_PASSWORD
  session_policy:
    scope: per_request
    retain_success: false
    retain_failed: true
  tools: disabled
  structured_output: prompt_only

roles:
  generator:
    model: opencode-go/deepseek-v4-flash
    max_output_tokens: 8192
    temperature: 0.2
  fidelity_reviewer:
    model: opencode-go/qwen3.7-plus
    max_output_tokens: 24576
    temperature: 0.0
  russian_selector:
    model: opencode-go/qwen3.7-plus
    max_output_tokens: 1024
    temperature: 0.0

pipeline:
  chunk_min_words: 280
  chunk_target_words: 450
  chunk_max_words: 640
  right_context_pids: 0
  seed: 7
  max_consecutive_terminal_nonselections: 3
```

Названия `fidelity_reviewer` и `russian_selector` являются config aliases.
Внутренние v4 gate names могут остаться `qwen_fidelity` и
`gemma_russian_preference`, чтобы не менять schemas и historical artifacts.

Пример смешанного профиля:

```yaml
runtime:
  kind: composite

backends:
  local:
    kind: managed_llama
    # нынешний SYCL profile
  opencode:
    kind: opencode_server
    base_url: http://127.0.0.1:4096

roles:
  generator:
    backend: opencode
    model: opencode-go/deepseek-v4-flash
  fidelity_reviewer:
    backend: opencode
    model: opencode-go/qwen3.7-plus
  russian_selector:
    backend: local
    model: gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf
```

Composite routing полезно реализовать сразу: это небольшой mapping role→backend,
а в v5 он всё равно понадобится. Но v4 не адаптирует topology к backend — вызовы
идут в прежнем строгом порядке.

## 9. Изменения strict-драйвера

### 9.1. Runtime coordinator

Ввести узкий `RuntimeCoordinator` вместо конкретной зависимости от
`ModelRouter`:

```python
class RuntimeCoordinator(Protocol):
    @property
    def backend_descriptor(self) -> BackendDescriptor: ...

    def event_count(self) -> int: ...
    def events_since(self, index: int) -> Sequence[BackendEvent]: ...
    def close(self) -> None: ...
    def summary(self) -> Mapping[str, Any]: ...
```

Реализации:

- `LocalLifecycleCoordinator` адаптирует нынешний `ModelRouter`;
- `RemoteRuntimeCoordinator` не делает model swaps, но агрегирует call usage;
- `CompositeRuntimeCoordinator` собирает local switch и remote call events.

Внутренний цикл `run_chapter_strict` не меняется. Механические замены:

```text
len(router.switches)       -> runtime.event_count()
router.release()           -> runtime.close()
router switch indices      -> backend event indices
_switch_aggregates(...)    -> runtime.summary()
```

Для совместимости journal v2 хранит:

```json
{
  "backend_event_indices": [1, 2],
  "switch_indices": [1]
}
```

`switch_indices` остаётся для локальных run readers. Для remote он пустой.

### 9.2. Backend config

Не расширять `StrictBackendConfig` десятками optional полей. Ввести tagged
конфигурации:

```text
LocalLlamaBackendConfig
OpenCodeBackendConfig
CompositeBackendConfig
```

У каждой общий интерфейс:

```text
identity_hash
public_record()
build_runtime()
```

Старый `StrictBackendConfig` можно временно сделать alias
`LocalLlamaBackendConfig`, сохранив существующие imports/tests.

### 9.3. Record schema

Создать `pact-v4-strict-chapter-trial/v2`:

```json
{
  "backend": {
    "kind": "opencode_server",
    "identity_hash": "...",
    "public_endpoint": "http://127.0.0.1:4096",
    "model_bindings": {
      "generator": "opencode-go/deepseek-v4-flash",
      "fidelity_reviewer": "opencode-go/qwen3.7-plus",
      "russian_selector": "opencode-go/qwen3.7-plus"
    },
    "transport_version": "..."
  },
  "runtime": {
    "local_lifecycle": null,
    "remote_calls": {
      "count": 23,
      "input_tokens": 0,
      "output_tokens": 0,
      "cached_input_tokens": 0,
      "reported_cost": null
    }
  }
}
```

Если provider не сообщил usage/cost, сохраняется `null`, а не вычисленная
догадка.

## 10. Retry, ошибки и rate limits

Ошибки делятся на классы:

```text
transport_timeout
transport_network
provider_429
provider_5xx
provider_auth
provider_model_unavailable
structured_output_failed
invalid_model_output
semantic_gate_failed
```

Правила:

- `401/403` не retry'ятся;
- `429` соблюдает `Retry-After`, но ограничивается run budget;
- network/5xx получают bounded retry;
- invalid JSON получает только configured structured-output retry;
- model unavailable не вызывает silent fallback на другую модель;
- semantic `passed=False` никогда не retry'ится как transport error;
- после исчерпания transport retries work unit остаётся incomplete/quarantined
  по существующим правилам, с точной причиной;
- OpenCode session/request IDs сохраняются до retry, чтобы различать реальные
  повторные оплачиваемые вызовы.

Добавить run budgets:

```yaml
remote_budget:
  max_requests_per_chapter: 100
  max_retry_requests_per_chapter: 10
  max_wait_seconds_on_rate_limit: 900
  max_reported_cost: null
```

Budget exhaustion — явная operational failure/debt, не semantic verdict.

## 11. Resume и cache identity

Remote resume принимает старый journal только при совпадении:

- source/snapshot/chunk plan/config identities;
- backend identity;
- role→provider/model bindings;
- OpenCode adapter/system-agent identity;
- structured-output policy;
- effective request options.

API key rotation не инвалидирует cache, если provider/model/settings не
изменились. Смена `deepseek-v4-flash` на другую модель обязана инвалидировать
cache и context-dependent suffix.

Успешный Pact artifact является источником resume truth. OpenCode session history
не используется для восстановления: session может быть удалена, а candidate и
journal остаются воспроизводимо привязаны к prompt/backend identity.

## 12. Secrets и privacy

- OpenCode Go/Zen credentials остаются в OpenCode auth storage.
- Pact знает только URL локального OpenCode server и provider/model IDs.
- Basic Auth для `opencode serve` читается из environment variables.
- Значения secrets никогда не сериализуются.
- `public_record()` редактирует query strings, usernames и headers.
- Logs не содержат Authorization header.
- Пользователь должен понимать, что текст книги отправляется выбранному
  удалённому provider; это явно показывается до run.
- Provider retention/training policy не должна молча интерпретироваться Pact:
  профиль хранит только пользовательское acknowledgement и ссылку/дату policy.

## 13. Изменения по файлам

### Новые модули

```text
pact_v4/runtime/backend_protocol.py
  CompletionRequest/Response, BackendDescriptor, BackendCallRecord, Protocol

pact_v4/runtime/local_openai_backend.py
  adapter над текущим ApiClient

pact_v4/runtime/opencode_backend.py
  health/provider/session/message/structured-output client

pact_v4/runtime/backend_role_adapters.py
  BackendModelCaller/BackendQwenEvaluator/BackendGemmaSelector

pact_v4/runtime/runtime_coordinator.py
  local/remote/composite coordinators и telemetry

pact_v4/runtime/runtime_config.py
  tagged config loader, secret refs, validation
```

### Изменяемые модули

```text
pact_v4/runtime/model_caller.py
pact_v4/runtime/qwen_evaluator.py
pact_v4/runtime/gemma_selector.py
  -> переиспользовать backend-neutral transport; сохранить public behaviour

pact_v4/pipeline/v4_phase12_strict_runner.py
  -> RuntimeCoordinator, generic backend record, journal v2

pact_full_pipeline_runner_v1/v4_phase12_strict_run.py
  -> --runtime-config; прежний local default остаётся

pact_v4/runtime/__init__.py
  -> exports новых контрактов

DECISIONS.md
  -> записать scope и запрет на topology drift
```

Phase 1, Phase 2 algorithms, prompts, risk, cascade и deterministic checks не
изменяются.

## 14. Тестовая стратегия

### 14.1. Unit tests backend protocol

- canonical backend identity;
- secrets отсутствуют в identity/public record;
- unknown request option отвергается;
- usage/finish reason/request ID нормализуются;
- structured output повторно сериализуется канонически;
- `close()` идемпотентен.

### 14.2. Fake OpenCode server contract tests

Без реальной сети и платных вызовов:

- health success/version mismatch;
- provider/model missing;
- session create/message/result;
- tools действительно выключены;
- JSON text response;
- structured output response;
- malformed response;
- timeout/429/5xx/auth failure;
- retry budget;
- failed session retention;
- cleanup только собственных sessions.

### 14.3. Pipeline parity tests

Один и тот же canned model output подаётся через local fake backend и OpenCode
fake backend. Должны совпасть:

- `PromptBundle.bundle_hash` при одинаковом model/config identity fixture;
- rendered prompt bytes;
- Candidate PID-map;
- Qwen `GateResult`;
- Gemma selected candidate;
- `SelectionResult` и gate trace;
- final translations.

Backend/config identity в реальном run, разумеется, различается.

### 14.4. Resume tests

- remote run resume skips committed chunks;
- смена model binding отвергает journal;
- смена structured-output policy отвергает journal;
- смена API key не влияет на identity;
- composite profile не переиспользует artifacts другого routing map;
- remote run не требует существования старой OpenCode session.

### 14.5. Live smoke

Минимальный оплачиваемый smoke:

1. один искусственный low-risk chunk;
2. один high-risk A/B chunk;
3. два fidelity verdict;
4. один Russian preference;
5. проверка usage/provenance/secrets;
6. затем chapter_046 trial с прежним chunk plan.

Live smoke не является quality benchmark. После механической проверки нужен
отдельный blind quality comparison local vs selected remote profile.

## 15. Acceptance criteria

Интеграция принята, если:

1. Полный `tests/pact_v4/` зелёный.
2. Старый local strict command даёт прежний порядок model calls и lifecycle.
3. OpenCode profile обрабатывает те же chunks в том же порядке.
4. Rendered prompts не отличаются из-за backend.
5. `ChunkPlanArtifact.plan_hash` совпадает для одинакового source/snapshot и
   chunk config.
6. Все remote model outputs проходят существующие Pact parsers/validators.
7. Local, remote и composite profiles имеют разные корректные backend identity.
8. Resume отвергает foreign backend/model identity.
9. Ни один artifact/log не содержит credential values.
10. Provider/transport failure не маскируется как semantic gate failure.
11. Нет silent model fallback.
12. OpenCode не получает разрешений на tools/files/shell.
13. Trial record содержит provider/model, usage, latency и request/session IDs.
14. Никакая правка не меняет risk/chunking/cascade/repair policy.

## 16. Порядок реализации

### PR 1 — backend boundary, без нового поведения

- `CompletionBackend` contracts;
- `LocalOpenAIBackend` над текущим `ApiClient`;
- backend-neutral role adapters;
- перевести нынешние `Http*` wrappers на новый boundary;
- regression/parity tests.

Gate: local strict tests и chapter fixture полностью неизменны по смыслу.

### PR 2 — OpenCode client

- `OpenCodeServerBackend`;
- health/version/provider/model preflight;
- isolated tool-less sessions;
- prompt-only и JSON-schema modes;
- retry/error normalization;
- fake-server tests.

Gate: offline contract suite + один ручной smoke на тестовом prompt.

### PR 3 — generic runtime/provenance

- tagged backend configs;
- runtime coordinators;
- strict runner journal/record v2;
- backend identity/resume validation;
- local/remote/composite config loader.

Gate: old local run config работает; remote fake end-to-end работает.

### PR 4 — live trial и документация

- CLI `--runtime-config`;
- example configs без secrets;
- live one-chunk smoke;
- chapter_046 remote trial;
- сравнение operational metrics и quarantine causes;
- запись решения в `DECISIONS.md`.

## 17. Оценка масштаба

Ориентировочно:

```text
PR 1: S–M
PR 2: M
PR 3: M
PR 4: S кода + время оплачиваемого прогона
```

Наиболее рискованная часть — не Phase 2, а точное соответствие OpenCode OpenAPI
установленной версии и изоляция agent/session behaviour. Поэтому сначала нужен
fake-server contract suite, затем очень короткий live smoke.

Не рекомендуется экономить один PR и напрямую вставлять `subprocess(opencode
run)` в strict runner. Это быстро только для демонстрации, но создаёт технический
долг в parsing, sessions, cancellation, retries, provenance и v5 migration.

## 18. Решения, которые следует добавить в `DECISIONS.md`

1. V4 допускает local, OpenCode remote и composite backend profiles, но использует
   единый неизменный Phase 1/2 pipeline и один chunk topology.
2. OpenCode Server — основной OpenCode transport; CLI — diagnostic only.
3. Remote backend не получает tools и не переносит session context между Pact
   work units.
4. Backend/model/transport identity является частью cache/resume provenance;
   credentials — нет.
5. Provider failure и invalid structured output не являются semantic verdict.
6. Silent fallback между моделями запрещён.
7. Structured output усиливает transport reliability, но не заменяет Pact
   validators.
8. Изменения language pair, EPUB, research, whole-chapter context и adaptive
   topology отложены в v5.

## 19. Ссылки

- OpenCode CLI: https://dev.opencode.ai/docs/cli/
- OpenCode Server/OpenAPI: https://dev.opencode.ai/docs/server/
- OpenCode SDK/structured output: https://opencode.ai/docs/sdk/
- OpenCode Go endpoints/models: https://dev.opencode.ai/docs/go/
