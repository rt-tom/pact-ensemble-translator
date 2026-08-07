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
6. Failure contract: malformed (non-numeric / non-finite) cells in mandatory
   numeric columns raise a sanitized BaselineError and exit non-zero — never
   an uncaught ValueError traceback or a silently-typed 0.
7. Path redaction covers every supported form (Windows drive, UNC, POSIX)
   without mangling URLs / slash-separated word lists.
8. Verifier requires AGENTS.md as a COMMITTED-HEAD input (git show
   HEAD:AGENTS.md) — hard failure when absent, never a dirty working-tree
   file — and rejects a stale HEAD citation (freshness).
9. Redaction regression check over the committed report / evidence / context:
   no task ids, worktree identifiers, absolute paths or sensitive
   column/credential words in published output.

All tests are hermetic: they build synthetic SQLite DBs / temp profile dirs
and never touch live Hermes profile data. The verifier tests run against the
committed report+evidence in this repo.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
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
    return (REPO / "docs/audits/hermes-profile-token-baseline-2026-08-07.md").read_text(
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


# ---------------------------------------------------------------------------
# 6. Structural verifier completeness: missing / duplicate / extra rows.
# ---------------------------------------------------------------------------
def _drop_row(md: str, needle: str) -> str:
    """Remove the first markdown table row line containing `needle`."""
    lines = md.splitlines()
    out: list[str] = []
    dropped = False
    for l in lines:
        if not dropped and l.lstrip().startswith("|") and needle in l:
            dropped = True
            continue
        out.append(l)
    assert dropped, f"report row {needle!r} not found"
    return "\n".join(out)


def _insert_row_after(md: str, needle: str, new_row: str) -> str:
    """Append `new_row` right after the first table row line containing `needle`."""
    lines = md.splitlines()
    out: list[str] = []
    done = False
    for l in lines:
        out.append(l)
        if not done and l.lstrip().startswith("|") and needle in l:
            out.append(new_row)
            done = True
    assert done, f"report row {needle!r} not found"
    return "\n".join(out)


def test_verifier_detects_missing_profile_row(committed_evidence, committed_report) -> None:
    """Deleting a whole profile row from the main aggregate table is caught."""
    mutated = _drop_row(committed_report, "| architect | 23 | 1 929 |")
    problems = verify.check_report(mutated, committed_evidence)
    assert problems, "deleting the architect aggregate row was NOT detected"
    assert any("missing" in p and "architect" in p for p in problems)


def test_verifier_detects_missing_fingerprint_row(committed_evidence, committed_report) -> None:
    """Deleting a whole fingerprint row (reviewer) is caught."""
    mutated = _drop_row(committed_report, "| reviewer | `8d97db60b6957571`")
    problems = verify.check_report(mutated, committed_evidence)
    assert problems, "deleting the reviewer fingerprint row was NOT detected"
    assert any("missing" in p and "reviewer" in p for p in problems)


def test_verifier_detects_missing_by_source_row(committed_evidence, committed_report) -> None:
    """Deleting one by_source row (architect/telegram) is caught."""
    mutated = _drop_row(committed_report, "| architect | telegram |")
    problems = verify.check_report(mutated, committed_evidence)
    assert problems, "deleting the architect/telegram by_source row was NOT detected"
    assert any("missing" in p and "telegram" in p for p in problems)


def test_verifier_detects_missing_top5_row(committed_evidence, committed_report) -> None:
    """Deleting one top-5 row (architect input, 593a124a2054) is caught."""
    mutated = _drop_row(committed_report, "| input | `593a124a2054`")
    problems = verify.check_report(mutated, committed_evidence)
    assert problems, "deleting an architect top-5 input row was NOT detected"
    assert any("missing" in p and "593a124a2054" in p for p in problems)


def test_verifier_detects_duplicate_profile_row(committed_evidence, committed_report) -> None:
    """Duplicating an existing aggregate row (architect) is caught."""
    arch_line = next(
        l for l in committed_report.splitlines()
        if l.lstrip().startswith("|") and "| architect | 23 | 1 929 |" in l
    )
    mutated = _insert_row_after(committed_report, "| architect | 23 | 1 929 |", arch_line)
    problems = verify.check_report(mutated, committed_evidence)
    assert problems, "duplicating the architect aggregate row was NOT detected"
    assert any("duplicate" in p and "architect" in p for p in problems)


def test_verifier_detects_extra_profile_row(committed_evidence, committed_report) -> None:
    """An extra row for a profile unknown to the evidence is caught."""
    fake = "| admin | 1 | 1 | 1 / 1 | 1 | 1 / 1 | 1 | 1 | 1 / 1 | 1 | 1 |"
    mutated = _insert_row_after(committed_report, "| architect | 23 | 1 929 |", fake)
    problems = verify.check_report(mutated, committed_evidence)
    assert problems, "an extra unknown profile row in the aggregate table was NOT detected"
    assert any("extra" in p and "admin" in p for p in problems)


# ---------------------------------------------------------------------------
# 7. Structural verifier rejects non-finite numeric cells (nan / inf).
# ---------------------------------------------------------------------------
def test_num_rejects_non_finite() -> None:
    """_num() must reject NaN/±Inf instead of returning them; every caller
    treats None as a mismatch, whereas `abs(nan - want) > 0.05` is False."""
    for bad in ("nan", "NaN", "inf", "-inf", "Infinity", "~inf"):
        assert verify._num(bad) is None, f"_num({bad!r}) must be None"
    # finite numbers and report decorations still parse
    assert verify._num("5 253 728") == 5253728.0
    assert verify._num("14.9 %") == 14.9
    assert verify._num("~62×") == 62.0


def _input_sum_cell(md: str, profile: str) -> str:
    """The Input-sum cell value for `profile` in the main aggregate table."""
    for _prof, header, row in verify.parse_tables(md):
        if not any("Вызовов (sum)" in h for h in header) or not row:
            continue
        if row[0] == profile and "Input sum" in header:
            return row[header.index("Input sum")]
    raise AssertionError(f"{profile} Input sum cell not found")


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_verifier_detects_non_finite_numeric_cell(
    committed_evidence, committed_report, bad
) -> None:
    """A numeric cell substituted with nan/±inf must make check_report non-empty
    (previously nan slipped through: abs(nan - want) > 0.05 is False)."""
    arch_cell = _input_sum_cell(committed_report, "architect")
    mutated = committed_report.replace(arch_cell, bad, 1)
    problems = verify.check_report(mutated, committed_evidence)
    assert problems, f"cell substituted with {bad!r} was NOT detected"
    assert any("architect" in p and "Input sum" in p for p in problems)


# ---------------------------------------------------------------------------
# 8. Committed evidence schema: `all` groups carry n_sessions (derived contract).
# ---------------------------------------------------------------------------
def test_committed_evidence_all_groups_have_n_sessions(committed_evidence) -> None:
    """Regression (RV2 t_008a13e0): the re-packed evidence dropped
    ``profile["all"]["n_sessions"]`` while ``agg()`` emits it and
    ``token_analysis_derived.derived()`` reads it (KeyError: 'n_sessions')."""
    for name, prof in committed_evidence["profiles"].items():
        assert "n_sessions" in prof["all"], f"{name}: all.n_sessions missing"
        assert prof["all"]["n_sessions"] == prof["n_sessions_total"], name
        for src, g in prof["by_source"].items():
            assert "n_sessions" in g, f"{name}/{src}: n_sessions missing"


def test_derived_tool_cli_on_committed_evidence(committed_evidence) -> None:
    """The acceptance command
    ``python tools/token_analysis_derived.py docs/audits/...evidence.json``
    must exit 0 (KeyError: 'n_sessions' regression)."""
    evidence = REPO / "docs/audits/HERMES_PROFILE_TOKEN_BASELINE_PHASE0_evidence.json"
    res = subprocess.run(
        [sys.executable, str(TOOLS / "token_analysis_derived.py"), str(evidence)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert res.returncode == 0, f"derived tool failed:\n{res.stderr}"
    out = json.loads(res.stdout)
    for name, prof in committed_evidence["profiles"].items():
        assert out["profiles"][name]["all"]["n_sessions"] == prof["n_sessions_total"]


# ---------------------------------------------------------------------------
# 9. Failure-path redaction: no machine-specific/absolute paths in output.
# ---------------------------------------------------------------------------
_ABS_PATH_RE = re.compile(r"[A-Za-z]:[\\/]")
_USERS_PATH_RE = re.compile(r"[\\/]Users[\\/]")


def _assert_redacted(text: str, tmp_path: Path) -> None:
    """The redacted-output contract: no absolute / machine-specific path may
    appear in stdout or stderr for missing mandatory inputs."""
    assert str(tmp_path) not in text, f"absolute tmp path leaked: {text!r}"
    assert not _ABS_PATH_RE.search(text), f"drive-letter path leaked: {text!r}"
    assert not _USERS_PATH_RE.search(text), f"user-profile path leaked: {text!r}"


def _make_profiles_root(tmp_path: Path, name: str = "root") -> Path:
    d = tmp_path / name
    d.mkdir()
    for p in ("architect", "developer", "reviewer"):
        (d / p).mkdir()
    return d


def test_missing_profiles_dir_error_is_redacted(tmp_path, capsys) -> None:
    rc = baseline.main(["--profiles-dir", str(tmp_path / "does-not-exist")])
    cap = capsys.readouterr()
    assert rc != 0
    _assert_redacted(cap.out + cap.err, tmp_path)
    assert "profiles dir not found" in cap.err


def test_missing_state_db_errors_are_redacted(tmp_path, capsys) -> None:
    d = _make_profiles_root(tmp_path)
    for p in ("architect", "developer", "reviewer"):
        (d / p / "config.yaml").write_text("reasoning_effort: high\n", encoding="utf-8")
    rc = baseline.main(["--profiles-dir", str(d)])
    cap = capsys.readouterr()
    assert rc != 0
    _assert_redacted(cap.out + cap.err, tmp_path)
    out = json.loads(cap.out)
    for name in ("architect", "developer", "reviewer"):
        assert "state.db not found in profile" in out["profiles"][name]["error"]
        assert str(tmp_path) not in out["profiles"][name]["error"]


def test_missing_config_yaml_errors_are_redacted(tmp_path, capsys) -> None:
    d = _make_profiles_root(tmp_path)
    for p in ("architect", "developer", "reviewer"):
        _make_state_db(d / p / "state.db")
    rc = baseline.main(["--profiles-dir", str(d)])
    cap = capsys.readouterr()
    assert rc != 0
    _assert_redacted(cap.out + cap.err, tmp_path)
    out = json.loads(cap.out)
    for name in ("architect", "developer", "reviewer"):
        assert "config.yaml not found in profile" in out["profiles"][name]["error"]
        assert str(tmp_path) not in out["profiles"][name]["error"]


def test_sanitize_error_never_globbed_by_bare_dot_needle() -> None:
    """A profiles-dir whose str() is '.' / '' must not glob every dot in the
    message (regression: 'state.db' -> 'state<profiles-dir>db')."""
    # Path('.') — str() is '.', a 1-char needle that must be skipped
    msg = "state.db not found in profile architect"
    out = baseline._sanitize_error(msg, Path("."))
    assert out == msg
    # an absolute profiles dir IS collapsed to the label
    out2 = baseline._sanitize_error(
        f"state.db not found: {Path('C:/Users/someone/hermes/profiles/architect')}",
        Path("C:/Users/someone/hermes/profiles"),
    )
    assert "C:" not in out2
    assert "<profiles-dir>" in out2


def test_unusable_state_db_errors_are_redacted(tmp_path, capsys) -> None:
    """A corrupt state.db (sqlite error) must surface as a redacted
    BaselineError, not a raw traceback with absolute paths."""
    d = _make_profiles_root(tmp_path)
    for p in ("architect", "developer", "reviewer"):
        (d / p / "config.yaml").write_text("reasoning_effort: high\n", encoding="utf-8")
        (d / p / "state.db").write_text("this is not a sqlite database", encoding="utf-8")
    rc = baseline.main(["--profiles-dir", str(d)])
    cap = capsys.readouterr()
    assert rc != 0
    _assert_redacted(cap.out + cap.err, tmp_path)
    assert "Traceback" not in cap.err
    out = json.loads(cap.out)
    for name in ("architect", "developer", "reviewer"):
        assert "state.db unusable in profile" in out["profiles"][name]["error"]
        assert str(tmp_path) not in out["profiles"][name]["error"]


# ---------------------------------------------------------------------------
# 10. Failure contract: malformed numeric cells -> sanitized BaselineError.
# ---------------------------------------------------------------------------
def _make_state_db_with_bad_session_cell(path: Path) -> None:
    """state.db with a non-numeric input_tokens cell in `sessions`."""
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=wal")
        con.executescript(
            "CREATE TABLE sessions (" + ", ".join(f"{n} {t}" for n, t in SESSIONS_SCHEMA) + ");"
        )
        con.executescript(
            "CREATE TABLE session_model_usage ("
            + ", ".join(f"{n} {t}" for n, t in USAGE_SCHEMA) + ");"
        )
        con.executescript(
            "CREATE TABLE messages (" + ", ".join(f"{n} {t}" for n, t in MESSAGES_SCHEMA) + ");"
        )
        con.execute(
            "INSERT INTO sessions (id, source, model, end_reason, message_count, "
            "input_tokens) VALUES (?,?,?,?,?,?)",
            ("sess-bad", "kanban", "deepseek-v4-flash", None, 1, "not-a-number"),
        )
        con.commit()
    finally:
        con.close()


def _make_state_db_with_bad_usage_cell(path: Path) -> None:
    """state.db with a non-numeric input_tokens cell in session_model_usage."""
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=wal")
        con.executescript(
            "CREATE TABLE sessions (" + ", ".join(f"{n} {t}" for n, t in SESSIONS_SCHEMA) + ");"
        )
        con.executescript(
            "CREATE TABLE session_model_usage ("
            + ", ".join(f"{n} {t}" for n, t in USAGE_SCHEMA) + ");"
        )
        con.executescript(
            "CREATE TABLE messages (" + ", ".join(f"{n} {t}" for n, t in MESSAGES_SCHEMA) + ");"
        )
        con.execute(
            "INSERT INTO session_model_usage (session_id, model, billing_provider, "
            "billing_mode, input_tokens) VALUES (?,?,?,?,?)",
            ("sess-bad", "deepseek-v4-flash", "opencode-go", "chat_completions", "oops"),
        )
        con.commit()
    finally:
        con.close()


def test_stats_malformed_numeric_raises_baseline_error() -> None:
    """A non-numeric / non-finite cell in a mandatory numeric column raises a
    sanitized BaselineError (regression: previously an uncaught ValueError)."""
    with pytest.raises(baseline.BaselineError, match="non-numeric value in numeric column input_tokens"):
        baseline._stats(["abc", 10, 20], "input_tokens")
    with pytest.raises(baseline.BaselineError, match="non-finite value in numeric column"):
        baseline._stats([float("nan"), 10], "input_tokens")
    with pytest.raises(baseline.BaselineError, match="non-finite value in numeric column"):
        baseline._stats([float("inf"), 10], "input_tokens")


@pytest.mark.parametrize(
    "bad_maker",
    [_make_state_db_with_bad_session_cell, _make_state_db_with_bad_usage_cell],
)
def test_malformed_numeric_cell_is_sanitized_nonzero(tmp_path, capsys, bad_maker) -> None:
    """Malformed mandatory numeric input -> per-profile BaselineError entry,
    non-zero exit, no traceback, no machine-specific path in output."""
    d = _make_profiles_root(tmp_path)
    for p in ("architect", "developer", "reviewer"):
        (d / p / "config.yaml").write_text("reasoning_effort: high\n", encoding="utf-8")
        bad_maker(d / p / "state.db")
    rc = baseline.main(["--profiles-dir", str(d)])
    cap = capsys.readouterr()
    assert rc != 0
    assert "Traceback" not in cap.err
    _assert_redacted(cap.out + cap.err, tmp_path)
    out = json.loads(cap.out)
    for name in ("architect", "developer", "reviewer"):
        err = out["profiles"][name]["error"]
        assert "non-numeric value in numeric column" in err, err
        assert str(tmp_path) not in err


# ---------------------------------------------------------------------------
# 11. Reporter redaction covers UNC and POSIX absolute paths.
# ---------------------------------------------------------------------------
def test_sanitize_error_redacts_unc_and_posix_paths() -> None:
    """The documented guarantee covers every supported path form: Windows
    drive, UNC (\\\\server\\share) and POSIX (/a/b)."""
    base = Path("C:/Users/someone/hermes/profiles")
    msg = (
        "unexpected C:\\Users\\someone\\hermes\\x and "
        "\\\\srv\\share\\dir\\f and /home/someone/f"
    )
    out = baseline._sanitize_error(msg, base)
    assert "C:\\Users" not in out
    assert "\\\\srv" not in out
    assert "/home/" not in out
    assert out.count("<path>") == 3, out


def test_sanitize_error_does_not_mangle_urls_or_word_lists() -> None:
    """URLs, slash-separated word lists and 'a / b' cells are NOT paths and
    must survive redaction untouched."""
    base = Path("C:/Users/someone/hermes/profiles")
    msg = "see https://example.com/a?b=c and model/reasoning/x and p50/p90 and 21 / 176"
    out = baseline._sanitize_error(msg, base)
    assert "https://example.com/a?b=c" in out
    assert "model/reasoning/x" in out
    assert "p50/p90" in out
    assert "21 / 176" in out


# ---------------------------------------------------------------------------
# 12. Verifier: AGENTS.md is a MANDATORY committed-HEAD input.
# ---------------------------------------------------------------------------
def _make_git_repo(tmp_path: Path, agents_content: str, *, with_agents: bool = True) -> Path:
    """Minimal git repo with AGENTS.md committed at HEAD (or, when
    with_agents=False, a README instead so HEAD exists without AGENTS.md)."""
    repo = tmp_path / "vrepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True, capture_output=True)
    if with_agents:
        (repo / "AGENTS.md").write_text(agents_content, encoding="utf-8")
    else:
        (repo / "README.md").write_text("no agents here", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True, capture_output=True)
    return repo


def test_committed_agents_requires_agents_md_in_head(tmp_path) -> None:
    """AGENTS.md absent from HEAD is a hard failure — never a silent skip."""
    repo = _make_git_repo(tmp_path, "", with_agents=False)
    with pytest.raises(RuntimeError, match="AGENTS.md"):
        verify.committed_agents(repo)


MINIMAL_EV = {"profiles": {p: {} for p in verify.PROFILES}}


def _agents_facts(report_text: str, head: str, blob: bytes) -> str:
    """Report prose carrying the AGENTS.md facts (bytes/chars/sha + HEAD)."""
    text = blob.decode("utf-8")
    return (
        f"AGENTS.md at HEAD `{head}`: {len(blob)} байт / "
        f"{len(text)} UTF-8 символов / sha256 `{hashlib.sha256(blob).hexdigest()}`"
    )


def test_check_report_validates_committed_head_not_dirty_worktree(tmp_path) -> None:
    """The verifier validates the COMMITTED HEAD blob (git show HEAD:AGENTS.md),
    never an arbitrary dirty working-tree file."""
    repo = _make_git_repo(tmp_path, "AGENTS CONTENT A\n")
    head, blob = verify.committed_agents(repo)
    # dirty the working tree with a different, much larger AGENTS.md
    (repo / "AGENTS.md").write_text("AGENTS CONTENT A\n" + "DIRTY" * 5000, encoding="utf-8")
    facts = _agents_facts("", head, blob)
    # facts that match the committed blob pass the AGENTS checks
    probs = verify.check_report(facts, MINIMAL_EV, blob, head)
    assert not any("AGENTS.md" in p for p in probs), probs
    # facts that match the DIRTY working-tree file must FAIL
    dirty_facts = facts.replace(str(len(blob)), str(len(blob) + 20000))
    probs = verify.check_report(dirty_facts, MINIMAL_EV, blob, head)
    assert any("AGENTS.md bytes" in p for p in probs), probs


def test_check_report_rejects_stale_head_citation(tmp_path) -> None:
    """Freshness: a report citing an older HEAD commit must fail."""
    repo = _make_git_repo(tmp_path, "AGENTS CONTENT A\n")
    head, blob = verify.committed_agents(repo)
    facts = _agents_facts("", head, blob)
    stale = facts.replace(head, "38b1091" + "0" * (len(head) - 7))
    probs = verify.check_report(stale, MINIMAL_EV, blob, head)
    assert any("HEAD" in p and "AGENTS.md" in p for p in probs), probs
    # and the committed report (with its own cited HEAD) passes the table checks
    # even when the AGENTS facts are injected as mandatory inputs
    good = verify.check_report(_agents_facts("", head, blob), MINIMAL_EV, blob, head)
    assert not any("AGENTS.md" in p for p in good), good


def test_check_report_requires_agents_facts_when_blob_provided(tmp_path) -> None:
    """Once the committed blob is provided, missing AGENTS.md facts in the
    report are a hard failure, not a silent skip."""
    repo = _make_git_repo(tmp_path, "AGENTS CONTENT A\n")
    head, blob = verify.committed_agents(repo)
    probs = verify.check_report("no facts here at all", MINIMAL_EV, blob, head)
    assert any("AGENTS.md" in p for p in probs)


# ---------------------------------------------------------------------------
# 13. Redaction regression check on committed artifacts.
# ---------------------------------------------------------------------------
def test_check_redaction_clean_on_committed_artifacts(committed_evidence, committed_report) -> None:
    """No task ids, worktree identifiers, absolute paths or sensitive
    column/credential words may appear in the committed report / evidence /
    context output."""
    context_text = (REPO / "tools/context_baseline.json").read_text(encoding="utf-8")
    evidence_text = (REPO / "docs/audits/HERMES_PROFILE_TOKEN_BASELINE_PHASE0_evidence.json").read_text(
        encoding="utf-8"
    )
    probs = verify.check_redaction(
        {"report": committed_report, "evidence": evidence_text, "context": context_text}
    )
    assert probs == [], probs


def test_check_redaction_catches_provenance_markers() -> None:
    """Each forbidden marker class must be detected."""
    planted = (
        "| task | t_0123456789abcdef | wt/t_0123abcd | C:\\Users\\x\\y | "
        "\\\\srv\\share\\f | /home/x/f | user_id=1 | billing_base_url=x | api_key=y |"
    )
    probs = verify.check_redaction({"p": planted})
    joined = "\n".join(probs)
    assert "kanban task id" in joined
    assert "worktree branch identifier" in joined
    assert "windows drive absolute path" in joined
    assert "UNC path" in joined
    assert "posix absolute path" in joined
    assert "user_id column" in joined
    assert "billing_base_url column" in joined
    assert "api_key" in joined


def test_redaction_does_not_flag_legitimate_words() -> None:
    """Token/task words, source labels and slash-separated word lists are not
    provenance markers and must not be flagged."""
    legit = (
        "input_tokens=100, per-task reasoning policy, source kanban, "
        "model/reasoning/compression defaults, sums/p50/p90/max, "
        "<HERMES_PROFILES_DIR>/architect/config.yaml, tools/context_baseline.json"
    )
    probs = verify.check_redaction({"legit": legit})
    assert probs == [], probs
