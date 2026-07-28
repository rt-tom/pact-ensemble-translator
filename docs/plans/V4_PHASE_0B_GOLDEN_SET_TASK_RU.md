# V4 Phase 0B — Golden set (глава 44) — tooling task

Backing spec:
`docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md` (§ Phase 0B),
`docs/architecture/V4_MVP_SPEC_RU.md` (§8 Phase 0 measurement).

## Роли

- **Codex (эта задача):** read-only tooling, schema, тесты, CLI для быстрой
  кураторской работы. Никаких моделей, никакого production pipeline, никаких
  правок v3.
- **Human + Claude (не эта задача):** финальная разметка 50–100 PID и rubric.

## Границы

- Читаем только:
  - EN original: `0044_subordination-6-1.html` (файл лежит вне репозитория).
  - RU human reference: `pact_ru.epub` (внутренний entry
    `EPUB/chapter_044.xhtml`) или уже извлечённый xhtml.
- **Не** коммитим в git:
  - полный текст главы 44 (EN);
  - human RU перевод (полный или фрагменты, кроме синтетических fixtures в
    тестах);
  - draft-outputs, runs, logs, caches, pipeline artifacts;
  - собранные golden-record JSON с реальным контентом.
- Human RU используется как **справка** (что примерно ожидается), не как
  exact-match эталон. Cascade selection в v4 требует semantic equivalence,
  а не буквальное совпадение с переводом переводчика-человека.

## Артефакты

Коммитим:

- `docs/schemas/v4_golden_record.schema.json` — контракт `pact-v4-golden-record/v1`.
- `docs/plans/V4_PHASE_0B_GOLDEN_SET_TASK_RU.md` — этот файл.
- `pact_v4/phase0b/` — Python-модули (см. `pact_v4/phase0b/README.md`).
- `tests/pact_v4/phase0b/` — тесты на синтетических fixtures.
- `.gitignore` update: `/golden_sets/`, `/pact_v4/**/_out/`.

Не коммитим:

- `golden_sets/chapter_044/…` — вывод локального прогона по реальным файлам.

## Функциональные требования к tooling

1. `extract` — распарсить EN HTML и RU xhtml/epub-entry в стабильные списки
   блоков. PID для EN: `p00001`, `p00002`, … (совместимо с v3 leaf-block
   схемой). RU-сегменты — просто индексированы, без назначения PID.
2. `align` — построить структурный alignment (порядок блоков). При равной
   длине списков — 1:1 с `structural_order`, confidence≥0.8. При неравной —
   `heuristic_length` пропорциональной проекцией, confidence≈0.35, авто-верdict
   `needs_review`.
3. `risk` — детерминированный source-only risk pre-screen (numbers, negation,
   modality, names, quotation, temporal, measurement, formatting, dialogue,
   long_span). Без вызовов моделей.
4. `build` — собрать golden-record JSON per `pact-v4-golden-record/v1`.
   Auto-verdict: `needs_review`, если risk=high или confidence<0.8, иначе
   `unreviewed`. Ничего не помечать `accepted` автоматически.
5. `validate` — проверка выходного файла против схемы (минимальный in-repo
   validator, без новых внешних зависимостей).
6. `curate` — интерактивная CLI-петля: показывает следующий unreviewed/
   needs_review record (source EN, reference RU, invariants, risk), принимает
   verdict (`a`ccept / `r`eject / `n`eeds_review / `s`kip / `q`uit), пишет
   `reviewer`, `reviewed_at`, `notes`. Файл переписывается атомарно.
7. `report` — сводка по verdicts, risk-bands, alignment-confidence.

## Acceptance

- Схема + tooling + tests лежат в дереве и проходят `python -m pytest tests/pact_v4`.
- Никаких новых обязательных зависимостей (используем `beautifulsoup4` уже
  из v3, JSON schema — свой минимальный validator).
- Из документации ясно, как локально сгенерировать 50–100 записей и
  прокурировать их через `curate`, не коммитя их в git.
- Никакого запуска моделей и production pipeline при работе tooling.

## Как использовать локально (не в git)

```powershell
py -m pact_v4.phase0b.cli extract `
   --source-html D:\pact\pact_translator_v3\pact_chapters\0044_subordination-6-1.html `
   --reference D:\path\pact_ru.epub `
   --reference-entry EPUB/chapter_044.xhtml `
   --chapter 044 `
   --out-dir .\golden_sets\chapter_044

py -m pact_v4.phase0b.cli build --chapter 044 --in-dir .\golden_sets\chapter_044
py -m pact_v4.phase0b.cli validate --records .\golden_sets\chapter_044\records.json
py -m pact_v4.phase0b.cli curate  --records .\golden_sets\chapter_044\records.json --reviewer rt
py -m pact_v4.phase0b.cli report  --records .\golden_sets\chapter_044\records.json
```

Каталог `golden_sets/` уже добавлен в `.gitignore` — файлы главы, alignment
draft и собранные records в репозиторий не попадут.
