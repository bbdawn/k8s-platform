"""API 테스트.

두 묶음으로 나뉜다:
  * 기본 테스트는 OCR 모델을 로드하지 않으므로 어느 머신에서나 돌아간다
  * 실제 추론 테스트는 RUN_OCR_TESTS=1 이고 PaddleOCR이 정상 설치된
    환경에서만 돈다

`app` 디렉터리에서 실행할 것:  pytest tests
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

# 테스트가 app 모듈들과 같은 위치에 있고, 그 모듈들은 평평하게 import된다(main, ocr).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
import ocr  # noqa: E402

# fixture가 patch하기 전에 미리 잡아 둔다. 그래야 아래 실제 엔진 fixture가
# 빠른 `client` fixture가 심어 놓은 mock의 영향을 받지 않는다.
REAL_CREATE_OCR_ENGINE = ocr.create_ocr_engine


def make_png(text: str = "TEST 12345", size: tuple[int, int] = (480, 160)) -> bytes:
    """글자가 들어간 작은 PNG를 메모리에서 만든다."""
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((20, 60), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(scope="module")
def client():
    """OCR 엔진 로드를 일부러 실패시킨 client. 모델을 내려받지 않는다."""
    with mock.patch.object(
        ocr, "create_ocr_engine", side_effect=RuntimeError("model not loaded in tests")
    ):
        with TestClient(main.app) as test_client:
            yield test_client


@pytest.fixture(scope="module")
def ocr_client():
    """실제 엔진을 올린 client. 명시적으로 켜지 않으면 skip한다."""
    if os.getenv("RUN_OCR_TESTS") != "1":
        pytest.skip("set RUN_OCR_TESTS=1 to run tests that load the OCR model")
    with mock.patch.object(ocr, "create_ocr_engine", REAL_CREATE_OCR_ENGINE):
        with TestClient(main.app) as test_client:
            if main.app.state.ocr_engine is None:
                pytest.skip(f"OCR engine unavailable: {main.app.state.ocr_error}")
            yield test_client


# --- /health ----------------------------------------------------------------


def test_health_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- image decoding ---------------------------------------------------------


def test_phone_sized_image_is_downscaled():
    """원본 해상도 휴대폰 사진이 그대로 PaddleOCR에 들어가면 안 된다.

    3024x4032에서는 파이프라인이 만드는 float32 사본이 하나에 약 140MB다.
    메모리 제한이 걸린 파드에서는 요청 처리 도중 컨테이너가 죽기에 충분하다.
    그러면 클라이언트는 상태 코드도 없이 연결이 끊기는 것만 보게 되어 밖에서는
    원인이 보이지 않는다. 여기서 확인해 두는 편이 훨씬 싸다.
    """
    array = ocr.decode_image(make_png(size=(3024, 4032)))
    height, width = array.shape[:2]
    assert max(width, height) == ocr.MAX_IMAGE_SIDE
    # 종횡비가 유지되는지: 3024/4032 == 0.75
    assert round(width / height, 2) == 0.75


def test_small_image_is_not_touched():
    """축소는 한도를 넘을 때만 동작한다. 작은 이미지는 그대로 통과한다."""
    array = ocr.decode_image(make_png(size=(640, 480)))
    assert array.shape[:2] == (480, 640)


# --- /ready -----------------------------------------------------------------


def test_ready_returns_503_when_engine_failed_to_load(client):
    """/ready가 존재하는 이유 자체다. 엔진이 없으면 Ready라고 답하면 안 된다.

    여기서 200이 나가면, readiness probe가 엔진이 죽은 파드를 Ready로 표시하고
    Service는 거절밖에 못 하는 파드로 트래픽을 보낸다.
    """
    response = client.get("/ready")
    assert response.status_code == 503
    assert "not available" in response.json()["detail"]


def test_ready_returns_ok_with_a_loaded_engine(ocr_client):
    response = ocr_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


# --- /info ------------------------------------------------------------------


def test_info_answers_even_with_a_dead_engine(client):
    """파드가 왜 서비스를 못 하는지 알아보러 오는 엔드포인트다.

    그러므로 엔진 초기화가 실패한 상태에서도 답해야 한다. 이 fixture가
    재현하는 상황이 정확히 그것이다.
    """
    response = client.get("/info")
    assert response.status_code == 200

    body = response.json()
    assert body["device"] == "cpu"
    assert body["ocr_ready"] is False


# --- /ocr: bad requests -----------------------------------------------------


def test_ocr_without_file_is_rejected(client):
    response = client.post("/ocr")
    assert response.status_code == 422


def test_ocr_with_empty_file_is_rejected(client):
    response = client.post("/ocr", files={"file": ("empty.png", b"", "image/png")})
    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_ocr_with_unsupported_extension_is_rejected(client):
    response = client.post("/ocr", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert response.status_code == 400


def test_ocr_with_undecodable_image_is_rejected(client):
    response = client.post("/ocr", files={"file": ("broken.png", b"not-an-image", "image/png")})
    assert response.status_code == 400
    assert "decode" in response.json()["detail"].lower()


def test_ocr_returns_503_when_engine_failed_to_load(client):
    """이미지가 정상이어도 엔진이 없으면 처리할 수 없다."""
    response = client.post("/ocr", files={"file": ("ok.png", make_png(), "image/png")})
    assert response.status_code == 503
    assert "not available" in response.json()["detail"]


# --- /ocr: real inference ---------------------------------------------------


def test_ocr_returns_expected_shape(ocr_client):
    response = ocr_client.post("/ocr", files={"file": ("receipt.png", make_png(), "image/png")})
    assert response.status_code == 200

    body = response.json()
    assert body["count"] == len(body["text"])
    assert body["elapsed_ms"] > 0
    for item in body["text"]:
        assert isinstance(item["text"], str)
        assert 0.0 <= item["confidence"] <= 1.0
