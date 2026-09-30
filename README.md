# k8s-platform

Kubernetes에서 OCR 추론 workload를 돌리고 성능을 측정하는 프로젝트.

OCR 서비스를 만드는 것이 목적이 아니다. OCR은 **재현 가능하고 측정하기 쉬운
workload**로서만 존재한다. 따라서 OCR 기능은 최소한으로 유지하고, 측정의 정확성과
환경 간 동일성을 우선한다. 비교 축은 **pod 개수**다(5.1).

---

## 1. 현재 상태

| 항목 | 상태 |
|---|---|
| OCR workload (FastAPI + PaddleOCR) | ✅ 완료 |
| 로컬 검증 | ✅ 완료 (수치는 8장) |
| 업로드 UI (정적 HTML) | ✅ 완료 |
| Dockerfile | ✅ 완료 — 빌드는 **amd64 리눅스에서만** 가능 (4.1) |
| Kubernetes 매니페스트 | ✅ 적용 완료 |
| 클러스터 배포 | ✅ 파드 Running (4.7) |
| 클러스터 추론 검증 | ⚠️ 진행 중 — 엔드포인트·추론 동작 확인. 실제 영수증 수치는 아직 |
| 벤치마크 실행 | ❌ 미착수 (6장) |

### 디렉터리

```
k8s-platform/
├── deploy/
│   └── k8s/                        Kubernetes 매니페스트 (4장)
└── workloads/
    └── inference/
        ├── Dockerfile
        ├── .dockerignore
        └── app/
            ├── main.py              FastAPI 앱 (/, /health, /ready, /info, /ocr)
            ├── ocr.py               엔진 생성·워밍업·업로드 검증·추론
            ├── requirements.txt     앱 의존성 + PaddlePaddle 설치법 주석
            ├── static/
            │   └── index.html       업로드 화면 (바닐라 JS, 빌드/npm 없음)
            └── tests/
                └── test_api.py      테스트 10개
```

---

## 2. 실행

### 다시 실행할 때

설치가 끝난 뒤 서버를 띄우는 명령은 이것뿐이다.

```bash
cd workloads/inference/app
uvicorn main:app --host 0.0.0.0 --port 8000
```

`uvicorn: command not found`가 나면 가상환경이 활성화되지 않은 것이다.
아래 최초 설치를 먼저 하거나, 만들어 둔 가상환경을 활성화한다.

```bash
source .venv/bin/activate
```

포트를 바꾸려면 `--port 8001`처럼 지정한다. 코드를 고치면서 쓸 때는 `--reload`를
붙이면 자동으로 다시 로드된다. 단, **벤치마크 측정 시에는 `--reload`를 쓰지 말 것**
(파일 감시가 측정에 개입한다).

### 최초 설치 — CPU (개발/검증용)

```bash
cd workloads/inference/app

python3.12 -m venv .venv                 # PaddlePaddle은 Python 3.9~3.13만 지원
source .venv/bin/activate

pip install paddlepaddle==3.3.1          # CPU 빌드
pip install -r requirements.txt

uvicorn main:app --host 0.0.0.0 --port 8000
```

### 확인

- 업로드 화면: <http://localhost:8000/> — 여러 장 동시 업로드 가능
- Swagger UI: <http://localhost:8000/docs>
- `curl http://localhost:8000/health`
- `curl http://localhost:8000/info` ← **엔진이 실제로 떴는지 여기서 확인**

### 테스트

```bash
cd workloads/inference/app
pytest tests                  # 8개. 모델을 로드하지 않아 어디서든 실행됨
RUN_OCR_TESTS=1 pytest tests  # 실제 모델을 로드하는 2개까지 더해 10개
```

---

## 3. API

| 엔드포인트 | 설명 |
|---|---|
| `GET /` | 업로드 화면 |
| `GET /health` | liveness. OCR 엔진을 건드리지 않음 |
| `GET /ready` | readiness. 엔진이 없으면 503 + 원인 |
| `GET /info` | paddle 버전 / device / 엔진 상태. **엔진이 죽어 있어도 200을 반환** |
| `POST /ocr` | jpg/png 1장 → 텍스트 + confidence |

`POST /ocr` 응답:

```json
{
  "text": [{ "text": "스타벅스 강남점", "confidence": 0.936 }],
  "count": 1,
  "elapsed_ms": 1450.12
}
```

`elapsed_ms`는 **서버 측 추론 시간**이다. 네트워크 왕복 시간을 빼고 순수 추론만
비교하기 위해 넣었다. 벤치마크에서는 이 값을 쓴다.

### 업로드 화면의 파일명 생성

영수증 경비 처리를 위해 `26.08.05(주)우아한형제들_점심_메머드` 형식의 문자열을
만들어 복사할 수 있게 해 둔다. 추출 규칙은 다음과 같다.

| 자리 | 출처 |
|---|---|
| 날짜 | **결제일시**의 날짜를 `YY.MM.DD`로 정규화 |
| 가맹점 | **가맹점 정보 > 상호** 값 (결제 주체) |
| 용도 | 결제 **시각이 17시 이후면 `저녁`, 이전이면 `점심`** |
| 판매자 | **판매자 정보 > 상호**의 **첫 번째 단어** (실제 매장) |

배달앱 영수증처럼 결제 주체(가맹점)와 실제 매장(판매자)이 다른 경우를 전제한다.
두 섹션은 서로의 구간을 침범하지 않도록 경계를 두고 각각 탐색한다. 섹션이 없으면
가맹점은 판매자 섹션 앞쪽의 `상호`를, 판매자는 영수증 첫 줄을 폴백으로 쓴다.

네 항목 모두 화면에서 수정할 수 있다. OCR 추출은 휴리스틱이라 반드시 빗나가는
경우가 있기 때문이다.

만들어진 문자열은 **복사**하거나, 업로드한 원본 이미지를 그 이름으로 **다운로드**할 수
있다(확장자는 원본 유지). 여러 장일 때는 [전체 복사] / [전체 다운로드]를 쓴다.
파일명에 쓸 수 없는 문자(`/ \ : * ? " < > |`)는 저장 시 걷어낸다.

**이 후처리는 전부 프론트엔드(JS)에서 한다.** 서버에 넣으면 `elapsed_ms`에
후처리 시간이 섞여 벤치마크 측정이 오염되므로 `/ocr` API는 손대지 않는다.

> PaddleOCR은 `결제일시 : 2026-08-05 10:48:07`을 `결제일시:2026-08-0510:48:07`처럼
> **공백을 지워서** 내놓는다. 날짜 파싱은 날짜와 시각을 한 덩어리로 잡아 이 형태를
> 처리한다. 사업자번호(`120-87-65763`)를 날짜로 오인하지 않도록 후보를 순회하며
> 달/일 범위가 유효한 첫 번째 것만 채택한다.

### 환경변수

ConfigMap으로 분리하기 쉽도록 모든 설정을 환경변수로 뺐다.

| 변수 | 기본값 | 용도 |
|---|---|---|
| `OCR_LANG` | `korean` | 인식 언어 모델 |
| `OCR_MAX_CONCURRENCY` | `1` | 동시 `predict()` 허용 수 (아래 5장 참고) |
| `OCR_WARMUP` | `true` | 시작 시 워밍업 추론 |
| `OCR_MAX_IMAGE_BYTES` | `10485760` | 업로드 크기 제한 |
| `OCR_MAX_IMAGE_SIDE` | `1600` | 추론 전 긴 변을 이 값으로 축소. `0`이면 끔 (7장) |
| `OCR_USE_TEXTLINE_ORIENTATION` | `false` | 방향 분류. 측정 일관성을 위해 off |
| `OCR_ENABLE_MKLDNN` | `false` | oneDNN. **켜면 CPU 추론이 죽는다** (7장) |
| `OCR_CPU_THREADS` | `2` | paddle `cpu_threads`. CPU limit과 맞출 것 |
| `LOG_LEVEL` | `INFO` | 로그 레벨 |

---

## 4. 배포 (Kubernetes)

클러스터에 올려 매니페스트와 이미지를 검증하는 것이 이 장의 목표다.

```
deploy/k8s/
├── namespace.yaml       ocr-bench
├── configmap.yaml       앱 환경변수 (3장의 표와 동일)
├── deployment.yaml      replicas 1, probe, resources
├── service.yaml         NodePort 30800
└── kustomization.yaml   이미지 이름을 여기서 갈아끼운다

workloads/inference/Dockerfile   워크로드 이미지
```

### 4.1 빌드는 amd64 리눅스에서 해야 한다

**macOS(Apple Silicon)에서는 이 이미지를 빌드할 수 없다.** `--platform linux/amd64`로
pip 설치까지는 전부 통과하지만, 모델을 굽는 마지막 단계에서 죽는다.

```
#15 [9/9] RUN python -c "... ocr.create_ocr_engine() ..."
#15 1.349 Illegal instruction
#15 ERROR: ... exit code: 132
```

원인은 에뮬레이션 게스트가 내놓는 CPU 플래그다. `/proc/cpuinfo`를 보면
**`sse4_2` 하나뿐이고 AVX / AVX2 / FMA가 없다.** PaddlePaddle 기본 휠은 AVX를
요구하므로 첫 커널에서 바로 SIGILL이 난다. `import paddle` 하나만 돌려도
8분 넘게 끝나지 않는다.

**실제 amd64 서버 CPU는 전부 AVX2를 갖고 있으므로 거기서는 나지 않는 문제다.**
다만 Mac에서는 이미지를 완성할 수도, 스모크 테스트를 할 수도 없다는 뜻이다.

빌드 위치는 둘 중 하나:

- ~~Jenkins~~ — 사내 Jenkins/Nexus는 쓸 수 없다. 새로 세우는 비용이 이 프로젝트의 산출물(벤치마크 수치)에 기여하지 않으므로 하지 않는다.
- **클러스터 노드에서 직접** — 현재 이 방식을 쓴다. 4.2 참고.

빌드 명령 자체는 어디서 돌리든 같다(리눅스 amd64에서는 `--platform` 불필요).

```bash
docker build -t nexus.<사내도메인>:8082/ocr-workload:0.1.0-cpu workloads/inference
```

빌드 마지막 단계에서 **OCR 모델을 이미지에 굽는다.** 폐쇄망 대응이기도 하지만
더 중요한 이유는 모델 다운로드 시간이 pod 기동 시간에 섞이면 안 되기 때문이다.
그래서 실패를 삼키는 `warm_up()` 대신 실제 추론을 돌려, 다운로드가 실패하면
**빌드가 깨지게** 해 두었다. 굽는 단계를 빼고 런타임에 받게 하려면 pod에서
인터넷이 되어야 하고 `startupProbe` 여유를 더 줘야 한다.

### 4.2 레지스트리 없이 — 노드에서 빌드해 바로 쓰기

**현재 이 프로젝트가 쓰는 경로다.** 사내 Nexus/Jenkins를 쓸 수 없고, 이미지가
하나뿐이라 레지스트리와 CI를 새로 세울 이유가 없다. 클러스터 노드가
곧 amd64 빌드 머신이므로 4.1의 AVX 문제도 같이 해결된다.

```bash
# worker1에서 (k8s.io 네임스페이스에 넣는 것이 핵심)
sudo nerdctl -n k8s.io build -t ocr-workload:0.1.0-cpu workloads/inference
```

`-n k8s.io`를 빼면 **빌드는 성공하는데 kubelet이 그 이미지를 못 찾는다.**
containerd는 이미지 네임스페이스가 나뉘어 있고, kubelet은 `k8s.io`만 본다.
`imagePullPolicy: IfNotPresent`는 이 전제에 맞춰 이미 설정되어 있다.

이미지가 **한 노드에만** 존재하므로 `deployment.yaml`의 `nodeSelector`를 그 노드로
반드시 채울 것. 비워 두면 파드가 `Pending`으로 멈춘다(의도된 동작 — 다른 노드에
배치됐다가 `ErrImageNeverPull`로 죽는 것보다 원인이 명확하다).

```bash
kubectl get nodes    # 이름 확인 후 deployment.yaml의 REPLACE_WITH_BUILD_NODE_HOSTNAME 교체
```

나중에 다른 노드에도 필요해지면 그때 옮기면 된다. 미리 할 일은 아니다.

```bash
sudo nerdctl -n k8s.io save ocr-workload:0.1.0-cpu \
  | ssh <worker2> 'sudo nerdctl -n k8s.io load'
```

### 4.3 배포

빌드한 태그가 `kustomization.yaml`의 기본값(`ocr-workload:0.1.0-cpu`)과 같으므로
이미지는 손댈 것이 없다. `deployment.yaml`의 `nodeSelector`만 채우면 된다.

```bash
sed -i 's/REPLACE_WITH_BUILD_NODE_HOSTNAME/<worker1 노드명>/' deploy/k8s/deployment.yaml

kubectl apply -k deploy/k8s
kubectl -n ocr-bench rollout status deploy/ocr
```

`rollout status`가 오래 걸려도 정상이다 — 모델 로드와 워밍업에 수십 초가 든다(4.5).

확인:

```bash
kubectl -n ocr-bench get pods
kubectl -n ocr-bench logs deploy/ocr | head -20   # "PaddleOCR ready on cpu"

curl http://<노드IP>:30800/health
curl http://<노드IP>:30800/info    # device: cpu, ocr_ready: true 가 정상
```

NodePort를 못 쓰는 환경이면 `kubectl -n ocr-bench port-forward svc/ocr 8000:8000`.

### 4.4 노드 역할 분담

worker가 2대라면 나누는 편이 낫다.

```
worker1  ← OCR 파드 (측정 대상, nodeSelector로 고정)
worker2  ← k6 / hey (부하 생성)
```

부하 도구를 측정 대상과 같은 노드에서 돌리면 **측정 도구가 측정 대상의 CPU를
갉아먹는다.** 노드를 나누면 그 오염이 없다.

`nodeSelector`는 이미지 위치 때문만이 아니라 측정 설계상으로도 필요하다. 고정하지
않으면 replica를 늘렸을 때 스케줄러가 worker들에 나눠 배치하고, `elapsed_ms`에
**서로 다른 두 머신의 수치가 섞인다.**

### 4.5 CPU 배포에서 주의할 것

- **`OMP_NUM_THREADS`를 CPU limit과 맞출 것.** paddle의 CPU 커널은 컨테이너
  limit이 아니라 **호스트 코어 수**를 보고 스레드를 만든다. 안 맞추면 2코어짜리
  pod이 수십 개 스레드를 띄우고 서로 밟는다. 현재 둘 다 `2`.
- **기동이 느리다.** 모델 로드 + 워밍업이 수십 초라서 `startupProbe`를
  5초 × 60회로 잡아 두었다. 이게 없으면 liveness가 부팅 중인 pod을 죽인다.
- OCR 엔진 초기화가 실패해도 pod은 뜬다(7장 설계 의도). `Running`인데 `/ocr`이
  503이면 `/ready`의 detail과 `/info`의 `error` 필드를 먼저 볼 것.
- **이 단계의 목적은 매니페스트와 이미지 검증이지 성능 측정이 아니다.**
  CPU 수치는 8장에 있고, 노드 사양이 다르면 비교 대상도 안 된다.

---

### 4.6 (참고) 레지스트리를 쓰게 될 경우

```bash
docker login nexus.<사내도메인>:8082
docker push nexus.<사내도메인>:8082/ocr-workload:0.1.0-cpu
```

pull 인증이 필요하면 시크릿을 만들고 `deployment.yaml`의 `imagePullSecrets`
주석을 푼다.

```bash
kubectl -n ocr-bench create secret docker-registry nexus-cred \
  --docker-server=nexus.<사내도메인>:8082 \
  --docker-username=<id> --docker-password=<pw>
```

> Nexus가 HTTPS가 아니면 노드의 containerd에 insecure registry 설정이 필요하다.
> 사내에서 이미 Nexus를 쓰고 있다면 대개 되어 있지만, `ImagePullBackOff`에
> `http: server gave HTTP response to HTTPS client`가 보이면 이 경우다.
> 현재는 레지스트리를 쓰지 않으므로 이 절은 참고용이다. 4.2를 볼 것.

### 4.7 클러스터 배포에서 실제로 겪은 것 (2026-09-14)

맥에서 이미지까지 만들고 worker1에서 다시 빌드해 배포한 기록. 순서대로 걸렸다.

**빌드 도구가 노드에 없다.** kubeadm 노드에는 `ctr`만 있고 이미지를 빌드할 수
없다. nerdctl-full이나 docker를 넣으면 번들에 containerd가 딸려 와 돌고 있는
클러스터 런타임과 충돌할 위험이 있다. **buildkit만 단독으로** 넣는 것이 가장
안전하다 — 바이너리가 `buildkitd`/`buildctl`뿐이고 containerd를 건드리지 않는다.

```bash
# /usr/local 에 풀기 전에 bin/containerd 가 없는지 확인할 것
tar tzf buildkit-v0.33.0.linux-amd64.tar.gz
tar Cxzf /usr/local buildkit-v0.33.0.linux-amd64.tar.gz
```

`--containerd-worker-namespace` 플래그는 **없다.** 네임스페이스는 설정 파일로
지정한다. 이게 빠지면 빌드는 성공하는데 kubelet이 이미지를 못 찾는다.

```toml
# /etc/buildkit/buildkitd.toml
[worker.oci]
  enabled = false
[worker.containerd]
  enabled = true
  namespace = "k8s.io"
```

```bash
nohup buildkitd > /var/log/buildkitd.log 2>&1 &
buildctl debug workers -v      # namespace:k8s.io 라벨을 확인
buildctl build --frontend dockerfile.v0 \
  --local context=workloads/inference --local dockerfile=workloads/inference \
  --output type=image,name=docker.io/library/ocr-workload:0.1.0-cpu --progress plain
```

`buildkitd`는 `nohup`으로 띄운 포그라운드 프로세스라 **재부팅하면 죽는다.**
재빌드할 때마다 `pgrep -a buildkitd`로 확인할 것.

**`runAsUser`와 이미지가 어긋나 있었다.** 빌드는 root로 돌아 모델이
`/root/.paddlex`(권한 700)에 구워지는데 런타임은 UID 10001이다. 그 UID는
이미지의 `/etc/passwd`에 없어서 `HOME`이 `/`가 되고, paddlex가 `/.paddlex`에
쓰려다 `PermissionError`로 죽었다. Dockerfile에서 `HOME`을 고정하고 굽고 나서
`chown`으로 넘기고 `USER`를 박아 해결했다. `USER`를 이미지에 박은 것이 핵심이다
— 그러지 않으면 같은 문제가 클러스터에서만 드러난다.

**probe 3개가 모두 `/health`를 보고 있었다.** `/health`는 엔진 상태를 보지 않아
엔진이 죽은 파드가 Ready로 서고 Service가 트래픽을 보냈다. `rollout status`가
몇 초에 끝난 것이 신호였다 — 모델 로드에 수십 초가 드는데 기다릴 것이 없었다.
`/ready`를 만들어 startup·readiness에 걸고, liveness는 `/health`로 남겼다.
liveness를 `/ready`로 걸면 초기화 실패 시 무한 재시작에 빠져 원인을 못 본다.

**재배포가 교착됐다.** `nodeSelector`로 파드를 한 노드에 고정해 놓고 기본
`RollingUpdate`를 쓰면, 새 파드가 그 노드에 두 번째 몫을 요구해 `Pending`이
되고 예전 파드는 새 파드를 기다려 아무것도 진행되지 않는다.

```
0/3 nodes are available: 1 Insufficient cpu, 1 Insufficient memory,
  1 node(s) didn't match Pod's node affinity/selector,
  1 node(s) had untolerated taint(s).
```

`strategy: Recreate`로 해결했다. 무중단이 필요 없고, 측정 대상 노드에서 워크로드
두 개가 잠시라도 CPU를 나눠 쓰는 편이 오히려 해롭다.

**라이브러리 설정은 파드 안에서 먼저 시험했다.** 재빌드가 5~10분이라 추측으로
고치면 그만큼을 반복한다. 돌고 있는 파드에서 `kubectl exec`로 엔진을 하나 더
만들어 빈 이미지로 `predict()`까지 돌려 보면 30초에 답이 나온다. `predict()`는
지연 평가라 `list()`로 감싸지 않으면 추론이 돌지 않고 통과한 것처럼 보인다.

---

## 5. 벤치마크 설계

### 5.1 동시성을 어디서 올릴 것인가

측정 결과가 설계 근거를 제공한다 (영수증 1장 반복, 8장 참고):

```
OCR_MAX_CONCURRENCY=1          OCR_MAX_CONCURRENCY=4
동시 1 → 0.70 req/s             동시 1 → 0.69 req/s
동시 2 → 0.72 req/s             동시 2 → 0.72 req/s
동시 4 → 0.72 req/s             동시 4 → 0.72 req/s
         추론평균 3468ms                  추론평균 5297ms
```

**처리량이 완전히 동일하다.** 연산 장치가 이미 포화 상태라 프로세스 내부에서
스레드를 늘려도 처리량은 늘지 않고 지연만 나빠진다.

→ **비교 축은 pod(프로세스) 개수여야 한다.** `OCR_MAX_CONCURRENCY=1`을 기본값으로
두고 replica를 늘리는 것이 맞는 설계인 이유다.

- ❌ pod 내부 스레드 동시성 늘리기
- ✅ pod 개수를 늘려 비교

### 5.2 측정 지표

| 지표 | 출처 |
|---|---|
| 추론 지연 (p50/p95/p99) | 응답의 `elapsed_ms` |
| 처리량 (req/s) | 부하 도구 집계 |
| 노드 CPU / 메모리 | `kubectl top`, node-exporter |
| 이웃 pod 영향 | OOM 발생 여부, 같은 노드 pod의 지연 변화 |

부하는 `k6` / `hey` 같은 도구로 건다. **UI는 기능 확인용이지 측정용이 아니다** —
브라우저·네트워크·JS 타이머 노이즈가 섞인다.

### 5.3 측정 정확도를 위해 이미 반영한 것

- OCR 모델은 lifespan에서 **1회 초기화 후 재사용** → 모델 로딩 시간이 측정에 안 섞임
- 시작 시 **워밍업 추론 1회** → 첫 요청의 lazy-load 지연 제거
- `OCR_USE_TEXTLINE_ORIENTATION=false` → 파이프라인 단계를 고정해 환경 간 동일성 확보
- 응답에 서버 측 `elapsed_ms` 포함 → 네트워크 오버헤드 분리

---

## 6. 앞으로 확인해야 할 것

### 6.1 클러스터 추론 검증 (최우선)

- [x] ~~큰 이미지에서 컨테이너가 죽는 문제~~ — `OCR_MAX_IMAGE_SIDE`로 추론 전에
      축소해 막았다 (7장)
- [ ] 파드 `limits`를 노드보다 작게 내릴 것 — 현재 `cpu 2 / memory 4Gi`가 노드
      전체(2C4M)와 같아, 메모리를 넘겨도 `OOMKilled`로 기록되지 않고 노드가 먼저
      흔들린다. 축소로 증상은 막았지만 이 구조는 그대로다
- [ ] 한글 영수증 인식 결과가 로컬(8장)과 **동일한가**
- [ ] `RUN_OCR_TESTS=1 pytest tests` 10개 통과

### 6.2 벤치마크 실행

- [ ] 부하 도구 선정 및 시나리오 스크립트 작성
- [ ] 고정할 변수 정의 (이미지 크기, 요청 수, 워밍업 요청 수, 측정 시간)
- [ ] pod 개수(1/2/4/8) 측정
- [ ] 결과 표/그래프 정리

### 6.3 열려 있는 결정 사항

- [ ] 인식 모델 조합을 고정할 것인가 — 현재 검출은 `PP-OCRv5_server_det`, 인식은
      `korean_PP-OCRv5_mobile_rec`가 자동 선택된다. server판 검출은 무거우므로
      벤치마크 변수로 삼을지, mobile로 고정할지 결정 필요
- [ ] 입력 이미지를 1종으로 고정할지, 크기별로 나눌지
- [ ] 모델 파일을 이미지에 굽을지, PVC/initContainer로 뺄지 (pod 기동 시간에 영향)
- [ ] 배치 추론(`predict()`에 리스트 전달) 시나리오를 별도 축으로 추가할지
- [ ] 측정 대상 노드를 키울지 — 2C4M은 파드 하나가 노드를 다 쓰는 크기라
      시스템 구성요소와 CPU를 다툰다

---

## 7. 알려진 제약 / 주의사항

- **Python 3.9 ~ 3.13만 지원.** PaddlePaddle 3.3.x는 3.14 휠을 제공하지 않는다.
  이미지는 `python:3.11` 또는 `3.12`로 빌드할 것.
- **PaddleOCR predictor는 thread-safe하지 않다.** `OCR_MAX_CONCURRENCY` 기본값 1이
  이를 보장한다. 올리려면 결과 정합성을 먼저 검증할 것.
- 모델은 첫 실행 시 `~/.paddlex/official_models/`로 다운로드된다. 컨테이너에서는
  이 경로가 기동 시간과 이미지 크기에 직접 영향을 준다.
- OCR 엔진 초기화가 실패해도 앱은 뜬다. `/ready`가 503으로 원인을 노출하고
  `/info`는 200으로 상태를 알려준다. CrashLoopBackOff 대신 원인이 보이도록 한
  의도적 설계다.
- **oneDNN을 켜면 CPU 추론이 전부 실패한다.** paddle 3.3.1의 PIR 실행기가 이
  파이프라인이 만드는 oneDNN 커널을 처리하지 못한다.

  ```
  NotImplementedError: (Unimplemented) ConvertPirAttribute2RuntimeAttribute
    not support [pir::ArrayAttribute<pir::DoubleAttribute>]
    (at .../new_executor/instruction/onednn/onednn_instruction.cc:116)
  ```

  PaddleOCR 기본값이 `enable_mkldnn=True`라서, 그대로 두면 CPU 추론이 아예
  안 된다. `OCR_ENABLE_MKLDNN=false`가 기본값인 이유다.

  **이것이 수치에 미치는 영향을 반드시 같이 적어야 한다.** oneDNN은 CPU 추론
  가속 라이브러리이므로 끄면 그만큼 느려진다. 즉 이 프로젝트의 모든 수치는
  **"가속을 끈"** 값이다. paddle을 올려 고쳐지면 다시 측정해 비교할 것.

  엔진 생성과 모델 로드는 성공하고 첫 `predict()`에서 죽는다. 그래서 `/health`도
  `/ready`도 통과하는데 `/ocr`만 500이 된다 — 파드가 Ready인 것이 추론 가능을
  뜻하지 않는 경우다.
- **큰 이미지를 그대로 추론하면 컨테이너가 죽는다.** 3024×4032 휴대폰 사진은
  디코딩만 35MB이고 파이프라인이 만드는 float32 사본은 하나에 140MB다. 사본이
  여러 개 동시에 뜨면 파드 메모리를 넘겨 요청 처리 도중 컨테이너가 죽는다.

  ```
  브라우저   TypeError: Failed to fetch
  curl       exit 52 (Empty reply from server), HTTP 000
  ```

  **상태 코드가 없다**는 것이 이 실패의 특징이다. 500이 아니라 답할 주체가
  사라진 것이라, 밖에서는 원인이 보이지 않는다. 파드는 1~2분 뒤 스스로 복구된다.

  메모리 제한이 없는 노트북에서는 재현되지 않는다. 컨테이너에 넣어야 나타난다.

  `OCR_MAX_IMAGE_SIDE`(기본 1600)로 추론 전에 축소해서 막는다. PaddleOCR의
  `text_det_limit_side_len`이 아니라 `decode_image()`에서 줄이는 이유는, 그
  설정은 검출 입력만 제한하고 인식 단계는 여전히 원본 배열에서 잘라 쓰기
  때문이다. 배열 자체를 줄여야 양쪽이 같이 잡힌다.

- **스레드 수를 두 군데서 맞춰야 한다.** `OMP_NUM_THREADS`(OpenMP)와 paddle 자체의
  `cpu_threads`는 별개다. 후자의 PaddleOCR 기본값이 **10**이라 컨테이너의 CPU
  limit과 무관하게 스레드 10개가 뜬다. 2코어 파드에서 서로 밀어내면 지연이
  불안정해져 벤치마크의 재현성이 떨어진다. 둘 다 CPU limit에 맞출 것.

---

## 8. 검증 기록

### 로컬 (2026-09-13, macOS arm64 / Python 3.12 / paddlepaddle 3.3.1 / paddleocr 3.7.0)

```
pytest              8 passed (실제 추론 테스트 포함)
GET /info           device: cpu, ocr_ready: true
POST /ocr (영수증)   10건 인식, confidence 0.936 ~ 0.9999, elapsed_ms 1400~1560
POST /ocr (영수증2)  7건 인식
동시성              MAX_CONCURRENCY 1/4 모두 0.72 req/s (5.1 참고)
```

한글 영수증 10줄을 **전부 정확히** 인식했다. 실제 인식 결과:

```
스타벅스 강남점 · 아메리카노 · 4,500 · 카페라떼 · 5,000
치즈케이크 · 6,500 · 합계 · 16,000 · 2026-09-13 14:22
```

### 클러스터

아직 추론 검증이 끝나지 않았다. 6.1 참고.
