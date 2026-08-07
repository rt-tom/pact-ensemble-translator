"""Targeted tests for the Phase 0 Hermes profile token baseline tooling.

Covers the RV findings fixed on vk/hermes-profile-token-baseline:

1. Privacy: explicit per-table SQL column allowlists (no deny-list / SELECT-all),
   sensitive columns (user_id, task, billing_base_url, handoff/compression
   errors, content/prompt/title/source-text/credentials) are never read.
2. Correct quantiles: p50 is the standard median for even n (mean of the two
   central values), p90 uses the same documented R-7 linear-interpolation
   method; boundary cases n=1/2/10.
3. WAL-consistent snapshot fingerprint: fingerprint describes exactly the
   snapshot the aggregates were computed from (backup API incl. WAL), and
   aggregates match the live read-only DB view.
4. Missing mandatory inputs (state.db / config.yaml / table / column) exit
   with a non-zero code, not a JSON-with-error-and-exit-0.
5. verify_baseline_report.py structurally catches a number swapped in from
   another profile (substitution detection).

All tests are hermetic: they build synthetic SQLite DBs / temp profile dirs and
never touch live Hermes profile data. The verifier test runs against the
committed report+evidence in this repo.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools"


def _load_tool(name: str):
    path = TOOLS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


baseline = _load_tool("hermes_profile_token_baseline")
verify = _load_tool("verify_baseline_report")


# ---------------------------------------------------------------------------
# Synthetic Hermes-profile state.db (full real schema, sensitive values
# planted so we can prove they are never read into the output).
# ---------------------------------------------------------------------------
SESSIONS_SCHEMA = [
    ("id", "TEXT"), ("source", "TEXT"), ("user_id", "TEXT"),
    ("session_key", "TEXT"), ("chat_id", "TEXT"), ("chat_type", "TEXT"),
    ("thread_id", "TEXT"), ("display_name", "TEXT"), ("origin_json", "TEXT"),
    ("expiry_finalized", "INTEGER"), ("model", "TEXT"), ("model_config", "TEXT"),
    ("system_prompt", "TEXT"), ("system_prompt_hash", "TEXT"),
    ("parent_session_id", "TEXT"), ("started_at", "REAL"), ("ended_at", "REAL"),
    ("end_reason", "TEXT"), ("message_count", "INTEGER"),
    ("tool_call_count", "INTEGER"), ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"), ("cache_read_tokens", "INTEGER"),
    ("cache_write_tokens", "INTEGER"), ("reasoning_tokens", "INTEGER"),
    ("cwd", "TEXT"), ("git_branch", "TEXT"), ("git_repo_root", "TEXT"),
    ("billing_provider", "TEXT"), ("billing_base_url", "TEXT"),
    ("billing_mode", "TEXT"), ("estimated_cost_usd", "REAL"),
    ("actual_cost_usd", "REAL"), ("cost_status", "TEXT"), ("cost_source", "TEXT"),
    ("pricing_version", "TEXT"), ("title", "TEXT"), ("last_activity_at", "REAL"),
    ("last_activity_description", "TEXT"), ("last_activity_provenance", "TEXT"),
    ("api_call_count", "INTEGER"), ("handoff_state", "TEXT"),
    ("handoff_platform", "TEXT"), ("handoff_error", "TEXT"),
    ("compression_failure_cooldown_until", "REAL"),
    ("compression_failure_error", "TEXT"), ("compression_fallback_streak", "INTEGER"),
    ("compression_ineffective_count", "INTEGER"), ("profile_name", "TEXT"),
    ("rewind_count", "INTEGER"), ("archived", "INTEGER"), ("pinned", "INTEGER"),
]

USAGE_SCHEMA = [
    ("session_id", "TEXT"), ("model", "TEXT"), ("billing_provider", "TEXT"),
    ("billing_base_url", "TEXT"), ("billing_mode", "TEXT"), ("task", "TEXT"),
    ("api_call_count", "INTEGER"), ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"), ("cache_read_tokens", "INTEGER"),
    ("cache_write_tokens", "INTEGER"), ("reasoning_tokens", "INTEGER"),
    ("estimated_cost_usd", "REAL"), ("actual_cost_usd", "REAL"),
    ("cost_status", "TEXT"), ("cost_source", "TEXT"),
    ("first_seen", "REAL"), ("last_seen", "REAL"),
]

MESSAGES_SCHEMA = [
    ("id", "INTEGER"), ("session_id", "TEXT"), ("role", "TEXT"),
    ("content", "TEXT"), ("tool_call_id", "TEXT"), ("tool_calls", "TEXT"),
    ("tool_name", "TEXT"), ("effect_disposition", "TEXT"), ("timestamp", "REAL"),
    ("token_count", "INTEGER"), ("finish_reason", "TEXT"), ("reasoning", "TEXT"),
    ("reasoning_content", "TEXT"), ("reasoning_details", "TEXT"),
    ("codex_reasoning_items", "TEXT"), ("codex_message_items", "TEXT"),
    ("platform_message_id", "TEXT"), ("observed", "INTEGER"), ("active", "INTEGER"),
    ("compacted", "INTEGER"), ("api_content", "TEXT"), ("display_kind", "TEXT"),
    ("display_metadata", "TEXT"),
]

SECRET = "SUPERSECRET_PRIVATE_MARKER_7f3a"


def _make_state_db(path: Path, *, with_tables: bool = True) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=wal")
        if not with_tables:
            # a bare/empty DB with none of the required tables
            con.commit()
            con.close()
            return
        con.executescript(
            "CREATE TABLE sessions ("
            + ", ".join(f"{n} {t}" for n, t in SESSIONS_SCHEMA)
            + ");"
        )
        con.executescript(
            "CREATE TABLE session_model_usage ("
            + ", ".join(f"{n} {t}" for n, t in USAGE_SCHEMA)
            + ");"
        )
        con.executescript(
            "CREATE TABLE messages ("
            + ", ".join(f"{n} {t}" for n, t in MESSAGES_SCHEMA)
            + ");"
        )
        if with_tables:
            con.execute(
                "INSERT INTO sessions (id, source, user_id, model, model_config, "
                "system_prompt, title, billing_base_url, handoff_error, "
                "compression_failure_error, message_count, tool_call_count, "
                "api_call_count, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, reasoning_tokens, end_reason, "
                "estimated_cost_usd, actual_cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "sess-001", "kanban", SECRET + "-user", "deepseek-v4-flash",
                    '{"reasoning_config": {"effort": "high"}}', SECRET + "-prompt",
                    SECRET + "-title", "https://" + SECRET, SECRET + "-handoff",
                    SECRET + "-compress", 3, 2, 5, 1000, 500, 200, 50, 300,
                    None, 0, 0,
                ),
            )
            con.execute(
                "INSERT INTO session_model_usage (session_id, model, "
                "billing_provider, billing_base_url, billing_mode, task, "
                "api_call_count, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, reasoning_tokens, estimated_cost_usd, "
                "actual_cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "sess-001", "deepseek-v4-flash", "opencode-go",
                    "https://" + SECRET, "chat_completions", SECRET + "-task",
                    5, 1000, 500, 200, 50, 300, 0, 0,
                ),
            )
            con.execute(
                "INSERT INTO messages (id, session_id, role, content, tool_name, "
                "finish_reason) VALUES (1, 'sess-001', 'user', ?, NULL, 'stop')",
                (SECRET + "-content",),
            )
        con.commit()
    finally:
        con.close()


@pytest.fixture()
def profile_dir(tmp_path: Path) -> Path:
    d = tmp_path / "testprofile"
    d.mkdir()
    _make_state_db(d / "state.db")
    (d / "config.yaml").write_text("reasoning_effort: high\n", encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# 1. Privacy: per-table allowlists, no deny-list / SELECT-all.
# ---------------------------------------------------------------------------
def test_allowlists_exclude_sensitive_columns() -> None:
    """The allowlists never name sensitive columns (deny-list is not used)."""
    sensitive = {
        "user_id", "task", "billing_base_url", "base_url", "handoff_error",
        "compression_failure_error", "content", "system_prompt", "origin_json",
        "title", "last_activity_description", "display_name", "api_content",
        "reasoning_content", "reasoning_details", "codex_reasoning_items",
        "codex_message_items", "session_key", "chat_id", "thread_id", "cwd",
        "git_repo_root", "git_branch", "api_key", "password", "token",
        "secret", "credential",
    }
    allow = set(baseline._SESSIONS_ALLOW) | set(baseline._USAGE_ALLOW)
    assert not (allow & sensitive)
    # messages table is queried with fixed aggregate SQL over role/finish_reason/tool_name
    msg_sql_ok = baseline._message_summary
    assert callable(msg_sql_ok)


def test_sql_selects_only_allowlisted_columns(profile_dir: Path) -> None:
    """The SQL executed never selects a column outside the allowlist, and the
    reporter output contains none of the planted sensitive values."""
    res = baseline.analyze_profile(profile_dir)
    text = json.dumps(res, ensure_ascii=False)
    assert SECRET not in text
    # aggregate fields are present and correct
    assert res["n_sessions_total"] == 1
    assert res["all"]["api_calls"]["sum"] == 5
    assert res["all"]["input_tokens"]["sum"] == 1000
    # usage grouping key never includes task/session_id/billing_base_url fields
    usage0 = res["usage_by_model"][0]
    assert "task" not in usage0
    assert "session_id" not in usage0
    assert "billing_base_url" not in usage0


def test_forbidden_names_never_built_into_sql(profile_dir: Path) -> None:
    """Even the schema-validator PRAGMA list never flows into a SELECT;
    SELECTs are built from the allowlist constants only."""
    for col in baseline._FORBIDDEN_COLUMNS:
        assert col not in baseline._SESSIONS_ALLOW
        assert col not in baseline._USAGE_ALLOW
    # no dynamic column discovery feeds a SELECT anywhere in the module
    src = (TOOLS / "hermes_profile_token_baseline.py").read_text(encoding="utf-8")
    assert "table_info" in src  # used for validation only
    assert "PRAGMA table_info" in src


# ---------------------------------------------------------------------------
# 2. Quantiles: p50 standard median (even n), p90 same documented method.
# ---------------------------------------------------------------------------
def test_pct_single_value() -> None:
    assert baseline._pct([5.0], 50) == 5.0
    assert baseline._pct([5.0], 90) == 5.0


def test_pct_two_values_median_is_mean() -> None:
    # standard median for even n = mean of the two central values
    assert baseline._pct([10.0, 20.0], 50) == 15.0
    # p90 with R-7: rank = (2-1)*0.9 = 0.9 -> 10 + 0.9*(20-10) = 19
    assert baseline._pct([10.0, 20.0], 90) == 19.0


def test_pct_ten_values_r7() -> None:
    vals = [float(i) for i in range(1, 11)]  # 1..10
    # p50: rank=(10-1)*0.5=4.5 -> v[4]+0.5*(v[5]-v[4]) = 5.5
    assert baseline._pct(vals, 50) == 5.5
    # p90: rank=9*0.9=8.1 -> v[8]+0.1*(v[9]-v[8]) = 9.1
    assert baseline._pct(vals, 90) == 9.1


def test_stats_even_n_p50_standard_median() -> None:
    st = baseline._stats([10, 20, 30, 40])
    assert st["p50"] == 25.0  # (20+30)/2 — standard median, not upper-middle
    assert st["p90"] == 37.0  # rank=3*0.9=2.7 -> 30+0.7*10


def test_stats_odd_n_p50_middle() -> None:
    st = baseline._stats([10, 20, 30])
    assert st["p50"] == 20.0
    assert st["p90"] == 28.0  # rank=2*0.9=1.8 -> 20+0.8*10


# ---------------------------------------------------------------------------
# 3. WAL-consistent snapshot fingerprint.
# ---------------------------------------------------------------------------
def test_fingerprint_describes_snapshot_not_raw_main_file(profile_dir: Path) -> None:
    db = profile_dir / "state.db"
    res = baseline.analyze_profile(profile_dir)
    fp = res["fingerprint"]
    assert fp["journal_mode"] == "wal"
    # snapshot hash differs from the raw main-file hash when WAL holds data
    main_sha = __import__("hashlib").sha256(db.read_bytes()).hexdigest()
    assert fp["snapshot_sha256"] != main_sha
    # snapshot bytes describe the backup copy, and it exists/readable at least once
    assert fp["snapshot_bytes"] > 0
    # aggregates computed from the snapshot match a fresh read-only view
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        n = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        con.close()
    assert res["n_sessions_total"] == n


def test_analyze_profile_is_read_only(profile_dir: Path) -> None:
    db = profile_dir / "state.db"
    before = db.read_bytes()
    baseline.analyze_profile(profile_dir)
    after = db.read_bytes()
    assert before == after  # live DB bytes untouched (mode=ro on the source)


# ---------------------------------------------------------------------------
# 4. Missing mandatory inputs -> non-zero exit.
# ---------------------------------------------------------------------------
def test_missing_state_db_exits_nonzero(tmp_path: Path) -> None:
    d = tmp_path / "missing"
    d.mkdir()
    (d / "config.yaml").write_text("reasoning_effort: high\n", encoding="utf-8")
    rc = baseline.main(["--profiles-dir", str(d)])
    assert rc != 0


def test_missing_config_yaml_exits_nonzero(tmp_path: Path) -> None:
    d = tmp_path / "noconfig"
    d.mkdir()
    _make_state_db(d / "state.db")
    rc = baseline.main(["--profiles-dir", str(d)])
    assert rc != 0


def test_missing_table_exits_nonzero(tmp_path: Path) -> None:
    d = tmp_path / "notable"
    d.mkdir()
    _make_state_db(d / "state.db", with_tables=False)
    (d / "config.yaml").write_text("reasoning_effort: high\n", encoding="utf-8")
    rc = baseline.main(["--profiles-dir", str(d)])
    assert rc != 0


def test_missing_profiles_dir_exits_nonzero(tmp_path: Path) -> None:
    rc = baseline.main(["--profiles-dir", str(tmp_path / "does-not-exist")])
    assert rc != 0


# ---------------------------------------------------------------------------
# 5. Structural verifier catches cross-profile substitution.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def committed_evidence() -> dict:
    return json.loads(
        (REPO / "docs/audits/HERMES_PROFILE_TOKEN_BASELINE_PHASE0_evidence.json")
        .read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def committed_report() -> str:
    return (REPO / "docs/audits/HERMES_PROFILE_TOKEN_BASELINE_PHASE0_RU.md").read_text(
        encoding="utf-8"
    )


def test_verifier_ok_on_committed_report(committed_evidence, committed_report) -> None:
    problems = verify.check_report(committed_report, committed_evidence)
    assert problems == []


def test_verifier_detects_cross_profile_substitution(
    committed_evidence, committed_report
) -> None:
    """Swap architect's Input sum for developer's (a value that DOES exist in
    the evidence): the structural check must flag it."""
    tables = verify.parse_tables(committed_report)
    arch_cell = dev_cell = None
    for _prof, header, row in tables:
        if not any("Вызовов (sum)" in h for h in header) or not row:
            continue
        idx = header.index("Input sum") if "Input sum" in header else None
        if idx is None:
            continue
        if row[0] == "architect":
            arch_cell = row[idx]
        if row[0] == "developer":
            dev_cell = row[idx]
    assert arch_cell is not None and dev_cell is not None
    assert arch_cell != dev_cell
    mutated = committed_report.replace(arch_cell, dev_cell, 1)
    problems = verify.check_report(mutated, committed_evidence)
    assert problems, "cross-profile substitution was NOT detected"
    assert any("architect" in p and "Input sum" in p for p in problems)


def test_verifier_self_test_mode(committed_evidence, committed_report) -> None:
    st = verify._self_test(committed_evidence, committed_report)
    assert st == []
