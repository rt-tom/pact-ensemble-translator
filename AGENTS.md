# Pact Translator Agent Instructions

## Priorities

1. Translation quality and integrity.
2. Preserve runs, caches, and production state.
3. Fast, simple development with economical token use.

This is a home project. Prefer the smallest reliable workflow; do not add enterprise ceremony without a concrete risk.

## Repository and safety

- Production: `D:\pact\pact_translator_v4` (checked out on `main`)
- Development worktrees: `D:\pact\pact_translator_worktrees\` or `%LOCALAPPDATA%\Temp\vibe-kanban\worktrees\` (Vibe Kanban-managed)
- Stable branch: `main`
- Develop only in a separate branch/worktree. Never edit tracked production files directly.
- Do not stop a pipeline or `llama-server` without explicit authorization.
- Do not use Reset, RedoTranslation, RedoQuality, force, destructive checkout, cache deletion, or fabricated artifacts without explicit approval.
- Do not silently modify book text, glossary, book bible, or persistent memory.
- Do not change tuned Qwen/Gemma settings without explicit reason and benchmark evidence.
- Do not attach to or stop a foreign `llama-server`.
- Do not run production pipeline during development/testing.
- For an archived V3 release only, a mismatch between its production `HEAD` and `deployment_provenance.v31.json` is release drift: stop deployment work, preserve the active tree/runs, and reconcile through a reviewed release path. Never repair drift with reset, force, destructive checkout, or manual replacement of tracked files. `deployment_provenance.v31.json` records the last archived V3 deployment and is not a V4 deployment prerequisite; V4 runs use their own run/provenance artifacts in `pact_v4/`.
- V3 releases are archived (tags `archive/v3-*`, `v3.1.3*`) and no longer used. v4 development happens on `main` or short-lived branches/worktrees from `main`; a v4 run/schema change must not be back-ported into archived v3 release tags.

## Source of truth

1. Active production code.
2. Git commit/tag/branch/diff.
3. Generated run config.
4. Run artifacts and caches.
5. Tests.
6. `DECISIONS.md` — architectural decisions and their rationale.
7. Documentation.
8. Patch markers and installer messages.


Do not trust a marker or success message without checking active code.

## V4 roadmap

V4 development follows `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` (потоки
A/B/C/D: A — provider boundary, PR1 интеграционного плана; B — достройка quality
engine (Phase 3B → драйвер, Phase 4A/4A2/4B, Phase 5); C — OpenCode (PR2/PR3/PR4);
D — Phase 6/0D/7). OpenCode-интеграция не заменяет согласованный фазовый план
Phase 0–7. Задачи вне плана — только по решению владельца. strict-архитектура —
производственная архитектура v4 (решение владельца, `DECISIONS.md`).

## Token-efficient behavior

- Start with the reported artifact/traceback, implicated code, immediate consumers, and relevant tests.
- Do not scan the whole repository, print full logs/diffs/status, narrate commands, or repeat unchanged facts.
- Run targeted tests by default. Expand only if root cause is unclear, data may be lost, lifecycle/cache behavior changes, or targeted tests fail.
- Git and PRs are shared memory. Do not require the user to relay long summaries.

## Shell syntax (PowerShell)

The user's shell is PowerShell on Windows. Any command you give the user
must use PowerShell syntax, not POSIX:

- Prefer a single-line command when reasonable.
- For line continuation use a backtick at the end of the line with NO
  trailing whitespace after it (a trailing space silently breaks it). Do
  not use POSIX `^` or `\` continuations.
- Quote paths with double quotes, not single quotes.

## Short commands

### `Проверь`

Read-only inspection. Do not edit files, create a PR, or stop processes unless separately asked.

### `Вот ошибка` / `Ошибка`

Investigate production read-only; determine root cause and resume safety; then create a minimal separate-worktree fix with regression test and draft PR. Do not merge, deploy, or start pipeline.

### `Сделай` / `Реализуй`

Implement in a separate worktree and create a draft PR. Use the named target branch. If omitted: production incident -> `main`; release-development task -> current development branch.

### `Forward-port`

Create/update a separate PR adapting an approved production fix to the development branch. Do not deploy or merge unless also approved.

### `Утвержден` / `PR #N утвержден`

Apply the target-aware approved-PR workflow below. If multiple plausible PRs are open, ask one short question listing numbers.

### `Deploy отложить`

Merge may proceed, but production deployment is forbidden until explicitly requested later.

### `Запускай` / `Возобновляй`

Start the normal production pipeline only after successful deployment checks.

## Incident workflow

1. Determine root cause, run/cache preservation, recurrence risk, plain-resume safety, and whether a fix already exists elsewhere.
2. Trace downstream consumers only when changing lifecycle fields: decision, confidence, repair scope, issue identity, cache identity, gate or terminal status.
3. Ask one precise question only if multiple safe policies exist, model tuning changes, cache invalidation is needed, a running process must stop, or data may be damaged.
4. Otherwise make the smallest complete change in a separate worktree, add a regression test, run targeted validation, commit, push, and open a draft PR.

Never refactor unrelated code in an incident fix.

When a change reverses a prior decision, abandons a branch, or resolves a non-obvious tradeoff, append a dated entry to `DECISIONS.md` in the same commit. A one-line "what and why" is sufficient. Default `git revert` messages are not acceptable as the sole record.

## Open PRs and parallel agents

- Independent fixes use independent branches from the current target branch.
- Do not base a fix on an unmerged PR unless it truly depends on it.
- Before merge, refresh against the target when needed and rerun relevant tests.
- Codex and Claude Code may work in parallel only in distinct branches/worktrees.
- For handoff, push a checkpoint commit; the next agent fetches and continues with a new commit.

## Kanban-практика (Vibe Kanban, rule 2026-08-04)

Урок B9 (25+ карточек вместо 6): раздувание доски — следствие параллельных
сессий на одну задачу и по-фиксовых карточек, а не гранулярности как таковой.
Правила для всех агентов:

- **Гранулярность — средняя**: одна implementation-карточка на верифицируемую
  единицу (модуль / интеграция / docs) + одна review-карточка на неё. Не
  дробить на микро-задачи, не сливать крупный объём в одну карточку (ревью
  диффа в тысячи строк пропускает дефекты).
- **Цикл «2 карточки», полностью автоматический (правило владельца 2026-08-05)**:
  на задачу создаётся РОВНО 2 карточки — I (developer) и RV (reviewer); никаких
  fix-карточек (F1/F2/RV2) и никаких ручных дёрганий архитектором. Воркеры
  дёргают друг друга kanban-командами:
  1. Developer завершил (коммит + **Draft PR создан**) → `hermes kanban block
     <I> ready_for_review --kind dependency` (I — пауза) → **developer сам
     создаёт RV** (`hermes kanban create <RV> --assignee reviewer ...` готовой
     в ready) → диспетчер клеймит → ревью стартует.
  2. Ревьюер: **approve** → коммент в PR + `complete <RV>` + `complete <I>`
     (обе закрываются); **changes requested** → замечания комментарием в PR +
     `unblock <I>` (I снова ready) + `block <RV> waiting_for_fix --kind
     dependency` (RV — пауза, не переклеймится).
  3. Developer (после фикса): коммит + push → `block <I> ready_for_review
     --kind dependency` + **`unblock <RV>`** («проверь меня») → ревью снова.
  4. Цикл повторяется, пока approve.
  Архитектор: только создаёт I и вмешивается при тупиках/застреваниях. RV
  создаётся БЕЗ --parent (claim-гейт `claim_rejected parents_not_done`,
  урок B14); RV не создаётся заранее (dependency_wait-цикл = переклеймы и
  расход токенов, урок 2026-08-06: 3 RV висели в ожидании).
  **ВАЖНО (урок 2026-08-06, D1)**: диспетчер НЕ переклеймит завершённую
  developer-задачу с активным PR (`respawn_guarded active_pr` — защита от
  повторного запуска; снятие guard'а через close PR / block-unblock /
  рестарт gateway НЕ работает). Поэтому фикс-цикл после changes-requested —
  **НЕ реклайм I, а новая I-fix карточка** (замечания — в комментариях/PR;
  ветка та же). Это допускает до +1 карточки на цикл фикса.
- **Draft PR обязателен + общение через PR-комментарии (правило владельца
  2026-08-05)**: developer после коммита ОБЯЗАН создать Draft PR
  (`gh pr create --draft`) для любой код-задачи (замечание B13: PR не
  создавался). Вся история ревью — комментарии в PR (inline + общие), а не
  только в задачах; вердикт ревьюера — в PR + кратко в комментарий задачи.
  Задача считается готовой к мержу, когда PR одобрен (approve-комментарий) и
  обе карточки complete.
- **Не блокировать фикс на ревью при созданной review-карточке**: если
  review-карточка уже создана с parent на фикс-карточку, фикс после готовности
  работы закрывают (complete) — иначе тупик: фикс ждёт ревью, ревью ждёт фикс
  (случаи I3b→RV6-docs, F10→RV10). Незакоммиченный дифф ревьюер проверяет до
  коммита, если такова инструкция задачи.
- **Одна задача = один активный владелец**: не запускать параллельные сессии
  на одну и ту же задачу (дубли карточек и ветки-близнецы — дороже любой
  гранулярности). Дубликаты карточек отменять сразу (complete с пометкой).
- **Новая сессия архитектора = статус-чек**: первым действием выполнить
  `hermes kanban list` + `hermes kanban diagnostics`; новую карточку создавать
  только если аналогичной нет на доске; решения владельца фиксировать в
  карточке и/или DECISIONS.md, а не только в чате сессии (worker'ы не видят
  историю чата другой сессии).
- **Developer до отправки на ревью**: контракт-тесты, включая повреждённые
  входные данные (corrupt/empty/ambiguous chunk_plan и т.п.), полный suite,
  статический doc-vs-code чек. Часть HIGH-замечаний отсекается самопроверкой.
- **Тесты — минимально необходимые** (правило владельца 2026-08-05): для
  правки/фикса — один регрессионный тест на изменение + 1–2 граничных случая
  контракта; не раздувать (пример-антипаттерн: 404 строки тестов на 124 строки
  кода). Полный suite обязателен, но сами тесты — компактные.
- **Дифф-фёрст чтение (экономия токенов)**: для изменений кода использовать
  `git diff`, а не чтение полных файлов; полные файлы/документы читать только
  точечно (read_file с offset/limit), не целиком; в отчётах не дублировать
  прочитанные тексты. Input-бюджет задач: у нас ~100–270k токенов на задачу,
  основная часть — чтение полного контекста, а не диффов.

## Risk classification

Use `LOW RISK` only for a narrow, unambiguous implementation defect that does not alter translation semantics, issue merging, verification, repair lifecycle, gates, terminal policy, cache identity/invalidation, persistent memory, or model/prompt policy. A regression test is required.

Use `REVIEW REQUIRED` for changes to those areas, data recomputation, multiple defensible policies, uncertain downstream effects, or large scope.

For `REVIEW REQUIRED`, PR body needs: cause, policy, affected lifecycle, changed files, cache/resume impact, tests, and one review question. Do not create a separate review packet unless requested.

## Approved PR workflow

### PR targeting a development branch

Refresh if needed, run relevant tests, and merge. Do not tag, deploy, or run pipeline.

### Documentation-only edits

Documentation-only edits (e.g. `AGENTS.md`, `DECISIONS.md` notes) do not get
their own branch or PR. Commit them directly on the current working branch;
if a docs change accompanies code, it rides with the code's PR. A dedicated
PR is reserved for a docs change that is itself the reviewed deliverable
(e.g. a plan/architecture update the owner explicitly requested).

### Production-code PR targeting `main`

Confirm reviewed diff has not materially changed, tests pass, and whether pipeline is active. Merge may proceed while pipeline runs, but deployment must be deferred. If deployable and safe, tag/deploy only when pipeline is stopped and deployment is not deferred.

## Lightweight guarded deployment

Before a V4 deployment: verify exact tag target, expected production HEAD, clean tracked tree, stopped pipeline, changed files, and create a backup. When deploying or resuming a V4 run, validate that run's own source/snapshot/chunk-plan/config/backend provenance; do not require or copy `deployment_provenance.v31.json`.

For an archived V3 release only, also verify that `deployment_provenance.v31.json` exists, its tag resolves to its recorded commit, and that commit equals the active V3 production `HEAD`; otherwise classify the state as release drift and stop.

Hash caches only if the change affects cache/resume/repair/terminal artifacts, the user asks, or the cache is known fragile.

Fast-forward production to the exact reviewed tag. Never use reset, force, ZIP overlays, or manual copying of tracked files.

After deployment: confirm production HEAD/clean tree, run relevant offline tests, confirm active version values, and check affected run/cache data when applicable. Do not start pipeline automatically.

## Pipeline start

Before `Запускай` / `Возобновляй`: confirm production tag/HEAD, clean tracked tree, successful last deployment, and no destructive flags. Use normal resume.

After start, report only command, version, current stage, reuse/clean-start, and first unfinished item when relevant.

## Compact reports

### Incident/draft PR

```text
PR:
Risk:
Cause:
Fix:
Files:
Tests:
Caches:
Production:
Resume:
Decision needed:
```

### Approved merge/deployment

```text
PR:
Target:
Merge:
Tag:
Production:
Tests:
Caches:
Deployment:
Resume:
```

Omit irrelevant fields. Do not output full logs or repeated Git state unless requested.

## Permanent pipeline rules

- Partial per-item cache is not a completed stage.
- Skip a model only when a completed authoritative aggregate permits reuse.
- Never accept truncated JSON.
- Green JSON status is not proof of translation quality.
- Diagnostic monitor metrics never determine authoritative status/resume.
- Model-free stages must not make hidden model HTTP calls.
- Foreign servers must not be reused or stopped.
- Merge, deployment, and pipeline execution are separate actions.

## Data restrictions

Do not commit pipeline runs, source/translated chapters, models, logs, secrets, or backups. Existing absolute paths in the historical private baseline may remain, but do not introduce new machine-specific paths when local config/templates are practical.
