"""Hardware detection for system-aware resource management.

Probes the system for NVIDIA GPU presence (via nvidia-smi), RAM, and CPU
cores, then returns a :class:`HardwareProfile` used for resource throttling.

No torch dependency — GPU detection uses nvidia-smi only.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger("calux_book.hardware")


@dataclasses.dataclass(frozen=True)
class HardwareProfile:
    """Immutable snapshot of system capabilities."""

    has_cuda: bool
    gpu_name: str
    vram_mb: int
    ram_mb: int
    cpu_cores: int
    tier: str  # "gpu" | "cpu"

    # -- Convenience helpers -------------------------------------------------

    @property
    def is_gpu(self) -> bool:
        return self.tier == "gpu"


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------

def detect_hardware() -> HardwareProfile:
    """Detect GPU presence, VRAM, RAM, and CPU cores.

    Uses nvidia-smi to detect NVIDIA GPUs (no torch required).
    """
    has_cuda = False
    gpu_name = ""
    vram_mb = 0

    # nvidia-smi detection ---------------------------------------------------
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(",")
            gpu_name = parts[0].strip()
            vram_mb = int(float(parts[1].strip()))
            has_cuda = True
    except Exception:
        pass

    # System stats ----------------------------------------------------------
    ram_mb = _get_ram_mb()
    cpu_cores = os.cpu_count() or 1

    # Tier decision ---------------------------------------------------------
    tier = "gpu" if has_cuda and vram_mb >= 4096 else "cpu"

    profile = HardwareProfile(
        has_cuda=has_cuda,
        gpu_name=gpu_name,
        vram_mb=vram_mb,
        ram_mb=ram_mb,
        cpu_cores=cpu_cores,
        tier=tier,
    )

    logger.info("Hardware: %s", profile)
    return profile


# ---------------------------------------------------------------------------
# RAM detection helpers
# ---------------------------------------------------------------------------

def _get_ram_mb() -> int:
    """Best-effort total RAM in MiB."""
    # psutil is the cleanest cross-platform way
    try:
        import psutil

        return psutil.virtual_memory().total // (1024 * 1024)
    except ImportError:
        pass

    # Windows ctypes fallback
    try:
        import ctypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
        return stat.ullTotalPhys // (1024 * 1024)
    except Exception:
        pass

    # Linux /proc fallback
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    return int(line.split()[1]) // 1024  # kB → MB
    except Exception:
        pass

    return 0


# ---------------------------------------------------------------------------
# Hardware-adaptive defaults
# ---------------------------------------------------------------------------

def apply_hardware_defaults(cfg: Any, profile: HardwareProfile | None = None) -> None:
    """Adjust *cfg* settings based on detected hardware.

    Only overrides settings that are still at their default values (i.e. not
    explicitly set by the user via env vars).  Safe to call multiple times.

    Tuning targets:
        8 GB RAM / 4-core  → conservative (laptop)
        16 GB RAM / 8-core → balanced (desktop)
        GPU ≥4 GB VRAM     → generous
    """
    if profile is None:
        profile = get_hardware_profile()

    ram = profile.ram_mb
    cores = profile.cpu_cores

    # -- Embedding threads --------------------------------------------------
    if cfg.embedding_threads == 2:  # still at default
        if ram >= 16_000:
            cfg.embedding_threads = min(cores - 1, 4)
        else:
            cfg.embedding_threads = min(cores - 1, 2)
        cfg.embedding_threads = max(1, cfg.embedding_threads)

    # -- Embedding batch size -----------------------------------------------
    if cfg.embedding_batch_size == 16:  # still at default
        if profile.is_gpu:
            cfg.embedding_batch_size = 64
        elif ram >= 16_000:
            cfg.embedding_batch_size = 32
        else:
            cfg.embedding_batch_size = 16

    # -- Rerank candidates --------------------------------------------------
    if cfg.rerank_candidates == 20:  # still at default
        if profile.is_gpu:
            cfg.rerank_candidates = 30
        elif ram >= 16_000:
            cfg.rerank_candidates = 20
        else:
            cfg.rerank_candidates = 15

    # -- Summary concurrency ------------------------------------------------
    if cfg.summary_concurrency == 6:  # still at default
        if ram < 10_000:
            cfg.summary_concurrency = 2
        elif ram < 16_000:
            cfg.summary_concurrency = 3
        else:
            cfg.summary_concurrency = 4

    logger.info(
        "Hardware-adaptive defaults applied: threads=%d, batch=%d, "
        "rerank_k=%d, summary_conc=%d (RAM=%dMB, cores=%d, tier=%s)",
        cfg.embedding_threads, cfg.embedding_batch_size,
        cfg.rerank_candidates, cfg.summary_concurrency,
        ram, cores, profile.tier,
    )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_profile: HardwareProfile | None = None


def get_hardware_profile() -> HardwareProfile:
    """Return the cached hardware profile (created on first call)."""
    global _profile
    if _profile is None:
        _profile = detect_hardware()
    return _profile
