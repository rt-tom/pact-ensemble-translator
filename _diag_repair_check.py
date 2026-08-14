"""Воспроизведение с repair-кодом (клон @ 7e90332): 400 ключей, omit, reasoning=3.
Если repair сработает — parse OK; если нет — покажем, ЧТО именно не парсится."""
import sys, json
sys.path.insert(0, r"D:\pact\pact_translator_v4_1")

from pact_v4.runtime.opencode_backend import (
    OpenCodeServerBackend, OpenCodeServerBackendConfig,
)
from pact_v4.runtime.backend_protocol import CompletionRequest, Message
from pact_v4.runtime.json_resilience import classify_response_text, repair_pid_colon_comma

cfg = OpenCodeServerBackendConfig(
    base_url="http://127.0.0.1:4098",
    username="pact", password="test123",
    timeout_seconds=1500,
    server_version_policy="compatible_minor",
    pinned_server_version="1.4.7",
    structured_output_mode="prompt_only",
)
backend = OpenCodeServerBackend(config=cfg)

prompt = (
    "Return a JSON object with 400 keys p00001..p00400, each a Russian translation "
    "of a literary sentence (1-2 sentences each). The text is a family drama about "
    "a boy named Blake visiting his relatives' house, dialogue and description. "
    "Strict JSON only, no markdown, no commentary. "
    'Format exactly: {"p00001": "...", "p00002": "...", ...}. '
    "Keys MUST be p00001 through p00400 in order."
)
request = CompletionRequest(
    model_ref="opencode-go/deepseek-v4-flash",
    messages=(Message(role="user", content=prompt),),
    max_output_tokens=32768, temperature=0.0,
    response_schema={"type": "object", "additionalProperties": {"type": "string"}},
    label="diag_repair_check",
    request_options={"reasoning": 3},
    omit_system_tools=True,
)

resp = backend.complete(request)
text = resp.text or ""
print(f"finish: {resp.finish_reason}, len(text)={len(text)}")
# Сначала: применился бы repair?
repaired, n = repair_pid_colon_comma(text)
print(f"repair_pid_colon_comma: n_subs={n}")
if n:
    print(f"  пример: {text[:80]!r} -> {repaired[:80]!r}")
try:
    classify_response_text(text)
    print("classify: OK")
except Exception as exc:
    print(f"classify FAIL: {type(exc).__name__}: {str(exc)[:140]}")
    # разбор ошибки
    try:
        json.loads(text)
    except json.JSONDecodeError as je:
        s = max(0, je.pos - 60); e2 = min(len(text), je.pos + 60)
        print(f"  pos={je.pos}: {je.msg}")
        print(f"  ...{text[s:e2]!r}...")
    # после repair?
    if n:
        try:
            json.loads(repaired)
            print("  ПОСЛЕ REPAIR: OK")
        except json.JSONDecodeError as je2:
            s = max(0, je2.pos - 60); e2 = min(len(repaired), je2.pos + 60)
            print(f"  после repair FAIL pos={je2.pos}: {je2.msg}")
            print(f"  ...{repaired[s:e2]!r}...")
