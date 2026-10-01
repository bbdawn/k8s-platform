"""PaddleOCR 엔진 생성과 추론.

엔진은 애플리케이션이 뜰 때 한 번만 만들고 모든 요청이 재사용한다. 모델
로딩 시간이 벤치마크 수치에 섞이지 않게 하기 위해서다.
"""

from __future__ import annotations

import io
import logging
import os
import threading

import numpy as np
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

# PaddleOCR은 device 문자열을 명시적으로 받는다. 여기서 고정해 두면 다른 것을
# 자동으로 골라잡는 일이 없다.
DEVICE = "cpu"

# --- configuration (전부 env / ConfigMap 으로 덮어쓸 수 있다) -----------------

# PaddleOCR 인식 언어 모델. korean / en / ch / japan 등.
OCR_LANG = os.getenv("OCR_LANG", "korean")

# 검출 모델. 빈 문자열이면 PaddleOCR 기본값을 쓴다(lang=korean이면 server판).
#
# 기본값으로 두는 것이 맞다. mobile판으로 바꾸면 2코어 노드의 메모리 문제가
# 풀릴 것 같지만, 한국어를 읽지 못한다. 맥에서 같은 영수증으로 잰 값:
#
#   검출              elapsed_ms   RSS      인식 결과
#   server(기본값)       6,200ms   2,628MB   한글 10줄 전부 정확
#   PP-OCRv5_mobile_det  1,400ms   1,782MB   숫자만. 한글은 confidence 0.0, 빈 문자열
#
# 4.5배 빠르고 메모리도 800MB 적지만 쓸 수 없다. 더 고약한 것은 **count가 10으로
# 똑같다**는 점이다. 검출은 상자를 10개 그대로 찾아내고 인식만 실패하므로,
# "10건 인식됨"만 보는 검증은 이 고장을 통과시킨다. 바꿀 일이 있으면 건수가
# 아니라 텍스트 내용을 비교할 것.
#
# 벤치마크에서는 이 값을 고정해 둘 것. 검출 모델이 바뀌면 수치를 비교할 수 없다.
DET_MODEL = os.getenv("OCR_DET_MODEL", "").strip()

# 텍스트 줄 방향 분류는 추론 시간을 더 쓴다. 벤치마크가 검출 + 인식만 재도록
# 기본값은 꺼 둔다.
USE_TEXTLINE_ORIENTATION = os.getenv("OCR_USE_TEXTLINE_ORIENTATION", "false").lower() == "true"

# oneDNN은 paddle의 CPU 가속 라이브러리다. 기본값을 끔으로 둔 이유는, paddle
# 3.3.1의 PIR 실행기가 이 파이프라인이 만드는 oneDNN 커널을 처리하지 못하기
# 때문이다:
#   NotImplementedError: (Unimplemented) ConvertPirAttribute2RuntimeAttribute
#   not support [pir::ArrayAttribute<pir::DoubleAttribute>]
#     at .../new_executor/instruction/onednn/onednn_instruction.cc:116
# 엔진 생성과 모델 로드는 성공하고 첫 predict()에서 죽는다. 그래서 /health도
# /ready도 멀쩡해 보이는데 /ocr만 전부 500이 된다.
#
# PaddleOCR 기본값이 True라서 그대로 두면 CPU 추론이 아예 안 된다. 끄면 추론이
# 느려지므로, 이 workload가 내는 모든 수치는 "oneDNN을 끈" 값이다. 수치를 적을
# 때 이 조건을 같이 밝힐 것.
ENABLE_MKLDNN = os.getenv("OCR_ENABLE_MKLDNN", "false").lower() == "true"

# paddle 자체의 스레드 수. OMP_NUM_THREADS와는 다른 층위다. PaddleOCR 기본값이
# 10이고 컨테이너의 CPU limit을 전혀 보지 않아서, 2코어로 제한된 파드가 스레드
# 10개를 띄우고 서로 밟는다. 지연이 들쭉날쭉해지는데 벤치마크가 가장 피해야 할
# 일이다. deployment.yaml의 CPU limit과 맞춰 둘 것.
CPU_THREADS = max(1, int(os.getenv("OCR_CPU_THREADS", "2")))

# PaddleOCR predictor는 thread-safe하지 않다. 공유 엔진에 동시에 들어갈 수 있는
# predict() 호출 수를 여기서 제한한다:
#   1 -> 안전한 기본값. 파드당 한 번에 하나씩 추론한다(replica로 늘린다)
#   N -> 동시 N건 허용. 파드 내부 동시성을 실험할 때만
MAX_CONCURRENCY = max(1, int(os.getenv("OCR_MAX_CONCURRENCY", "1")))

# 디코딩하기 전에 너무 큰 업로드를 먼저 거른다.
MAX_IMAGE_BYTES = int(os.getenv("OCR_MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))

# 디코딩한 이미지의 긴 변을 이 값으로 제한한다. 0이면 끈다.
#
# 휴대폰 카메라는 3000~4000px 사진을 만든다. 원본 그대로 두면 파이프라인이
# 디코딩한 배열과 그 float32 사본들을 함께 들고 있게 되는데(3024x4032 RGB는
# uint8로 36MB, float32로는 146MB이고 사본이 하나가 아니다), 메모리 제한이
# 걸린 파드에서는 요청 처리 도중 컨테이너가 죽기에 충분하다. 클라이언트는
# 상태 코드도 없이 연결이 끊기는 것만 본다 - 브라우저는 "TypeError: Failed to
# fetch", curl은 exit 52. 답할 주체가 사라졌기 때문이다. 16GB 노트북에서는
# 여기까지 가지 않아서, 제한이 걸린 컨테이너에 넣어야 비로소 나타난다.
#
# PaddleOCR의 text_det_limit_side_len이 아니라 여기서 줄이는 것은 의도다.
# 그 설정은 검출 입력만 제한하고, 인식 단계는 여전히 우리가 넘긴 배열에서
# 잘라 쓴다. 배열 자체를 줄여야 양쪽이 같이 잡힌다.
#
# 입력 크기를 고정하는 효과도 있는데 벤치마크에는 어차피 필요한 것이다.
# 그러지 않으면 수치가 "휴대폰이 어쩌다 만든 해상도"에 좌우된다.
MAX_IMAGE_SIDE = int(os.getenv("OCR_MAX_IMAGE_SIDE", "1600"))

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png"}
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG"}

_predict_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)


class ImageError(ValueError):
    """업로드된 바이트가 쓸 수 있는 jpg/png 이미지가 아닐 때 발생한다."""


def create_ocr_engine():
    """PaddleOCR 엔진을 만든다. 기동 중 한 번만 호출된다."""
    from paddleocr import PaddleOCR

    logger.info(
        "initializing PaddleOCR (device=%s, lang=%s, det_model=%s, "
        "textline_orientation=%s, max_concurrency=%d, mkldnn=%s, cpu_threads=%d)",
        DEVICE,
        OCR_LANG,
        DET_MODEL or "(PaddleOCR 기본값)",
        USE_TEXTLINE_ORIENTATION,
        MAX_CONCURRENCY,
        ENABLE_MKLDNN,
        CPU_THREADS,
    )

    # 문서 방향 보정과 왜곡 보정은 벤치마크 workload에 필요 없는 추가 단계다.
    # 꺼 두어야 실행 간 비교가 가능하다.
    # enable_mkldnn / cpu_threads는 **kwargs를 통해 PaddleOCR에 들어가고,
    # PaddleOCR이 그대로 PaddleX에 넘긴다.
    # 빈 값이면 인자를 아예 넘기지 않는다. PaddleOCR이 기본값을 고르게 둔다.
    det_kwargs = {"text_detection_model_name": DET_MODEL} if DET_MODEL else {}

    engine = PaddleOCR(
        device=DEVICE,
        lang=OCR_LANG,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=USE_TEXTLINE_ORIENTATION,
        enable_mkldnn=ENABLE_MKLDNN,
        cpu_threads=CPU_THREADS,
        **det_kwargs,
    )
    logger.info("PaddleOCR ready on %s", DEVICE)
    return engine


def runtime_info() -> dict:
    """/info 엔드포인트가 쓸 스냅샷. 예외를 던지지 않는다.

    paddle은 늦게, 예외 처리와 함께 import한다. 설치가 깨졌으면 그 사실이
    보고되어야지, 파드에 무슨 문제가 있는지 알아보려고 부른 엔드포인트가
    같은 문제로 500을 내면 안 된다.
    """
    info = {"paddle_version": None, "device": DEVICE, "error": None}
    try:
        import paddle

        info["paddle_version"] = paddle.__version__
    except Exception as exc:  # noqa: BLE001 - reporting beats crashing here
        info["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("paddle import failed: %s", exc)
    return info


def _warm_up_image() -> np.ndarray:
    """워밍업용. 실제로 표시가 있는 작은 이미지다.

    빈 캔버스가 아니다. 흰 배경에서는 검출이 아무것도 찾지 못해 인식 단계가
    아예 실행되지 않고, 그러면 워밍업이 파이프라인의 절반만 확인하게 된다.
    이 틈이 실제 장애를 가렸다. "warm-up inference finished"를 찍은 파드가
    글자가 있는 첫 요청에서 OOMKilled로 죽었다.
    """
    image = np.full((320, 320, 3), 255, dtype=np.uint8)
    # 채운 사각형 몇 개면 검출이 상자를 내놓고 인식에 넘기기에 충분하다.
    # 폰트가 필요 없으므로 어떤 베이스 이미지에서도 동작한다.
    for row in range(60, 260, 70):
        image[row : row + 24, 40:280] = 0
    return image


def warm_up(engine) -> None:
    """버리는 추론을 한 번 돌려서, 첫 실제 요청이 가장 느리지 않게 한다."""
    blank = _warm_up_image()
    try:
        run_ocr(engine, blank)
        logger.info("warm-up inference finished")
    except Exception as exc:  # noqa: BLE001 - warm-up must never block startup
        logger.warning("warm-up inference failed: %s", exc)


def validate_upload(filename: str | None, content_type: str | None) -> None:
    """본문을 읽기 전에 업로드 메타데이터만 가볍게 검사한다."""
    extension = os.path.splitext(filename or "")[1].lower()
    if extension and extension not in ALLOWED_EXTENSIONS:
        raise ImageError(f"unsupported file extension '{extension}', allowed: jpg, jpeg, png")

    if content_type and content_type.split(";")[0].strip().lower() not in ALLOWED_CONTENT_TYPES:
        raise ImageError(f"unsupported content type '{content_type}', allowed: image/jpeg, image/png")


def _downscale(image: Image.Image) -> Image.Image:
    """긴 변이 MAX_IMAGE_SIDE 이하가 되도록 이미지를 줄인다."""
    if MAX_IMAGE_SIDE <= 0 or max(image.size) <= MAX_IMAGE_SIDE:
        return image

    ratio = MAX_IMAGE_SIDE / max(image.size)
    resized = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
    logger.info(
        "downscaled %dx%d -> %dx%d", image.width, image.height, resized[0], resized[1]
    )
    # LANCZOS는 작은 글씨를 알아볼 수 있게 남긴다. 싼 필터는 뭉갠다.
    return image.resize(resized, Image.LANCZOS)


def decode_image(raw: bytes) -> np.ndarray:
    """업로드된 바이트를 BGR numpy 배열로 디코딩한다. 전부 메모리에서 처리한다."""
    if not raw:
        raise ImageError("uploaded file is empty")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageError(f"image is larger than the {MAX_IMAGE_BYTES} byte limit")

    try:
        with Image.open(io.BytesIO(raw)) as image:
            image_format = image.format
            if image_format not in ALLOWED_IMAGE_FORMATS:
                raise ImageError(f"unsupported image format '{image_format}', allowed: JPEG, PNG")
            rgb = _downscale(image.convert("RGB"))
            array = np.asarray(rgb)
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageError(f"failed to decode image: {exc}") from exc

    # PaddleOCR은 OpenCV 관행대로 BGR 채널 순서를 기대한다.
    return array[:, :, ::-1].copy()


def _extract_texts(page) -> tuple[list, list]:
    """PaddleOCR 결과 page 하나에서 (rec_texts, rec_scores)를 꺼낸다."""
    data = page
    try:
        # pipeline 버전에 따라 결과가 "res" 키 아래에 한 겹 더 들어 있다.
        if "res" in data and isinstance(data["res"], dict):
            data = data["res"]
        return list(data.get("rec_texts", [])), list(data.get("rec_scores", []))
    except (TypeError, KeyError, AttributeError):
        logger.warning("unexpected OCR result shape: %s", type(page))
        return [], []


def run_ocr(engine, image: np.ndarray) -> list[dict]:
    """OCR을 돌리고 결과를 [{"text": ..., "confidence": ...}] 형태로 펴서 반환한다."""
    with _predict_slots:
        pages = engine.predict(image)

    items: list[dict] = []
    for page in pages or []:
        texts, scores = _extract_texts(page)
        for index, text in enumerate(texts):
            confidence = float(scores[index]) if index < len(scores) else 0.0
            items.append({"text": str(text), "confidence": round(confidence, 4)})
    return items
