"""Single-resident model lifecycle adapter + router.

Promoted from ``pact_full_pipeline_runner_v1/v4_model_lifecycle_bench.py``
(Measurement Task 2) into a reusable ``pact_v4`` module, per
``docs/plans/V4_STRICT_DRIVER_CHAPTER_TRIAL_TASK_RU.md``: the strict
single-resident driver needs the same validated lifecycle mechanics
(VRAM-confirmed release, ownership safety, driver-settle delay), not a
re-implementation. The bench script now imports ``LifecycleAdapter`` from
here instead of defining its own copy.

Two layers:

* ``LifecycleAdapter`` -- owns exactly one ``llama-server`` process at a
  time. Never attaches to or stops a server it did not start itself.
  Confirms VRAM release by polling the Windows "GPU Process Memory"
  performance counter for the owned PID's dedicated-memory usage --
  process exit / HTTP close is not treated as proof of release.
* ``ModelRouter`` -- tracks which model key is currently resident behind
  a fixed ``host:port`` and only triggers a real stop+start when the
  requested model differs from what's already loaded. This is what makes
  ``Gpref(N)`` and ``Ggen(N+1)`` share one Gemma lease (no restart
  between them) instead of always restarting, matching the architecture
  doc's "Подсчёт перезапусков" accounting -- unlike the bench script's
  synthetic segment plan, which always restarts by design.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

import requests

LOG = logging.getLogger(__name__)


class LifecycleError(RuntimeError):
    """Raised for any lifecycle adapter failure (start/stop/health/VRAM)."""


# --------------------------------------------------------------------------
# GPU VRAM observation (no NVIDIA GPU on this host; use the Windows
# "GPU Process Memory" perf counter, which works per-process for both the
# Intel Arc B580 (SYCL0/Vulkan0) and the AMD iGPU without vendor tools).
# --------------------------------------------------------------------------

_GPU_COUNTER_PS = (
    "$s = Get-Counter -Counter '\\GPU Process Memory(*)\\Dedicated Usage' "
    "-ErrorAction SilentlyContinue; "
    "if (-not $s) { Write-Output '0'; exit }; "
    "$total = ($s.CounterSamples | Where-Object { $_.Path -match "
    '"pid___PID___"'
    " } | Measure-Object -Property CookedValue -Sum).Sum; "
    "if (-not $total) { $total = 0 }; "
    "Write-Output ([int64]$total)"
)


def gpu_dedicated_bytes(pid: int) -> int:
    script = _GPU_COUNTER_PS.replace("__PID__", str(pid))
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return -1
    text = out.stdout.strip()
    try:
        return int(text)
    except ValueError:
        return -1


def wait_vram_released(pid: int, timeout_seconds: float) -> tuple[bool, float, int]:
    """Poll until the PID's dedicated GPU usage reads 0, or timeout."""
    t0 = time.monotonic()
    last = -1
    while True:
        last = gpu_dedicated_bytes(pid)
        if last == 0:
            return True, time.monotonic() - t0, last
        if time.monotonic() - t0 >= timeout_seconds:
            return False, time.monotonic() - t0, last
        time.sleep(0.5)


def _tail(path: Path, n_lines: int = 40) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n_lines:])
    except Exception:
        return "<log unavailable>"


# --------------------------------------------------------------------------
# Lifecycle adapter
# --------------------------------------------------------------------------

@dataclass
class RunningServer:
    process: subprocess.Popen
    profile: str
    pid: int
    args: list[str]
    stdout_path: Path
    stderr_path: Path


class LifecycleAdapter:
    """Owns exactly one llama-server process at a time; never touches others."""

    def __init__(self, exe: Path, device: str, host: str, port: int, log_dir: Path,
                 model_paths: Mapping[str, Path],
                 startup_timeout: float = 240.0, unload_timeout: float = 30.0,
                 settle_seconds: float = 3.0):
        self.exe = exe
        self.device = device
        self.host = host
        self.port = port
        self.log_dir = log_dir
        self.model_paths = dict(model_paths)
        self.startup_timeout = startup_timeout
        self.unload_timeout = unload_timeout
        self._settle_seconds = settle_seconds
        self._server: Optional[RunningServer] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _health(self) -> Optional[dict]:
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=2)
            return resp.json()
        except Exception:
            return None

    def assert_port_free_or_owned(self) -> None:
        if self._server is not None:
            return
        health = self._health()
        if health is not None:
            raise LifecycleError(
                f"Port {self.port} is already served by an unowned endpoint "
                f"(health={health}); refusing to attach to or stop it."
            )

    def start(self, model_key: str, profile: str, extra_args: list[str], retries: int = 1) -> tuple[float, int]:
        """Start a server for ``model_key``. Returns (cold_acquire_seconds, retries_used).

        Observed on Vulkan on this hardware: loading a model immediately
        after the previous one's VRAM release is confirmed can crash the
        driver mid-load (``vk::Queue::submit: ErrorDeviceLost``) even
        though the same model loads fine standalone (not reproduced on
        SYCL in 20/20 attempts, see Measurement 2). Retry is bounded,
        explicit, and counted via the returned ``retries_used`` -- never
        silently swallowed.
        """
        attempts = 0
        last_exc: Optional[Exception] = None
        while attempts <= retries:
            try:
                acquire_seconds = self._start_once(model_key, profile, extra_args)
                return acquire_seconds, attempts
            except LifecycleError as exc:
                last_exc = exc
                attempts += 1
                if attempts > retries:
                    raise
                LOG.warning("start(%s) attempt %d failed (%s); retrying after settle delay", profile, attempts, exc)
                time.sleep(5.0)
        raise last_exc  # pragma: no cover

    def _start_once(self, model_key: str, profile: str, extra_args: list[str]) -> float:
        if self._server is not None:
            raise LifecycleError("start() called while a server is already owned; stop() first.")
        self.assert_port_free_or_owned()
        model_path = self.model_paths.get(model_key)
        if model_path is None:
            raise LifecycleError(f"No model path configured for model_key={model_key!r}")
        if not model_path.exists():
            raise LifecycleError(f"Model file not found: {model_path}")
        args = [
            str(self.exe),
            "-m", str(model_path),
            "--device", self.device,
            "--host", self.host,
            "--port", str(self.port),
        ] + extra_args
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        stdout_path = self.log_dir / f"{profile}_{stamp}_stdout.log"
        stderr_path = self.log_dir / f"{profile}_{stamp}_stderr.log"
        t0 = time.monotonic()
        with open(stdout_path, "w", encoding="utf-8") as out_f, \
                open(stderr_path, "w", encoding="utf-8") as err_f:
            proc = subprocess.Popen(
                args, stdout=out_f, stderr=err_f,
                cwd=str(self.exe.parent),
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        self._server = RunningServer(
            process=proc, profile=profile, pid=proc.pid, args=args,
            stdout_path=stdout_path, stderr_path=stderr_path,
        )
        ready = False
        while time.monotonic() - t0 < self.startup_timeout:
            if proc.poll() is not None:
                tail = _tail(stderr_path)
                self._server = None
                raise LifecycleError(
                    f"{profile} llama-server exited during startup (code={proc.returncode}). "
                    f"stderr tail:\n{tail}"
                )
            health = self._health()
            if health is not None and health.get("status") in ("ok", "no slot available"):
                ready = True
                break
            time.sleep(0.25)
        if not ready:
            self._force_kill()
            raise LifecycleError(f"{profile} server did not become ready within {self.startup_timeout}s.")
        return time.monotonic() - t0

    def _force_kill(self) -> None:
        if self._server is None:
            return
        proc = self._server.process
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        self._server = None

    def stop(self) -> tuple[float, bool, int]:
        """Stop the owned server. Returns (unload_seconds, vram_released, final_bytes)."""
        if self._server is None:
            raise LifecycleError("stop() called with no owned server.")
        pid = self._server.pid
        proc = self._server.process
        t0 = time.monotonic()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)
        released, elapsed, final_bytes = wait_vram_released(pid, self.unload_timeout)
        # Driver settle delay: see start()'s retry docstring. Included in
        # unload_seconds since it is part of the real cost of a clean
        # handoff to the next model on this hardware/backend, not hidden.
        time.sleep(self._settle_seconds)
        unload_seconds = time.monotonic() - t0
        self._server = None
        if not released:
            raise LifecycleError(
                f"VRAM for PID {pid} not confirmed released within {self.unload_timeout}s "
                f"(last observed {final_bytes} bytes dedicated usage)."
            )
        return unload_seconds, released, final_bytes

    def sample_vram(self) -> int:
        if self._server is None:
            return 0
        return gpu_dedicated_bytes(self._server.pid)

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.process.poll() is None


# --------------------------------------------------------------------------
# Router: swap-on-demand, only restarts when the requested model differs
# from what is already resident.
# --------------------------------------------------------------------------

@dataclass
class SwitchRecord:
    from_model: Optional[str]
    to_model: str
    cold_acquire_seconds: float
    unload_seconds: Optional[float]
    load_retries: int
    peak_vram_mb: Optional[float]
    timestamp: str


class ModelRouter:
    """Ensures the requested model is resident, swapping only when needed.

    This is what lets ``Gpref(N)`` and ``Ggen(N+1)`` share one Gemma
    lease: two consecutive ``ensure_resident("gemma")`` calls with no
    intervening ``ensure_resident("qwen")`` do not restart anything.
    """

    def __init__(self, adapter: LifecycleAdapter, *,
                 role_profile_names: Mapping[str, str],
                 role_args: Mapping[str, list[str]]):
        self._adapter = adapter
        self._role_profile_names = dict(role_profile_names)
        self._role_args = dict(role_args)
        self.current_model: Optional[str] = None
        self.switches: list[SwitchRecord] = []

    @property
    def base_url(self) -> str:
        return self._adapter.base_url

    def ensure_resident(self, model_key: str) -> Optional[SwitchRecord]:
        if self.current_model == model_key:
            return None
        from_model = self.current_model
        unload_seconds: Optional[float] = None
        if self.current_model is not None:
            unload_seconds, _released, _final_bytes = self._adapter.stop()
        cold_acquire_seconds, load_retries = self._adapter.start(
            model_key,
            self._role_profile_names.get(model_key, model_key),
            self._role_args.get(model_key, []),
        )
        self.current_model = model_key
        peak_vram = self._adapter.sample_vram()
        record = SwitchRecord(
            from_model=from_model,
            to_model=model_key,
            cold_acquire_seconds=cold_acquire_seconds,
            unload_seconds=unload_seconds,
            load_retries=load_retries,
            peak_vram_mb=(peak_vram / (1024 * 1024)) if peak_vram and peak_vram > 0 else None,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self.switches.append(record)
        return record

    def release(self) -> Optional[float]:
        """Stop whatever is resident (e.g. at the end of a run)."""
        if self.current_model is None:
            return None
        unload_seconds, _released, _final_bytes = self._adapter.stop()
        self.current_model = None
        return unload_seconds
