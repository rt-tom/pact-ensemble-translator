"""V4 C3 live one-chunk smoke (owner-run, plan §14.5).

Runs the real strict driver on a small synthetic chapter (1 low-risk chunk +
1 high-risk A/B chunk) against a ``--runtime-config`` backend profile, then
checks usage/provenance/secrets and prints request/session IDs. This is a
**paid** run when the profile is remote/composite: the chapter text is sent
to the remote provider configured in the profile (plan §12). Confirm the
acknowledgement below before any model call is made.

This is NOT a quality benchmark (plan §14.5) and not the chapter_046 trial;
it is the mechanical transport/provenance check that unlocks that trial.

Usage (PowerShell, from the repo root):

    python _smoke_c3.py --config configs/runtime_remote.example.yaml
    python _smoke_c3.py --config configs/runtime_composite.example.yaml --managed

Options:
    --config FILE   runtime profile (default configs/runtime_remote.example.yaml)
    --managed       force Pact-managed `opencode serve` (server_mode=managed)
    --out-dir DIR   where the run artifacts go (default: temp dir; use a
                    persistent dir to compare with chapter_046 metrics)
    --yes           skip the interactive acknowledgement (automation only)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional, Sequence

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictRunConfig,
    run_chapter_strict,
)
from pact_v4.runtime.runtime_config import build_role_adapters

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pact_full_pipeline_runner_v1.v4_phase12_strict_run import (  # noqa: E402
    _load_runtime_config_file,
    _warn_remote_acknowledgement,
    force_managed,
)

DEFAULT_CONFIG = ROOT / "configs" / "runtime_remote.example.yaml"

# 18x35 words = 630 words fills one 640-word-cap chunk entirely with the
# low-risk "word0.." text (same fixture shape as the strict-runner tests),
# so the "box 7" section cannot spill back into it; the 14x35-word "box 7"
# section then forms a second, MEDIUM A/B chunk (same trigger content the
# runner tests use for an A/B preference call).
LOW_PARAGRAPHS = 18
HIGH_PARAGRAPHS = 14
LOW_WORDS = 35
HIGH_WORDS = 35


def _write_chapter(dir_path: Path) -> Path:
    body_parts: list = []
    for _ in range(LOW_PARAGRAPHS):
        words = [f"word{i}" for i in range(LOW_WORDS)]
        body_parts.append(f"<p>{' '.join(words)}</p>")
    high_sentence = ("You must not open box 7. " * (HIGH_WORDS // 5)).strip()
    for _ in range(HIGH_PARAGRAPHS):
        body_parts.append(f"<p>{high_sentence}</p>")
    html = dir_path / "smoke_chapter.html"
    html.write_text("<html><body>" + "".join(body_parts) + "</body></html>", encoding="utf-8")
    return html


def _write_empty_memory(dir_path: Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "glossary.json").write_text("{}", encoding="utf-8")
    (dir_path / "book_memory.json").write_text("{}", encoding="utf-8")


def _confirm_remote(cfg: Any, *, skip: bool) -> None:
    """Plan §12 acknowledgement: text will be sent to the remote provider."""
    _warn_remote_acknowledgement(cfg)
    if skip:
        return
    try:
        answer = input(
            "Type 'yes' to send the chapter text to the remote provider "
            "configured in the profile, or anything else to abort: "
        )
    except EOFError:
        answer = ""
    if answer.strip().lower() != "yes":
        print("Aborted before any model call.")
        raise SystemExit(3)


def _secret_values_to_scan(cfg: Any) -> list:
    """Env var values the profile might resolve to basic-auth credentials."""
    refs: list = []
    stack = [cfg]
    while stack:
        cur = stack.pop()
        if hasattr(cur, "server"):
            refs.append(cur.server.username_env)
            refs.append(cur.server.password_env)
        if hasattr(cur, "backends"):
            stack.extend(cur.backends.values())
    values = []
    for name in refs:
        if name:
            value = os.environ.get(name)
            if value:
                values.append(value)
    return values


def _scan_secrets(blob: str, secrets: Sequence[str], where: str) -> bool:
    found = [s for s in secrets if s and s in blob]
    if found:
        print(f"SECRETS FAIL: {len(found)} credential value(s) found in {where}")
        return False
    print(f"secrets: OK ({len(secrets)} env credential value(s) checked, "
          f"none present in {where})")
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--managed", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--yes", action="store_true",
                        help="skip the interactive §12 acknowledgement")
    args = parser.parse_args(argv)

    cfg = _load_runtime_config_file(args.config)
    if args.managed:
        cfg = force_managed(cfg)
    _confirm_remote(cfg, skip=args.yes)

    out_dir = args.out_dir or Path(tempfile.mkdtemp(prefix="v4_c3_smoke_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="v4_c3_smoke_work_"))
    chapter_html = _write_chapter(work)
    memory_dir = work / "memory"
    _write_empty_memory(memory_dir)

    run_cfg = StrictRunConfig(
        chapter_id="smoke_c3",
        chapter_html_path=chapter_html,
        memory_dir=memory_dir,
        out_dir=out_dir,
        backend=cfg,
        run_label="v4-c3-live-smoke",
    )

    print(f"\n== profile ==")
    print(json.dumps(cfg.public_record(), ensure_ascii=False, indent=2))
    print(f"\n== build runtime (this may start a managed opencode serve) ==")
    runtime = cfg.build_runtime(log_dir=out_dir / "server_logs")
    model_caller, qwen_evaluator, gemma_selector, \
        qwen_audit_evaluator, gemma_audit_evaluator = build_role_adapters(cfg, runtime)

    print(f"\n== run strict driver on synthetic chapter (paid calls follow) ==")
    result = run_chapter_strict(
        run_cfg, runtime=runtime, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=qwen_audit_evaluator,
        gemma_audit_evaluator=gemma_audit_evaluator,
    )

    print(f"\n== run result ==")
    print(f"chunks: total={result.chunk_count} processed={result.processed_count}")
    print(f"selected={result.selected_count} quarantined={result.quarantined_count} "
          f"needs_synthesis={result.needs_synthesis_count} "
          f"incomplete_generation={result.incomplete_generation_count}")
    print(f"halted_early={result.halted_early} wall_clock={result.record['wall_clock_seconds']:.1f}s")
    print(f"step6={result.step6.get('status')}")

    remote_calls = result.record["runtime"].get("remote_calls")
    print(f"\n== remote_calls (usage/provenance) ==")
    print(json.dumps(remote_calls, ensure_ascii=False, indent=2))

    print(f"\n== per-call request/session IDs (from runtime backend events) ==")
    events = runtime.events_since(0)
    for event in events:
        if event.kind == "remote_call":
            print(
                f"{event.label}: model={event.model_ref} req={event.request_id} "
                f"ses={event.session_id} finish={event.finish_reason} "
                f"retries={event.retry_count} usage={event.usage}"
            )

    print(f"\n== secrets scan ==")
    secrets = _secret_values_to_scan(cfg)
    artifacts_blob = ""
    for path in (
        result.record_path,
        out_dir / "strict_chapter_trial_record.json",
        out_dir / "chunk_plan.json",
        out_dir / "generation_outcomes.json",
        out_dir / "selection_results.json",
    ):
        if path.exists():
            artifacts_blob += path.read_text(encoding="utf-8", errors="replace")
    ok = _scan_secrets(artifacts_blob, secrets, "run artifacts")
    record = json.loads(result.record_path.read_text(encoding="utf-8"))
    backend_blob = json.dumps(record["backend"], ensure_ascii=False)
    ok = _scan_secrets(backend_blob, secrets, "record backend block") and ok

    print(f"\n== artifacts ==")
    print(f"record: {result.record_path}")
    print(f"journal: {result.journal_path}")
    print(f"out-dir: {out_dir}")
    print(f"\n== SMOKE {'PASS' if ok else 'FAIL'} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
