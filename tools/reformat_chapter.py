#!/usr/bin/env python3
"""Standalone Phase-5 formatting recovery for a single chapter.

Runs the v4.1 formatting model-call + deterministic wrap OUTSIDE the book-run
pipeline, against any OpenAI-compatible server (e.g. ``opencode serve``).

Inputs (both already on disk after a normal run):
  * the English source HTML (carries ``inline_spans`` / ``<em>``)
  * the existing plain ``translations.json`` (no tags)

It resolves ``target_text`` via the model, applies ``<em>``/``<strong>``/...``
wrappers deterministically, and writes a formatted translations file +
``formatting_report.json``. Translation is NOT regenerated.

Usage (run from a checkout that has the formatting fix, e.g. the
``work/v41-formatting-reasoning-fix`` worktree):

    python tools/reformat_chapter.py \
        --source-html D:\\pact\\pact_chapters\\0031_collateral-4-8.html \
        --translations "D:\\pact\\gate_bench_runs\\book_0031-...\\chapter_0031_collateral-4-8\\translations.json" \
        --base-url http://127.0.0.1:8000/v1 \
        --model <opencode-model-ref> \
        --api-key sk-local \
        --out translations.formatted.json

By default the formatted file is written next to the input as
``<stem>.formatted.json`` (non-destructive). Use ``--apply`` to overwrite the
input ``translations.json`` in place (then re-render the book and re-promote).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

# Make repo root importable when run as a script.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pact_v4.phase0b.source_html import parse_source_html  # noqa: E402
from pact_v4.phase5 import formatting as F  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal OpenAI-compatible client (no heavy backend deps)
# ---------------------------------------------------------------------------


@dataclass
class _Gen:
    content: str
    text: str
    reasoning: str = ""
    reasoning_content: str = ""
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    response_format_attempted: bool = True
    raw_metadata: Dict[str, Any] = field(default_factory=dict)


class OpenAIBackendClient:
    """Talks to an OpenAI-compatible ``/v1/chat/completions`` endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.1,
        timeout: int = 900,
        response_format: str = "json_object",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.response_format = response_format

    def complete(self, messages, cfg, max_tokens, label=None):  # noqa: ANN001
        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": [
                {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
                for m in messages
            ],
            "temperature": float(cfg.get("temperature", self.temperature)),
            "max_tokens": int(max_tokens),
            "stream": False,
            "response_format": {"type": self.response_format},
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"request to {url} failed: {e}") from e

        choice = (payload.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
        finish = choice.get("finish_reason") or ""
        usage = payload.get("usage") or {}
        return _Gen(
            content=content,
            text=content,
            reasoning=reasoning,
            reasoning_content=reasoning,
            finish_reason=finish,
            usage=usage,
            response_format_attempted=True,
            raw_metadata={
                "response_format_attempted": True,
                "usage": usage,
                "finish_reason": finish,
            },
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Standalone Phase-5 formatting recovery for one chapter.",
    )
    p.add_argument("--source-html", required=True,
                   help="English source HTML with inline_spans/<em>")
    p.add_argument("--translations", required=True,
                   help="Existing plain translations.json (no tags)")
    p.add_argument("--out", default=None,
                   help="Output formatted translations.json "
                        "(default: <translations>.formatted.json)")
    p.add_argument("--base-url",
                   default=os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:8000/v1"),
                   help="OpenAI-compatible base URL (opencode serve: http://127.0.0.1:8000/v1)")
    p.add_argument("--api-key", default=os.environ.get("OPENCODE_API_KEY", "sk-local"),
                   help="API key for the endpoint (opencode serve often accepts any)")
    p.add_argument("--model", required=True, help="Model ref, e.g. the opencode model id")
    p.add_argument("--temperature", type=float, default=0.1)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--max-blocks-per-call", type=int, default=999,
                   help="High value => single call for the whole chapter")
    p.add_argument("--max-tokens-override", type=int, default=None,
                   help="Force max_tokens (else dynamic 40*spans+500, min 800, cap 8192)")
    p.add_argument("--single-call", action="store_true", default=True,
                   help="Single-call whole chapter (default on)")
    p.add_argument("--no-single-call", dest="single_call", action="store_false")
    p.add_argument("--diag-dir", default=None,
                   help="Where to write per-call diagnostics "
                        "(default: <out>.diag/)")
    p.add_argument("--max-formatting-incidents", type=int, default=999,
                   help="Lenient debt limit (non-blocking)")
    p.add_argument("--apply", action="store_true",
                   help="Overwrite the input translations.json in place")
    args = p.parse_args(argv)

    out_path = (
        Path(args.translations)
        if args.apply
        else (Path(args.out) if args.out else
              Path(args.translations).with_name(Path(args.translations).stem + ".formatted.json"))
    ).resolve()
    diag_dir = (
        Path(args.diag_dir)
        if args.diag_dir
        else out_path.parent / (out_path.stem + ".diag")
    )
    diag_dir.mkdir(parents=True, exist_ok=True)
    if args.apply:
        print(f"[reformat] --apply: will OVERWRITE {out_path}", file=sys.stderr)

    # Load inputs.
    html_text = Path(args.source_html).read_text(encoding="utf-8-sig", errors="replace")
    blocks = parse_source_html(html_text)
    translations = json.loads(Path(args.translations).read_text(encoding="utf-8"))

    fmt_cfg = {
        "enabled": True,
        "max_blocks_per_call": args.max_blocks_per_call,
        "generation_retries": args.retries,
        "temperature": args.temperature,
        "formatting_single_call_whole_chapter": args.single_call,
        "max_tokens": args.max_tokens_override,
    }

    client = OpenAIBackendClient(
        base_url=args.base_url,
        api_key=args.api_key,
        model=args.model,
        temperature=args.temperature,
    )

    n_spans = sum(len(b.inline_spans) for b in blocks)
    print(
        f"[reformat] blocks={len(blocks)} "
        f"pids_with_spans={sum(1 for b in blocks if b.inline_spans)} "
        f"spans={n_spans} translations={len(translations)} "
        f"single_call={args.single_call}",
        file=sys.stderr,
    )

    mappings = F.resolve_format_mappings(
        client, fmt_cfg, blocks, translations, out_dir=diag_dir,
    )
    print(f"[reformat] resolved mappings: {len(mappings)}", file=sys.stderr)

    outcome = F.run_formatting_align(
        blocks=blocks,
        translation=translations,
        mappings=mappings,
        backend_identity_hash="manual-opencode-serve",
        policy_version=F.FORMATTING_POLICY_VERSION,
        max_formatting_incidents=args.max_formatting_incidents,
    )

    formatted = dict(outcome.formatted_text)
    out_path.write_text(json.dumps(formatted, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = out_path.parent / "formatting_report.json"
    report_path.write_text(json.dumps(outcome.to_payload(), ensure_ascii=False, indent=2), encoding="utf-8")

    em_count = sum(t.count("<em>") for t in formatted.values())
    print(f"[reformat] wrote {out_path}", file=sys.stderr)
    print(f"[reformat] wrote {report_path}", file=sys.stderr)
    print(
        f"[reformat] resolved={outcome.resolved_count} "
        f"incidents={outcome.incident_count} blocking={outcome.blocking} "
        f"em_in_output={em_count}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
