#!/usr/bin/env python3
"""Structural cross-check: Phase 0 report tables vs the committed evidence JSON.

The old verifier only checked that every digit token in the report appears
somewhere in the evidence (a swapped-in number from another profile passed).
This verifier instead:

1. Parses every markdown table in the report (fingerprints, per-profile
   aggregates, derived ratios, by-source split, model/provider usage,
   reasoning effort, finish_reason, top-5 sessions).
2. Regenerates the expected cell values FROM THE EVIDENCE JSON (same
   arithmetic the reporter uses: sums, R-7 p50/p90, derived percentages and
   ratios, fingerprint prefixes).
3. Fails on ANY cell mismatch — including a value that exists in the
   evidence but belongs to another profile — and on ANY missing,
   duplicate or extra row in the mandatory tables (completeness).
4. Verifies the AGENTS.md size/char-count/hash claims against the COMMITTED
   HEAD blob (``git show HEAD:AGENTS.md`` — never an arbitrary dirty
   working-tree file). AGENTS.md is a mandatory input: if it is absent from
   HEAD the check fails; it is never silently skipped. Freshness: the
   report's cited HEAD commit must carry the SAME AGENTS.md blob as the
   committed HEAD (an artifact regenerated against an older commit with a
   different AGENTS.md fails).
5. Runs a redaction regression check over the committed report / evidence /
   context artifacts: no kanban task ids, no ``wt/`` worktree identifiers,
   no absolute (drive / UNC / POSIX) paths, no sensitive column/credential
   words may appear in the published output.

Prose is not trusted; only tables, explicit AGENTS.md facts and the
redaction allowlist are checked.

Usage:
    python tools/verify_baseline_report.py            # check committed report
    python tools/verify_baseline_report.py --self-test  # prove substitution is caught
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EVIDENCE = REPO / "docs/audits/HERMES_PROFILE_TOKEN_BASELINE_PHASE0_evidence.json"
REPORT = REPO / "docs/audits/hermes-profile-token-baseline-2026-08-07.md"
CONTEXT = REPO / "tools/context_baseline.json"
AGENTS = REPO / "AGENTS.md"

PROFILES = ("architect", "developer", "reviewer")


# ---------------------------------------------------------------------------
# Committed-HEAD resolution (AGENTS.md is a MANDATORY input).
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> bytes:
    """Run a read-only git command; raise RuntimeError when git fails."""
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        timeout=60,
    )
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {err}")
    return res.stdout


def committed_agents(repo: Path) -> tuple[str, bytes]:
    """Return ``(HEAD sha, AGENTS.md blob as committed at HEAD)``.

    Raises RuntimeError when git is unavailable, HEAD is unborn, or AGENTS.md
    is not committed at HEAD — the caller must treat that as a hard failure,
    never a silent skip.
    """
    head = _git(repo, "rev-parse", "HEAD").strip().decode("ascii")
    blob = _git(repo, "show", "HEAD:AGENTS.md")
    return head, blob


# ---------------------------------------------------------------------------
# Redaction regression check (published artifacts).
# ---------------------------------------------------------------------------
# Forbidden provenance / privacy markers in the committed report, evidence
# and context-baseline output. "token"/"task" words themselves are NOT banned
# (legitimate prose and column names like input_tokens); task provenance is
# identified by kanban task ids (t_<hex>), worktree identifiers (wt/...) and
# machine-specific paths.
_REDACTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bt_[0-9a-f]{6,}\b"), "kanban task id"),
    (re.compile(r"\bwt/[A-Za-z0-9_.-]+"), "worktree branch identifier"),
    (re.compile(r"(?<!\w)[A-Za-z]:[\\/]"), "windows drive absolute path"),
    (re.compile(r"(?<![\w.:/<>-])//[^/\s\"']+/[^\s\"']+"), "UNC path (forward slash)"),
    (re.compile(r"\\\\[^\\\s\"']+\\[^\s\"']+"), "UNC path (backslash)"),
    (re.compile(r"(?<![\w.:/<>-])/(?:[A-Za-z0-9_.~-]+/)+[A-Za-z0-9_.~-]*"),
     "posix absolute path"),
    (re.compile(r"\buser_id\b"), "user_id column"),
    (re.compile(r"\bbilling_base_url\b"), "billing_base_url column"),
    (re.compile(r"\bapi_key\b"), "api_key"),
    (re.compile(r"\bpassword\b"), "password"),
    (re.compile(r"\bcredential"), "credential"),
    (re.compile(r"\bsecret\b"), "secret"),
    (re.compile(r"\bhandoff_error\b"), "handoff_error column"),
    (re.compile(r"\bcompression_failure_error\b"), "compression_failure_error column"),
    (re.compile(r"\bsystem_prompt\b"), "system_prompt column"),
    (re.compile(r"\borigin_json\b"), "origin_json column"),
    (re.compile(r"\bsession_key\b"), "session_key column"),
    (re.compile(r"\bchat_id\b"), "chat_id column"),
    (re.compile(r"\bthread_id\b"), "thread_id column"),
    (re.compile(r"\bdisplay_name\b"), "display_name column"),
    (re.compile(r"\bapi_content\b"), "api_content column"),
    (re.compile(r"\breasoning_content\b"), "reasoning_content column"),
    (re.compile(r"\breasoning_details\b"), "reasoning_details column"),
    (re.compile(r"\bcodex_reasoning_items\b"), "codex_reasoning_items column"),
    (re.compile(r"\bcodex_message_items\b"), "codex_message_items column"),
    (re.compile(r"\bgit_repo_root\b"), "git_repo_root column"),
    (re.compile(r"\bgit_branch\b"), "git_branch column"),
    (re.compile(r"\blast_activity_description\b"), "last_activity_description column"),
    (re.compile(r"\bcwd\b"), "cwd column"),
    (re.compile(r"\bbase_url\b"), "base_url column"),
]


def check_redaction(artifacts: dict[str, str]) -> list[str]:
    """Scan named artifact texts for forbidden provenance markers.

    Returns a list of problems; empty list means the artifacts are clean.
    """
    problems: list[str] = []
    for name, text in artifacts.items():
        for pat, label in _REDACTION_PATTERNS:
            for m in pat.finditer(text):
                problems.append(f"redaction[{name}]: {label}: {m.group(0)!r}")
    return problems


# ---------------------------------------------------------------------------
# Number parsing helpers (report formatting: "1 678", "40 / 276", "14.9 %",
# "~62×", "120").
# ---------------------------------------------------------------------------
def _num(cell: str):
    """Parse a report cell into a float. Returns None for non-numeric cells.

    Handles space thousands separators, '~'/'×'/'%' decorations, bold/italic
    markdown and backticks. Non-finite values ('nan', 'inf', '-inf') are
    rejected too: every numeric comparison in this module is
    ``abs(got - want) > eps``, which is silently False for NaN, so a cell
    substituted with 'nan' would otherwise pass as valid.
    """
    s = cell.strip().replace(" ", "").replace("\u00a0", "")
    s = s.replace("~", "").replace("×", "").replace("%", "").replace(",", ".")
    s = s.replace("*", "").replace("`", "")
    if s in ("", "—", "-", "(null)", "None", "null"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if not math.isfinite(v):
        return None
    return v


def _fmt_int(v) -> str:
    """'1 678' formatting used by the report for integer aggregates."""
    return f"{int(round(v)):,}".replace(",", " ")


def _fmt_pct(v) -> str:
    return f"{v:.1f} %"


def _fmt_times(v) -> str:
    return f"~{int(round(v))}×"


# ---------------------------------------------------------------------------
# Markdown table parsing.
# ---------------------------------------------------------------------------
def parse_tables(md: str):
    """Return [(profile_heading_or_None, header_cells, [row_cells, ...]), ...].

    Only lines that look like markdown table rows (start with '|') are kept;
    separator rows (all '---') are dropped. The nearest preceding '### '
    heading is attached so top-5 blocks know their profile.
    """
    tables = []
    cur_profile = None
    header = None
    for line in md.splitlines():
        line = line.rstrip()
        if line.startswith("### "):
            cur_profile = line[4:].strip()
        if not line.strip():
            header = None  # blank line ends the current table block
            continue
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue  # separator row
        if header is None:
            header = cells
            continue
        tables.append((cur_profile, header, cells))
    return tables


# ---------------------------------------------------------------------------
# Section row collection + completeness helpers.
# ---------------------------------------------------------------------------
def _rows(tables, pred):
    """All (profile_heading, header, row) whose header satisfies pred."""
    return [
        (prof, header, row)
        for prof, header, row in tables
        if row and pred(header)
    ]


def _complete_profiles(problems, section, seen, label="profile row"):
    """Require every mandatory profile; flag duplicate and extra rows."""
    for p in PROFILES:
        if p not in seen:
            problems.append(f"{section}: missing {label} {p}")
    for name, cnt in seen.items():
        if cnt > 1:
            problems.append(f"{section}: duplicate {label} {name}")
    for name in seen:
        if name not in PROFILES:
            problems.append(f"{section}: extra {label} {name!r}")


# ---------------------------------------------------------------------------
# Expected values from evidence.
# ---------------------------------------------------------------------------
def _expected_main(ev_p: dict) -> dict:
    a = ev_p["all"]
    return {
        "n_sessions": ev_p["n_sessions_total"],
        "api_calls_sum": a["api_calls"]["sum"],
        "api_calls_p50": a["api_calls"]["p50"],
        "api_calls_p90": a["api_calls"]["p90"],
        "input_sum": a["input_tokens"]["sum"],
        "input_p50": a["input_tokens"]["p50"],
        "input_p90": a["input_tokens"]["p90"],
        "output_sum": a["output_tokens"]["sum"],
        "reasoning_sum": a["reasoning_tokens"]["sum"],
        "reasoning_p50": a["reasoning_tokens"]["p50"],
        "reasoning_p90": a["reasoning_tokens"]["p90"],
        "cache_read_sum": a["cache_read_tokens"]["sum"],
        "cache_write_sum": a["cache_write_tokens"]["sum"],
    }


def _expected_derived(ev_p: dict) -> dict:
    a = ev_p["all"]
    inp = a["input_tokens"]["sum"] or 1.0
    n = max(ev_p["n_sessions_total"], 1)
    return {
        "reasoning_pct": a["reasoning_tokens"]["sum"] / inp * 100.0,
        "cache_times": a["cache_read_tokens"]["sum"] / inp,
        "output_pct": a["output_tokens"]["sum"] / inp * 100.0,
        "calls_per_session": a["api_calls"]["sum"] / n,
    }


def _provider_display(p: str, mode) -> str:
    """Reconstruct the report's provider cell from evidence fields."""
    p = p or ""
    mode = mode or ""
    if p and mode:
        return f"{p} ({mode})"
    return p or mode or "—"


def _top5_rows(ev_p: dict, metric: str):
    return ev_p[f"top5_by_{metric}"]


# ---------------------------------------------------------------------------
# Mandatory table schemas: the exact header each required table must carry.
# A table whose header differs (renamed / extra / duplicated column) is a
# structural mutation and must fail closed even when every value still
# matches the evidence. Row widths must equal the header width exactly —
# an extra or missing cell is rejected even when the checked labels all
# still compare equal.
# ---------------------------------------------------------------------------
_MANDATORY_TABLES = (
    (
        "fingerprint",
        lambda h: any("sha256" in x for x in h),
        ("Профиль", "snapshot sha256 (первые 16)", "snapshot байт",
         "config.yaml sha256 (первые 16)"),
    ),
    (
        "aggregates",
        lambda h: any("Вызовов (sum)" in x for x in h),
        ("Профиль", "Сессий", "Вызовов (sum)", "Вызовов p50/p90",
         "Input sum", "Input p50/p90", "Output sum", "Reasoning sum",
         "Reasoning p50/p90", "Cache-read sum", "Cache-write sum"),
    ),
    (
        "derived",
        lambda h: any("reasoning/input" in x for x in h),
        ("Профиль", "reasoning/input", "cache-read/input", "output/input",
         "вызовов на сессию (avg)"),
    ),
    (
        "by_source",
        lambda h: any("source" in x for x in h)
        and not any("метрика" in x for x in h),
        ("Профиль", "source", "Сессий", "Вызовы", "Input", "Output",
         "Reasoning", "Cache-read"),
    ),
    (
        "usage_by_model",
        lambda h: any("provider" in x for x in h),
        ("Профиль", "model", "provider", "Вызовов", "Input", "Output",
         "Reasoning", "Cache-read", "Сессий"),
    ),
    (
        "reasoning effort",
        lambda h: any("medium" in x for x in h),
        ("Профиль", "medium", "high"),
    ),
    (
        "finish_reason",
        lambda h: any("tool_calls" in x for x in h),
        ("Профиль", "stop", "tool_calls", "length", "(null)"),
    ),
    (
        "top-5",
        lambda h: any("метрика" in x for x in h),
        ("метрика", "id (ред.)", "source", "model", "effort", "вызовов",
         "msgs", "tools", "input", "reasoning"),
    ),
)


def _mandatory_rows(tables, name: str):
    """Rows belonging to one mandatory table, found by its schema predicate."""
    pred = next(spec[1] for spec in _MANDATORY_TABLES if spec[0] == name)
    return _rows(tables, pred)


def _check_table_structure(problems, tables) -> None:
    """Fail-closed structural validation over every parsed markdown table.

    Each data row must have EXACTLY as many cells as its header (an extra
    cell appended to a valid row previously slipped through — RV2 finding),
    and the header itself must not contain duplicated column names (a
    duplicated header previously collapsed silently in the cells dict).
    """
    for prof, header, row in tables:
        if len(row) != len(header):
            problems.append(
                f"table structure: row width {len(row)} != header width "
                f"{len(header)} (heading {prof!r}, row {row!r})"
            )
        dups = [c for c in header if header.count(c) > 1]
        if dups:
            problems.append(
                f"table structure: duplicate header cells {dups!r} (heading {prof!r})"
            )


def _check_mandatory_headers(problems, tables) -> None:
    """Every mandatory table must carry its exact documented header schema."""
    for name, _pred, expected in _MANDATORY_TABLES:
        for prof, header, _row in _rows(tables, _pred):
            if tuple(header) != expected:
                problems.append(
                    f"{name} table: header {list(header)!r} != expected "
                    f"{list(expected)!r} (heading {prof!r})"
                )


# ---------------------------------------------------------------------------
# The actual check.
# ---------------------------------------------------------------------------
def check_report(
    report_text: str,
    evidence: dict,
    agents_bytes: bytes | None = None,
    head_sha: str | None = None,
    repo: Path = REPO,
) -> list[str]:
    """Return a list of problems; empty list means the report is consistent.

    ``agents_bytes`` is the AGENTS.md blob COMMITTED at HEAD (see
    :func:`committed_agents`); when provided, the AGENTS.md facts are
    mandatory — the report must cite bytes/UTF-8 chars/sha256 and they must
    match the committed blob exactly. ``head_sha`` is ``git rev-parse HEAD``;
    when provided, the report's cited HEAD must carry the same AGENTS.md blob
    as the committed HEAD (freshness: artifacts must not describe an older
    AGENTS.md target). ``repo`` is the repository used for the freshness
    blob lookup (defaults to this repo).
    """
    problems: list[str] = []
    ev = evidence["profiles"]
    tables = parse_tables(report_text)
    # fail-closed structure first: exact row widths, unique headers, exact
    # mandatory header schemas — a mutation must never be silently ignored
    _check_table_structure(problems, tables)
    _check_mandatory_headers(problems, tables)
    if problems:
        # A malformed table structure (duplicated / renamed header cell, row
        # width mismatch) makes the section-specific checks below unsafe:
        # they assume every row matches its header width and would index past
        # the row (crashing on None, or silently misreading a partial cell).
        # Fail closed: report the structural problems and stop. RV3 finding:
        # duplicating the first header cell of the reasoning-effort,
        # finish_reason and top-5 tables used to raise an uncaught
        # AttributeError ('NoneType' object has no attribute 'strip') after
        # the structural problems were already appended.
        return problems

    # -- 1) fingerprint table (§1) -----------------------------------------
    fp_rows = _mandatory_rows(tables, "fingerprint")
    if not fp_rows:
        problems.append("fingerprint table not found")
    else:
        seen_fp: dict[str, int] = {}
        for prof, header, row in fp_rows:
            name = row[0]
            seen_fp[name] = seen_fp.get(name, 0) + 1
            if name not in PROFILES:
                continue
            fp = ev[name]["fingerprint"]
            exp = {
                "snapshot_sha16": fp["snapshot_sha256"][:16],
                "snapshot_bytes": fp["snapshot_bytes"],
                "config_sha16": fp["config_yaml_sha256"][:16],
            }
            cells = {h: row[i] for i, h in enumerate(header) if i < len(row)}
            got_sha = next((v for k, v in cells.items() if "sha256" in k and "snapshot" in k), None)
            got_bytes = next((v for k, v in cells.items() if "байт" in k or "bytes" in k.lower()), None)
            got_cfg = next((v for k, v in cells.items() if "config" in k.lower() or "config.yaml" in k), None)
            if got_sha is None or got_bytes is None or got_cfg is None:
                problems.append(f"fingerprint table row for {name}: column layout unexpected")
                continue
            if got_sha.strip().strip("`") != exp["snapshot_sha16"]:
                problems.append(
                    f"{name}: snapshot sha256 prefix {got_sha.strip()!r} != evidence {exp['snapshot_sha16']!r}"
                )
            if _num(got_bytes) != exp["snapshot_bytes"]:
                problems.append(
                    f"{name}: snapshot bytes {got_bytes.strip()!r} != evidence {exp['snapshot_bytes']}"
                )
            if got_cfg.strip().strip("`") != exp["config_sha16"]:
                problems.append(
                    f"{name}: config.yaml sha256 prefix {got_cfg.strip()!r} != evidence {exp['config_sha16']!r}"
                )
        _complete_profiles(problems, "fingerprint", seen_fp)

    # -- 2) per-profile aggregate table (§2, "Вызовов (sum)") --------------
    main_rows = _mandatory_rows(tables, "aggregates")
    if not main_rows:
        problems.append("main aggregate table not found")
    else:
        seen_main: dict[str, int] = {}
        for prof, header, row in main_rows:
            name = row[0]
            seen_main[name] = seen_main.get(name, 0) + 1
            if name not in PROFILES:
                continue
            cells = {h: row[i] for i, h in enumerate(header) if i < len(row)}
            exp = _expected_main(ev[name])
            checks = [
                ("Сессий", "n_sessions", None),
                ("Вызовов (sum)", "api_calls_sum", None),
                ("Input sum", "input_sum", None),
                ("Output sum", "output_sum", None),
                ("Reasoning sum", "reasoning_sum", None),
                ("Cache-read sum", "cache_read_sum", None),
                ("Cache-write sum", "cache_write_sum", None),
            ]
            for label, key, _ in checks:
                cell = cells.get(label)
                if cell is None:
                    problems.append(f"{name}: column {label!r} missing in aggregate table")
                    continue
                got = _num(cell)
                want = exp[key]
                if got is None or abs(got - want) > 0.05:
                    problems.append(
                        f"{name}: {label} = {cell.strip()!r}, evidence {_fmt_int(want)}"
                    )
            # p50/p90 paired cells
            for label, k50, k90 in (
                ("Вызовов p50/p90", "api_calls_p50", "api_calls_p90"),
                ("Input p50/p90", "input_p50", "input_p90"),
                ("Reasoning p50/p90", "reasoning_p50", "reasoning_p90"),
            ):
                cell = cells.get(label)
                if cell is None:
                    problems.append(f"{name}: column {label!r} missing in aggregate table")
                    continue
                parts = [p.strip() for p in cell.split("/") if p.strip()]
                if len(parts) != 2:
                    problems.append(f"{name}: {label} cell {cell!r} not 'a / b'")
                    continue
                g50, g90 = _num(parts[0]), _num(parts[1])
                if g50 is None or abs(g50 - exp[k50]) > 0.05:
                    problems.append(
                        f"{name}: {label} p50 = {parts[0]!r}, evidence {_fmt_int(exp[k50])}"
                    )
                if g90 is None or abs(g90 - exp[k90]) > 0.05:
                    problems.append(
                        f"{name}: {label} p90 = {parts[1]!r}, evidence {_fmt_int(exp[k90])}"
                    )
        _complete_profiles(problems, "aggregates", seen_main)

    # -- 3) derived ratio table (§2, "reasoning/input") ---------------------
    der_rows = _mandatory_rows(tables, "derived")
    if not der_rows:
        problems.append("derived ratio table not found")
    else:
        seen_der: dict[str, int] = {}
        for prof, header, row in der_rows:
            name = row[0]
            seen_der[name] = seen_der.get(name, 0) + 1
            if name not in PROFILES:
                continue
            cells = {h: row[i] for i, h in enumerate(header) if i < len(row)}
            exp = _expected_derived(ev[name])
            # reasoning/input %
            got = cells.get("reasoning/input")
            if got is None or _num(got) is None or abs(_num(got) - round(exp["reasoning_pct"], 1)) > 0.05:
                problems.append(f"{name}: reasoning/input = {got.strip() if got else None!r}, evidence {_fmt_pct(exp['reasoning_pct'])}")
            got = cells.get("cache-read/input")
            if got is None or _num(got) is None or abs(_num(got) - round(exp["cache_times"])) > 0.05:
                problems.append(f"{name}: cache-read/input = {got.strip() if got else None!r}, evidence {_fmt_times(exp['cache_times'])}")
            got = cells.get("output/input")
            if got is None or _num(got) is None or abs(_num(got) - round(exp["output_pct"], 1)) > 0.05:
                problems.append(f"{name}: output/input = {got.strip() if got else None!r}, evidence {_fmt_pct(exp['output_pct'])}")
            got = cells.get("вызовов на сессию (avg)")
            if got is None or _num(got) is None or abs(_num(got) - round(exp["calls_per_session"])) > 0.05:
                problems.append(f"{name}: avg calls/session = {got.strip() if got else None!r}, evidence {int(round(exp['calls_per_session']))}")
        _complete_profiles(problems, "derived", seen_der)

    # -- 4) by_source table (§3) -------------------------------------------
    src_rows = _mandatory_rows(tables, "by_source")
    if not src_rows:
        problems.append("by_source table not found")
    else:
        seen_src: dict[tuple, int] = {}
        for prof, header, row in src_rows:
            name = row[0]
            src = row[1] if len(row) > 1 else None
            seen_src[(name, src)] = seen_src.get((name, src), 0) + 1
            if name not in PROFILES:
                continue
            if src is None or len(row) < 8:
                problems.append(f"{name}: by_source row malformed ({len(row)} columns)")
                continue
            bs = ev[name].get("by_source", {}).get(src)
            if bs is None:
                problems.append(f"{name}/{src}: source split missing in evidence")
                continue
            cells = {h: row[i] for i, h in enumerate(header) if i < len(row)}
            exp = {
                "Сессий": bs["n_sessions"],
                "Вызовы": bs["api_calls"]["sum"],
                "Input": bs["input_tokens"]["sum"],
                "Output": bs["output_tokens"]["sum"],
                "Reasoning": bs["reasoning_tokens"]["sum"],
                "Cache-read": bs["cache_read_tokens"]["sum"],
            }
            for label, want in exp.items():
                cell = cells.get(label)
                if cell is None or _num(cell) is None or abs(_num(cell) - want) > 0.05:
                    problems.append(
                        f"{name}/{src}: {label} = {cell.strip() if cell else None!r}, evidence {_fmt_int(want)}"
                    )
        for p in PROFILES:
            for s in ev[p].get("by_source", {}):
                if (p, s) not in seen_src:
                    problems.append(f"by_source: missing row {p}/{s}")
        for (name, src), cnt in seen_src.items():
            if cnt > 1:
                problems.append(f"by_source: duplicate row {name}/{src}")
        for name, src in seen_src:
            if name not in PROFILES:
                problems.append(f"by_source: extra profile row {name!r}")

    # -- 5) usage_by_model table (§4) --------------------------------------
    use_rows = _mandatory_rows(tables, "usage_by_model")
    if not use_rows:
        problems.append("usage_by_model table not found")
    else:
        seen_use: dict[tuple, int] = {}
        for prof, header, row in use_rows:
            name = row[0]
            model = row[1] if len(row) > 1 else None
            prov = row[2] if len(row) > 2 else None
            seen_use[(name, model, prov)] = seen_use.get((name, model, prov), 0) + 1
            if name not in PROFILES:
                continue
            cells = {h: row[i] for i, h in enumerate(header) if i < len(row)}
            found = None
            for u in ev[name]["usage_by_model"]:
                if u["model"] == model and _provider_display(u["billing_provider"], u["billing_mode"]) == prov:
                    found = u
                    break
            if found is None:
                problems.append(f"{name}: usage row {model!r}/{prov!r} not in evidence")
                continue
            exp = {
                "Вызовов": found["api_call_count"],
                "Input": found["input_tokens"],
                "Output": found["output_tokens"],
                "Reasoning": found["reasoning_tokens"],
                "Cache-read": found["cache_read_tokens"],
                "Сессий": found["sessions"],
            }
            for label, want in exp.items():
                cell = cells.get(label)
                if cell is None or _num(cell) is None or abs(_num(cell) - want) > 0.05:
                    problems.append(
                        f"{name}/{model}: {label} = {cell.strip() if cell else None!r}, evidence {_fmt_int(want)}"
                    )
        for p in PROFILES:
            for u in ev[p]["usage_by_model"]:
                k = (p, u["model"], _provider_display(u["billing_provider"], u["billing_mode"]))
                if k not in seen_use:
                    problems.append(f"usage_by_model: missing row {k[0]}/{k[1]}/{k[2]}")
        for (name, model, prov), cnt in seen_use.items():
            if cnt > 1:
                problems.append(f"usage_by_model: duplicate row {name}/{model}/{prov}")
        for name, model, prov in seen_use:
            if name not in PROFILES:
                problems.append(f"usage_by_model: extra profile row {name!r}")

    # -- 6) reasoning effort table (§5) ------------------------------------
    eff_rows = _mandatory_rows(tables, "reasoning effort")
    if not eff_rows:
        problems.append("reasoning effort table not found")
    else:
        seen_eff: dict[str, int] = {}
        for prof, header, row in eff_rows:
            name = row[0]
            seen_eff[name] = seen_eff.get(name, 0) + 1
            if name not in PROFILES:
                continue
            dist = ev[name].get("reasoning_effort_distribution", {})
            cells = {h: row[i] for i, h in enumerate(header) if i < len(row)}
            for label in header[1:]:
                got = _num(cells.get(label))
                want = dist.get(label, 0)
                if got is None or abs(got - want) > 0.05:
                    problems.append(f"{name}: effort[{label}] = {cells.get(label)!r}, evidence {want}")
            for k, v in dist.items():
                if k not in header[1:]:
                    problems.append(f"{name}: effort[{k}]={v} missing from report table")
        _complete_profiles(problems, "reasoning effort", seen_eff)

    # -- 7) finish_reason table (§5) ---------------------------------------
    fin_rows = _mandatory_rows(tables, "finish_reason")
    if not fin_rows:
        problems.append("finish_reason table not found")
    else:
        seen_fin: dict[str, int] = {}
        for prof, header, row in fin_rows:
            name = row[0]
            seen_fin[name] = seen_fin.get(name, 0) + 1
            if name not in PROFILES:
                continue
            counts = {f["finish_reason"]: f["count"] for f in ev[name]["messages"]["finish_reason"]}
            cells = {h: row[i] for i, h in enumerate(header) if i < len(row)}
            for label in header[1:]:
                got = _num(cells.get(label))
                want = counts.get(label, 0)
                if got is None or abs(got - want) > 0.05:
                    problems.append(f"{name}: finish_reason[{label}] = {cells.get(label)!r}, evidence {want}")
            for k, v in counts.items():
                if k not in header[1:]:
                    problems.append(f"{name}: finish_reason[{k}]={v} missing from report table")
        _complete_profiles(problems, "finish_reason", seen_fin)

    # -- 8) top-5 tables (§7) ----------------------------------------------
    top5_rows = _mandatory_rows(tables, "top-5")
    if not top5_rows:
        problems.append("top-5 tables not found")
    else:
        seen_top: dict[tuple, int] = {}
        bad_headings: set[str] = set()
        for prof, header, row in top5_rows:
            metric = row[0] if row else None
            # Profile identity for top-5 rows comes EXCLUSIVELY from the
            # section heading, and it must be exact: a suffixed / unknown /
            # ambiguous heading (e.g. "### architect altered") is a
            # misattribution and must fail closed. The old split()[0]
            # mapping accepted any heading starting with a valid profile.
            name = prof if prof else None
            if name not in PROFILES and name not in bad_headings:
                bad_headings.add(name)
                problems.append(
                    f"top-5: section heading {prof!r} is not exactly one of "
                    f"{list(PROFILES)}"
                )
            cells = {h: row[i] for i, h in enumerate(header) if i < len(row)}
            id_cell = cells.get("id (ред.)", "").strip().strip("`")
            seen_top[(name, metric, id_cell)] = seen_top.get((name, metric, id_cell), 0) + 1
            if name not in PROFILES or metric not in ("input", "reasoning"):
                continue
            rows = _top5_rows(ev[name], metric)
            inp = _num(cells.get("input"))
            rsn = _num(cells.get("reasoning"))
            calls = _num(cells.get("вызовов"))
            msgs = _num(cells.get("msgs"))
            tools = _num(cells.get("tools"))
            match = next((t for t in rows if t["id_redacted"] == id_cell), None)
            if match is None:
                problems.append(f"{name}: top5-{metric} id {id_cell!r} not in evidence")
                continue
            if inp is None or abs(inp - match["input_tokens"]) > 0.05:
                problems.append(f"{name}: top5-{metric} {id_cell} input = {cells.get('input')!r}, evidence {match['input_tokens']}")
            if rsn is None or abs(rsn - match["reasoning_tokens"]) > 0.05:
                problems.append(f"{name}: top5-{metric} {id_cell} reasoning = {cells.get('reasoning')!r}, evidence {match['reasoning_tokens']}")
            if calls is None or abs(calls - match["api_call_count"]) > 0.05:
                problems.append(f"{name}: top5-{metric} {id_cell} calls = {cells.get('вызовов')!r}, evidence {match['api_call_count']}")
            if msgs is None or abs(msgs - match["message_count"]) > 0.05:
                problems.append(f"{name}: top5-{metric} {id_cell} msgs = {cells.get('msgs')!r}, evidence {match['message_count']}")
            if tools is None or abs(tools - match["tool_call_count"]) > 0.05:
                problems.append(f"{name}: top5-{metric} {id_cell} tools = {cells.get('tools')!r}, evidence {match['tool_call_count']}")
            if "source" in cells and cells["source"].strip() != (match["source"] or ""):
                problems.append(f"{name}: top5-{metric} {id_cell} source = {cells.get('source')!r}, evidence {match['source']!r}")
            if "model" in cells and cells["model"].strip() != (match["model"] or ""):
                problems.append(f"{name}: top5-{metric} {id_cell} model = {cells.get('model')!r}, evidence {match['model']!r}")
            if "effort" in cells and cells["effort"].strip() != (match["reasoning_effort"] or "(none)"):
                problems.append(
                    f"{name}: top5-{metric} {id_cell} effort = {cells.get('effort')!r}, evidence {match['reasoning_effort']!r}"
                )
        for p in PROFILES:
            for metric in ("input", "reasoning"):
                for t in ev[p].get(f"top5_by_{metric}", []):
                    k = (p, metric, t["id_redacted"])
                    if k not in seen_top:
                        problems.append(f"top-5: missing row {p}/{metric}/{t['id_redacted']}")
        for (name, metric, id_cell), cnt in seen_top.items():
            if cnt > 1:
                problems.append(f"top-5: duplicate row {name}/{metric}/{id_cell}")
        for name, metric, id_cell in seen_top:
            if name not in PROFILES or metric not in ("input", "reasoning"):
                problems.append(f"top-5: extra row {name!r}/{metric!r}/{id_cell!r}")

    # -- 9) AGENTS.md facts (§6) vs the COMMITTED HEAD blob -----------------
    # AGENTS.md is a mandatory input: when the committed blob was resolved the
    # report MUST cite the facts and they MUST match the committed HEAD file
    # (never an arbitrary dirty working-tree file).
    if agents_bytes is not None:
        text = agents_bytes.decode("utf-8", errors="replace")
        m_bytes = re.search(r"(\d[\d ]*)\s*байт", report_text)
        m_chars = re.search(r"(\d[\d ]*)\s*UTF-8 символов", report_text)
        m_sha = re.search(r"sha256\s*`?([0-9a-f]{64})", report_text)
        if m_bytes is None:
            problems.append("AGENTS.md: report does not state the byte count (mandatory)")
        elif _num(m_bytes.group(1)) != len(agents_bytes):
            problems.append(
                f"AGENTS.md bytes: report {m_bytes.group(1)!r}, committed HEAD {len(agents_bytes)}"
            )
        if m_chars is None:
            problems.append("AGENTS.md: report does not state the UTF-8 char count (mandatory)")
        elif _num(m_chars.group(1)) != len(text):
            problems.append(
                f"AGENTS.md chars: report {m_chars.group(1)!r}, committed HEAD {len(text)}"
            )
        if m_sha is None:
            problems.append("AGENTS.md: report does not state the sha256 (mandatory)")
        else:
            real = hashlib.sha256(agents_bytes).hexdigest()
            if m_sha.group(1) != real:
                problems.append(f"AGENTS.md sha256: report {m_sha.group(1)}, committed HEAD {real}")

    # -- 10) freshness: the report's cited HEAD must describe the current target --
    # The artifacts may legitimately cite the measurement commit (the commit
    # they were generated at, e.g. the branch tip before the artifact commit
    # itself). Freshness therefore means: the cited commit's AGENTS.md blob
    # must be IDENTICAL to the committed HEAD blob (git show HEAD:AGENTS.md).
    # An artifact regenerated against an old commit with a different AGENTS.md
    # (the 38b1091 regression) fails here; an exact citation of the current
    # HEAD passes trivially.
    if head_sha is not None:
        m_head = re.search(r"HEAD\s+`?([0-9a-f]{7,40})`?", report_text)
        if m_head is None:
            problems.append(
                "AGENTS.md: report does not cite the HEAD commit (expected 'HEAD <sha>')"
            )
        elif head_sha[: len(m_head.group(1))] != m_head.group(1):
            try:
                cited_blob = _git(repo, "show", f"{m_head.group(1)}:AGENTS.md")
            except RuntimeError:
                cited_blob = None
            if cited_blob != agents_bytes:
                problems.append(
                    f"AGENTS.md: report cites HEAD {m_head.group(1)!r} whose AGENTS.md "
                    f"differs from committed HEAD {head_sha[:12]} — refresh the "
                    f"artifacts on the current target"
                )

    return problems


def _self_test(evidence: dict, report_text: str) -> list[str]:
    """Prove the structural check catches a number swapped from another profile.

    The swapped value (developer's input sum) DOES exist in the evidence —
    the old digit-token verifier would have let it through. The structural
    check must flag it because it sits in the architect row.
    """
    # find architect & developer input-sum cells in the main table
    tables = parse_tables(report_text)
    arch_cell = dev_cell = None
    for prof, header, row in tables:
        if not any("Вызовов (sum)" in h for h in header) or not row:
            continue
        if row[0] == "architect":
            arch_cell = row[[h for h in header].index("Input sum")]
        if row[0] == "developer":
            dev_cell = row[[h for h in header].index("Input sum")]
    if arch_cell is None or dev_cell is None:
        return ["self-test: could not locate Input sum cells"]
    mutated = report_text.replace(arch_cell, dev_cell, 1)
    problems = check_report(mutated, evidence)
    if not problems:
        return ["self-test FAILED: swapped architect Input sum with developer's was NOT detected"]
    return []


def main() -> int:
    evidence_text = EVIDENCE.read_text(encoding="utf-8")
    report_text = REPORT.read_text(encoding="utf-8")
    context_text = CONTEXT.read_text(encoding="utf-8")
    evidence = json.loads(evidence_text)

    problems: list[str] = []
    # AGENTS.md is a MANDATORY input: resolve the COMMITTED HEAD blob. A git
    # failure or an AGENTS.md absent from HEAD is a hard failure — never a
    # silent skip — and a dirty working-tree file is never trusted.
    try:
        head_sha, agents_bytes = committed_agents(REPO)
    except RuntimeError as e:
        problems.append(f"AGENTS.md: committed HEAD file required but unavailable: {e}")
        head_sha, agents_bytes = None, None

    problems += check_report(report_text, evidence, agents_bytes, head_sha)
    problems += check_redaction(
        {
            "report": report_text,
            "evidence": evidence_text,
            "context_baseline": context_text,
        }
    )

    if "--self-test" in sys.argv:
        st = _self_test(evidence, report_text)
        # prove the redaction regression check catches provenance markers
        red_mutated = report_text + "\n| task | t_0123456789abcdef | wt/t_0123abcd |\n"
        red_problems = check_redaction({"self-test": red_mutated})
        if not red_problems:
            st.append(
                "self-test FAILED: planted kanban task id / worktree marker was NOT detected"
            )
        print("self-test:", "OK (substitution detected)" if not st else "\n".join(st))
        if st:
            return 1

    if problems:
        print("REPORT MISMATCHES EVIDENCE / REDACTION:")
        for p in problems:
            print("  ", p)
        return 1
    print(
        "OK: report tables/fingerprints/derived ratios match the committed evidence; "
        "AGENTS.md facts match committed HEAD; redaction clean."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
