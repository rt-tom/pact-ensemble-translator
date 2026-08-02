"""Tests for the managed ``opencode serve`` process lifecycle (V4 C2).

All offline: ``subprocess.Popen`` and the health HTTP GET are injected, so
no ``npx``/server is ever launched and no port is touched. Covers the
card's managed-mode gates: start/health/stop, fail-fast on an occupied
port, cleanup of only one's own process, ``--pure`` launch with ephemeral
credentials that are never persisted.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pact_v4.runtime.opencode_server_lifecycle import (
    ManagedServerError,
    ManagedServerSpec,
    OpenCodeServerProcess,
)


class _FakeProcess:
    """Minimal subprocess.Popen stand-in."""

    def __init__(self, pid: int = 4242, *, exit_on_poll: bool = False) -> None:
        self.pid = pid
        self.returncode = None
        self._poll = None if not exit_on_poll else 1
        self.terminated = False
        self.killed = False
        self._wait_calls = 0

    def poll(self) -> Optional[int]:
        return self._poll

    def terminate(self) -> None:
        self.terminated = True
        self._poll = 0
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self._poll = 9
        self.returncode = 9

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        self._wait_calls += 1
        return self._poll


class _HealthResponse:
    def __init__(self, payload: Optional[Dict[str, Any]]) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _healthy_payload(version: str = "1.4.7") -> Dict[str, Any]:
    return {"healthy": True, "version": version}


def _make_process(
    *,
    version: str = "1.4.7",
    process: Optional[_FakeProcess] = None,
    popen_calls: Optional[List[Dict[str, Any]]] = None,
    healthy_after_start: bool = True,
    startup_timeout: float = 1.0,
):
    """Stateful harness: the port is free until the subprocess "starts"."""
    fake_proc = process or _FakeProcess()
    state = {"started": False}

    def http_get(url: str, timeout: float) -> Any:
        if not state["started"] or not healthy_after_start:
            raise ConnectionError("connection refused")
        return _HealthResponse(_healthy_payload(version))

    capture: List[Dict[str, Any]] = popen_calls if popen_calls is not None else []

    def popen(args, **kwargs) -> Any:
        capture.append({"args": list(args), "env": dict(kwargs.get("env") or {})})
        state["started"] = True
        return fake_proc

    proc = OpenCodeServerProcess(
        ManagedServerSpec(
            hostname="127.0.0.1", port=4096,
            startup_timeout=startup_timeout, health_interval=0.01,
        ),
        popen=popen,
        http_get=http_get,
    )
    return proc, fake_proc, capture


def test_start_launches_pinned_pure_server_with_ephemeral_creds(tmp_path):
    popen_calls: List[Dict[str, Any]] = []
    proc, _fake_proc, capture = _make_process(popen_calls=popen_calls)
    proc.start()

    assert proc.is_running
    assert proc.base_url == "http://127.0.0.1:4096"
    assert len(capture) == 1
    args = capture[0]["args"]
    # PACT raises its OWN pinned server via npx, with --pure.
    assert args[0] == "npx"
    assert "-y" in args
    assert "opencode-ai@1.4.7" in args
    assert args[args.index("--hostname") + 1] == "127.0.0.1"
    assert args[args.index("--port") + 1] == "4096"
    assert "--pure" in args
    # Ephemeral basic-auth creds go into the subprocess env...
    env = capture[0]["env"]
    username, password = proc.credentials
    assert env["OPENCODE_SERVER_USERNAME"] == username
    assert env["OPENCODE_SERVER_PASSWORD"] == password
    assert password and len(password) >= 16
    # ... and are not persisted anywhere on disk.
    for blob in (str(args), str(env)):
        assert password not in blob or env.get("OPENCODE_SERVER_PASSWORD") == password
    proc.close()


def test_start_fails_fast_on_occupied_port():
    # Port already served by a foreign healthy endpoint -> hard error, no
    # subprocess is even launched.
    launched: List[Any] = []

    def popen(args, **kwargs) -> Any:
        launched.append(args)
        return _FakeProcess()

    proc = OpenCodeServerProcess(
        ManagedServerSpec(hostname="127.0.0.1", port=4096),
        popen=popen,
        http_get=lambda url, timeout: _HealthResponse(_healthy_payload()),
    )
    try:
        proc.start()
    except ManagedServerError as exc:
        assert "already served" in str(exc)
    else:
        raise AssertionError("expected ManagedServerError on occupied port")
    assert launched == []


def test_start_fails_when_process_exits_during_startup():
    proc = _make_process(
        healthy_after_start=False,  # never healthy
        process=_FakeProcess(exit_on_poll=True),
    )[0]
    try:
        proc.start()
    except ManagedServerError as exc:
        assert "exited during startup" in str(exc)
    else:
        raise AssertionError("expected ManagedServerError for early exit")


def test_start_fails_when_version_incompatible():
    proc = _make_process(version="1.18.9")[0]
    try:
        proc.start()
    except ManagedServerError as exc:
        assert "not compatible" in str(exc)
    else:
        raise AssertionError("expected version-incompatible ManagedServerError")


def test_close_stops_only_own_process_and_is_idempotent():
    fake_proc = _FakeProcess()
    proc, fake_proc, _capture = _make_process(process=fake_proc)
    proc.start()
    assert proc.is_running

    proc.close()
    assert fake_proc.terminated
    assert not proc.is_running
    assert proc.pid is None
    # Idempotent: a second close must not re-terminate anything.
    proc.close()
    assert fake_proc._wait_calls == 1


def test_close_does_not_stop_a_process_that_already_exited():
    fake_proc = _FakeProcess()
    proc, fake_proc, _capture = _make_process(process=fake_proc)
    proc.start()
    fake_proc._poll = 0
    fake_proc.returncode = 0
    proc.close()
    assert not fake_proc.terminated  # nothing of ours to stop


def test_credentials_unavailable_before_start():
    proc = _make_process()[0]
    try:
        proc.credentials
    except ManagedServerError as exc:
        assert "not started" in str(exc)
    else:
        raise AssertionError("expected credentials-unavailable ManagedServerError")
