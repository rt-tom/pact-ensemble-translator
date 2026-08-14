"""Offline fake OpenCode server harness (plan §14.2).

A scriptable, in-process stand-in for ``opencode serve`` (verified against
1.4.7 and 1.18.18) used by the contract suite and the pipeline-parity
tests. It exposes the same request/response contract the backend speaks
(health/provider/session/message/tool-ids) and records every request so
tests can assert exact wire-level behaviour (tools disabled, explicit
model, session cleanup, etc.). It never touches the network and never
makes a paid call.

The harness implements the small slice of ``requests.Session`` the
``OpenCodeServerBackend`` uses (``request(method, url, ...)`` and
``close()``), so it can be injected as the backend's HTTP session.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Response helper
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        *,
        headers: Optional[Dict[str, str]] = None,
        text: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._text = text if text is not None else (
            json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        )
        self.reason = "OK" if 200 <= status_code < 300 else "Error"

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload

    @property
    def text(self) -> str:
        return self._text


# ---------------------------------------------------------------------------
# Fake server
# ---------------------------------------------------------------------------


def _default_providers() -> List[Dict[str, Any]]:
    return [
        {
            "id": "opencode-go",
            "name": "OpenCode Go",
            "source": "api",
            "env": [],
            "models": {
                "deepseek-v4-flash": {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash"},
                "qwen3.7-plus": {"id": "qwen3.7-plus", "name": "Qwen3.7 Plus"},
            },
        },
        {
            "id": "other-provider",
            "name": "Other",
            "source": "api",
            "env": [],
            "models": {"other-model": {"id": "other-model", "name": "Other Model"}},
        },
    ]


def _default_tool_ids() -> List[str]:
    return [
        "bash",
        "read",
        "glob",
        "grep",
        "edit",
        "write",
        "task",
        "webfetch",
        "todowrite",
        "websearch",
        "codesearch",
        "skill",
        "apply_patch",
        "question",
    ]


class FakeOpenCodeServer:
    """Scriptable in-process stand-in for ``opencode serve`` (1.18.18).

    Behaviour is controlled by:

    * ``version`` / ``healthy`` — the ``/global/health`` payload;
    * ``providers`` / ``connected`` — the ``/provider`` payload;
    * ``tool_ids`` — the ``/experimental/tool/ids`` payload;
    * ``message_responses`` — a queue of responses for
      ``POST /session/{id}/message``. Each item is either a dict
      ``{info: ..., parts: [...]}``, an ``int`` HTTP status, a
      ``FakeResponse``, or an ``Exception`` to raise (for network/timeout
      failures).
    * ``delete_responses`` — a queue of responses for ``DELETE /session/{id}``
      (defaults to 200/true when empty).

    Every request is appended to ``requests_log`` as a tuple
    ``(method, path, json_body)`` so tests can assert exact wire behaviour.
    """

    def __init__(
        self,
        *,
        version: str = "1.18.18",
        healthy: bool = True,
        providers: Optional[List[Dict[str, Any]]] = None,
        connected: Optional[List[str]] = None,
        tool_ids: Optional[List[str]] = None,
    ) -> None:
        self.version = version
        self.healthy = healthy
        self.providers = providers if providers is not None else _default_providers()
        self.connected = connected if connected is not None else ["opencode-go"]
        self.tool_ids = tool_ids if tool_ids is not None else _default_tool_ids()

        self.message_responses: List[Any] = []
        self.delete_responses: List[Any] = []
        # Optional override for the POST /session payload (e.g. a non-dict
        # response to exercise the malformed-session-response path).
        self.session_create_response: Optional[Any] = None

        self.requests_log: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []
        # Per-request ``timeout`` kwarg forwarded by the backend (TIMEOUT-FIX:
        # proves the transport budget passed to the session is the configured
        # timeout_seconds, so a long generation is not cut at an old default).
        self.timeouts_log: List[Optional[float]] = []
        # Deterministic virtual-duration simulation (TIMEOUT-FIX): the
        # simulated duration of one POST message generation, in seconds. No
        # wall-clock sleep is performed; instead the fake ENFORCES the
        # per-request ``timeout`` it received against this duration and raises
        # ``requests.exceptions.Timeout`` when the budget is exceeded — the
        # same abort the real ``requests.Session`` would perform. A test can
        # therefore prove "a generation longer than 600s is accepted with a
        # 900s budget but aborted with the old 600s budget" in milliseconds
        # instead of sleeping 10-15 real minutes. Applies to message POSTs
        # only (the generation itself); preflight/session/delete stay instant.
        self.virtual_generation_seconds: float = 0.0
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.created_titles: List[str] = []
        self._session_seq = 0
        self.closed = False

    # -- session-like surface used by OpenCodeServerBackend ------------------

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        path = _path_of(url)
        body = kwargs.get("json")
        self.requests_log.append((method, path, body))
        self.timeouts_log.append(kwargs.get("timeout"))
        if method == "GET" and path == "/global/health":
            return FakeResponse(200, {"healthy": self.healthy, "version": self.version})
        if method == "GET" and path == "/provider":
            return FakeResponse(
                200,
                {
                    "all": self.providers,
                    "default": {},
                    "connected": list(self.connected),
                },
            )
        if method == "GET" and path == "/experimental/tool/ids":
            return FakeResponse(200, list(self.tool_ids))
        if method == "POST" and path == "/session":
            return self._create_session(body)
        if method == "DELETE" and path.startswith("/session/"):
            session_id = path[len("/session/") :]
            return self._delete_session(session_id)
        if method == "POST" and _match_message_path(path):
            return self._post_message(path, timeout=kwargs.get("timeout"))
        if method == "GET" and _match_message_path(path):
            return self._list_messages(path)
        return FakeResponse(404, {"error": f"unexpected request: {method} {path}"})

    def close(self) -> None:
        self.closed = True

    # -- internals -----------------------------------------------------------

    def _create_session(self, body: Optional[Dict[str, Any]]) -> FakeResponse:
        if self.session_create_response is not None:
            item = self.session_create_response
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, FakeResponse):
                return item
            return FakeResponse(200, item)
        self._session_seq += 1
        session_id = f"ses_fake_{self._session_seq}"
        title = (body or {}).get("title", "")
        self.created_titles.append(title)
        self.sessions[session_id] = {"id": session_id, "title": title, "messages": []}
        return FakeResponse(
            200,
            {
                "id": session_id,
                "slug": "fake",
                "version": self.version,
                "title": title,
                "time": {"created": 0, "updated": 0},
            },
        )

    def _delete_session(self, session_id: str) -> FakeResponse:
        if self.delete_responses:
            item = self.delete_responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, FakeResponse):
                return item
            status = int(item) if isinstance(item, int) else 200
            return FakeResponse(status, True)
        if session_id in self.sessions:
            del self.sessions[session_id]
            return FakeResponse(200, True)
        return FakeResponse(404, {"error": f"Session not found: {session_id}"})

    def _post_message(self, path: str, *, timeout: Optional[float] = None) -> FakeResponse:
        session_id = _session_id_of(path)
        if not self.message_responses:
            return FakeResponse(500, {"error": "no scripted message response"})
        if (
            self.virtual_generation_seconds > 0
            and timeout is not None
            and self.virtual_generation_seconds > timeout
        ):
            # Simulate the client-side transport aborting the call: the real
            # requests.Session raises Timeout when the generation outlives the
            # configured budget, and the backend normalizes it to
            # ERROR_TRANSPORT_TIMEOUT. The scripted response stays queued, so
            # a bounded transport retry would re-raise deterministically.
            raise requests.exceptions.Timeout(
                f"simulated generation of {self.virtual_generation_seconds}s "
                f"exceeded the {timeout}s transport timeout"
            )
        item = self.message_responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FakeResponse):
            return item
        if isinstance(item, int):
            return FakeResponse(item, {"error": f"scripted HTTP {item}"})
        # dict payload -> {info, parts}
        info = dict(item.get("info") or {})
        info.setdefault("id", f"msg_fake_{len(self.sessions.get(session_id, {}).get('messages', [])) + 1}")
        info.setdefault("sessionID", session_id)
        info.setdefault("providerID", "opencode-go")
        info.setdefault("modelID", "deepseek-v4-flash")
        payload = {"info": info, "parts": list(item.get("parts") or [])}
        self.sessions.setdefault(session_id, {"id": session_id, "messages": []})["messages"].append(payload)
        return FakeResponse(200, payload)

    def _list_messages(self, path: str) -> FakeResponse:
        session_id = _session_id_of(path)
        messages = self.sessions.get(session_id, {}).get("messages", [])
        return FakeResponse(200, list(messages))

    # -- test helpers --------------------------------------------------------

    def script_message(self, *responses: Any) -> None:
        """Queue message responses (dict payload / HTTP status / exception)."""
        self.message_responses.extend(responses)

    def script_delete(self, *responses: Any) -> None:
        self.delete_responses.extend(responses)

    def last_message_body(self) -> Optional[Dict[str, Any]]:
        """The json body of the most recent POST message request."""
        for method, path, body in reversed(self.requests_log):
            if method == "POST" and _match_message_path(path):
                return body
        return None

    def message_bodies(self) -> List[Dict[str, Any]]:
        return [b for m, p, b in self.requests_log if m == "POST" and _match_message_path(p)]

    def created_session_titles(self) -> List[str]:
        return list(self.created_titles)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _path_of(url: str) -> str:
    return _raw_path(url)


def _raw_path(url: str) -> str:
    # "http://host:port/session/ses_x/message" -> "/session/ses_x/message"
    after = url.split("://", 1)[-1]
    path = after.split("/", 1)[1] if "/" in after else ""
    return "/" + path.split("?")[0]


_MESSAGE_PATH = re.compile(r"^/session/[^/]+/message$")


def _match_message_path(path: str) -> bool:
    return bool(_MESSAGE_PATH.match(path))


def _session_id_of(path: str) -> str:
    return path.split("/")[2]


__all__ = [
    "FakeResponse",
    "FakeOpenCodeServer",
    "_default_providers",
    "_default_tool_ids",
]
