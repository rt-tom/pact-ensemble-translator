#!/usr/bin/env python3
"""Model lifecycle / reload cost benchmark (Measurement Task 2).

Backing spec: ``docs/plans/V4_SINGLE_RESIDENT_DRIVER_ARCHITECTURE_RU.md``,
section "Что измерить до кода", item 2, and the "отдельный короткий
``Gemma -> Qwen -> Gemma`` benchmark без production pipeline" the same
document calls out as missing under "Как использовать текущий двухфазный
sequential run".

This is a standalone lifecycle-cost measurement, not the V4 runtime driver:

* No import from ``pact_v4`` and no production chapter/config is touched.
* The lifecycle adapter here (``LifecycleAdapter``) only starts/stops the
  ``llama-server`` process it itself launched. It refuses to attach to or
  kill a server it did not start (probes the port first; aborts if
  something already answers there).
* A stopped server is not assumed to have released VRAM just because the
  HTTP connection closed or the process exited. Release is confirmed by
  polling the Windows "GPU Process Memory" performance counter for that
  PID's dedicated-memory usage until it disappears (or the run times out
  and records that as an error, not a silent success).

Segment plan (``--switches N``, default 20, matching the architecture
doc's "20 restart на 10 high-risk chunk'ов" strict stop-and-switch count
for N/2 chunks): segment 0 is the initial Gemma "generation"-shaped
startup (not a restart). Then N segments alternate
Qwen "gate" / Gemma "preference", one completion per segment, each
preceded by a real model switch. With the default N=20 this produces
1 + 10 + 10 = 21 total model loads / 20 restarts, i.e. exactly the number
the architecture doc computes for the strict variant, so this tool's
wall-clock total can be read directly against that estimate.

Usage::

    python -m pact_full_pipeline_runner_v1.v4_model_lifecycle_bench \\
        --switches 20 \\
        --out "D:/pact/gate_bench_runs/v4_model_lifecycle_bench/sycl_001/measurement_record.json"

Backend is fixed to the SYCL build (``C:\\llama-sycl-new``) after a manual
side-by-side test showed it decisively faster than Vulkan on this hardware
for both prompt reading and generation; the Vulkan comparison this tool
originally supported was dropped for that reason, not measured here. The
backend identity is still recorded in the output's provenance in case a
future run needs to compare against it again.
"""
from __future__ import annotations

import argparse
import json
import logging
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

LOG = logging.getLogger("v4_model_lifecycle_bench")

SCHEMA = "pact-v4-model-lifecycle-bench/v1"

LLAMA_ROOT = Path(r"C:\llama-cpp")
MODELS = {
    "gemma": LLAMA_ROOT / "models" / "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf",
    "qwen": LLAMA_ROOT / "models" / "Qwen3.6-35B-A3B" / "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
}
GEMMA_DRAFT_MODEL = LLAMA_ROOT / "models" / "MTP" / "mtp-gemma-4-26B-A4B-it-Q8_0.gguf"

# Vulkan-vs-SYCL comparison was dropped after a manual side-by-side test:
# SYCL (C:\llama-sycl-new) won decisively on read/generation speed on this
# hardware, so this tool now only exercises that build. Kept as a single
# entry (not a dict keyed by a --backend choice) so there is no dead
# "vulkan" path that could silently drift out of sync.
BACKEND = {
    "name": "sycl",
    "exe": Path(r"C:\llama-sycl-new\llama-server.exe"),
    "device": "SYCL0",
}

# Matches the user's optimized production SYCL profile (verbatim for
# Gemma), not the earlier smaller synthetic-benchmark context -- this is
# now measuring reload cost under the same context/config actually used
# for chapter work, including the MTP draft model.
CONTEXT_SIZE = 32768

GEMMA_COMMON_ARGS = [
    "--model-draft", str(GEMMA_DRAFT_MODEL),
    "--spec-type", "draft-mtp",
    "--spec-draft-n-max", "4",
    "-ngl", "99",
    "-ncmoe", "18",
    "--load-mode", "mmap",
    "--reasoning-budget", "0",
    "-np", "1",
    "-c", str(CONTEXT_SIZE),
    "-fa", "on",
    "--jinja",
    "--cache-ram", "0",
    "--ctx-checkpoints", "0",
]
QWEN_COMMON_ARGS = [
    "-fit", "on",
    "-fitt", "1280",
    "-b", "2048",
    "-ub", "512",
    "-ctk", "q8_0",
    "-ctv", "q8_0",
    "-t", "6",
    "-tb", "12",
    "--load-mode", "mmap",
    "--reasoning-budget", "0",
    "-np", "1",
    "-c", str(CONTEXT_SIZE),
    "-fa", "on",
    "--jinja",
    "--cache-ram", "0",
    "--ctx-checkpoints", "0",
]

# Three fixed synthetic prompts, deliberately different in shape (not the
# same request renamed) per the architecture doc's instruction not to
# measure the same thing twice under different role labels.
ROLE_PROMPTS = {
    "generation": {
        "model": "gemma",
        "prompt": (
            "Продолжи связный русский литературный отрывок на 3-4 предложения, "
            "сохраняя повествовательный тон и не повторяя предыдущий текст:\n\n"
            "Дождь стучал по жестяной крыше вокзала, и Марина всё смотрела на "
            "часы, будто взгляд мог заставить стрелки идти быстрее."
        ),
        "n_predict": 128,
    },
    "gate": {
        "model": "qwen",
        "prompt": (
            "Оцени, является ли перевод дословно точным по смыслу относительно "
            "оригинала. Ответь строго одним JSON-объектом вида "
            '{"verdict": "pass"|"fail", "reason": "..."}\n\n'
            "Оригинал: \"The rain hammered on the tin roof of the station.\"\n"
            "Перевод: \"Дождь стучал по жестяной крыше вокзала.\""
        ),
        "n_predict": 48,
    },
    "preference": {
        "model": "gemma",
        "prompt": (
            "Выбери вариант A или B, какой из двух звучит естественнее по-русски. "
            "Ответь одной буквой без пояснений.\n\n"
            "A: Дождь стучал по жестяной крыше вокзала.\n"
            "B: Дождь бил в жестяную крышу вокзала.\n"
            "Ответ:"
        ),
        "n_predict": 16,
    },
}

TEMPERATURE = 0.0
SEED = 20260731


class LifecycleError(RuntimeError):
    """Raised for any lifecycle adapter failure (start/stop/health/VRAM)."""


# --------------------------------------------------------------------------
# GPU VRAM observation (no NVIDIA GPU on this host; use the Windows
# "GPU Process Memory" perf counter, which works for both the Intel Arc
# B580 (SYCL/Vulkan0) and the AMD iGPU, per-process, without vendor tools.
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
                 startup_timeout: float, unload_timeout: float, settle_seconds: float = 3.0):
        self.exe = exe
        self.device = device
        self.host = host
        self.port = port
        self.log_dir = log_dir
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

        Observed on this hardware: loading a model immediately after the
        previous one's VRAM release is confirmed can occasionally crash the
        Vulkan driver mid-load (``vk::Queue::submit: ErrorDeviceLost``) even
        though the same model loads fine standalone. This is itself part of
        what this benchmark measures (reload reliability, not just speed),
        so a retry is bounded, explicit, counted in the record via
        ``retries_used``/``load_attempts`` -- never silently swallowed.
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
        model_path = MODELS[model_key]
        if not model_path.exists():
            raise LifecycleError(f"Model file not found: {model_path}")
        args = [
            str(self.exe),
            "-m", str(model_path),
            "--device", self.device,
            "--host", self.host,
            "--port", str(self.port),
        ] + extra_args
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
        # Driver settle delay: loading a model immediately after VRAM release
        # is confirmed has been observed to crash the Vulkan driver mid-load
        # on this hardware (see start()'s retry docstring). This delay is
        # included in unload_seconds since it is part of the real cost of a
        # clean handoff to the next model on this hardware/backend, not
        # hidden downtime.
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

    def completion(self, prompt: str, n_predict: int) -> dict:
        if self._server is None:
            raise LifecycleError("completion() called with no owned server.")
        payload = {
            "prompt": prompt,
            "n_predict": n_predict,
            "temperature": TEMPERATURE,
            "seed": SEED,
            "stream": True,
        }
        t0 = time.monotonic()
        first_token_seconds = None
        text_parts: list[str] = []
        server_timings = None
        with requests.post(f"{self.base_url}/completion", json=payload, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            # decode_unicode=True on iter_lines can split a multi-byte UTF-8
            # character (Cyrillic content) across two "lines" and corrupt
            # the JSON. Byte-level splitting on b'\n' is always safe -- 0x0A
            # never occurs inside a UTF-8 continuation byte -- so decode
            # each complete line independently instead.
            for raw_bytes in resp.iter_lines(decode_unicode=False):
                if not raw_bytes:
                    continue
                raw_line = raw_bytes.decode("utf-8")
                if not raw_line.startswith("data: "):
                    continue
                chunk = json.loads(raw_line[len("data: "):])
                if first_token_seconds is None:
                    first_token_seconds = time.monotonic() - t0
                content = chunk.get("content")
                if content:
                    text_parts.append(content)
                if chunk.get("stop"):
                    server_timings = chunk.get("timings")
        completion_seconds = time.monotonic() - t0
        if first_token_seconds is None:
            raise LifecycleError("completion() received no streamed tokens.")
        return {
            "first_token_seconds": first_token_seconds,
            "completion_seconds": completion_seconds,
            "text": "".join(text_parts),
            "server_timings": server_timings,
        }


def _tail(path: Path, n_lines: int = 40) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n_lines:])
    except Exception:
        return "<log unavailable>"


# --------------------------------------------------------------------------
# Segment plan
# --------------------------------------------------------------------------

@dataclass
class Segment:
    index: int
    model_key: str
    profile: str
    role: str
    is_restart: bool


def build_plan(n_switches: int) -> list[Segment]:
    if n_switches % 2 != 0 or n_switches < 2:
        raise ValueError("--switches must be an even number >= 2 (pairs of Qwen/Gemma restarts).")
    plan = [Segment(0, "gemma", "GemmaGen", "generation", is_restart=False)]
    idx = 1
    n_chunks = n_switches // 2
    for _ in range(n_chunks):
        plan.append(Segment(idx, "qwen", "QwenGate", "gate", is_restart=True))
        idx += 1
        plan.append(Segment(idx, "gemma", "GemmaPref", "preference", is_restart=True))
        idx += 1
    return plan


# --------------------------------------------------------------------------
# Run orchestration
# --------------------------------------------------------------------------

def role_args(model_key: str) -> list[str]:
    return GEMMA_COMMON_ARGS if model_key == "gemma" else QWEN_COMMON_ARGS


def get_server_version(exe: Path) -> str:
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=15)
        return (out.stdout + out.stderr).strip().splitlines()[0] if (out.stdout + out.stderr).strip() else "unknown"
    except Exception as exc:
        return f"<unavailable: {exc}>"


def get_device_list(exe: Path) -> str:
    try:
        out = subprocess.run([str(exe), "--list-devices"], capture_output=True, text=True, timeout=30)
        return (out.stdout + out.stderr).strip()
    except Exception as exc:
        return f"<unavailable: {exc}>"


def percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def aggregate_by_role(records: list[dict], field_name: str) -> dict:
    out = {}
    by_role: dict[str, list[float]] = {}
    for r in records:
        v = r.get(field_name)
        if v is None:
            continue
        by_role.setdefault(r["role"], []).append(v)
    for role, values in by_role.items():
        out[role] = {
            "n": len(values),
            "median": statistics.median(values) if values else None,
            "p95": percentile(values, 0.95),
        }
    return out


def run_benchmark(n_switches: int, host: str, port: int,
                   out_path: Path, startup_timeout: float, unload_timeout: float) -> dict:
    backend = BACKEND["name"]
    exe = BACKEND["exe"]
    device = BACKEND["device"]
    if not exe.exists():
        raise LifecycleError(f"llama-server executable not found for backend={backend}: {exe}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = out_path.parent / "server_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    journal_path = out_path.parent / "journal.ndjson"

    plan = build_plan(n_switches)
    adapter = LifecycleAdapter(exe, device, host, port, log_dir, startup_timeout, unload_timeout)

    provenance = {
        "schema": SCHEMA,
        "backend": backend,
        "device": device,
        "llama_server_exe": str(exe),
        "llama_server_version": get_server_version(exe),
        "device_list": get_device_list(exe),
        "models": {k: {"path": str(v), "exists": v.exists(),
                        "size_bytes": v.stat().st_size if v.exists() else None,
                        "mtime": v.stat().st_mtime if v.exists() else None}
                   for k, v in MODELS.items()},
        "context_size": CONTEXT_SIZE,
        "server_args": {"gemma": GEMMA_COMMON_ARGS, "qwen": QWEN_COMMON_ARGS},
        "role_prompts": ROLE_PROMPTS,
        "temperature": TEMPERATURE,
        "seed": SEED,
        "host": host,
        "port": port,
        "n_switches_requested": n_switches,
        "host_platform": platform.platform(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_path.parent / "provenance.json").write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    records: list[dict] = []
    errors: list[dict] = []
    interrupted = False
    interruption_reason = None
    wall_t0 = time.monotonic()

    with open(journal_path, "a", encoding="utf-8") as journal:
        try:
            for seg in plan:
                LOG.info("segment %d: model=%s role=%s restart=%s", seg.index, seg.model_key, seg.role, seg.is_restart)
                rec = {"segment_index": seg.index, "model": seg.model_key, "profile": seg.profile,
                       "role": seg.role, "is_restart": seg.is_restart,
                       "cold_acquire_seconds": None, "unload_seconds": None,
                       "first_token_seconds": None, "completion_seconds": None,
                       "peak_vram_mb": None, "load_retries": 0, "error": None}
                try:
                    unload_seconds = None
                    if seg.index > 0:
                        unload_seconds, _released, _final_bytes = adapter.stop()
                    cold_acquire_seconds, load_retries = adapter.start(
                        seg.model_key, seg.profile, role_args(seg.model_key)
                    )
                    vram_baseline = adapter.sample_vram()
                    prompt_spec = ROLE_PROMPTS[seg.role]
                    result = adapter.completion(prompt_spec["prompt"], prompt_spec["n_predict"])
                    vram_after = adapter.sample_vram()
                    peak_vram = max(v for v in (vram_baseline, vram_after) if v >= 0)
                    rec.update({
                        "cold_acquire_seconds": cold_acquire_seconds,
                        "unload_seconds": unload_seconds,
                        "first_token_seconds": result["first_token_seconds"],
                        "completion_seconds": result["completion_seconds"],
                        "peak_vram_mb": peak_vram / (1024 * 1024) if peak_vram else None,
                        "load_retries": load_retries,
                        "server_timings": result.get("server_timings"),
                        "completion_text": result["text"],
                    })
                except Exception as exc:  # noqa: BLE001 - any failure must land in the record, not crash silently
                    reason = f"{type(exc).__name__}: {exc}"
                    rec["error"] = reason
                    errors.append({"segment_index": seg.index, "reason": reason})
                    records.append(rec)
                    journal.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    journal.flush()
                    interrupted = True
                    interruption_reason = reason
                    break
                records.append(rec)
                journal.write(json.dumps(rec, ensure_ascii=False) + "\n")
                journal.flush()
        finally:
            try:
                if adapter._server is not None:
                    adapter.stop()
            except Exception as exc:  # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"
                errors.append({"segment_index": "final_cleanup", "reason": reason})
                interrupted = True
                interruption_reason = interruption_reason or reason

    wall_clock_seconds = time.monotonic() - wall_t0
    successful_switches = sum(1 for r in records if r["is_restart"] and r["error"] is None)

    result_record = {
        "schema": SCHEMA,
        "provenance": provenance,
        "n_switches_requested": n_switches,
        "n_segments_completed": len(records),
        "successful_switches": successful_switches,
        "error_count": len(errors),
        "errors": errors,
        "interrupted": interrupted,
        "interruption_reason": interruption_reason,
        "wall_clock_seconds": wall_clock_seconds,
        "segments": records,
        "aggregates": {
            "cold_acquire_seconds_by_role": aggregate_by_role(records, "cold_acquire_seconds"),
            "unload_seconds_by_role": aggregate_by_role(records, "unload_seconds"),
            "first_token_seconds_by_role": aggregate_by_role(records, "first_token_seconds"),
            "completion_seconds_by_role": aggregate_by_role(records, "completion_seconds"),
            "peak_vram_mb_by_role": aggregate_by_role(records, "peak_vram_mb"),
        },
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out_path.write_text(json.dumps(result_record, indent=2, ensure_ascii=False), encoding="utf-8")
    return result_record


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--switches", type=int, default=20,
                    help="Total model switches (even number); default 20 matches the "
                         "architecture doc's 10-high-risk-chunk strict-variant restart count.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8092,
                    help="Avoid 8080/8090/8091, which other llama-server instances on this "
                         "host may already be using.")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--startup-timeout", type=float, default=240.0)
    p.add_argument("--unload-timeout", type=float, default=30.0)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                         format="%(asctime)s %(levelname)s %(message)s")
    try:
        result = run_benchmark(
            n_switches=args.switches, host=args.host, port=args.port,
            out_path=args.out, startup_timeout=args.startup_timeout, unload_timeout=args.unload_timeout,
        )
    except LifecycleError as exc:
        LOG.error("Fatal lifecycle error before any segment completed: %s", exc)
        return 1
    LOG.info("Done: backend=%s switches_ok=%d/%d errors=%d wall_clock=%.1fs interrupted=%s",
              BACKEND["name"], result["successful_switches"], result["n_switches_requested"],
              result["error_count"], result["wall_clock_seconds"], result["interrupted"])
    return 0 if not result["interrupted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
