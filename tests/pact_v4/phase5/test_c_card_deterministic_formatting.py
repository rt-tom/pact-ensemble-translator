"""Card C validation on the frozen chapter-0001 whole-chapter artifacts.

Runs the model-free formatting (``run_formatting_align`` with no caller)
over the frozen whole-chapter translation (``0001_bonds-1-1.ru.html`` — the
artifact the audit measured as holding ``<em>`` 101/101) and asserts the
card-C invariants:

  * 0 model calls (model_fallback_count == model_call_count == 0);
  * every already-present inline span resolves through the ``preserved``
    tier without a model (the whole-chapter case);
  * any span the deterministic tiers cannot locate is a blocking incident
    (debt), never a silent loss.

The external artifacts (the chapter 0001 source HTML and the independent
whole-chapter translation) are not part of the repository — they live on a
development machine. Point at them with the environment variables
``PACT_C_CHAPTER_HTML`` and ``PACT_C_INDEPENDENT_HTML``; the whole module is
skipped when either variable is unset or points at a missing path.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from pact_v4.phase0b.source_html import parse_source_html
from pact_v4.phase5.formatting import TIER_PRESERVED, run_formatting_align

PACT_C_CHAPTER_HTML_ENV = "PACT_C_CHAPTER_HTML"
PACT_C_INDEPENDENT_HTML_ENV = "PACT_C_INDEPENDENT_HTML"


def _resolve_external_paths() -> tuple[Path, Path] | None:
    """Resolve chapter 0001 + its whole-chapter translation from the env.

    Returns ``(chapter_html, independent_html)`` when both env vars are set
    and point at existing files; returns ``None`` (skip) otherwise.
    """
    chapter = os.environ.get(PACT_C_CHAPTER_HTML_ENV)
    indep = os.environ.get(PACT_C_INDEPENDENT_HTML_ENV)
    if not chapter or not indep:
        return None
    chapter_path, indep_path = Path(chapter), Path(indep)
    if not chapter_path.is_file() or not indep_path.is_file():
        return None
    return chapter_path, indep_path


_EXTERNAL = _resolve_external_paths()
_CHAPTER_HTML, _INDEPENDENT_HTML = _EXTERNAL or (None, None)

pytestmark = pytest.mark.skipif(
    _EXTERNAL is None,
    reason=(
        "set PACT_C_CHAPTER_HTML and PACT_C_INDEPENDENT_HTML to the chapter "
        "0001 source + its whole-chapter translation (they are not part of "
        "the repository)"
    ),
)


def test_card_c_formatting_is_model_free_on_chapter_0001():
    assert _CHAPTER_HTML is not None and _INDEPENDENT_HTML is not None
    blocks = parse_source_html(_CHAPTER_HTML.read_text(encoding="utf-8"))

    # Reconstruct the whole-chapter translation's per-PID text with the
    # inline markup intact (the .ru.html mirrors the source block structure).
    import re

    body = re.search(
        r"<body>(.*)</body>", _INDEPENDENT_HTML.read_text(encoding="utf-8"), re.S
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

    source_spans = sum(len(b.inline_spans) for b in blocks)
    assert source_spans > 0

    out = run_formatting_align(
        blocks=blocks, translation=raw_by_pid,
        backend_identity_hash="x" * 32,
    )

    # Card C: formatting = 0 model calls, always.
    assert out.model_call_count == 0
    assert out.model_fallback_count == 0
    # The whole-chapter translation already carries the emphasis inline, so
    # the preserved tier resolves ~all of them deterministically.
    assert out.resolved_count >= source_spans - 2, (
        f"expected ~0 unresolved on chapter 0001, got {out.incident_count} "
        "incidents"
    )
    assert all(r.tier == TIER_PRESERVED for r in out.span_mapping)
    # Any residual unresolved span is debt (blocking incident), never a
    # silent loss.
    assert out.incident_count == len(out.incidents)
    if out.incidents:
        assert out.blocking
        assert all(i.required for i in out.incidents)


def test_card_c_module_skip_reason_documents_the_env_vars():
    reasons = []
    markers = pytestmark if isinstance(pytestmark, list) else [pytestmark]
    for marker in markers:
        if getattr(marker, "markname", "") == "skipif" and "reason" in marker.kwargs:
            reasons.append(marker.kwargs["reason"])
    text = " ".join(reasons)
    assert PACT_C_CHAPTER_HTML_ENV in text
    assert PACT_C_INDEPENDENT_HTML_ENV in text
