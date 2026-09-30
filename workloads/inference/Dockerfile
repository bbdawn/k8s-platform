# OCR inference workload 이미지.
#
#   build:  docker build --platform linux/amd64 -t ocr-workload:cpu workloads/inference
#   run:    docker run --rm -p 8000:8000 ocr-workload:cpu
#
# PaddlePaddle은 linux/arm64 휠을 제공하지 않는다. Apple Silicon에서 빌드하더라도
# --platform linux/amd64 로 만들어야 한다.
#
# 단, Apple Silicon의 QEMU 에뮬레이션은 AVX를 지원하지 않아 에뮬레이션된 컨테이너
# 안에서는 paddle이 'Illegal instruction'(SIGILL)으로 죽는다. 즉 맥에서는 이미지를
# 만들 수는 있어도 실행해 볼 수 없다. 실행 검증은 x86 환경에서 해야 한다.
# 맥에서 빌드할 때는 모델 굽기를 꺼야 한다:
#
#   docker build --platform linux/amd64 --build-arg BAKE_MODELS=0 \
#     -t ocr-workload:cpu workloads/inference

FROM python:3.12-slim

# slim 이미지에 없는 런타임 라이브러리들. 빠뜨리면 빌드는 되는데 import 단계에서 죽는다.
#   libgomp1              paddle의 libpaddle.so가 요구 -> 'ImportError: libgomp.so.1'
#   libgl1, libglib2.0-0  paddleocr가 쓰는 opencv가 요구 -> 'ImportError: libGL.so.1'
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libgomp1 \
      libgl1 \
      libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# 로그가 버퍼에 갇히면 kubectl logs에 늦게 나타난다.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 런타임 UID. deployment.yaml의 runAsUser와 같은 값이어야 한다.
ARG APP_UID=10001

# HOME을 명시적으로 고정한다. 베이스 이미지가 HOME을 설정하지 않아서,
# /etc/passwd에 없는 UID로 실행되면 컨테이너의 HOME이 '/'가 된다. 그러면 paddlex가
# '~/.paddlex'를 '/.paddlex'로 해석해 루트 디렉터리에 쓰려다 죽는다:
#   PermissionError: [Errno 13] Permission denied: '/.paddlex'
# 게다가 아래에서 굽는 모델은 root의 HOME(/root/.paddlex, 권한 700)에 들어가
# 런타임 UID가 읽지도 못한다. 모델을 굽는 의미가 사라지는 자리라 HOME을 먼저 정한다.
ENV HOME=/home/app
RUN mkdir -p ${HOME}

WORKDIR /app

# 의존성 레이어를 먼저 만들어 둔다. 앱 코드만 고치면 재설치하지 않는다.
COPY app/requirements.txt .

# PaddlePaddle은 requirements.txt에 없다. 여기서 따로 설치해 레이어를 나눠 두면
# 앱 의존성이 바뀌어도 1.4GB짜리 재설치를 피할 수 있다.
RUN pip install paddlepaddle==3.3.1 \
 && pip install -r requirements.txt

COPY app/ .

# 모델을 이미지에 구워 둔다. 굽지 않으면 pod가 뜰 때마다 내려받아 기동이 느려지고,
# 벤치마크에서 pod를 반복 재시작할 때마다 되풀이된다.
# BAKE_MODELS=0 으로 끌 수 있다(AVX가 없는 에뮬레이션 빌드용). 끄고 만든 이미지는
# 첫 요청 전에 모델을 내려받으므로 벤치마크용으로 쓰지 말 것.
ARG BAKE_MODELS=1
RUN if [ "$BAKE_MODELS" = "1" ]; then \
      python -c "import ocr; ocr.create_ocr_engine()"; \
    else \
      echo "BAKE_MODELS=0: 모델을 굽지 않는다. 이 이미지는 런타임에 모델을 내려받는다."; \
    fi

# 여기까지는 root로 실행하고, 결과물을 런타임 UID에게 넘긴다.
# 그룹 0에 사용자와 같은 권한을 주는 이유: runAsUser가 다른 값으로 바뀌어도
# (그룹은 0으로 유지되므로) 모델과 코드를 계속 읽을 수 있다.
RUN chown -R ${APP_UID}:0 ${HOME} /app \
 && chmod -R g=u ${HOME} /app

# 이미지 자체에 박아 둔다. k8s 밖에서 docker run 할 때도 같은 조건이 되어,
# 권한 문제가 클러스터에서만 드러나는 일을 막는다.
USER ${APP_UID}

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
