"""Managed ``opencode serve`` process lifecycle (V4 C2 / PR 3).

Per the owner's decision (``DECISIONS.md`` 2026-08-01, version-agnostic
2026-08-14) PACT raises its **own** ``opencode serve`` via the ``opencode``
PATH shim (fallback: unpinned ``npx -y opencode-ai serve``) on a separate
port, independent of any pin embedded in other tools. This module owns
exactly that subprocess:

* ``start()`` first runs ``assert_port_free_or_owned`` -- if the configured
  port is already served, it **fails fast** rather than attaching to or
  stopping a foreign server;
* launches ``opencode serve --hostname 127.0.0.1 --port <port> --pure`` with
  ephemeral basic-auth credentials generated here and injected into the
  subprocess environment (``OPENCODE_SERVER_USERNAME`` / ``OPENCODE_SERVER_PASSWORD``);
* health-waits on ``GET /global/health`` (the running version is logged,
  never gated) and returns once the server is ready;
* ``close()`` stops **only** the process this instance started (never a
  foreign one).

The credentials are ephemeral per process and are never persisted: the same
values are returned to the caller so an ``OpenCodeServerBackendConfig`` can
use them for the HTTP calls (the backend already supports direct
``username``/``password`` and never serializes them).
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

import requests

from pact_v4.runtime.opencode_backend import (
    OPENCODE_PINNED_SERVER_VERSION,
    _version_compatible,
)

LOG = logging.getLogger(__name__)

DEFAULT_HOSTNAME = "127.0.0.1"
DEFAULT_USERNAME = "pact"

# Health endpoint payload keys the adapter contract is verified against
# (C1, opencode 1.4.7 / 1.18.18): ``GET /global/health`` ->
# ``{"healthy": true, "version": "..."}``.
_HEALTH_PATH = "/global/health"


class ManagedServerError(RuntimeError):
    """Raised for any managed-server lifecycle failure (start/health/stop)."""


def _default_http_get(
    url: str, timeout: float, *, auth: Optional[Tuple[str, str]] = None
) -> Any:
    return requests.get(url, timeout=timeout, auth=auth)


@dataclass(frozen=True)
class ManagedServerSpec:
    """Identity-relevant settings for a managed ``opencode serve``.

    ``pinned_server_version`` defaults to ``"latest"`` (no pin, owner
    2026-08-14): the server is spawned from the ``opencode`` PATH shim or
    via unpinned ``npx -y opencode-ai``; the version actually running is
    read from health and logged, never gated. An explicit pin is kept only
    for the informational version log.
    """

    hostname: str = DEFAULT_HOSTNAME
    port: int = 4096
    pinned_server_version: str = OPENCODE_PINNED_SERVER_VERSION
    server_version_policy: str = "compatible_minor"
    startup_timeout: float = 120.0
    health_interval: float = 0.5


def _build_launch_args(spec: ManagedServerSpec) -> list:
    """The subprocess args for one managed ``opencode serve``.

    Version-agnostic since 2026-08-14 (owner decision): PACT spawns the
    ``opencode`` binary from PATH (whatever version the operator has
    installed — the version is logged from health, never pinned), falling
    back to unpinned ``npx -y opencode-ai`` when no ``opencode`` shim is on
    PATH. The old ``npx -y opencode-ai@<pin>`` form is gone.

    On Windows the ``opencode``/``npx`` shims are ``.cmd`` batch files:
    Windows ``CreateProcess`` only resolves ``.exe`` and cannot execute a
    ``.cmd`` directly, so a bare ``Popen(["opencode", ...])`` fails with
    ``[WinError 2]``. The launch is routed through ``cmd.exe /d /s /c``,
    which performs the ``PATHEXT`` lookup and runs the shim. Non-Windows
    keeps the direct invocation (a native/scripted binary).
    """
    if _find_opencode_shim() is not None:
        args = [
            "opencode",
            "serve",
            "--hostname", spec.hostname,
            "--port", str(spec.port),
            "--pure",
        ]
    else:
        # Fallback: latest opencode-ai via npx, no version pin.
        args = [
            "npx", "-y", "opencode-ai",
            "serve",
            "--hostname", spec.hostname,
            "--port", str(spec.port),
            "--pure",
        ]
    if sys.platform == "win32":
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d", "/s", "/c",
            *args,
        ]
    return args


def _find_opencode_shim() -> Optional[str]:
    """Locate the ``opencode`` executable on PATH (or None).

    Used by ``_build_launch_args`` to pick ``opencode serve`` over the
    ``npx -y opencode-ai`` fallback. Kept as a module-level helper so tests
    can monkeypatch it instead of relying on the operator's PATH.
    """
    from shutil import which

    return which("opencode")


class OpenCodeServerProcess:
    """Owns one managed ``opencode serve`` subprocess; never touches others.

    ``popen`` and ``http_get`` are injectable so tests run fully offline
    (fake process / fake HTTP response) exactly like the backend's
    injected ``session``.
    """

    def __init__(
        self,
        spec: Optional[ManagedServerSpec] = None,
        *,
        log_dir: Optional[Path] = None,
        popen: Callable[..., Any] = subprocess.Popen,
        http_get: Optional[Callable[[str, float], Any]] = None,
    ) -> None:
        self._spec = spec or ManagedServerSpec()
        self._log_dir = log_dir
        self._popen = popen
        self._http_get = http_get or _default_http_get
        self._proc: Optional[Any] = None
        self._username: Optional[str] = None
        self._password: Optional[str] = None
        self._closed = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self._spec.hostname}:{self._spec.port}"

    @property
    def credentials(self) -> Tuple[str, str]:
        """The ephemeral basic-auth pair, valid only for this owned server."""
        if self._username is None or self._password is None:
            raise ManagedServerError(
                "OpenCodeServerProcess: server not started; no credentials yet"
            )
        return self._username, self._password

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    # ------------------------------------------------------------------
    # Health / port ownership (fail-fast, plan §5 / DECISIONS 2026-08-01)
    # ------------------------------------------------------------------

    def _health(self, *, auth: Optional[Tuple[str, str]] = None) -> Optional[dict]:
        try:
            resp = self._http_get(
                f"{self.base_url}{_HEALTH_PATH}", timeout=2.0, auth=auth
            )
            payload = resp.json()
            return payload if isinstance(payload, dict) else None
        except Exception:  # noqa: BLE001 -- any probe failure == no server yet
            return None

    def assert_port_free_or_owned(self) -> None:
        """Fail fast if the configured port is already served by someone else.

        Never attaches to or stops a foreign server: an unowned endpoint on
        our port is a hard error, not something to adopt. Any HTTP response
        on the health path counts as occupancy -- including a 401 from an
        auth-protected foreign server (an HTTP server is listening even if
        it does not answer the anonymous probe). Without this, a foreign
        auth-protected server would be silently treated as "free", our own
        server would fail to bind, and start() would only surface a cryptic
        "exited during startup (code=1)".
        """
        try:
            resp = self._http_get(
                f"{self.base_url}{_HEALTH_PATH}", timeout=2.0
            )
        except Exception:  # noqa: BLE001 -- no HTTP answer on the port -> free
            return
        status = getattr(resp, "status_code", "n/a")
        raise ManagedServerError(
            f"Port {self._spec.port} is already served by an unowned endpoint "
            f"(HTTP status {status}); refusing to attach to or stop it."
        )

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self) -> "OpenCodeServerProcess":
        if self._proc is not None:
            return self
        self.assert_port_free_or_owned()

        username = DEFAULT_USERNAME
        password = secrets.token_urlsafe(24)
        spec = self._spec
        args = _build_launch_args(spec)
        env = dict(os.environ)
        env["OPENCODE_SERVER_USERNAME"] = username
        env["OPENCODE_SERVER_PASSWORD"] = password
        # WORKAROUND (2026-08-21, owner): opencode serve (v1) hard-caps the
        # model output at ~32k tokens (32768), silently ignoring our
        # max_completion_tokens. Whole-chapter generations (e.g. Muse) were
        # truncated at exactly 32000 total tokens (finish=length). The
        # official escape hatch is this experimental env var; verified on
        # chapter 0026: generation then reached 41514 tokens with
        # finish=stop. Raised to 1MiB so even the longest chapter fits.
        env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] = "1048576"

        stdout_path: Optional[Path] = None
        stderr_path: Optional[Path] = None
        if self._log_dir is not None:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            # Collision-proof stamp: second + microsecond + pid + random suffix so
            # strict server and immediately following formatting server on the same
            # second never collide and overwrite each other's log (round 4 fix).
            base = time.strftime("%Y%m%d_%H%M%S")
            micros = int(time.time() * 1_000_000) % 1_000_000
            stamp = f"{base}_{micros:06d}_{os.getpid()}_{secrets.token_hex(2)}"
            stdout_path = self._log_dir / f"opencode_serve_{stamp}_stdout.log"
            stderr_path = self._log_dir / f"opencode_serve_{stamp}_stderr.log"

        LOG.info(
            "Starting managed opencode serve: %s (log dir: %s)",
            " ".join(args), self._log_dir,
        )
        try:
            self._proc = self._popen(
                args,
                env=env,
                cwd=str(self._log_dir) if self._log_dir is not None else None,
                stdout=(
                    open(stdout_path, "w", encoding="utf-8")
                    if stdout_path is not None else subprocess.DEVNULL
                ),
                stderr=(
                    open(stderr_path, "w", encoding="utf-8")
                    if stderr_path is not None else subprocess.DEVNULL
                ),
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )
        except OSError as exc:
            self._proc = None
            raise ManagedServerError(
                f"failed to start managed opencode serve: {exc}"
            ) from exc
        self._username = username
        self._password = password

        try:
            self._wait_healthy()
        except BaseException:
            # Guarantee (review UPGRADE-SERVE-1.18 HIGH): ANY startup
            # failure — including an unexpected exception inside the health
            # probe/version check — must not leave the managed process
            # running. ``_force_kill`` is idempotent and also covers the
            # cases ``_wait_healthy`` already cleans up (timeout, exit).
            self._force_kill()
            raise
        return self

    def _wait_healthy(self) -> None:
        spec = self._spec
        t0 = time.monotonic()
        last_error: Optional[str] = None
        while time.monotonic() - t0 < spec.startup_timeout:
            if self._proc is None or self._proc.poll() is not None:
                code = self._proc.returncode if self._proc is not None else None
                self._proc = None
                raise ManagedServerError(
                    f"managed opencode serve exited during startup "
                    f"(code={code}); last probe: {last_error}"
                )
            health = self._health(auth=(self._username, self._password))
            if health is not None and health.get("healthy"):
                version = str(health.get("version") or "")
                if not version:
                    last_error = "health reported no version"
                elif _version_compatible(
                    version,
                    policy=spec.server_version_policy,
                    pinned=spec.pinned_server_version,
                ):
                    LOG.info(
                        "Managed opencode serve ready on %s (version %s, pid %s)",
                        self.base_url, version, self.pid,
                    )
                    return
                else:
                    # Version-agnostic (owner 2026-08-14): a healthy server
                    # of a different version is NOT a startup failure — the
                    # version is logged (info below) and the server is used.
                    # The contract suite, not the version string, is the gate.
                    LOG.warning(
                        "Managed opencode serve version %r does not match "
                        "configured policy %r / pinned %r; proceeding "
                        "version-agnostic",
                        version,
                        spec.server_version_policy,
                        spec.pinned_server_version,
                    )
                    LOG.info(
                        "Managed opencode serve ready on %s (version %s, pid %s)",
                        self.base_url, version, self.pid,
                    )
                    return
            else:
                last_error = "not healthy yet"
            time.sleep(spec.health_interval)
        self._force_kill()
        raise ManagedServerError(
            f"managed opencode serve did not become ready within "
            f"{spec.startup_timeout}s; last probe: {last_error}"
        )

    def _kill_tree(self, proc: Any) -> None:
        """Kill the whole subprocess tree (cmd -> npx -> node) on Windows.

        ``proc`` is the ``cmd.exe`` wrapper Popen; ``terminate()`` on it
        would kill only cmd, orphaning the ``opencode`` node process which
        keeps the port bound. ``taskkill /T /F`` kills the entire tree.
        Non-Windows (and non-``Popen`` fakes in tests) fall back to plain
        ``terminate`` by the caller.
        """
        if (
            sys.platform == "win32"
            and isinstance(proc, subprocess.Popen)
            and proc.poll() is None
        ):
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except Exception:  # noqa: BLE001 -- best-effort; caller retries below
                LOG.warning(
                    "OpenCodeServerProcess: taskkill /T failed for pid %s", proc.pid
                )

    def _force_kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        self._kill_tree(proc)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    def close(self) -> None:
        """Stop only the server this instance started (idempotent)."""
        if self._closed:
            return
        self._closed = True
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        if proc.poll() is not None:
            return
        self._kill_tree(proc)
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001 -- best-effort teardown
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                LOG.warning("OpenCodeServerProcess: failed to stop pid %s", proc.pid)


__all__ = [
    "DEFAULT_HOSTNAME",
    "DEFAULT_USERNAME",
    "ManagedServerError",
    "ManagedServerSpec",
    "OpenCodeServerProcess",
]
