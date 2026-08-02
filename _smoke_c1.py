"""C1 live smoke (S7) — one paid call against a live ``opencode serve``.

Usage (PowerShell, from the repo root):

    $env:OPENCODE_BASE_URL = "http://127.0.0.1:4096"   # or default
    python _smoke_c1.py

Prints the preflight result and one ``BackendCallRecord`` (label, model,
usage, request_id, session_id, wall_seconds). This is the S7 gate for
PR #105: health + provider/model + one paid model call.
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from pact_v4.runtime.backend_protocol import JSON_OBJECT_SCHEMA, CompletionRequest, Message
from pact_v4.runtime.opencode_backend import (
    OpenCodeServerBackend,
    OpenCodeServerBackendConfig,
)

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
MODEL = os.environ.get("OPENCODE_MODEL", "opencode-go/deepseek-v4-flash")

cfg = OpenCodeServerBackendConfig(
    base_url=BASE_URL,
    model_bindings={"generator": MODEL},
    default_temperature=0.2,
    default_max_output_tokens=512,
    http_retries=0,
    retry_delay_seconds=0.0,
)
backend = OpenCodeServerBackend(cfg)

print("== preflight ==")
print(backend.preflight())

print("\n== one call ==")
request = CompletionRequest(
    model_ref=MODEL,
    messages=(Message(role="user", content="Respond with JSON: {\"p1\": \"ok\"}"),),
    max_output_tokens=512,
    temperature=0.2,
    response_schema=JSON_OBJECT_SCHEMA,
    label="c1-live-smoke",
)
response = backend.complete(request)
print("text:", response.text[:200])
print("finish_reason:", response.finish_reason)
print("usage:", response.usage)
print("request_id:", response.request_id)
print("session_id:", response.session_id)
print("retry_count:", response.retry_count)
print("wall_seconds:", response.wall_seconds)

print("\n== call record ==")
record = backend.call_records()[0]
print(
    f"label={record.label} model={record.model_ref} "
    f"request_id={record.request_id} session_id={record.session_id} "
    f"usage={record.usage} wall={record.wall_seconds}"
)
