"""Thin OpenAI-compatible chat-completions client used by the v4 adapters.

The v3 production pipeline has its own ``ApiClient`` in ``pact_translate_v3.py``
that is tightly coupled to v3 lifecycle code; v4 driver code re-uses only the
HTTP contract, not the class. This module is a minimal, self-contained
implementation of that contract, sufficient for ``llama-server``:

* One ``requests.Session`` per client (connection reuse).
* Bounded retries with linear backoff on transient network/5xx errors.
* A *single* permanent fall-back: when llama-server's Gemma grammar rejects
  ``response_format={"type": "json_object"}`` (gemma-4 sometimes emits the
  ``does not match the expected peg-gemma4 format`` error), retry the same
  call exactly once without that flag and remember the decision for the rest
  of this client's lifetime. This is the same behaviour v3 has, scoped here
  to the v4 adapters only.
* All other errors propagate as ``ApiClientError`` so callers can decide
  how to surface them; v4 code uses the existing per-call
  ``GenerationError`` / ``GateResult`` reporting rather than swallowing them.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

import requests

LOG = logging.getLogger(__name__)


class ApiClientError(RuntimeError):
    """All non-recoverable HTTP/parse failures raise this."""


@dataclass(frozen=True)
class ApiClientConfig:
    """Configuration for one chat-completions endpoint.

    Defaults match the v3 production stack (``llama-server`` on
    ``127.0.0.1:8080``, Gemma 4 26B). Override per call site — Qwen
    evaluator / Gemma selector may use a different model name and even a
    different port.
    """

    chat_url: str = "http://127.0.0.1:8080/v1/chat/completions"
    model: str = "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf"
    timeout_seconds: float = 1800.0
    http_retries: int = 3
    retry_delay_seconds: float = 8.0
    context_size: int = 32768
    top_p: float = 0.95
    top_k: int = 40
    temperature: float = 0.2


@dataclass
class CallRecord:
    """One in-process record of an HTTP call (for provenance / debugging)."""

    label: str
    model: str
    messages: List[Dict[str, str]]
    response_format_requested: bool
    response_format_attempted: bool
    http_status: int
    finish_reason: str
    usage: Dict[str, Any]
    wall_seconds: float


class ApiClient:
    """Minimal OpenAI-compatible chat client with bounded retries."""

    _GRAMMAR_REJECT_MARKER = "does not match the expected peg-gemma4 format"

    def __init__(
        self,
        cfg: ApiClientConfig,
        *,
        name: str = "v4-adapter",
        session: Optional[requests.Session] = None,
    ) -> None:
        self._cfg = cfg
        self._name = name
        self._session = session or requests.Session()
        # Per-client permanent decision: do we still ask the server for
        # JSON-object response_format, or did the server tell us it can't
        # do grammar and we should parse JSON ourselves? See
        # ``ApiClient.complete`` below.
        self._json_response_format_supported: bool = True
        self.calls: List[CallRecord] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def config(self) -> ApiClientConfig:
        return self._cfg

    @property
    def call_records(self) -> List[CallRecord]:
        return list(self.calls)

    # ------------------------------------------------------------------
    # Payload assembly
    # ------------------------------------------------------------------

    def build_payload(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        response_format_json: bool = True,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self._cfg.model,
            "messages": list(messages),
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "top_p": float(self._cfg.top_p),
            "top_k": int(self._cfg.top_k),
            "stream": False,
        }
        if response_format_json and self._json_response_format_supported:
            payload["response_format"] = {"type": "json_object"}
        return payload

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: int,
        temperature: Optional[float] = None,
        response_format_json: bool = True,
        label: str = "v4-call",
    ) -> str:
        """Send a single chat-completions request, return the model text.

        Returns the raw assistant ``content`` string. JSON validity is the
        caller's responsibility (Phase 2B's generation module already does
        strict validation). Retries handle transient errors only.
        """
        if not messages:
            raise ApiClientError(f"{self._name}: empty messages list")

        temp = self._cfg.temperature if temperature is None else float(temperature)
        payload = self.build_payload(
            messages,
            max_tokens=max_tokens,
            temperature=temp,
            response_format_json=response_format_json,
        )
        started = time.perf_counter()
        try:
            data, http_status, fmt_attempted = self._post_with_retry(payload)
        finally:
            wall = time.perf_counter() - started

        text, finish_reason, usage = self._extract_message(data)
        self.calls.append(CallRecord(
            label=label,
            model=self._cfg.model,
            messages=list(messages),
            response_format_requested=response_format_json,
            response_format_attempted=fmt_attempted,
            http_status=http_status,
            finish_reason=finish_reason or "",
            usage=usage or {},
            wall_seconds=round(wall, 3),
        ))
        return text

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _post_with_retry(
        self, payload: Mapping[str, Any]
    ) -> tuple[Dict[str, Any], int, bool]:
        """POST with bounded transient-error retries, plus a single
        free fallback for the well-known Gemma grammar-reject message.

        Transient errors (network exceptions and 5xx) retry the *same*
        payload up to ``http_retries`` total attempts. The Gemma
        ``peg-gemma4`` grammar-reject (a 400 with that marker) is a
        permanent client-level recovery, not a transient failure: the
        client disables ``response_format=json_object`` for the rest
        of its lifetime and retries the same payload once. That retry
        does **not** consume an attempt slot, so even
        ``http_retries=1`` still gets the fallback.

        All other 4xx errors propagate as ``ApiClientError``.
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, int(self._cfg.http_retries) + 1):
            try:
                response = self._session.post(
                    self._cfg.chat_url,
                    json=dict(payload),
                    timeout=float(self._cfg.timeout_seconds),
                )
            except requests.RequestException as exc:
                last_error = exc
                LOG.warning(
                    "%s HTTP attempt %s failed: %s",
                    self._name, attempt, exc,
                )
                self._backoff(attempt)
                continue

            status = response.status_code
            if 200 <= status < 300:
                try:
                    return response.json(), status, "response_format" in payload
                except ValueError as exc:
                    raise ApiClientError(
                        f"{self._name}: non-JSON response body: "
                        f"{response.text[:500]!r}"
                    ) from exc

            if 500 <= status < 600:
                last_error = ApiClientError(
                    f"{self._name}: HTTP {status} {response.reason}; "
                    f"body={response.text[:500]!r}"
                )
                LOG.warning(
                    "%s HTTP attempt %s failed: %s",
                    self._name, attempt, last_error,
                )
                self._backoff(attempt)
                continue

            body = response.text
            if (
                status == 400
                and "response_format" in payload
                and self._GRAMMAR_REJECT_MARKER in body
                and self._json_response_format_supported
            ):
                # Free fallback: disable response_format permanently
                # for this client, retry once, and crucially **do not**
                # consume an attempt slot. We achieve that by
                # post-processing the result instead of looping back
                # into the retry counter.
                LOG.warning(
                    "%s: server rejects response_format=json_object; "
                    "disabling for the rest of this client lifetime",
                    self._name,
                )
                self._json_response_format_supported = False
                stripped_payload = {
                    key: value
                    for key, value in payload.items()
                    if key != "response_format"
                }
                try:
                    retry_response = self._session.post(
                        self._cfg.chat_url,
                        json=stripped_payload,
                        timeout=float(self._cfg.timeout_seconds),
                    )
                except requests.RequestException as exc:
                    raise ApiClientError(
                        f"{self._name}: HTTP retry after grammar reject "
                        f"failed: {exc}"
                    ) from exc
                if not (200 <= retry_response.status_code < 300):
                    raise ApiClientError(
                        f"{self._name}: HTTP "
                        f"{retry_response.status_code} "
                        f"{retry_response.reason}; "
                        f"body={retry_response.text[:500]!r}"
                    )
                try:
                    return (
                        retry_response.json(),
                        retry_response.status_code,
                        False,
                    )
                except ValueError as exc:
                    raise ApiClientError(
                        f"{self._name}: non-JSON response body: "
                        f"{retry_response.text[:500]!r}"
                    ) from exc

            raise ApiClientError(
                f"{self._name}: HTTP {status} {response.reason}; "
                f"body={body[:500]!r}"
            )

        raise ApiClientError(
            f"{self._name}: API failed after "
            f"{int(self._cfg.http_retries)} attempts: {last_error}"
        )

    def _backoff(self, attempt: int) -> None:
        if attempt < int(self._cfg.http_retries):
            time.sleep(float(self._cfg.retry_delay_seconds))

    @staticmethod
    def _extract_message(
        data: Mapping[str, Any]
    ) -> tuple[str, Optional[str], Dict[str, Any]]:
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
        except (KeyError, TypeError, IndexError) as exc:
            raise ApiClientError(
                f"Malformed API response: {json.dumps(data)[:500]!r}"
            ) from exc
        finish_reason = choice.get("finish_reason") if isinstance(choice, Mapping) else None
        usage = data.get("usage") if isinstance(data, Mapping) else None
        return content, finish_reason, usage if isinstance(usage, dict) else {}
