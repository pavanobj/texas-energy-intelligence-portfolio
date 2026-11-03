import importlib.util
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "bilstm_trainer", Path(__file__).parents[1] / "scripts" / "train_weather_aware_bilstm_curve.py"
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


class _CpuOnlyTorch:
    class cuda:
        @staticmethod
        def is_available() -> bool:
            return False

    @staticmethod
    def device(value: str) -> str:
        return value


def test_bilstm_cuda_request_refuses_cpu_only_runtime() -> None:
    with pytest.raises(RuntimeError, match="CUDA-enabled"):
        _MODULE._training_device(_CpuOnlyTorch, "cuda")


def test_bilstm_cpu_mode_remains_explicit_for_local_technical_smoke() -> None:
    assert _MODULE._training_device(_CpuOnlyTorch, "cpu") == "cpu"
