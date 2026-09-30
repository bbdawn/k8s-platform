"""Kubernetes 벤치마크용 OCR 추론 API.

OCR 기능을 만드는 것이 목적이 아니다. 환경이 바뀌어도 그대로 돌릴 수 있고
비교할 수 있는, 작고 예측 가능한 workload로서 존재한다. 단순하게 유지할 것.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ocr

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("ocr-api")

WARM_UP_ON_STARTUP = os.getenv("OCR_WARMUP", "true").lower() == "true"
STATIC_DIR = Path(__file__).resolve().parent / "static"


# --- response models --------------------------------------------------------


class HealthResponse(BaseModel):
    status: str


class InfoResponse(BaseModel):
    paddle_version: str | None = None
    device: str
    ocr_ready: bool
    error: str | None = None


class TextItem(BaseModel):
    text: str
    confidence: float


class OcrResponse(BaseModel):
    text: list[TextItem]
    count: int
    elapsed_ms: float


# --- application ------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """첫 요청을 받기 전에 OCR 모델을 한 번만 로드한다."""
    app.state.ocr_engine = None
    app.state.ocr_error = None
    try:
        app.state.ocr_engine = ocr.create_ocr_engine()
        if WARM_UP_ON_STARTUP:
            await run_in_threadpool(ocr.warm_up, app.state.ocr_engine)
    except Exception as exc:  # noqa: BLE001
        # 엔진 없이 뜨는 것은 의도된 동작이다. /health와 /info가 살아 있어야
        # CrashLoopBackOff 대신 실패 원인이 보인다.
        app.state.ocr_error = f"{type(exc).__name__}: {exc}"
        logger.error("OCR engine initialization failed: %s", app.state.ocr_error)

    yield

    app.state.ocr_engine = None


app = FastAPI(
    title="OCR Inference Workload",
    description="PaddleOCR inference API for Kubernetes benchmarking.",
    version="0.1.0",
    lifespan=lifespan,
)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """최소한의 업로드 화면. API 자체는 /docs 로도 쓸 수 있다."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """liveness 확인. OCR 엔진을 일부러 건드리지 않는다."""
    return HealthResponse(status="ok")


@app.get("/ready", response_model=HealthResponse)
def ready() -> HealthResponse:
    """readiness. OCR 엔진이 실제로 올라오기 전에는 503을 반환한다.

    /health와 일부러 분리했다. /health는 프로세스가 떠 있다는 것만 알려주므로,
    readiness probe를 거기에 걸면 엔진 초기화가 실패한 파드도 Ready가 된다.
    그러면 Service가 트래픽을 보내고 /ocr은 전부 503을 반환한다. 그 Ready
    신호를 믿고 도는 벤치마크는 서비스하지 못하는 서비스를 측정하게 된다.
    """
    if app.state.ocr_engine is None:
        raise HTTPException(
            status_code=503,
            detail=f"OCR engine is not available: {app.state.ocr_error}",
        )
    return HealthResponse(status="ready")


@app.get("/info", response_model=InfoResponse)
def info() -> InfoResponse:
    """이 프로세스가 실제로 무엇을 돌리고 있는지: paddle 버전, device, 엔진 상태.

    /ready와 따로 둔 이유는, 엔진이 죽었을 때도 답해야 하기 때문이다.
    "왜 죽었는지"를 물어보러 오는 엔드포인트다.
    """
    return InfoResponse(
        **ocr.runtime_info(),
        ocr_ready=app.state.ocr_engine is not None,
    )


@app.post("/ocr", response_model=OcrResponse)
async def run_ocr(file: UploadFile = File(...)) -> OcrResponse:
    """업로드된 jpg/png 이미지 한 장을 OCR 처리한다."""
    # 요청 검증을 먼저 한다. 그래야 엔진이 없는 상태에서도 잘못된 요청이
    # 503이 아니라 400으로 분명하게 나온다.
    try:
        ocr.validate_upload(file.filename, file.content_type)
        raw = await file.read()
        image = ocr.decode_image(raw)
    except ocr.ImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await file.close()

    engine = app.state.ocr_engine
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail=f"OCR engine is not available: {app.state.ocr_error}",
        )

    started = time.perf_counter()
    try:
        # predict()는 블로킹 CPU 작업이므로 이벤트 루프에서 떼어 놓는다.
        items = await run_in_threadpool(ocr.run_ocr, engine, image)
    except Exception as exc:  # noqa: BLE001
        logger.exception("OCR inference failed")
        raise HTTPException(
            status_code=500, detail=f"OCR inference failed: {type(exc).__name__}: {exc}"
        ) from exc
    elapsed_ms = (time.perf_counter() - started) * 1000

    logger.info("ocr done: %d texts in %.1f ms", len(items), elapsed_ms)
    return OcrResponse(
        text=[TextItem(**item) for item in items],
        count=len(items),
        elapsed_ms=round(elapsed_ms, 2),
    )
