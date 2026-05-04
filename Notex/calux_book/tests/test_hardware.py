"""Tests for calux_book.hardware — GPU detection and hardware profiling."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from calux_book.hardware import HardwareProfile, detect_hardware


class TestHardwareProfile:
    """Test HardwareProfile data class and properties."""

    def test_gpu_tier(self):
        hp = HardwareProfile(
            has_cuda=True,
            gpu_name="RTX 4060", vram_mb=8192,
            ram_mb=16384, cpu_cores=8, tier="gpu",
        )
        assert hp.is_gpu is True

    def test_cpu_tier(self):
        hp = HardwareProfile(
            has_cuda=False,
            gpu_name="", vram_mb=0,
            ram_mb=16384, cpu_cores=8, tier="cpu",
        )
        assert hp.is_gpu is False

    def test_frozen(self):
        hp = HardwareProfile(
            has_cuda=False,
            gpu_name="", vram_mb=0,
            ram_mb=8192, cpu_cores=4, tier="cpu",
        )
        with pytest.raises(AttributeError):
            hp.tier = "gpu"  # type: ignore[misc]


class TestDetectHardware:
    """Test detect_hardware() with mocked backends."""

    def test_no_gpu_at_all(self):
        """Simulates a system with no GPU and no nvidia-smi."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            profile = detect_hardware()

        assert profile.has_cuda is False
        assert profile.tier == "cpu"
        assert profile.gpu_name == ""

    def test_nvidia_smi_detection(self):
        """GPU detected via nvidia-smi."""
        smi_result = MagicMock()
        smi_result.returncode = 0
        smi_result.stdout = "NVIDIA GeForce RTX 3080, 10240"

        with patch("subprocess.run", return_value=smi_result):
            profile = detect_hardware()

        assert profile.has_cuda is True
        assert profile.gpu_name == "NVIDIA GeForce RTX 3080"
        assert profile.vram_mb == 10240
        assert profile.tier == "gpu"

    def test_small_gpu_gets_cpu_tier(self):
        """GPU with < 4 GB VRAM gets classified as CPU tier."""
        smi_result = MagicMock()
        smi_result.returncode = 0
        smi_result.stdout = "GT 1030, 2048"

        with patch("subprocess.run", return_value=smi_result):
            profile = detect_hardware()

        assert profile.has_cuda is True
        assert profile.tier == "cpu"  # too little VRAM


class TestGetHardwareProfile:
    """Test the singleton getter."""

    def test_singleton_caching(self):
        import calux_book.hardware as hw_mod

        saved = hw_mod._profile
        try:
            hw_mod._profile = None
            p1 = hw_mod.get_hardware_profile()
            p2 = hw_mod.get_hardware_profile()
            assert p1 is p2  # same object
        finally:
            hw_mod._profile = saved
