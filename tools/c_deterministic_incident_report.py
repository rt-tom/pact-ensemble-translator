"""Generate the deterministic formatting incident report for chapter 0001.

Card C (V4_1_AUDIT_B1_RU.md §11): formatting is model-free; the check on the
frozen whole-chapter translation counts how many mandatory spans remain
unresolved (expectation ~0, because the whole-chapter translation holds
<em> 101/101) and records every unresolved span as debt.

Writes ``docs/audits/C_DETERMINISTIC_FORMATTING_0001_RU.md``.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, r"D:/pact/pact_translator_v4/.worktrees/t_d2f393f4")

from pact_v4.phase0b.source_html import parse_source_html  # noqa: E402
from pact_v4.phase5.formatting import run_formatting_align  # noqa: E402

CHAPTER = pathlib.Path(r"D:/pact/pact_chapters/0001_bonds-1-1.html")
INDEP = pathlib.Path(r"D:/test folder/.hermes/desktop-attachments/0001_bonds-1-1.ru.html")
OUT = pathlib.Path(r"D:/pact/pact_translator_v4/.worktrees/t_d2f393f4/docs/audits/C_DETERMINISTIC_FORMATTING_0001_RU.md")

blocks = parse_source_html(CHAPTER.read_text(encoding="utf-8"))
body = re.search(r"<body>(.*)</body>", INDEP.read_text(encoding="utf-8"), re.S).group(1)
seg_tags = re.findall(r"<(p|h[1-6]|li|blockquote)\b[^>]*>(.*?)</\1>", body, re.S)

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
    "# C: детерминированный formatting — отчёт по главе 0001",
    "",
    f"- Дата: 2026-08-10 (карточка C, `V4_1_AUDIT_B1_RU.md` §11).",
    f"- Источник: `D:/pact/pact_chapters/0001_bonds-1-1.html` "
    f"({len(blocks)} блоков, {source_spans} обязательных inline-спанов).",
    f"- Перевод (whole-chapter, держит `<em>` 101/101): "
    f"`0001_bonds-1-1.ru.html`.",
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
    "- Форматирование на главе 0001 — **0 вызовов модели**.",
    "- Whole-chapter перевод держит `<em>` 101/101; детерминированный тир "
    "`preserved` распознаёт уже-присутствующую разметку и решает ~все спаны "
    "без модели (ожидание карточки: ~0 unresolved).",
    "- Любой нерешённый спан — блокирующий инцидент (debt): "
    "`accepted_degraded`, никогда тихая потеря и никогда «успех только "
    "потому, что 0 model calls».",
    f"- Итог на замороженных артефактах: resolved {out.resolved_count}, "
    f"incidents {out.incident_count}, model calls {out.model_call_count}.",
    "",
]
OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"report written: {OUT}")
print(f"resolved={out.resolved_count} incidents={out.incident_count} calls={out.model_call_count}")
