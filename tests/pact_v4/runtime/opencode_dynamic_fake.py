"""Dynamic in-process stand-in for ``opencode serve`` (pipeline e2e).

Unlike the queue-scripted ``FakeOpenCodeServer`` (contract tests), this
responds to each ``POST /session/{id}/message`` by *content*: it recognizes
the four Pact prompt families (Phase 2B generation, Qwen fidelity/audit,
Gemma preference/audit) and returns the same canned behaviour the strict
driver's stub callers use, so ``run_chapter_strict`` can run a whole
chapter over the real ``OpenCodeServerBackend`` wire contract without a
paid call or a pre-scripted queue.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict

from tests.pact_v4.runtime.opencode_fake_server import (
    FakeOpenCodeServer,
    FakeResponse,
    _match_message_path,
    _path_of,
    _session_id_of,
)

_OWNED_SOURCE_BLOCK = re.compile(
    r"OWNED_SOURCE \(translate exactly these PIDs, in this order\):\n"
    r"(.*?)\nleft_context", re.DOTALL,
)
_PID_LINE = re.compile(r"^  (\S+): (.*)$", re.MULTILINE)
_CANDIDATE_ID = re.compile(r"candidate_id=([^\s\)]+)")

_FORMAT_PID = re.compile(r"FORMAT_PID: (\S+)")
_FORMAT_TRANSLATION = re.compile(r"^TRANSLATION: (.*)$", re.MULTILINE)
_FORMAT_SPANS = re.compile(r"^SOURCE_SPANS: (.*)$", re.MULTILINE)

_QWEN_PASS_VERDICT = json.dumps({
    "faithful_to_source": True,
    "completeness": True,
    "introduced_errors": False,
    "confidence": "high",
    "reason": "Dynamic fake verdict.",
    "passed": True,
})

_NO_ISSUES = json.dumps({"issues": []})


def _generation_response(text: str) -> str:
    """Mirror the strict-driver ``StubModelCaller`` (digits carried through)."""
    block = _OWNED_SOURCE_BLOCK.search(text)
    pairs: list[tuple[str, str]] = []
    if block:
        pairs = [
            (match.group(1), match.group(2))
            for match in _PID_LINE.finditer(block.group(1))
        ]
    out: Dict[str, str] = {}
    for index, (pid, src_text) in enumerate(pairs, start=1):
        digits = "".join(ch for ch in src_text if ch.isdigit())
        digit_part = f" ({digits})" if digits else ""
        out[pid] = f"Перевод номер{index}{digit_part}"
    return json.dumps(out, ensure_ascii=False)


def _formatting_response(text: str) -> str:
    """Mirror the phase-5 test ``CannedFormattingCaller``.

    Maps each unresolved source span to the corresponding word of the
    translation (span i -> word i), so a local fake and this remote fake
    produce byte-identical mappings for the same prompt. Used by the
    dual-mode parity test (§14.3).
    """
    pid = _FORMAT_PID.search(text).group(1)
    translation = _FORMAT_TRANSLATION.search(text).group(1)
    spans = json.loads(_FORMAT_SPANS.search(text).group(1))
    words = translation.split()
    mappings: list[Dict[str, Any]] = []
    for index, span in enumerate(spans):
        target = words[index] if index < len(words) else ""
        mappings.append({
            "pid": pid, "span_id": span["span_id"],
            "target_text": target, "occurrence": 1,
        })
    return json.dumps({"mappings": mappings}, ensure_ascii=False)


def _respond_to_prompt(text: str) -> str:
    """Return the canned assistant text for a Pact prompt."""
    if "OWNED_SOURCE" in text:
        return _generation_response(text)
    if "SOURCE_SPANS:" in text:
        return _formatting_response(text)  # Phase 5 formatting fallback
    if "candidate_id=" in text:
        # Gemma Russian preference: prefer the first candidate id.
        match = _CANDIDATE_ID.search(text)
        preferred = match.group(1) if match else ""
        return json.dumps({"preferred_candidate_id": preferred, "reason": "dynamic fake"})
    if "strict fidelity auditor" in text:
        return _NO_ISSUES  # Qwen Step 6 audit
    if "strict fidelity reviewer" in text:
        return _QWEN_PASS_VERDICT  # Qwen fidelity gate
    if "Russian-language editor" in text:
        return _NO_ISSUES  # Gemma Step 6 audit
    raise AssertionError(f"unrecognized Pact prompt family: {text[:120]!r}")


class DynamicFakeOpenCodeServer(FakeOpenCodeServer):
    """``FakeOpenCodeServer`` that answers messages by prompt content."""

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        path = _path_of(url)
        body = kwargs.get("json")
        self.requests_log.append((method, path, body))
        if method == "POST" and _match_message_path(path):
            return self._dynamic_message(path, body)
        return super().request(method, url, **kwargs)

    def _dynamic_message(self, path: str, body: Dict[str, Any]) -> FakeResponse:
        parts = body.get("parts") or []
        text = parts[0].get("text", "") if parts else ""
        out_text = _respond_to_prompt(text)
        session_id = _session_id_of(path)
        info = {
            "id": "msg_dynamic",
            "role": "assistant",
            "sessionID": session_id,
            "providerID": "opencode-go",
            "modelID": "deepseek-v4-flash",
            "finish": "end_turn",
            "cost": 0.01,
            "tokens": {
                "input": 10, "output": 20, "reasoning": 0,
                "cache": {"read": 0, "write": 0},
            },
        }
        payload = {"info": info, "parts": [{"id": "p1", "type": "text", "text": out_text}]}
        self.sessions.setdefault(session_id, {"id": session_id, "messages": []})["messages"].append(payload)
        return FakeResponse(200, payload)

    def message_count(self) -> int:
        return sum(
            1 for method, path, _body in self.requests_log
            if method == "POST" and _match_message_path(path)
        )
