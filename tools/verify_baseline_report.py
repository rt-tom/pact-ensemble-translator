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
4. Verifies the AGENTS.md size/char-count/hash claims against the live repo
   file (read-only).

Prose is not trusted; only tables and explicit AGENTS.md facts are checked.

Usage:
    python tools/verify_baseline_report.py            # check committed report
    python tools/verify_baseline_report.py --self-test  # prove substitution is caught
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EVIDENCE = REPO / "docs/audits/HERMES_PROFILE_TOKEN_BASELINE_PHASE0_evidence.json"
REPORT = REPO / "docs/audits/hermes-profile-token-baseline-2026-08-07.md"
AGENTS = REPO / "AGENTS.md"

PROFILES = ("architect", "developer", "reviewer")


# ---------------------------------------------------------------------------
# Number parsing helpers (report formatting: "1 678", "40 / 276", "14.9 %",
# "~62×", "120").
# ---------------------------------------------------------------------------
def _num(cell: str):
    """Parse a report cell into a float. Returns None for non-numeric cells.

    Handles space thousands separators, '~'/'×'/'%' decorations, bold/italic
    markdown and backticks.
    """
    s = cell.strip().replace(" ", "").replace("\u00a0", "")
    s = s.replace("~", "").replace("×", "").replace("%", "").replace(",", ".")
    s = s.replace("*", "").replace("`", "")
    if s in ("", "—", "-", "(null)", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


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
# The actual check.
# ---------------------------------------------------------------------------
def check_report(report_text: str, evidence: dict, agents_bytes: bytes | None = None) -> list[str]:
    """Return a list of problems; empty list means the report is consistent."""
    problems: list[str] = []
    ev = evidence["profiles"]
    tables = parse_tables(report_text)

    # -- 1) fingerprint table (§1) -----------------------------------------
    fp_rows = _rows(tables, lambda h: any("sha256" in x for x in h))
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
    main_rows = _rows(tables, lambda h: any("Вызовов (sum)" in x for x in h))
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
    der_rows = _rows(tables, lambda h: any("reasoning/input" in x for x in h))
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
    src_rows = _rows(
        tables,
        lambda h: any("source" in x for x in h) and not any("метрика" in x for x in h),
    )
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
    use_rows = _rows(tables, lambda h: any("provider" in x for x in h))
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
    eff_rows = _rows(tables, lambda h: any("medium" in x for x in h))
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
    fin_rows = _rows(tables, lambda h: any("tool_calls" in x for x in h))
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
    top5_rows = _rows(tables, lambda h: any("метрика" in x for x in h))
    if not top5_rows:
        problems.append("top-5 tables not found")
    else:
        seen_top: dict[tuple, int] = {}
        for prof, header, row in top5_rows:
            metric = row[0] if row else None
            name = prof.split()[0] if prof else None
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

    # -- 9) AGENTS.md facts (§6) vs the live repo file ---------------------
    if agents_bytes is not None:
        text = agents_bytes.decode("utf-8", errors="replace")
        m_bytes = re.search(r"(\d[\d ]*)\s*байт", report_text)
        m_chars = re.search(r"(\d[\d ]*)\s*UTF-8 символов", report_text)
        m_sha = re.search(r"sha256\s*`?([0-9a-f]{64})", report_text)
        if m_bytes and _num(m_bytes.group(1)) != len(agents_bytes):
            problems.append(f"AGENTS.md bytes: report {m_bytes.group(1)!r}, file {len(agents_bytes)}")
        if m_chars and _num(m_chars.group(1)) != len(text):
            problems.append(f"AGENTS.md chars: report {m_chars.group(1)!r}, file {len(text)}")
        if m_sha:
            import hashlib
            real = hashlib.sha256(agents_bytes).hexdigest()
            if m_sha.group(1) != real:
                problems.append(f"AGENTS.md sha256: report {m_sha.group(1)}, file {real}")

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
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    report_text = REPORT.read_text(encoding="utf-8")
    agents_bytes = AGENTS.read_bytes() if AGENTS.exists() else None

    problems = check_report(report_text, evidence, agents_bytes)
    if "--self-test" in sys.argv:
        st = _self_test(evidence, report_text)
        print("self-test:", "OK (substitution detected)" if not st else "\n".join(st))
        if st:
            return 1

    if problems:
        print("REPORT MISMATCHES EVIDENCE:")
        for p in problems:
            print("  ", p)
        return 1
    print("OK: report tables/fingerprints/derived ratios match the committed evidence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
