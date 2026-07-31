# Pact v4.0 — финальное ревью и план реализации (редакция 2)

Основание: `D:\pact\pact_translator_v3\docs\architecture\V4_MVP_SPEC_RU.md`.

## Решение

**V4 MVP принять и вести отдельной веткой `v4.0`.** Это не расширение v3:
меняются единица генерации, порядок принятия решений и стоимость контроля.
v3 остаётся baseline и получает только critical fixes.

Основа правильна: scene/chunk generation при PID-контроле, deterministic risk
до моделей, каскад Qwen → deterministic → Gemma, один full chapter audit,
targeted convergence и promotion памяти только после `complete`.

## Исполнимые контракты до реализации

1. **Candidate.** A/B/C и repair имеют `chunk_id`, `candidate_id`, ordered
   PID-map, source/snapshot/config identity, validation и decision trace.
2. **Risk.** Признаки, веса, пороги и версия policy детерминированы; значения
   порогов меняются только benchmark-config.
3. **A/B diversity.** A=fidelity-first, B=balanced-literary. Это versioned
   role prompts с одинаковыми source invariants/PID contract. Seed/temperature
   — experiment parameters, не смысловое различие кандидатов.
4. **C trigger.** String diff лишь отмечает различие; semantic disagreement
   подтверждает Qwen candidate evaluation. Только затем разрешены C/synthesis.
5. **Selection/repair.** Qwen fidelity и deterministic — pass/fail; Gemma
   выбирает русский только среди прошедших. Repair проходит релевантные gates.
6. **Findings.** Immutable stable IDs с detector/category/evidence/region.
   Разные findings нельзя объединять по span overlap.
7. **Chunk plan.** Deterministic boundaries, hard cap, ownership, contexts и
   single-PID exception; результат — authoritative artifact.
8. **Memory.** Promotion thresholds, provenance, conflicts, atomic write/lock
   и rollback. Quarantined observations non-authoritative.
9. **Terminal state.** Write-once transitions
   `complete/accepted_degraded/failed`; `quarantined` — internal repair state,
   output debt segregation и explicit resume policy.
10. **Formatting.** Source span contract создаётся при preparation и проходит
    generation/repair; Phase 5 реализует alignment, не позднюю фиксацию spans.
11. **Provenance.** Минимум: `source_hash`, `chapter_snapshot_hash`,
    `chunk_plan_hash`, `policy_versions`, `prompt_bundle_hash`, model/config и
    code/artifact version. Prompt templates — versioned design artifacts.

## Сквозные правила

- Codex реализует; Claude делает независимый adversarial review по acceptance
  criteria. Один этап — один тематический draft PR.
- Все разработческие тесты offline. Production pipeline не запускается до
  Phase 7 и отдельного решения.
- Golden set и adversarial fixtures (formatting, false agreement, terminal
  states) с Phase 2 являются сквозным regression corpus для каждого этапа.
- Benchmark results и выбранные runtime parameters — versioned records/config,
  не переписка и не prompt.

## Phase 0 — measurement (критический путь)

### 0A. Harness — Codex

Read-only import finished v3 runs, normalised measurement records, output
comparison, JSON/CSV export. Повторный запуск идентичен; неизвестное остаётся
`unknown`, исходные artifacts не меняются.

### 0B. Golden set — человек + Claude; Codex даёт schema/tooling

50–100 PID: EN, permitted invariants, known violations, risk type,
formatting expectation, human verdict; без единственного «правильного» RU.
Независимый reviewer применяет rubric без чата.

**Это long pole:** начать сейчас. Phases 1–2 идут параллельно, но Phase-2
benchmark gate заблокирован до 0B и 0C.

### 0C. Baseline — Codex, review Claude

Зафиксировать v3 baseline и grid `8–12 / 12–20 × right-context on/off`.
Метрики: semantic recall/FP, bad-repair, residual, Russian rubric, LTCR,
deterministic integrity, time/tokens/reloads.

## Phase 1 — foundation

### 1A. Contracts/state — Codex; review Claude

Schemas/dataclasses/validators для snapshot, chunk plan, candidates, findings,
repair, terminal state/provenance. Reject partial JSON, duplicate PID, foreign
identity and non-monotonic terminal transition. No v3 artifact compatibility.

### 1B. JSON memory shadow mode — Codex; review Claude

`glossary.json`, `book_memory.json`, frozen `chapter_memory.json`, observations.
Atomic snapshot/promotion/conflict/rollback. Only complete promotes;
accepted_degraded, quarantined and failed do not alter authoritative memory.

### 1C. Structure-aware chunk planner — Codex; review Claude

8–20 PID with paragraph/dialogue boundaries, hard cap, owned PID and read-only
left RU/right EN contexts. All PIDs exactly once; source spans fixed here.

## Phase 2 — risk, generation, selection

### 2A. Deterministic risk pre-screen — Codex; review Claude

Versioned feature extractor and explainable bands. No model calls, no generator
confidence. Regression includes known low/high-risk cases.

### 2B. Batch A/B generation — Codex; review Claude

Gemma scene/chunk ordered PID-map, frozen book memory + source-side discourse
plan and ownership validation; primary generation never gets unverified RU
draft as left context.
Low=1; med/high=A/B via versioned fidelity-first/balanced-literary prompts.
Cache includes prompt bundle hash; no reasoning or random C.

### 2C. Admission + cascaded Russian selection — Codex; review Claude

Qwen fidelity → deterministic consistency → Gemma Russian-only preference and
neighbour coherence among passed candidates. Phase 2 is candidate admission,
not a duplicate final audit. C/synthesis only after documented semantic
disagreement/no passing candidate; bounded automatic fallback yields explicit
`accepted_degraded` only with a complete structurally-valid PID-map and trace.

**Gate:** run v3/v4 A/B and chunk benchmark using 0A/0B/0C. Only result record
freezes chunk range, right context, temperature/seed and risk thresholds.

## Phase 3 — assembled-chapter audit

### 3A. Immutable finding store — Codex; review Claude

Separate detector findings, region resolver, no overlap merge. Evidence and
multiple findings per region remain intact.

### 3B. One windowed assembled-chapter audit — Codex; review Claude

Qwen EN↔RU, Gemma RU-only, deterministic integrity/formatting/HTML. Each
finding belongs to a full central chunk; model prompts carry only bounded
read-only neighbour excerpts by default, escalating to three full chunks only
for predeclared discourse risk. Full PID coverage, resumable partial units,
audit cannot claim complete on model failure.

## Phase 4 — repair, convergence, terminal state

### 4A. Minimal repair — Codex; review Claude

Exact finding-linked region/PID repair; full sentence only with documented
reason. Challenge needs evidence and never auto-accepts. Relevant gates pass.

### 4A2. Gemma finding closure — Codex; review Claude

If repair resolves a finding originally raised by Gemma Russian review in
Step 6, Gemma re-check of that region is mandatory (not just risk-triggered)
— deterministic checks alone cannot prove a Russian-only finding is closed.

### 4B. Targeted convergence — Codex; review Claude

One required repair-round re-audits changed PID plus discourse neighbours; a
second is allowed only for a remaining blocking finding or changed boundary.
Then final integrity check (deterministic default, conditional narrow Qwen
smoke only when post-convergence text changes fall outside Step 7's re-audited
scope — see `V4_MVP_SPEC_RU.md` §2 Step 8) and monotonic terminal transition.
A complete valid PID map may be `accepted_degraded` with debt trace and no
memory promotion; only absent valid PID map is `failed`.

## Phase 5 — formatting alignment

> Порядок фаз здесь — порядок РАЗРАБОТКИ (Phase 4 реализуется и тестируется
> до Phase 5), не порядок RUNTIME-шагов. В runtime pipeline formatting
> contract применяется ДО Step 8 (см. `V4_MVP_SPEC_RU.md` §2 Step 6). Phase 4
> нельзя реализовывать и принимать как самостоятельный финальный pipeline
> без formatting contract — Step 8/terminal transition runtime-корректны
> только вместе с Phase 5.

**Codex; review Claude.** Exact → occurrence-aware → conservative fuzzy → model
fallback, all with provenance. Any unrecovered required span enters automatic
repair/fallback; it is `accepted_degraded` only if the chosen output profile
remains structurally valid, otherwise failed. No marker leakage; duplicate
occurrence/HTML/PID/number fixtures pass.

## Phase 6 — operations after quality MVP

**Codex; review Claude.** Role batching, fewer reloads, truthful read-only
monitor and timing/cost record. Reasoning/third model stay feature-flagged
experiments. Optimisation must preserve contracts and benchmark result.

## Phase 7 — A/B release decision

Codex supplies tooling; Claude and human independently review. Same source and
frozen snapshot for v3/v4. Switch only if residual semantic errors lower,
bad-repair not higher, Russian/formatting not worse, cost and quarantine rate
accepted. Otherwise v4 remains experimental.

## PR order and compact prompts

`1A → 1B → 1C → 2A → 2B → 2C → benchmark gate → 3A → 3B → 4A → 4B → 5 → 6 → 7`.
0A/0B start immediately; 0C completes before the benchmark gate.

```text
Реализуй v4 Phase 1A из V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md.
Target: v4.0. Draft PR. Не трогай v3 и production.
```

```text
Сделай независимый review PR <N> для v4 Phase <X> по acceptance criteria
в V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md. Только review, без правок.
```
