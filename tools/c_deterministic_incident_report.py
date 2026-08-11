"""Generate the deterministic formatting incident report for a chapter.

Card C (V4_1_AUDIT_B1_RU.md §11): formatting is model-free; the check on a
frozen whole-chapter translation counts how many mandatory inline spans
remain unresolved and records every unresolved span as debt.

Portable CLI — the input/output paths are passed as arguments, never
hardcoded; importing this module performs no work (all report generation
happens inside ``main()`` / ``generate_report()``):

    python tools/c_deterministic_incident_report.py \
        --chapter-html <source chapter html> \
        --independent-html <whole-chapter translation html> \
        --out <output markdown path>
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from collections import Counter
from datetime import date
from typing import Optional, Sequence

# Repository root, resolved from this file's own location — portable across
# branches/clones/worktrees (never a machine- or worktree-specific path).
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Card C deterministic-formatting incident report: run the "
            "model-free formatting alignment over a frozen whole-chapter "
            "translation and write the resolved/debt report as markdown."
        ),
    )
    parser.add_argument(
        "--chapter-html", type=pathlib.Path, required=True,
        help="Source chapter HTML (the chapter whose inline spans are restored).",
    )
    parser.add_argument(
        "--independent-html", type=pathlib.Path, required=True,
        help="Whole-chapter translation HTML (holds the inline markup).",
    )
    parser.add_argument(
        "--out", type=pathlib.Path, required=True,
        help="Output markdown report path.",
    )
    return parser


def generate_report(
    *,
    chapter_html: pathlib.Path,
    independent_html: pathlib.Path,
) -> tuple[str, int, int, int]:
    """Run the model-free formatting alignment and render the markdown report.

    ``chapter_html`` is the source chapter; ``independent_html`` is the
    whole-chapter translation that already carries the inline markup. The
    report has the same shape as the card-C audit: resolved/incident counts,
    tier histogram, incident reasons, and a per-incident table.

    Returns ``(report_text, resolved_count, incident_count, model_call_count)``
    so callers can print the same summary line as the card-C audit.
    """
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from pact_v4.phase0b.source_html import parse_source_html  # noqa: E402
    from pact_v4.phase5.formatting import run_formatting_align  # noqa: E402

    blocks = parse_source_html(chapter_html.read_text(encoding="utf-8"))
    body = re.search(
        r"<body>(.*)</body>", independent_html.read_text(encoding="utf-8"), re.S
    ).group(1)
    seg_tags = re.findall(
        r"<(p|h[1-6]|li|blockquote)\b[^>]*>(.*?)</\1>", body, re.S
    )

    raw_by_pid: dict[str, str] = {}
    j = 0
    for block in blocks:
        while j < len(seg_tags) and seg_tags[j][0] != block.tag:
            j += 1
        if j < len(seg_tags):
            raw_by_pid[block.pid] = seg_tags[j][1]
            j += 1

    out = run_formatting_align(
        blocks=blocks, translation=raw_by_pid, backend_identity_hash="x" * 32,
    )

    source_spans = sum(len(b.inline_spans) for b in blocks)
    tiers = Counter(r.tier for r in out.span_mapping)
    by_reason = Counter(i.reason for i in out.incidents)

    lines = [
        "# C: детерминированный formatting — отчёт по главе",
        "",
        f"- Дата: {date.today().isoformat()} (карточка C, "
        "`V4_1_AUDIT_B1_RU.md` §11).",
        f"- Источник: `{chapter_html}` "
        f"({len(blocks)} блоков, {source_spans} обязательных inline-спанов).",
        f"- Перевод (whole-chapter, держит inline-разметку): "
        f"`{independent_html}`.",
        f"- Режим: **model-free** — `run_formatting_align` без caller "
        f"(правило «formatting = 0 model calls»).",
        "",
        "## Результат",
        "",
        f"- resolved: **{out.resolved_count}** / {source_spans} "
        f"({100.0 * out.resolved_count / max(source_spans, 1):.1f}%)",
        f"- incidents (unresolved, blocking, debt): **{out.incident_count}**",
        f"- blocking: `{out.blocking}` (max_formatting_incidents=0)",
        f"- model_call_count: **{out.model_call_count}**",
        f"- model_fallback_count: **{out.model_fallback_count}**",
        f"- тиры: `{dict(tiers)}`",
        f"- причины инцидентов: `{dict(by_reason)}`",
        "",
        "## Инциденты (unresolved → debt, не тихая потеря)",
        "",
    ]
    if out.incidents:
        lines.append("| PID | span | tier | reason | перевод (фрагмент) |")
        lines.append("|---|---|---|---|---|")
        for inc in sorted(out.incidents, key=lambda i: (i.pid, i.span_id)):
            frag = raw_by_pid.get(inc.pid, "")[:60].replace("|", "\\|")
            lines.append(
                f"| {inc.pid} | {inc.span_id} | {inc.tier} | {inc.reason} | {frag} |"
            )
    else:
        lines.append("Нет — все обязательные spans решены детерминированно.")
    lines += [
        "",
        "## Вывод",
        "",
        "- Форматирование — **0 вызовов модели**.",
        "- Whole-chapter перевод держит inline-разметку; детерминированный "
        "тир `preserved` распознаёт уже-присутствующую разметку и решает "
        "спаны без модели. Любой count/order mismatch существующей "
        "разметки — блокирующий debt, не claim и не повторная обёртка.",
        "- Любой нерешённый спан — блокирующий инцидент (debt): "
        "`accepted_degraded`, никогда тихая потеря и никогда «успех только "
        "потому, что 0 model calls».",
        f"- Итог: resolved {out.resolved_count}, "
        f"incidents {out.incident_count}, model calls {out.model_call_count}.",
        "",
    ]
    return "\n".join(lines), out.resolved_count, out.incident_count, out.model_call_count


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    report, resolved, incidents, calls = generate_report(
        chapter_html=args.chapter_html, independent_html=args.independent_html,
    )
    args.out.write_text(report, encoding="utf-8")
    print(f"report written: {args.out}")
    print(f"resolved={resolved} incidents={incidents} calls={calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
