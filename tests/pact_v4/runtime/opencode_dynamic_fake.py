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
    """Mirror the strict-driver stub generator (digits carried through).

    Card C whole-chapter fixture: when the source paragraph contains the
    emphasized word, it is kept inline in the translation (the same way the
    local ``_PreservingModelCaller`` stub behaves), so the deterministic
    formatting tiers resolve the span with 0 model calls on both paths.
    """
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
        if "emphasized" in src_text:
            out[pid] = f"{out[pid]} (emphasized)"
    return json.dumps(out, ensure_ascii=False)


def _respond_to_prompt(text: str) -> str:
    """Return the canned assistant text for a Pact prompt.

    Card C: there is no Phase 5 formatting prompt family anymore — formatting
    is model-free, so a formatting prompt would fail loudly here (defense in
    depth against a regression to the model-fallback path).
    """
    if "OWNED_SOURCE" in text:
        return _generation_response(text)
    if "candidate_id=" in text:
        # Gemma Russian preference: prefer the first candidate id.
        match = _CANDIDATE_ID.search(text)
        preferred = match.group(1) if match else ""
        return json.dumps({"preferred_candidate_id": preferred, "reason": "dynamic fake"})
    if "strict fidelity auditor" in text:
        return _NO_ISSUES  # Qwen Step 6 audit
    if "strict fidelity reviewer for several repaired regions" in text:
        # B12 batched narrow re-gate: one passing verdict per REGION block.
        count = len(re.findall(r"^REGION (\d+):", text, re.MULTILINE)) or 1
        return json.dumps(
            {"verdicts": [json.loads(_QWEN_PASS_VERDICT) for _ in range(count)]},
            ensure_ascii=False,
        )
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
