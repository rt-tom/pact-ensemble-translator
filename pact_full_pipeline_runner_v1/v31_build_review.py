#!/usr/bin/env python3
"""Build a compact human-readable review report for Pact v3.1 runs."""
from __future__ import annotations

import argparse
import html
from collections import Counter, defaultdict
from pathlib import Path

from v31_common import (
    VERSION, add_common_args, load_cfg, load_manifest, load_runtime, norm,
    read_json, selected_chapters, setup_logging, write_json,
)


def esc(value):
    return html.escape(str(value or ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser, include_pass=False)
    args = parser.parse_args()
    setup_logging()
    runtime = load_runtime(args.project_root.resolve())
    cfg = load_cfg(runtime, args.config.resolve())

    for source_path, work in selected_chapters(runtime, cfg, args.start, args.end):
        _, blocks, block_map = load_manifest(work)
        draft = read_json(work / "draft_translations.json", {})
        primary = read_json(work / "v31_primary_translations.json", draft)
        final = read_json(work / "v31_final_translations.json", primary)
        lifecycle = read_json(work / "issue_lifecycle.json", [])
        by_pid = defaultdict(list)
        for row in lifecycle:
            by_pid[row.get("pid")].append(row)
        changed = [pid for pid in final if draft.get(pid) != final.get(pid)]
        status_counts = Counter(row.get("status") for row in lifecycle)
        detector_counts = Counter()
        for row in lifecycle:
            for detector in row.get("detected_by") or []:
                detector_counts[detector] += 1

        rows = []
        for pid in changed:
            source = norm(block_map[pid].get("source_text"))
            issue_html = "".join(
                f"<li><code>{esc(x.get('issue_id'))}</code> {esc(x.get('status'))} — {esc(', '.join(x.get('detected_by') or []))}</li>"
                for x in by_pid.get(pid, [])
            ) or "<li>No lifecycle record</li>"
            rows.append(f"""
<section class="change">
<h2>{esc(pid)}</h2>
<div class="label">EN</div><p>{esc(source)}</p>
<div class="label">Draft RU</div><p>{esc(draft.get(pid,''))}</p>
<div class="label">After primary pass</div><p>{esc(primary.get(pid,''))}</p>
<div class="label">Final RU</div><p>{esc(final.get(pid,''))}</p>
<div class="label">Issues</div><ul>{issue_html}</ul>
</section>""")

        summary = {
            "version": VERSION,
            "chapter": source_path.name,
            "blocks": len(blocks),
            "changed_pids": len(changed),
            "lifecycle": dict(status_counts),
            "detectors": dict(detector_counts),
            "quality_gate": read_json(work / "v31_quality_gate.json", {}),
        }
        out_dir = work / "review_comparison_v31"
        out_dir.mkdir(parents=True, exist_ok=True)
        write_json(out_dir / "summary.json", summary)
        doc = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>{esc(source_path.name)} v3.1 review</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;line-height:1.55}}
.metrics{{display:flex;gap:1rem;flex-wrap:wrap}}.metric{{border:1px solid #bbb;border-radius:.6rem;padding:.7rem 1rem}}
.change{{border-top:1px solid #bbb;padding:1rem 0}}.label{{font-size:.8rem;text-transform:uppercase;color:#666;font-weight:700}}
p{{white-space:pre-wrap}}code{{background:#eee;padding:.1rem .25rem;border-radius:.2rem}}
</style></head><body>
<h1>{esc(source_path.name)} — Pact pipeline v3.1</h1>
<div class="metrics"><div class="metric">Blocks: {len(blocks)}</div><div class="metric">Changed PIDs: {len(changed)}</div><div class="metric">Issues: {len(lifecycle)}</div><div class="metric">Quality: {esc(summary['quality_gate'].get('ok'))}</div></div>
<h2>Lifecycle</h2><pre>{esc(dict(status_counts))}</pre>
<h2>Detector contribution</h2><pre>{esc(dict(detector_counts))}</pre>
{''.join(rows) if rows else '<p>No changed paragraphs.</p>'}
</body></html>"""
        (out_dir / "index.html").write_text(doc, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
