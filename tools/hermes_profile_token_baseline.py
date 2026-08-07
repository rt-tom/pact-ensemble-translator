#!/usr/bin/env python3
"""Hermes profile token baseline — read-only reporter (stdlib only).

Phase 0 baseline for the Hermes profile token-efficiency plan
(.hermes/plans/2026-08-06_221320-hermes-profile-token-efficiency.md).

Reads each profile's state.db strictly READ-ONLY (mode=ro) and prints a
redacted JSON aggregate: no prompts, no message contents, no titles, no
credentials, no source text. Only numeric/metadata aggregates are emitted;
session ids are sha256-redacted.

Privacy model:
- Explicit per-table ALLOWLISTS (``_SESSIONS_ALLOW`` / ``_USAGE_ALLOW``).
  Only the columns genuinely needed for the redacted aggregate are ever
  SELECTed; there is no deny-list and no SELECT * / SELECT-all. Columns that
  may carry private content (user_id, task, billing_base_url, handoff or
  compression errors, content/prompt/title/source-text/credentials fields)
  are never read at all.
- ``messages`` is queried with fixed aggregate SQL over role / finish_reason
  / tool_name only — never content columns.

Reproducibility:
- state.db runs in WAL mode, so the raw main-file bytes are NOT a valid
  fingerprint of what a read-only connection sees. The reporter first copies
  the live DB into a consistent snapshot with the SQLite backup API (this
  captures main file + WAL in one consistent state), then hashes the snapshot
  and computes every aggregate FROM THAT SNAPSHOT. ``fingerprint`` therefore
  describes exactly the bytes the aggregates were computed from.

Failure semantics:
- Any mandatory input missing (profile state.db, config.yaml, required table
  or required allowlisted column) raises :class:`BaselineError`; ``main``
  prints the partial JSON with per-profile ``error`` entries and exits with a
  NON-ZERO code.

Usage:
    python tools/hermes_profile_token_baseline.py [--profiles-dir DIR] [--json out.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROFILES = ("architect", "developer", "reviewer")

# ---------------------------------------------------------------------------
# Privacy: explicit per-table allowlists.
# ---------------------------------------------------------------------------
# Only these columns are ever SELECTed. Columns that may carry private
# content (user_id, task, billing_base_url, handoff/compression errors,
# content/prompt/title/source-text/credentials fields) are NOT in these
# allowlists and are therefore never read.
_SESSIONS_ALLOW = (
    "id",                 # redacted to a sha256 prefix before output
    "source",
    "model",
    "end_reason",
    "model_config",       # parsed locally for reasoning effort only
    "message_count",
    "tool_call_count",
    "api_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",  # used only as aggregate cost_nonzero flag
    "actual_cost_usd",     # used only as aggregate cost_nonzero flag
)

_USAGE_ALLOW = (
    "model",
    "billing_provider",
    "billing_mode",
    "api_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",  # used only as aggregate cost_nonzero flag
    "actual_cost_usd",     # used only as aggregate cost_nonzero flag
)

# Forbidden column names, kept for documentation and tests only. These names
# must never appear in any SQL built by this tool.
_FORBIDDEN_COLUMNS = frozenset(
    {
        "content",
        "system_prompt",
        "origin_json",
        "title",
        "last_activity_description",
        "display_name",
        "api_content",
        "reasoning_content",
        "reasoning_details",
        "codex_reasoning_items",
        "codex_message_items",
        "session_key",
        "chat_id",
        "thread_id",
        "cwd",
        "git_repo_root",
        "git_branch",
        "user_id",
        "task",
        "billing_base_url",
        "base_url",
        "handoff_error",
        "compression_failure_error",
        "api_key",
        "password",
        "token",
        "secret",
        "credential",
        ".env",
    }
)

_REQUIRED_TABLES = ("sessions", "session_model_usage", "messages")


class BaselineError(Exception):
    """A mandatory baseline input is missing or unusable."""


def _default_profiles_dir() -> str:
    """Resolve the Hermes profiles dir from the environment (no hard-coded path)."""
    env = os.environ.get("HERMES_PROFILES_DIR")
    if env:
        return env
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cand = Path(local) / "hermes" / "profiles"
        if cand.is_dir():
            return str(cand)
    home = Path.home()
    cand = home / "AppData" / "Local" / "hermes" / "profiles"
    if cand.is_dir():
        return str(cand)
    return str(home / ".hermes" / "profiles")


def _redact(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Percentiles.
# ---------------------------------------------------------------------------
def _pct(sorted_vals, p: float):
    """Linear-interpolated percentile (R-7; same as numpy default / Excel PERCENTILE.INC).

    ``p50`` is the standard median: for even n it is the mean of the two
    central values. ``p90`` uses the same interpolation rule. Documented in
    the Phase 0 report, §1 (method).
    """
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    rank = (n - 1) * p / 100.0
    lo = int(rank)
    frac = rank - lo
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _stats(values):
    sv = sorted(float(v) for v in values)
    if not sv:
        return None
    return {
        "count": len(sv),
        "sum": round(sum(sv), 1),
        "p50": round(_pct(sv, 50), 1),
        "p90": round(_pct(sv, 90), 1),
        "max": round(sv[-1], 1),
    }


# ---------------------------------------------------------------------------
# Schema validation: tables and allowlisted columns must exist.
# ---------------------------------------------------------------------------
def _table_exists(cur, table: str) -> bool:
    row = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _check_schema(cur) -> None:
    """Every required table and every allowlisted column must exist.

    Raises BaselineError otherwise — a baseline built on a partial schema is
    not a valid baseline.
    """
    for table in _REQUIRED_TABLES:
        if not _table_exists(cur, table):
            raise BaselineError(f"required table missing: {table}")
    present = {
        r[1] for r in cur.execute("PRAGMA table_info(sessions)").fetchall()
    }
    missing = [c for c in _SESSIONS_ALLOW if c not in present]
    if missing:
        raise BaselineError(f"sessions table missing required columns: {missing}")
    present = {
        r[1] for r in cur.execute("PRAGMA table_info(session_model_usage)").fetchall()
    }
    missing = [c for c in _USAGE_ALLOW if c not in present]
    if missing:
        raise BaselineError(
            f"session_model_usage table missing required columns: {missing}"
        )
    present = {r[1] for r in cur.execute("PRAGMA table_info(messages)").fetchall()}
    missing = [c for c in ("role", "finish_reason", "tool_name") if c not in present]
    if missing:
        raise BaselineError(f"messages table missing required columns: {missing}")


# ---------------------------------------------------------------------------
# Aggregates (all read from the consistent snapshot).
# ---------------------------------------------------------------------------
def _session_summary(cur):
    """Per-session aggregates from `sessions` — allowlisted columns only."""
    sel = ", ".join(_SESSIONS_ALLOW)
    rows = cur.execute(f"SELECT {sel} FROM sessions").fetchall()
    out = []
    for r in rows:
        d = dict(zip(_SESSIONS_ALLOW, r))
        # extract reasoning effort from model_config JSON (metadata only)
        effort = None
        mc = d.get("model_config")
        if mc:
            try:
                cfg = json.loads(mc)
                effort = (cfg.get("reasoning_config") or {}).get("effort")
            except Exception:
                effort = None
        rec = {
            "id_redacted": _redact(str(d.get("id") or "")),
            "source": d.get("source"),
            "model": d.get("model"),
            "end_reason": d.get("end_reason"),
            "reasoning_effort": effort,
            "message_count": d.get("message_count"),
            "tool_call_count": d.get("tool_call_count"),
            "api_call_count": d.get("api_call_count"),
            "input_tokens": d.get("input_tokens"),
            "output_tokens": d.get("output_tokens"),
            "cache_read_tokens": d.get("cache_read_tokens"),
            "cache_write_tokens": d.get("cache_write_tokens"),
            "reasoning_tokens": d.get("reasoning_tokens"),
        }
        # cost fields only as aggregate-safe flags (they are 0 / absent anyway)
        rec["cost_nonzero"] = any(
            (d.get(c) or 0) not in (0, None, "", "0")
            for c in ("estimated_cost_usd", "actual_cost_usd")
        )
        out.append(rec)
    return out


def _usage_summary(cur):
    """Per-model/provider aggregates from `session_model_usage` — allowlisted columns only."""
    sel = ", ".join(_USAGE_ALLOW)
    rows = cur.execute(f"SELECT {sel} FROM session_model_usage").fetchall()
    groups = {}
    for r in rows:
        d = dict(zip(_USAGE_ALLOW, r))
        key = (d.get("model"), d.get("billing_provider"), d.get("billing_mode"))
        g = groups.setdefault(
            key,
            {
                "model": d.get("model"),
                "billing_provider": d.get("billing_provider"),
                "billing_mode": d.get("billing_mode"),
                "api_call_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "sessions": 0,
                "cost_nonzero": False,
            },
        )
        for k in ("api_call_count", "input_tokens", "output_tokens",
                  "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
            v = d.get(k) or 0
            g[k] = (g[k] if isinstance(g[k], int) else 0) + (v if isinstance(v, int) else 0)
        g["sessions"] += 1
        g["cost_nonzero"] = g["cost_nonzero"] or bool(
            (d.get("estimated_cost_usd") or 0) not in (0, None, "", "0")
            or (d.get("actual_cost_usd") or 0) not in (0, None, "", "0")
        )
    return sorted(groups.values(), key=lambda g: -(g["input_tokens"] or 0))


def _message_summary(cur):
    """Aggregate-only message stats. Fixed SQL over role/finish_reason/tool_name —
    never selects content columns."""
    roles = cur.execute(
        "SELECT role, COUNT(*) FROM messages GROUP BY role ORDER BY 2 DESC"
    ).fetchall()
    finish = cur.execute(
        "SELECT COALESCE(finish_reason,'(null)'), COUNT(*) FROM messages "
        "GROUP BY finish_reason ORDER BY 2 DESC"
    ).fetchall()
    tools = cur.execute(
        "SELECT tool_name, COUNT(*) FROM messages WHERE role='tool' AND tool_name IS NOT NULL "
        "GROUP BY tool_name ORDER BY 2 DESC LIMIT 10"
    ).fetchall()
    return {
        "roles": [{"role": r, "count": c} for r, c in roles],
        "finish_reason": [{"finish_reason": f, "count": c} for f, c in finish],
        "top_tools": [{"tool": t, "count": c} for t, c in tools],
    }


# ---------------------------------------------------------------------------
# Consistent snapshot (WAL-aware).
# ---------------------------------------------------------------------------
def _consistent_snapshot(db: Path):
    """Copy a live (possibly WAL-mode) SQLite db into one consistent snapshot file.

    Uses the SQLite backup API from a read-only connection, so the snapshot
    includes the WAL content exactly as a read-only connection would see it.
    Returns (snapshot_path, journal_mode). The caller must delete the
    snapshot when done.
    """
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        journal_mode = src.execute("PRAGMA journal_mode").fetchone()[0]
        fd, tmp = tempfile.mkstemp(prefix="hermes_baseline_", suffix=".db")
        os.close(fd)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
        return Path(tmp), journal_mode
    finally:
        src.close()


def analyze_profile(profile_dir: Path) -> dict:
    db = profile_dir / "state.db"
    if not db.exists():
        raise BaselineError(f"state.db not found: {db}")
    cfg = profile_dir / "config.yaml"
    if not cfg.exists():
        raise BaselineError(f"config.yaml not found: {cfg}")

    snap, journal_mode = _consistent_snapshot(db)
    try:
        fingerprint = {
            # sha256 of the CONSISTENT snapshot the aggregates were computed
            # from (main file + WAL), not of the raw main file — raw main-file
            # bytes are not a stable fingerprint under WAL.
            "snapshot_sha256": hashlib.sha256(snap.read_bytes()).hexdigest(),
            "snapshot_bytes": snap.stat().st_size,
            "journal_mode": journal_mode,
            "config_yaml_sha256": (
                hashlib.sha256(cfg.read_bytes()).hexdigest() if cfg.exists() else None
            ),
        }
        con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        cur = con.cursor()
        try:
            _check_schema(cur)
            sessions = _session_summary(cur)
            usage = _usage_summary(cur)
            msgs = _message_summary(cur)
        finally:
            con.close()

        # source split (kanban vs non-kanban)
        by_source = {}
        for s in sessions:
            src = s["source"] or "(null)"
            by_source.setdefault(src, []).append(s)

        def agg(src_sessions):
            def col(name):
                return [s[name] for s in src_sessions if s.get(name) is not None]
            return {
                "n_sessions": len(src_sessions),
                "api_calls": _stats(col("api_call_count")),
                "input_tokens": _stats(col("input_tokens")),
                "output_tokens": _stats(col("output_tokens")),
                "cache_read_tokens": _stats(col("cache_read_tokens")),
                "cache_write_tokens": _stats(col("cache_write_tokens")),
                "reasoning_tokens": _stats(col("reasoning_tokens")),
            }

        def top5(src_sessions, metric):
            ranked = sorted(
                (s for s in src_sessions if s.get(metric) is not None),
                key=lambda s: s[metric],
                reverse=True,
            )[:5]
            return [
                {
                    "id_redacted": s["id_redacted"],
                    "source": s["source"],
                    "model": s["model"],
                    "reasoning_effort": s["reasoning_effort"],
                    "end_reason": s["end_reason"],
                    "api_call_count": s["api_call_count"],
                    "message_count": s["message_count"],
                    "tool_call_count": s["tool_call_count"],
                    "input_tokens": s["input_tokens"],
                    "reasoning_tokens": s["reasoning_tokens"],
                }
                for s in ranked
            ]

        # reasoning effort distribution
        effort_dist = {}
        for s in sessions:
            e = s["reasoning_effort"] or "(none)"
            effort_dist[e] = effort_dist.get(e, 0) + 1

        all_s = sessions
        return {
            "profile": profile_dir.name,
            "fingerprint": fingerprint,
            "n_sessions_total": len(sessions),
            "all": agg(all_s),
            "by_source": {k: agg(v) for k, v in sorted(by_source.items())},
            "reasoning_effort_distribution": effort_dist,
            "end_reason_distribution": {
                str(k): sum(1 for s in sessions if s["end_reason"] == k)
                for k in sorted({s["end_reason"] for s in sessions}, key=lambda x: str(x))
            },
            "top5_by_input": top5(all_s, "input_tokens"),
            "top5_by_reasoning": top5(all_s, "reasoning_tokens"),
            "usage_by_model": usage,
            "messages": msgs,
            "cost_fields_nonzero": any(u["cost_nonzero"] for u in usage),
        }
    finally:
        snap.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--profiles-dir",
        default=_default_profiles_dir(),
        help="Hermes profiles directory (default: %(default)s)",
    )
    ap.add_argument("--json", default="", help="optional path to also write JSON output")
    args = ap.parse_args(argv)

    base = Path(args.profiles_dir)
    if not base.is_dir():
        print(f"ERROR: profiles dir not found: {base}", file=sys.stderr)
        return 1

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profiles_dir": base.name,
        "profiles": {},
    }
    exit_code = 0
    for p in PROFILES:
        try:
            result["profiles"][p] = analyze_profile(base / p)
        except BaselineError as e:
            result["profiles"][p] = {"error": str(e)}
            print(f"ERROR [{p}]: {e}", file=sys.stderr)
            exit_code = 1

    text = json.dumps(result, indent=2, ensure_ascii=False)
    print(text)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
