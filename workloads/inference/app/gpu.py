"""실행 장치(CPU / GPU) 선택과 GPU 상태 보고.

이 프로젝트의 비교 축 하나가 **실행 장치**다. 같은 이미지, 같은 모델,
같은 노드에서 `OCR_DEVICE` 만 바꿔 CPU 와 GPU 를 비교하기 위해, 장치 결정을
이 한 곳에 모아 둔다.

    OCR_DEVICE=auto    CUDA 를 쓸 수 있으면 gpu:0, 아니면 cpu  (기본값)
    OCR_DEVICE=cpu     강제로 CPU
    OCR_DEVICE=gpu:0   강제로 첫 번째 GPU

**auto 를 벤치마크에 쓰지 말 것.** 측정 조건이 환경에 따라 조용히 달라진다.
ConfigMap 에서 cpu / gpu:0 을 명시한다. auto 는 맥이나 CI 에서 그냥
띄워 보기 위한 기본값이다.

paddle 호출은 전부 예외로 감싼다. CPU 전용 머신(맥, CI)이나 CPU 빌드
이미지에서도 "CUDA 없음" 을 보고하고 계속 떠야지, 죽으면 안 된다.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ConfigMap 의 OCR_DEVICE 로 덮어쓴다. 위 docstring 참고.
DEVICE_SETTING = os.getenv("OCR_DEVICE", "auto").strip().lower()


def _import_paddle():
    """Import paddle lazily so a missing/broken install does not kill the app."""
    try:
        import paddle

        return paddle
    except Exception as exc:  # noqa: BLE001 - any import failure is non-fatal here
        logger.warning("paddle import failed: %s", exc)
        return None


def cuda_is_usable() -> bool:
    """True only when paddle is built with CUDA and a device is actually visible."""
    paddle = _import_paddle()
    if paddle is None:
        return False
    try:
        if not paddle.is_compiled_with_cuda():
            return False
        return paddle.device.cuda.device_count() > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("CUDA probe failed: %s", exc)
        return False


def resolve_device() -> str:
    """Device string handed to PaddleOCR, e.g. "gpu:0" or "cpu"."""
    if DEVICE_SETTING != "auto":
        return DEVICE_SETTING
    return "gpu:0" if cuda_is_usable() else "cpu"


def get_gpu_info() -> dict:
    """Snapshot of the GPU state for the /gpu endpoint. Never raises."""
    info = {
        "cuda_available": False,
        "gpu_count": 0,
        "gpu_name": None,
        "gpu_memory_total_mb": None,
        "compiled_with_cuda": False,
        "paddle_version": None,
        "paddle_device": None,
        "device_setting": DEVICE_SETTING,
        "error": None,
    }

    paddle = _import_paddle()
    if paddle is None:
        info["error"] = "paddle is not installed or failed to import"
        return info

    try:
        info["paddle_version"] = paddle.__version__
        info["compiled_with_cuda"] = bool(paddle.is_compiled_with_cuda())
        # Device paddle currently runs on, e.g. "gpu:0" / "cpu".
        info["paddle_device"] = paddle.device.get_device()

        if info["compiled_with_cuda"]:
            count = paddle.device.cuda.device_count()
            info["gpu_count"] = count
            info["cuda_available"] = count > 0
            if count > 0:
                props = paddle.device.cuda.get_device_properties(0)
                info["gpu_name"] = props.name
                info["gpu_memory_total_mb"] = round(props.total_memory / 1024 / 1024)
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("failed to collect GPU info: %s", exc)

    return info
