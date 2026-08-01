# Pact v5 — архитектура универсальной системы литературного перевода

Дата: 2026-08-01
Статус: target architecture and staged implementation plan
Основание: Pact v4 quality engine + provider boundary из плана интеграции OpenCode

## 1. Видение

Pact v5 принимает книгу, языковую пару, переводческий профиль и набор доступных
моделей, после чего:

1. импортирует и нормализует книгу;
2. анализирует сам текст, автора и произведение;
3. при разрешении пользователя дополняет анализ интернет-источниками;
4. создаёт замороженный `BookResearchSnapshot`;
5. компилирует из него компактный `TranslationBrief`;
6. выбирает допустимую execution topology по возможностям моделей;
7. переводит с сохранением PID/formatting/provenance;
8. выполняет независимый semantic и target-language audit;
9. делает targeted repair/convergence;
10. экспортирует книгу в EPUB/HTML.

V5 не заменяет доказанные правила качества v4. Она обобщает входы, языки,
подготовку контекста, модели и topology вокруг этих правил.

## 2. Граница между v4 и v5

### В v4 остаётся

- текущая пара English → Russian;
- текущий HTML/PID input path;
- текущий word-based chunk plan;
- strict exact-left-context topology;
- A/B generation и cascade;
- deterministic risk/gates;
- существующие prompts;
- OpenCode/local/composite backend profiles через единый runtime boundary;
- Phase 3/4/5 по согласованному v4 плану.

### В v5 появляется

- выбор source/target language и locale;
- language packs;
- EPUB/универсальный HTML import/export;
- канонический `BookArtifact`;
- source-grounded анализ всей книги;
- опциональное web research автора/произведения/переводов;
- `BookResearchSnapshot` и `TranslationBrief`;
- provider/model capability discovery;
- adaptive execution planner;
- новые context strategies, включая full-chapter context;
- дополнительные языковые пары;
- продуктовый job API/UI.

## 3. Архитектурные принципы

1. **V4 quality contracts first.** PID ownership, deterministic integrity,
   semantic admission, immutable findings и targeted repair сохраняются.
2. **Языковая специфика — plugin, не if/else в pipeline.**
3. **Исследование отделено от перевода.** Интернет недоступен переводчику во
   время generation; используется только frozen research snapshot.
4. **Source-grounded facts выше web claims.** Интернет не переопределяет текст.
5. **Большой context — capability, не доказательство качества.** Topology
   разрешается benchmark'ом для model profile и language pair.
6. **Контекст отделён от output ownership.** Модель может видеть главу целиком,
   но возвращать PID-map одного chunk'а.
7. **Нет silent fallback.** Смена модели, topology, language pack или research
   snapshot меняет identity и provenance.
8. **Данные воспроизводимы.** Все решения перед первым model call заморожены в
   versioned artifacts.
9. **Пользователь контролирует внешнюю передачу текста.** Web search по умолчанию
   получает metadata, а не фрагменты книги.
10. **Новое внедряется вертикальными slices.** Каждая фаза заканчивается
    работающим end-to-end путём и regression gate.

## 4. Высокоуровневая схема

```text
Input adapters
  EPUB / HTML / directory
        |
        v
BookArtifact + FormattingContract
        |
        +--> LanguagePair + LanguagePack
        |
        +--> Source-grounded Book Analysis
        |          |
        |          +--> optional Web Research
        |                     |
        +--------------> BookResearchSnapshot
                               |
                               v
                       TranslationBrief
                               |
                               v
ModelRegistry + Capabilities -> ExecutionPlanner -> ExecutionPlan
                                                    |
                                                    v
                                      V4-derived Quality Engine
                                                    |
                                                    v
                                      EPUB / HTML exporters
```

## 5. Слои системы

### 5.1. Domain Core

Не зависит от LLM provider и конкретной языковой пары. Содержит:

- book/chapter/block identities;
- PID ownership;
- formatting spans;
- source/snapshot/config/provenance hashes;
- candidates/findings/repairs;
- terminal states;
- artifact store;
- journal/resume.

Существующие v4 dataclasses используются как исходная реализация. Перед
обобщением сначала фиксируются их regression fixtures и wire schemas.

### 5.2. Ingestion/Export

Преобразует входные форматы в `BookArtifact` и обратно. Pipeline после import не
должен знать, был ли вход EPUB, один HTML или каталог глав.

### 5.3. Language System

`LanguagePair` и `LanguagePack` предоставляют prompts, risk features,
tokenization, typography и deterministic target QA.

### 5.4. Research and Preparation

Анализирует книгу локально, опционально выполняет web research, разрешает
conflicts и создаёт frozen snapshot.

### 5.5. Prompt Compiler

Компилирует versioned prompt bundles из role template, language pack,
translation brief, work unit и model capability policy.

### 5.6. Model Runtime

Переиспользует v4 `CompletionBackend` boundary. Registry поддерживает local,
OpenCode и будущие transports.

### 5.7. Adaptive Execution Planner

Создаёт immutable `ExecutionPlan`: topology, context scopes, work units, role
bindings, budgets и fallback policy.

### 5.8. Quality Engine

Обобщённая версия v4 Phase 2–5:

- risk-gated generation;
- semantic admission;
- deterministic consistency;
- target-language selection;
- assembled-book/chapter audit;
- targeted repair;
- formatting recovery;
- final integrity and memory promotion.

### 5.9. Product/Job Layer

Upload, configuration, progress, resume, reports, artifacts и export. Этот слой
не содержит переводческой логики.

## 6. Канонические артефакты v5

### 6.1. `BookArtifact`

```json
{
  "schema": "pact-book/v1",
  "book_id": "...",
  "metadata": {
    "title": "...",
    "author": "...",
    "language_declared": "en",
    "identifiers": {"isbn": "..."}
  },
  "chapters": [
    {
      "chapter_id": "chapter-0001",
      "title": "...",
      "blocks": [
        {
          "pid": "p00001",
          "text": "...",
          "structural_role": "paragraph",
          "formatting_spans": []
        }
      ]
    }
  ],
  "toc": [],
  "assets": [],
  "source_hash": "..."
}
```

Инварианты:

- стабильный ordered PID для каждого переводимого блока;
- обратимая связь с исходным container/document path;
- отдельные non-translatable blocks;
- formatting не смешивается с plain translation text;
- импорт повторяем и content-addressed.

### 6.2. `LanguagePair`

```json
{
  "source_language": "en",
  "target_language": "ru",
  "target_locale": "ru-RU",
  "instruction_language": "en",
  "language_pack": "en-ru/v1"
}
```

Пользователь задаёт пару явно или принимает auto-detection. Detection никогда
не меняет выбор молча. Mixed-language blocks получают собственные language tags.

### 6.3. `LanguagePack`

Контракт plugin:

```python
class LanguagePack(Protocol):
    identity: LanguagePackIdentity

    def detect_source_features(...): ...
    def render_role_prompt(...): ...
    def target_integrity_checks(...): ...
    def typography_policy(...): ...
    def token_estimator(...): ...
    def evaluation_rubric(...): ...
```

Содержимое:

```text
source risk:
  negation, modality, referents, idioms, number words, register markers

target QA:
  morphology, gender, agreement, mixed script, spelling, punctuation

translation conventions:
  names/transliteration, quotes, dialogue dashes, units, profanity

prompts:
  translator, fidelity auditor, target-language reviewer, repairer
```

Текущая v4 EN/RU логика становится первым `en-ru/v1` pack.

### 6.4. `BookResearchSnapshot`

```json
{
  "schema": "pact-book-research/v1",
  "book_hash": "...",
  "language_pair_hash": "...",
  "author_profile": {},
  "work_style_profile": {},
  "narrator_profiles": [],
  "character_voice_cards": [],
  "entities": [],
  "glossary_candidates": [],
  "address_relationships": [],
  "typography_observations": [],
  "web_claims": [],
  "conflicts": [],
  "research_hash": "..."
}
```

Каждая запись содержит:

- claim/value;
- status;
- confidence;
- source type;
- evidence PID или URL;
- extractor/model identity;
- timestamp для web source;
- conflict/supersession trace.

Статусы:

```text
source_grounded
web_corroborated
provisional
established
locked
conflicted
rejected
```

### 6.5. `TranslationBrief`

Компактный prompt-facing артефакт, а не полный research report:

```json
{
  "language_pair": {},
  "narrative_constraints": [],
  "style_constraints": [],
  "character_voices": [],
  "address_register": [],
  "terminology": [],
  "typography": [],
  "forbidden_transformations": [],
  "brief_hash": "..."
}
```

Compiler включает только claims, прошедшие policy threshold. Неуверенные web
claims не становятся обязательными constraints.

### 6.6. `ModelProfile` и `ModelCapabilities`

```json
{
  "profile_id": "opencode-go/deepseek-v4-flash@translation-v1",
  "backend": "opencode_server",
  "model_ref": "opencode-go/deepseek-v4-flash",
  "capabilities": {
    "context_window": 0,
    "max_output_tokens": 0,
    "structured_output": ["prompt_only", "json_schema"],
    "supports_seed": false,
    "supports_reasoning_control": true,
    "concurrency": 1
  },
  "quality_approvals": [
    {
      "language_pair": "en-ru/v1",
      "role": "translator",
      "approved_topologies": ["bounded_chunk"]
    }
  ]
}
```

Capabilities получают из provider metadata/probe и ограничиваются user caps.
Quality approvals появляются только после benchmark; большая context window сама
по себе не разрешает whole-chapter mode.

### 6.7. `ExecutionPlan`

```json
{
  "topology": "chapter_context_chunk_output",
  "role_bindings": {},
  "context_policy": {},
  "generation_units": [],
  "audit_units": [],
  "repair_policy": {},
  "budgets": {},
  "fallback_policy": {},
  "execution_plan_hash": "..."
}
```

План строится и замораживается до первого model call.

## 7. Импорт и экспорт книги

### 7.1. HTML

Поддержать:

- один HTML document;
- каталог HTML/XHTML глав;
- web-serial export;
- существующие `data-pid`;
- генерацию PID при их отсутствии.

Importer классифицирует:

- heading;
- paragraph;
- dialogue block;
- epigraph;
- blockquote;
- footnote/endnote;
- navigation/copyright/non-translatable;
- image caption.

Первый gate — импорт текущих Pact HTML с тем же ordered source text и PID
identity, что v4.

### 7.2. EPUB

Importer:

- читает package metadata;
- соблюдает spine order;
- читает navigation/TOC;
- нормализует XHTML;
- сохраняет CSS/assets/fonts/images;
- извлекает notes/links;
- не пытается обходить DRM;
- создаёт обратимую mapping table EPUB node → chapter/block/PID.

Exporter:

- заменяет только переводимые text nodes;
- восстанавливает inline formatting;
- обновляет language metadata/title/TOC при configured policy;
- сохраняет assets;
- валидирует EPUB structure;
- генерирует export manifest и integrity report.

## 8. Подготовка книги и research

### 8.1. Source-grounded анализ — обязательный

Источник истины — сама книга. Анализ извлекает:

- POV, narrator identity/gender, tense;
- character entities, aliases, pronouns;
- relationships и ты/вы-equivalent register;
- recurring terminology и invented words;
- sentence length/rhythm;
- dialogue density и punctuation;
- profanity/intensity;
- dialect, idiolect, deliberate errors;
- irony/humour markers;
- recurring imagery;
- chapter/scene transitions;
- letters, dreams, quotes и other special forms.

Каждый claim должен иметь PID evidence. Global analysis выполняется bounded
work units с aggregate validation, даже если модель способна увидеть книгу
целиком.

### 8.2. Web research — опциональный enrichment

Query planner использует по умолчанию:

- title;
- author;
- ISBN/identifier;
- series;
- source/target language;
- publisher/edition.

Не отправляет текст книги без explicit consent.

Иерархия источников:

```text
official author/publisher
author interview
scholarly/critical source
professional review
library/catalog metadata
official translation metadata
community wiki/fandom
unverified forum/blog
```

Web pages являются untrusted data. Research extractor игнорирует инструкции на
странице и извлекает только claims с evidence/provenance. Claim не становится
locked только из-за одного источника.

### 8.3. Существующие переводы

Режимы:

```text
none
metadata_only
terminology_reference
licensed_alignment
```

- `metadata_only`: переводчик, издательство, год, ISBN, наличие перевода;
- `terminology_reference`: законно доступные установившиеся имена/названия;
- `licensed_alignment`: пользователь загрузил доступный ему перевод и разрешил
  alignment;
- полный чужой перевод не скачивается автоматически и не вставляется в prompts.

Результат существующего перевода используется как evidence/advisory terminology,
а не как неоспоримый эталон качества.

## 9. Prompt architecture

Prompt больше не хранится как English/Russian строка в общем модуле. Он
компилируется:

```text
RoleTemplate
+ LanguagePack
+ TranslationBrief
+ WorkUnit context
+ Risk instructions
+ Output schema
= PromptBundle
```

Prompt bundle identity включает hashes всех компонентов.

Основные блоки translation prompt:

```text
LANGUAGE_PAIR
ROLE_AND_PRIORITY
OWNED_SOURCE
READ_ONLY_CONTEXT
NARRATIVE_CONSTRAINTS
AUTHOR_AND_WORK_STYLE
CHARACTER_VOICES
ADDRESS_REGISTER
GLOSSARY_AND_NAMES
TYPOGRAPHY
RISK_SPECIFIC_INSTRUCTIONS
OUTPUT_CONTRACT
```

Не все блоки передаются всегда. Prompt budgeter выбирает только релевантные
entries, но не может удалить locked constraints.

## 10. Adaptive execution

### 10.1. Поддерживаемые topology

```text
strict_chunked
  Текущий v4 exact selected RU left-context.

batch_first
  Source-side frozen plan, role batching, boundary convergence.

chapter_context_chunk_output
  Модель видит source всей главы, но переводит только owned chunk PID-map.

whole_chapter_output
  Модель получает и возвращает всю главу одним generation unit.

hybrid
  Разные роли и/или risk bands используют разные local/remote profiles.
```

### 10.2. Предпочтительный long-context эксперимент

Первым испытывать `chapter_context_chunk_output`, а не сразу
`whole_chapter_output`. Он даёт модели глобальную информацию, сохраняя:

- bounded output;
- PID ownership;
- resume;
- дешёвый retry;
- region repair;
- ограниченный ущерб при malformed response.

`whole_chapter_output` разрешается, только если:

- input + expected output + safety reserve помещаются;
- provider max output подтверждён;
- structured output стабилен на chapter size;
- PID-map completeness проходит margin=0 gate;
- blind quality benchmark не хуже chunked control;
- bad retry cost приемлем;
- repair всё равно работает по region/chunk plan.

### 10.3. Planner inputs

- book/chapter token estimates;
- language pack tokenizer;
- model capabilities;
- quality approvals;
- local residency constraints;
- rate/concurrency limits;
- user cost/time budgets;
- data privacy policy;
- selected fallback policy.

Planner не выбирает непроверенную topology даже если она дешевле.

## 11. Quality engine v5

### 11.1. Сохраняемые v4 механизмы

- stable PID;
- source/snapshot/config/prompt identity;
- risk-gated candidate count;
- independent semantic admission;
- deterministic consistency;
- target-language preference;
- immutable findings;
- context staleness after repair;
- targeted convergence;
- deterministic final integrity;
- memory promotion только после `complete`.

### 11.2. Обобщаемые механизмы

```text
QwenEvaluator        -> SemanticFidelityEvaluator
GemmaSelector        -> TargetLanguageSelector
GemmaAuditEvaluator  -> TargetLanguageAuditEvaluator
```

Переименование делается в v5 с compatibility aliases при импорте v4 artifacts.

### 11.3. Generic и language-specific checks

Generic core:

- PID coverage/order;
- missing/extra content;
- formatting/HTML structure;
- digits;
- links/assets;
- empty output;
- model/schema integrity.

Language pack:

- number words;
- gender/agreement;
- morphology;
- spelling;
- quote/dialogue punctuation;
- transliteration;
- profanity/register;
- script mixing;
- locale-specific typography.

## 12. Memory

Разделить память:

```text
Research memory
  claims об авторе/книге/изданиях, citations

Translation memory
  glossary, names, character voice, address register, facts

Run memory
  candidates, findings, repairs, execution journal
```

Research snapshot frozen на книгу/edition. Chapter translation snapshot frozen
на главу. Web refresh не меняет активный run; он создаёт новую research version,
которую пользователь применяет только к новому run/resume fork.

## 13. Пользовательская конфигурация

```yaml
book:
  input: ./book.epub
  output_format: epub

language:
  source: auto
  target: ru
  target_locale: ru-RU
  require_detection_confirmation: true

research:
  source_analysis: true
  web:
    enabled: true
    send_book_text: false
    allowed_source_classes:
      - official
      - publisher
      - interview
      - scholarly
      - professional_review
  existing_translations:
    mode: metadata_only

models:
  profiles:
    translator_remote:
      backend: opencode_server
      model: opencode-go/deepseek-v4-flash
    auditor_remote:
      backend: opencode_server
      model: opencode-go/qwen3.7-plus
    reviewer_local:
      backend: managed_llama
      model: gemma-local

roles:
  source_profiler: auditor_remote
  translator: translator_remote
  semantic_auditor: auditor_remote
  target_language_reviewer: reviewer_local
  repairer: translator_remote

execution:
  topology: auto
  allowed_topologies:
    - strict_chunked
    - chapter_context_chunk_output
  max_parallel_remote_calls: 2
  fallback:
    silent: false

budgets:
  max_reported_cost_per_book: null
  max_requests_per_chapter: 100
```

## 14. Repository/module structure

Один из возможных layouts:

```text
pact_core/
  artifacts/
  identity/
  journal/
  quality/

pact_ingest/
  html/
  epub/
  export/

pact_languages/
  base.py
  en_ru/
  fixtures/

pact_research/
  source_analysis/
  web/
  claims/
  brief/

pact_runtime/
  backends/
  registry/
  capabilities/
  budgets/

pact_execution/
  planner/
  topologies/
  orchestration/

pact_app/
  cli/
  api/
  jobs/
```

На ранних фазах допустим namespace `pact_v5`, но границы пакетов нужно держать
такими с первого дня. Не создавать один новый `v5_pipeline.py`.

## 15. Последовательность реализации

Порядок специально идёт от максимального повторного использования v4 к новым
функциям.

### Phase 0 — заморозить v4 baseline и migration corpus

Работы:

- зафиксировать выбранные v4 run artifacts;
- собрать fixtures source/snapshot/chunk/candidate/findings;
- сохранить prompt bytes/hashes;
- зафиксировать EN→RU regression corpus;
- описать v4 artifact reader requirements.

Gate:

- v5 может прочитать выбранные v4 artifacts read-only;
- никакой новый дизайн не начинается без reproducible baseline.

### Phase 1 — перенести provider boundary v4

Работы:

- перенести `CompletionBackend`, descriptors, call records;
- local/OpenCode/composite backends;
- secret refs;
- generic runtime telemetry;
- не менять translation logic.

Gate:

- один v4 fixture одинаково проходит local fake и OpenCode fake transports.

### Phase 2 — выделить Domain Core

Работы:

- вынести identity/PID/candidate/finding/repair/journal contracts;
- создать v5 schema versions;
- compatibility readers v4;
- убрать imports из pipeline в transport и наоборот.

Gate:

- v4 fixture round-trip;
- foreign identity/cache poisoning tests;
- никаких model calls.

### Phase 3 — `BookArtifact` и HTML parity

Работы:

- canonical book/chapter/block model;
- HTML single/directory importer;
- stable PID assignment;
- formatting spans;
- HTML exporter;
- adapter в существующий v4-derived quality engine.

Gate:

- текущие Pact HTML дают эквивалентный source/PID order;
- export сохраняет structure/formatting;
- EN→RU strict topology работает end-to-end через `BookArtifact`.

### Phase 4 — `LanguagePair` и `en-ru/v1`

Работы:

- добавить explicit language/locale artifacts;
- вынести hardcoded English/Russian prompts;
- вынести EN source risk;
- вынести RU target QA/typography;
- compatibility aliases для старых gate names.

Gate:

- `en-ru/v1` не хуже frozen v4 regression;
- base pipeline не содержит English/Russian-specific regex/tables/text.

### Phase 5 — EPUB import/export

Работы:

- package/spine/TOC/XHTML parser;
- asset and note preservation;
- EPUB mapping artifact;
- translated EPUB builder;
- validation report.

Gate:

- EPUB → BookArtifact → unchanged-language EPUB round-trip fixtures;
- затем короткая EN→RU книга end-to-end;
- DRM input получает явный unsupported result.

### Phase 6 — source-grounded book analysis

Работы:

- claim/evidence schemas;
- narrator/character/entity/style analyzers;
- bounded book analysis units;
- conflict resolver;
- frozen source-only `BookResearchSnapshot`.

Gate:

- каждая blocking constraint имеет PID evidence;
- повторный анализ идентичен при deterministic fixtures;
- анализ не меняет authoritative translation memory напрямую.

### Phase 7 — `TranslationBrief` и prompt compiler

Работы:

- research-to-brief policy;
- role-specific brief views;
- prompt budgeter;
- prompt bundle identity;
- negative tests на потерю locked constraints.

Gate:

- current v4 glossary/style inputs выражаются новым brief;
- prompt changes полностью объясняются component hashes;
- brief улучшает seeded weak-spot corpus без integrity regression.

### Phase 8 — web research enrichment

Работы:

- query planner;
- search/fetch connectors;
- source classification/trust;
- untrusted-content extraction;
- citations/provenance;
- existing-translation modes;
- refresh/version policy.

Gate:

- по умолчанию поиску не отправляется book text;
- claims имеют URL/timestamp/source class;
- prompt injection fixtures не влияют на system policy;
- web-off и offline mode полностью работоспособны;
- web claims не переопределяют source-grounded facts без conflict record.

### Phase 9 — Model Registry и capability discovery

Работы:

- provider/model discovery;
- capability probes;
- context/output/concurrency metadata;
- cost/usage normalization;
- quality approval records по language pair/role/topology.

Gate:

- planner использует conservative effective capabilities;
- неизвестная capability не трактуется как unlimited;
- credentials отсутствуют в artifacts.

### Phase 10 — Adaptive Execution Planner

Работы:

- immutable `ExecutionPlan`;
- `strict_chunked` как первый control;
- `chapter_context_chunk_output` experiment;
- hybrid routing;
- budget/fallback planning;
- resume invalidation по plan hash.

Gate:

- strict plan воспроизводит v4-derived control;
- непроверенная topology не выбирается в `auto`;
- model/profile change перестраивает plan и invalidates cache корректно.

### Phase 11 — whole-chapter generation experiment

Работы:

- `ChapterGenerationUnit` отдельно от `ChunkPlan`;
- output budget estimation;
- full PID-map schema;
- bounded retries;
- conversion в обычные chunk-owned candidates для audit/repair;
- blind comparison.

Gate:

- PID/formatting margin=0;
- semantic/Russian quality non-inferior strict/chapter-context control;
- retry cost и truncation rate приемлемы;
- режим остаётся feature-flagged до достаточной выборки.

### Phase 12 — второй language pack

Выбирать пару по реальному спросу и доступным reviewer'ам. Работы:

- новый source risk;
- target QA/typography;
- prompts/rubric;
- golden set;
- model qualification.

Gate:

- наличие language pack недостаточно: нужны benchmark-approved модели для
  translator и independent semantic audit;
- EN→RU regression не затронут.

### Phase 13 — product job API/UI

Работы:

- upload/config wizard;
- language confirmation;
- research preview/conflicts;
- model/privacy/cost disclosure;
- progress/resume/cancel;
- artifact/report download;
- EPUB/HTML export.

Gate:

- UI только вызывает versioned domain commands;
- pipeline можно полностью запустить CLI/API без UI;
- cancel/resume не повреждает journal.

## 16. Benchmark gates

Для каждой новой topology/model/language pair:

```text
semantic residual
omission/addition rate
boundary/discourse defects
target-language rubric
voice/register consistency
terminology LTCR
bad-repair rate
PID/formatting integrity
degraded/quarantine rate
time/tokens/cost
```

Правила:

- integrity margin = 0;
- скорость/цена рассматриваются после quality non-inferiority;
- один привлекательный перевод не является benchmark;
- judge должен быть независим от generator;
- insufficient sample оставляет feature experimental.

## 17. Security, privacy и copyright

- Uploaded book не отправляется в web search без consent.
- Удалённым LLM отправляется только то, что разрешено выбранным data policy.
- Web content untrusted; claims не выполняют инструкции.
- Credentials хранятся provider/OpenCode/keyring, не artifact store.
- Existing translations не скачиваются целиком автоматически.
- `licensed_alignment` требует user-provided artifact/acknowledgement.
- Research сохраняет короткие evidence excerpts и ссылки, а не копии статей.
- Export manifest фиксирует source edition, language pair, models и research
  snapshot, но не раскрывает secrets.

## 18. Что сознательно не переносить из v4 как фундамент

- имена Qwen/Gemma в domain interfaces;
- English/Russian строки в общем prompt module;
- `StrictBackendConfig` с обязательными `.exe/device/GGUF` полями;
- один OpenAI-compatible transport для всех providers;
- глобальный 640-word cap как ограничение всех topology;
- model lifecycle telemetry как единственный runtime record;
- raw untyped `book_memory` как полный research schema;
- сцепление chapter input с одним HTML loader;
- предположение, что отсутствие model output означает semantic rejection.

## 19. Definition of Done v5

V5 можно считать достигшей целевой архитектуры, когда:

1. Пользователь загружает EPUB или HTML.
2. Source/target language выбираются и сохраняются как identity.
3. EN→RU работает через извлечённый language pack без regression против v4.
4. Source-grounded research создаёт evidence-backed brief.
5. Web research полностью опционален, cited и безопасен.
6. Local/OpenCode/other backends подключаются через один contract.
7. Execution plan выбирается из benchmark-approved topology.
8. Chapter-context и whole-chapter modes не ломают PID/audit/repair.
9. Результат экспортируется обратно в EPUB/HTML с formatting integrity.
10. Все model/research/config decisions воспроизводимы по hashes и provenance.
11. Ни одна provider/model substitution не происходит молча.
12. Добавление новой языковой пары не требует изменения общего pipeline.

## 20. Итоговая стратегия

V4 получает только дешёвую provider-neutral runtime границу и OpenCode backend.
Это немедленно позволяет использовать сильные удалённые модели, не смешивая
эксперимент с изменением chunk/context logic.

V5 строится последовательными вертикальными слоями:

```text
v4 runtime reuse
→ domain core
→ BookArtifact/HTML parity
→ EN-RU language pack
→ EPUB
→ source-grounded research
→ TranslationBrief
→ web enrichment
→ capability registry
→ adaptive context/topology
→ новые языки
→ product UI
```

Такой порядок сначала сохраняет и доказывает всё, что уже работает в v4, и
только затем добавляет самые новые и рискованные функции.
