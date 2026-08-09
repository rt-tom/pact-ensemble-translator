# Pact Translator Agent Instructions

## Priorities

1. Translation quality and integrity.
2. Preserve runs, caches, and production state.
3. Fast, simple development with economical token use.

This is a home project. Prefer the smallest reliable workflow; do not add enterprise ceremony without a concrete risk.

## Repository and safety

- Production: `D:\pact\pact_translator_v4` (checked out on `main`).
- Development worktrees: `D:\pact\pact_translator_worktrees\` or `%LOCALAPPDATA%\Temp\vibe-kanban\worktrees\` (Vibe Kanban-managed).
- Stable branch: `main`. **Develop only in a separate branch/worktree. Never edit tracked production files directly.**
- Do not stop a pipeline or `llama-server` without explicit authorization.
- Do not use Reset, RedoTranslation, RedoQuality, force, destructive checkout, cache deletion, or fabricated artifacts without explicit approval.
- Do not silently modify book text, glossary, book bible, or persistent memory.
- Do not change tuned Qwen/Gemma settings without explicit reason and benchmark evidence.
- Do not attach to or stop a foreign `llama-server`.
- Do not run production pipeline during development/testing.
- V3 releases are archived (tags `archive/v3-*`, `v3.1.3*`) and no longer used. v4 development happens on `main` or short-lived branches/worktrees from `main`; a v4 run/schema change must not be back-ported into archived v3 release tags.
- For an archived V3 release only: a mismatch between its production `HEAD` and `deployment_provenance.v31.json` is release drift — stop deployment work, preserve the active tree/runs, reconcile through a reviewed release path. Never repair drift with reset, force, destructive checkout, or manual replacement of tracked files. `deployment_provenance.v31.json` is not a V4 deployment prerequisite; V4 runs use their own artifacts in `pact_v4/`.

## Source of truth

1. Active production code.
2. Git commit/tag/branch/diff.
3. Generated run config.
4. Run artifacts and caches.
5. Tests.
6. `DECISIONS.md` — architectural decisions and rationale.
7. Documentation.
8. Patch markers and installer messages.

Do not trust a marker or success message without checking active code.

## V4 roadmap

Follow `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` (потоки A/B/C/D).
Задачи вне плана — только по решению владельца. strict-архитектура —
производственная архитектура v4 (`DECISIONS.md`).

## Token-efficient behavior

- Start with the reported artifact/traceback, implicated code, immediate consumers, and relevant tests.
- Do not scan the whole repository, print full logs/diffs/status, narrate commands, or repeat unchanged facts.
- Run targeted tests by default. Expand only if root cause is unclear, data may be lost, lifecycle/cache behavior changes, or targeted tests fail.
- Git and PRs are shared memory. Do not require the user to relay long summaries.
- Use `git diff` instead of reading full files; full files/docs only pointwise (read_file with offset/limit).

## Shell syntax (PowerShell)

The user's shell is PowerShell on Windows. Any command you give the user must use PowerShell syntax, not POSIX:

- Prefer a single-line command when reasonable.
- Line continuation: backtick at end of line with NO trailing whitespace. Do not use POSIX `^` or `\` continuations.
- Quote paths with double quotes, not single quotes.

## Short commands

- **`Проверь`** — read-only inspection. No edits, PRs, or process stops unless separately asked.
- **`Вот ошибка` / `Ошибка`** — investigate production read-only; root cause + resume safety; then minimal separate-worktree fix with regression test and draft PR. Do not merge, deploy, or start pipeline.
- **`Сделай` / `Реализуй`** — implement in a separate worktree and create a draft PR. Target branch as named; if omitted: production incident → `main`; release-development task → current development branch.
- **`Forward-port`** — separate PR adapting an approved production fix to the development branch. No deploy/merge unless approved.
- **`Утвержден` / `PR #N утвержден`** — target-aware approved-PR workflow below. If multiple plausible PRs open, ask one short question listing numbers.
- **`Deploy отложить`** — merge may proceed, production deployment forbidden until explicitly requested.
- **`Запускай` / `Возобновляй`** — start normal pipeline only after successful deployment checks. **Прогоны запускаются ТОЛЬКО владельцем вручную, вне чата (в своём терминале). Из чата (ни агентом, ни владельцем в этой сессии) команды запуска прогона не выдаются и не исполняются — правило владельца 2026-08-06. Агент не стартует пайплайн/llama-server ни по какой команде; максимум — подготовить команду запуска и ждать, пока владелец запустит сам.**

## Incident workflow

1. Determine root cause, run/cache preservation, recurrence risk, plain-resume safety, and whether a fix already exists elsewhere.
2. Trace downstream consumers only when changing lifecycle fields: decision, confidence, repair scope, issue identity, cache identity, gate or terminal status.
3. Ask one precise question only if multiple safe policies exist, model tuning changes, cache invalidation is needed, a running process must stop, or data may be damaged.
4. Otherwise make the smallest complete change in a separate worktree, add a regression test, run targeted validation, commit, push, and open a draft PR.

Never refactor unrelated code in an incident fix.

When a change reverses a prior decision, abandons a branch, or resolves a non-obvious tradeoff, append a dated entry to `DECISIONS.md` in the same commit (one-line "what and why" suffices). Default `git revert` messages are not acceptable as the sole record.

## Open PRs and parallel agents

- Independent fixes use independent branches from the current target branch.
- Do not base a fix on an unmerged PR unless it truly depends on it.
- Before merge, refresh against the target when needed and rerun relevant tests.
- Codex and Claude Code may work in parallel only in distinct branches/worktrees.
- For handoff, push a checkpoint commit; the next agent fetches and continues with a new commit.

## Kanban-практика (правило владельца 2026-08-04, ред. 2026-08-06/07)

Цикл «2 карточки»: на задачу РОВНО 2 карточки — I (developer) и RV (reviewer); никаких fix-карточек. Draft PR создаётся ПОСЛЕ approve, не после коммита (пока PR нет, guard `active_pr` не висит и developer возвращается на свою I сколько угодно раз).

Flow:
1. Developer работает → коммит(ы) в ветку (БЕЗ PR) → `block <I> ready_for_review` → developer сам создаёт RV (ready, БЕЗ `--parent`) → диспетчер клеймит.
2. Ревьюер ревьюит ВЕТКУ (`git diff origin/main..branch`), вердикт — комментариями в задачи:
   - **approve** → developer/архитектор создаёт Draft PR (`gh pr create --draft`) и заливает в PR ключевые комментарии ревью (`gh pr comment`) → сверка PR == одобренный дифф → `complete RV` + `complete I` → мерж;
   - **changes requested** → замечания комментариями → developer переклеймивает ту же I (guard нет) → правит → снова `block ready_for_review` → та же RV ревьюит.
3. Цикл до approve.

Правила:
- **Ревьюеру ЗАПРЕЩЕНО создавать любые карточки** (fix/RV2/etc.) — вердикт и замечания только комментариями; новые карточки плодит только архитектор (и то лишь при тупиках). Developer тоже не создаёт fix-карточек.
- Разъезд (fix-карточка/RV2 появились) = PROTOCOL-DRIFT: лишние карточки не плодятся — замечания переносятся на I, developer возвращается на ту же I. Воркер, заклеймлённый ДО обновления протокола, работает по старому плейбуку — не достраивать цепочку.
- RV создаётся БЕЗ `--parent` (claim-гейт `claim_rejected parents_not_done`) и не заранее (dependency_wait-цикл = переклеймы и расход токенов).
- **НЕ блокировать I `ready_for_review` повторно (урок 2026-08-07): системный авто-декомпозер.** Ядро имеет `BLOCK_RECURRENCE_LIMIT=2`: повторный block на той же I (штатный фикс-цикл) триггерит `block_loop_detected` → авто-декомпозер разваливает I на 6–11 детей и ломает цикл. Правило: **developer после changes-requested НЕ блокирует I повторно** — комментирует «фикс готов @ <sha>» и ждёт; архитектор паркует I с `--kind capability` (не `needs_input` — тот же kind наращивает счётчик; не `dependency` — автопромоут в ready = токен-спин) и создаёт RV2 сам. Перед парковкой проверить `show <I>` на `decomposed`/`block_loop_detected`.
- После CHANGES REQUESTED I остаётся blocked — архитектор портирует findings на I комментарием и `unblock` (иначе фикс-цикл замирает: диспетчер не клеймит blocked).
- Одна задача = один активный владелец: дубли карточек и ветки-близнецы дороже любой гранулярности; дубликаты отменять сразу (complete с пометкой).
- Новая сессия архитектора = статус-чек: первым делом `hermes kanban list` + `diagnostics`; новую карточку создавать только если аналогичной нет; решения владельца фиксировать в карточке и/или DECISIONS.md, не только в чате.
- Developer до отправки на ревью: контракт-тесты (включая corrupt/empty/ambiguous входные), полный suite, статический doc-vs-code чек.
- Тесты — минимально необходимые: один регрессионный тест на изменение + 1–2 граничных случая; не раздувать. Полный suite обязателен, сами тесты компактные.
- **Аналитические задачи (оценка/отчёт/исследование) — сходимость ревью (2026-08-07)**: ревьюер проверяет заявленный scope и корректность цифр/выводов; НЕ расширяет требования новыми пунктами в каждом раунде. Старая база ветки (дифф «удаляет» чужие коммиты) — не блокер содержания, а пункт «перебазировать перед мержем». Если раунд не приближает к approve — архитектор вмешивается: собирает замечания в один список, одна итерация фикса.

## Risk classification

- `LOW RISK` — only a narrow, unambiguous implementation defect that does not alter translation semantics, issue merging, verification, repair lifecycle, gates, terminal policy, cache identity/invalidation, persistent memory, or model/prompt policy. Regression test required.
- `REVIEW REQUIRED` — changes to those areas, data recomputation, multiple defensible policies, uncertain downstream effects, or large scope.

For `REVIEW REQUIRED`, PR body needs: cause, policy, affected lifecycle, changed files, cache/resume impact, tests, and one review question. No separate review packet unless requested.

## Approved PR workflow

- **PR targeting a development branch**: refresh if needed, run relevant tests, merge. No tag/deploy/pipeline.
- **Documentation-only edits** (AGENTS.md, DECISIONS.md, plan docs): no separate branch/PR — commit directly on the current working branch; if docs accompany code, they ride the code's PR. A dedicated PR is reserved for a docs change that is itself the reviewed deliverable (owner-requested plan/architecture update).
- **Production-code PR targeting `main`**: confirm reviewed diff has not materially changed, tests pass, pipeline inactive. Merge may proceed while pipeline runs, but deployment must be deferred. Deploy/tag only when pipeline is stopped and deployment not deferred.

## Lightweight guarded deployment

- Before a V4 deployment: verify exact tag target, expected production HEAD, clean tracked tree, stopped pipeline, changed files, and create a backup. Validate the run's own source/snapshot/chunk-plan/config/backend provenance; do not require `deployment_provenance.v31.json` for V4.
- For archived V3 only: verify `deployment_provenance.v31.json` exists, its tag resolves to its recorded commit, and that commit equals active V3 production `HEAD`; otherwise release drift — stop.
- Hash caches only if the change affects cache/resume/repair/terminal artifacts, the user asks, or the cache is known fragile.
- Fast-forward production to the exact reviewed tag. Never reset, force, ZIP overlays, or manual copying of tracked files.
- After deployment: confirm production HEAD/clean tree, run relevant offline tests, confirm active version values, check affected run/cache data. Do not start pipeline automatically.

## Pipeline start

Before `Запускай`/`Возобновляй`: confirm production tag/HEAD, clean tracked tree, successful last deployment, no destructive flags. Use normal resume. After start, report only command, version, current stage, reuse/clean-start, and first unfinished item when relevant.

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

## Справочник деталей

Исторические уроки, примеры и справочные команды — `docs/agent_operations/AGENTS_REFERENCE_RU.md`.
