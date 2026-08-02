"""Tests for the managed ``opencode serve`` process lifecycle (V4 C2).

All offline: ``subprocess.Popen`` and the health HTTP GET are injected, so
no ``npx``/server is ever launched and no port is touched. Covers the
card's managed-mode gates: start/health/stop, fail-fast on an occupied
port, cleanup of only one's own process, ``--pure`` launch with ephemeral
credentials that are never persisted.
"""
from __future__ import annotations

import os
import subprocess
import sys
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
    health_auth_calls: Optional[List[Any]] = None,
):
    """Stateful harness: the port is free until the subprocess "starts"."""
    fake_proc = process or _FakeProcess()
    state = {"started": False}

    def http_get(url, timeout, auth=None):
        if health_auth_calls is not None:
            health_auth_calls.append(auth)
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
    health_auth_calls: List[Any] = []
    proc, _fake_proc, capture = _make_process(
        popen_calls=popen_calls, health_auth_calls=health_auth_calls,
    )
    proc.start()

    assert proc.is_running
    assert proc.base_url == "http://127.0.0.1:4096"
    assert len(capture) == 1
    args = capture[0]["args"]
    # On Windows the npx shim is npx.CMD, which CreateProcess cannot launch
    # directly, so the launch is routed through cmd.exe /d /s /c; the actual
    # npx invocation is the same on every platform.
    if sys.platform == "win32":
        assert os.path.basename(args[0]).lower() == "cmd.exe"
        assert args[1:4] == ["/d", "/s", "/c"]
        npx_args = args[4:]
    else:
        npx_args = args
    # PACT raises its OWN pinned server via npx, with --pure.
    assert npx_args[0] == "npx"
    assert "-y" in npx_args
    assert "opencode-ai@1.4.7" in npx_args
    assert npx_args[npx_args.index("--hostname") + 1] == "127.0.0.1"
    assert npx_args[npx_args.index("--port") + 1] == "4096"
    assert "--pure" in npx_args
    # Ephemeral basic-auth creds go into the subprocess env...
    env = capture[0]["env"]
    username, password = proc.credentials
    assert env["OPENCODE_SERVER_USERNAME"] == username
    assert env["OPENCODE_SERVER_PASSWORD"] == password
    assert password and len(password) >= 16
    # ... are not persisted anywhere on disk...
    for blob in (str(args), str(env)):
        assert password not in blob or env.get("OPENCODE_SERVER_PASSWORD") == password
    # ... and the health-wait probe carries them (a real serve requires basic
    # auth). The pre-start port probe has no creds yet (None).
    assert health_auth_calls[0] is None
    assert health_auth_calls[-1] == (username, password)
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
        http_get=lambda url, timeout, auth=None: _HealthResponse(_healthy_payload()),
    )
    try:
        proc.start()
    except ManagedServerError as exc:
        assert "already served" in str(exc)
    else:
        raise AssertionError("expected ManagedServerError on occupied port")
    assert launched == []


def test_start_fails_fast_on_auth_protected_foreign_server():
    # A foreign server that requires basic auth answers the anonymous health
    # probe with 401 -- that is still an occupied port and must fail fast,
    # not let our server try to bind and die with a cryptic exit code.
    launched: List[Any] = []

    class _AuthRequiredResponse:
        status_code = 401

        def json(self):
            raise ValueError("not JSON")

    def popen(args, **kwargs) -> Any:
        launched.append(args)
        return _FakeProcess()

    proc = OpenCodeServerProcess(
        ManagedServerSpec(hostname="127.0.0.1", port=4096),
        popen=popen,
        http_get=lambda url, timeout, auth=None: _AuthRequiredResponse(),
    )
    try:
        proc.start()
    except ManagedServerError as exc:
        assert "already served" in str(exc)
        assert "401" in str(exc)
    else:
        raise AssertionError("expected ManagedServerError on auth-protected port")
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


# ---------------------------------------------------------------------------
# Launch-arg construction (regression for the Windows npx.CMD failure)
# ---------------------------------------------------------------------------


def test_launch_args_route_npx_through_cmd_exe_on_windows(monkeypatch):
    from pact_v4.runtime import opencode_server_lifecycle as life

    monkeypatch.setattr(life.sys, "platform", "win32")
    monkeypatch.setenv("COMSPEC", "C:\\Windows\\System32\\cmd.exe")
    args = life._build_launch_args(
        ManagedServerSpec(hostname="127.0.0.1", port=4096)
    )
    assert args[0] == "C:\\Windows\\System32\\cmd.exe"
    assert args[1:4] == ["/d", "/s", "/c"]
    assert args[4] == "npx"
    assert "opencode-ai@1.4.7" in args
    assert "--hostname" in args and "--pure" in args


def test_launch_args_keep_direct_npx_on_non_windows(monkeypatch):
    from pact_v4.runtime import opencode_server_lifecycle as life

    monkeypatch.setattr(life.sys, "platform", "linux")
    args = life._build_launch_args(ManagedServerSpec())
    assert args[0] == "npx"
    assert "-y" in args
    assert "--pure" in args
    assert not any("cmd.exe" in arg.lower() for arg in args)


def test_kill_tree_uses_taskkill_tree_on_windows(monkeypatch):
    # A real Popen stands in for the cmd.exe wrapper: _kill_tree must kill
    # the whole tree (/T) forcefully (/F), otherwise the opencode node child
    # is orphaned and keeps the port bound.
    from pact_v4.runtime import opencode_server_lifecycle as life

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    calls: List[List[str]] = []
    monkeypatch.setattr(life.sys, "platform", "win32")
    monkeypatch.setattr(
        life.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args),
    )
    try:
        life.OpenCodeServerProcess()._kill_tree(proc)
    finally:
        proc.terminate()
        proc.wait()
    assert calls and calls[0][:3] == ["taskkill", "/PID", str(proc.pid)]
    assert "/T" in calls[0] and "/F" in calls[0]


def test_kill_tree_does_not_taskkill_fake_process_on_windows(monkeypatch):
    # Offline fakes are not real subprocess.Popen instances: _kill_tree must
    # not shell out to taskkill on them (tests stay fully offline).
    from pact_v4.runtime import opencode_server_lifecycle as life

    calls: List[Any] = []
    monkeypatch.setattr(life.sys, "platform", "win32")
    monkeypatch.setattr(life.subprocess, "run", lambda args, **kwargs: calls.append(args))
    fake = _FakeProcess()
    life.OpenCodeServerProcess()._kill_tree(fake)
    assert calls == []


def test_start_times_out_when_server_never_becomes_healthy():
    # Process stays up but never reports healthy -> startup-timeout error.
    proc, _fake_proc, _capture = _make_process(healthy_after_start=False)
    try:
        proc.start()
    except ManagedServerError as exc:
        assert "did not become ready" in str(exc)
    else:
        raise AssertionError("expected startup-timeout ManagedServerError")
