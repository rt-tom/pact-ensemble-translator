"""Focused regression net for the local llama-server HTTP read timeout bump.

Default local timeout is 2700s (45 min) so slow local generation such as
phase2b-generation is not cut off at 30 min. Every local transport
construction path shares ``DEFAULT_LOCAL_READ_TIMEOUT_SECONDS``; explicit
per-client ``timeout_seconds`` overrides are preserved. Remote OpenCode
defaults (900s) are intentionally untouched.

Offline: ``requests.Session`` is replaced with a fake that records the
``timeout=`` kwarg actually passed to ``Session.post``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pact_v4.runtime.api_client import (
    ApiClient,
    ApiClientConfig,
    DEFAULT_LOCAL_READ_TIMEOUT_SECONDS,
)
from pact_v4.runtime.backend_protocol import CompletionRequest, Message
from pact_v4.runtime.model_lifecycle_adapters import (
    LifecycleGemmaAuditEvaluator,
    LifecycleGemmaSelector,
    LifecycleModelCaller,
    LifecycleQwenAuditEvaluator,
    LifecycleQwenEntityExtractor,
    LifecycleQwenEvaluator,
    LifecycleSelectiveRepairEvaluator,
)


class _FakeResponse:
    def __init__(self) -> None:
        self.status_code = 200
        self.text = "ok"
        self.reason = "OK"

    def json(self) -> Dict[str, Any]:
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        }

    def close(self) -> None:
        pass


class _FakeStreamResponse(_FakeResponse):
    def iter_lines(self, decode_unicode: bool = False):  # pragma: no cover
        raise AssertionError("stream path not used in these tests")


class _FakeSession:
    def __init__(self, script: Optional[List[Any]] = None) -> None:
        self._script = list(script) if script else [_FakeResponse()]
        self.posts: List[Dict[str, Any]] = []

    def post(self, url: str, *, json: Dict[str, Any], timeout: float, stream: bool = False):
        self.posts.append({"url": url, "json": json, "timeout": timeout, "stream": stream})
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, BaseException):
            raise item
        return item


class _NoopRouter:
    base_url = "http://router.invalid"

    def ensure_resident(self, model_key: str, **kwargs: Any) -> None:
        return None


def test_shared_local_timeout_constant_is_2700() -> None:
    assert DEFAULT_LOCAL_READ_TIMEOUT_SECONDS == 2700.0


def test_api_client_config_default_is_2700() -> None:
    assert ApiClientConfig().timeout_seconds == 2700.0


def test_default_client_posts_with_timeout_2700() -> None:
    session = _FakeSession()
    client = ApiClient(ApiClientConfig(), session=session)
    client.complete([{"role": "user", "content": "x"}], max_tokens=8)
    assert session.posts[0]["timeout"] == 2700.0


def test_explicit_override_is_honored_on_the_wire() -> None:
    session = _FakeSession()
    client = ApiClient(ApiClientConfig(timeout_seconds=123.0), session=session)
    client.complete([{"role": "user", "content": "x"}], max_tokens=8)
    assert session.posts[0]["timeout"] == 123.0


def test_lifecycle_generation_defaults_to_2700_and_honors_override() -> None:
    router = _NoopRouter()
    default = LifecycleModelCaller(router, model_name="gemma-fake")
    assert default._caller._api.config.timeout_seconds == 2700.0

    from pact_v4.runtime.model_caller import HttpModelCallerConfig

    custom = LifecycleModelCaller(
        router,
        model_name="gemma-fake",
        config=HttpModelCallerConfig(api=ApiClientConfig(timeout_seconds=321.0)),
    )
    assert custom._caller._api.config.timeout_seconds == 321.0


def test_lifecycle_qwen_selector_defaults_to_2700_and_honor_overrides() -> None:
    router = _NoopRouter()
    assert LifecycleQwenEvaluator(router, model_name="qwen-fake")._evaluator._api.config.timeout_seconds == 2700.0
    assert LifecycleGemmaSelector(router, model_name="gemma-fake")._selector._api.config.timeout_seconds == 2700.0

    from pact_v4.runtime.gemma_selector import HttpGemmaSelectorConfig
    from pact_v4.runtime.qwen_evaluator import HttpQwenEvaluatorConfig

    custom_q = LifecycleQwenEvaluator(
        router,
        model_name="qwen-fake",
        config=HttpQwenEvaluatorConfig(api=ApiClientConfig(timeout_seconds=222.0)),
    )
    assert custom_q._evaluator._api.config.timeout_seconds == 222.0
    custom_s = LifecycleGemmaSelector(
        router,
        model_name="gemma-fake",
        config=HttpGemmaSelectorConfig(api=ApiClientConfig(timeout_seconds=333.0)),
    )
    assert custom_s._selector._api.config.timeout_seconds == 333.0


def test_lifecycle_audit_entity_repair_defaults_to_2700() -> None:
    router = _NoopRouter()
    assert LifecycleQwenAuditEvaluator(router, model_name="qwen-fake")._backend._api.config.timeout_seconds == 2700.0
    assert LifecycleGemmaAuditEvaluator(router, model_name="gemma-fake")._backend._api.config.timeout_seconds == 2700.0
    assert LifecycleQwenEntityExtractor(router, model_name="qwen-fake")._backend._api.config.timeout_seconds == 2700.0
    repair = LifecycleSelectiveRepairEvaluator(
        router, repair_model_name="gemma-fake", reaudit_model_name="qwen-fake"
    )
    assert repair._repair_backend._api.config.timeout_seconds == 2700.0
    assert repair._reaudit_backend._api.config.timeout_seconds == 2700.0


def test_local_routing_backend_fallback_uses_2700() -> None:
    """The ``LocalRoutingBackend`` per-model fallback client (built lazily in
    ``complete()``) must carry the 2700s default on the wire."""
    import pact_v4.runtime.runtime_config as rc

    captured: Dict[str, Any] = {}

    class _RecordingApiClient(ApiClient):
        def __init__(self, cfg: ApiClientConfig, **kwargs: Any) -> None:
            captured.update(
                {"timeout_seconds": cfg.timeout_seconds, "model": cfg.model}
            )
            super().__init__(cfg, session=_FakeSession(), **kwargs)

    class _StubRouter:
        base_url = "http://router.invalid"

        def ensure_resident(self, key: str, **kwargs: Any) -> None:
            return None

    cfg = rc.LocalLlamaBackendConfig(
        exe=Path("/bin/false"),
        device="cpu",
        host="127.0.0.1",
        model_paths={"gemma": Path("/models/gemma.gguf")},
        model_names={"gemma": "gemma-fake"},
        server_args={"gemma": []},
    )
    backend = rc.LocalRoutingBackend(_StubRouter(), cfg)
    real_api_client = rc.ApiClient
    rc.ApiClient = _RecordingApiClient  # type: ignore[assignment]
    try:
        backend.complete(
            CompletionRequest(
                model_ref="gemma-fake",
                messages=(Message(role="user", content="x"),),
                max_output_tokens=8,
                temperature=0.2,
                response_schema=None,
                label="timeout-probe",
            )
        )
    finally:
        rc.ApiClient = real_api_client  # type: ignore[assignment]
    assert captured["timeout_seconds"] == 2700.0
    assert captured["model"] == "gemma-fake"


def test_remote_opencode_default_stays_900() -> None:
    from pact_v4.runtime.opencode_backend import OpenCodeServerBackendConfig

    assert OpenCodeServerBackendConfig().timeout_seconds == 900.0
