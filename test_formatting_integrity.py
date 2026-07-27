"""Regression coverage for authoritative inline-formatting incidents."""
import copy
import html
import tempfile
import unittest
from pathlib import Path

import pact_translate_v3 as runtime


def fixture():
    cfg = copy.deepcopy(runtime.DEFAULTS)
    cfg["html"]["remove_data_pid"] = True
    body = "".join(
        f"<p>paragraph {index} <em>focus {index}</em></p>"
        for index in range(1, 59)
    )
    normalized, blocks = runtime.prepare_html(f"<html><body>{body}</body></html>", cfg)
    translations = {block.pid: f"абзац {block.index + 1} акцент {block.index + 1}" for block in blocks}
    return cfg, normalized, blocks, translations


def mapped_inner(block, translation):
    span = block.inline_spans[0]
    target = f"акцент {block.index + 1}"
    return runtime.apply_inline_mappings(
        translation, block.inline_spans,
        {(block.pid, span.span_id): {"target_text": target, "occurrence": 1}},
        block.pid,
    )[0]


class FormattingIntegrityTests(unittest.TestCase):
    def render(self, cfg, normalized, blocks, translations, formatted):
        return runtime.build_final_html(normalized, blocks, translations, formatted, cfg, "fixture.html")

    def test_p00020_and_p00058_missing_required_spans_block(self):
        cfg, normalized, blocks, translations = fixture()
        formatted = {pid: html.escape(text) for pid, text in translations.items()}
        incidents = []
        for pid in ("p00020", "p00058"):
            block = next(item for item in blocks if item.pid == pid)
            _, found = runtime.apply_inline_mappings(translations[pid], block.inline_spans, {}, pid)
            incidents.extend(found)
        integrity = runtime.final_integrity(self.render(cfg, normalized, blocks, translations, formatted), blocks, translations, incidents, cfg)
        self.assertFalse(integrity["ok"])
        self.assertEqual(2, integrity["formatting_incident_counts"]["unresolved_required"])

    def test_flattened_fallback_and_partial_failure_are_incidents(self):
        cfg, normalized, blocks, translations = fixture()
        formatted = {pid: html.escape(text) for pid, text in translations.items()}
        p20 = next(item for item in blocks if item.pid == "p00020")
        p58 = next(item for item in blocks if item.pid == "p00058")
        formatted[p20.pid] = mapped_inner(p20, translations[p20.pid])
        _, incidents = runtime.apply_inline_mappings(translations[p58.pid], p58.inline_spans, {}, p58.pid)
        integrity = runtime.final_integrity(self.render(cfg, normalized, blocks, translations, formatted), blocks, translations, incidents, cfg)
        self.assertFalse(integrity["ok"])
        self.assertTrue(any(item["severity"] == "blocking" for item in incidents))

    def test_failed_batch_persists_required_incidents_instead_of_silent_flattening(self):
        class EmptyMappingsClient:
            calls = []
            cfg = {"context_size": 4096, "context_safety_margin": 64}

            def token_count(self, _messages, _stage):
                return 1

            def complete(self, *_args):
                self.calls.append({"result": "green-json"})
                return runtime.Generation("{\"mappings\": []}", "", "stop", {}, {}, 0.0)

        cfg, _normalized, blocks, translations = fixture()
        cfg["formatting"]["max_blocks_per_call"] = 100
        client = EmptyMappingsClient()
        with tempfile.TemporaryDirectory() as directory:
            formatted, incidents = runtime.run_formatting(
                client, cfg, blocks, {block.pid: block for block in blocks}, translations, Path(directory)
            )
        self.assertEqual(html.escape(translations["p00020"]), formatted["p00020"])
        self.assertEqual({"p00020", "p00058"}, {item["pid"] for item in incidents if item["pid"] in {"p00020", "p00058"}})
        self.assertTrue(all(item["severity"] == "blocking" for item in incidents))

    def test_successful_restoration_and_resolved_incident_can_complete(self):
        cfg, normalized, blocks, translations = fixture()
        formatted = {pid: mapped_inner(block, translations[pid]) for pid, block in ((b.pid, b) for b in blocks)}
        resolved = [{"pid": "p00020", "span_id": "em01", "status": "resolved", "required": True, "resolution": "retry_restored"}]
        integrity = runtime.final_integrity(self.render(cfg, normalized, blocks, translations, formatted), blocks, translations, resolved, cfg)
        self.assertTrue(integrity["ok"], integrity["errors"])
        self.assertEqual(1, integrity["formatting_incident_counts"]["resolved"])

    def test_green_json_cannot_hide_broken_final_html(self):
        cfg, normalized, blocks, translations = fixture()
        flattened = {pid: html.escape(text) for pid, text in translations.items()}
        integrity = runtime.final_integrity(self.render(cfg, normalized, blocks, translations, flattened), blocks, translations, [], cfg)
        self.assertFalse(integrity["ok"])
        self.assertTrue(any("required em spans missing" in error for error in integrity["errors"]))

    def test_optional_span_remains_a_warning(self):
        cfg, normalized, blocks, translations = fixture()
        cfg["formatting"]["optional_tags"] = ["em"]
        _, optional_blocks = runtime.prepare_html("<p>x <em>y</em></p>", cfg)
        block = optional_blocks[0]
        _, incidents = runtime.apply_inline_mappings("x y", block.inline_spans, {}, block.pid)
        self.assertFalse(incidents[0]["required"])
        self.assertEqual("warning", incidents[0]["severity"])


if __name__ == "__main__":
    unittest.main()
