# 진행 상황과 계획

마지막 갱신: 2026-11-25

이 문서는 "지금 어디까지 왔고 다음에 뭘 하는가"만 다룬다. 기술적 배경과 설계
근거는 [README.md](README.md), 클러스터 구축 절차는
[deploy/cluster/bootstrap.md](deploy/cluster/bootstrap.md)에 있다.

---

## 0. 무엇을 하려는 프로젝트인가

**GPU 자원을 여러 테넌트가 나눠 쓸 때, 한 장을 몇으로 쪼개 팔 수 있는가**를
측정으로 답하는 것.

동일한 추론 workload(한글 영수증 OCR)를 고정해 두고 아래를 바꿔 가며 잰다.

```
실행 장치     CPU  →  A100  →  H100
분할 방식     없음 →  MIG  /  time-slicing
동시 파드 수  1  →  2  →  4  →  8
```

workload 자체는 목적이 아니다. **환경이 바뀌어도 그대로 돌릴 수 있고 비교할 수
있는, 작고 예측 가능한 부하**로서 존재한다. 그래서 의도적으로 단순하게 유지한다.

측정하려는 것:

| | |
|---|---|
| 처리량 | 초당 처리 이미지 수 (파드 수를 올릴 때 선형인가) |
| 지연 | 요청당 소요 시간 분포 |
| 자원 상한 | 파드당 메모리/VRAM 한도와 그로부터 역산한 **수용 가능 테넌트 수** |
| 사용률 | GPU SM 점유율, VRAM, 전력 — 쪼갤수록 어떻게 변하는가 |

---

## 1. 지금까지 한 것

### 1.1 완료

- [x] **추론 workload** — FastAPI + PaddleOCR. 한글 영수증에서 텍스트를 뽑고
      날짜·가맹점·용도·판매자를 추출해 파일명을 짓는다
- [x] **컨테이너 이미지** — 모델을 이미지에 구워 둬서 파드 기동 시 다운로드 없음
- [x] **쿠버네티스 클러스터** — VM 3대, kubeadm, Calico
- [x] **로컬 레지스트리** — 파드를 worker 2대에 퍼뜨리기 위해 필수
- [x] **노드 자원 정책** — `kube-reserved` / `systemReserved` / `evictionHard`
- [x] **CPU 기준선 1파드** — 실제 영수증으로 동작 확인

### 1.2 환경

| | |
|---|---|
| OS | Ubuntu 24.04 LTS (amd64) |
| 런타임 | containerd 2.2.1 (SystemdCgroup) |
| 쿠버네티스 | v1.36.6 (kubeadm) |
| CNI | Calico v3.32.2 (IPIP always) |
| 빌더 | buildkit v0.35.1 (containerd worker, `k8s.io` namespace) |
| 레지스트리 | `registry:2`, cp, NodePort 30500, 평문 HTTP |

| 노드 | IP | 제원 | allocatable | 역할 |
|---|---|---|---|---|
| `k8s-cp` | 10.255.42.124 | 4 vCPU / 8GB / 80GB | — | control plane + 레지스트리 |
| `k8s-worker1` | 10.255.42.60 | 8 vCPU / 16GB / 100GB | 7 CPU / 13.6Gi | 측정 대상 |
| `k8s-worker2` | 10.255.42.77 | 8 vCPU / 16GB / 100GB | 7 CPU / 13.6Gi | 측정 대상 |

`capacity 8 CPU / 16,273,380Ki` 에서 예약분을 뺀 결과가
`allocatable 7 CPU / 14,290,916Ki` 다.

### 1.3 모델 구성

```
검출  PP-OCRv5_server_det          84MB
인식  korean_PP-OCRv5_mobile_rec   13MB
```

`lang="korean"` 이면 PaddleOCR가 이 조합을 **강제**한다
(`paddleocr/_pipelines/ocr.py:360~391`). PP-OCRv5 계열에 한국어 server 인식
모델은 존재하지 않는다 — server 판은 중국어·번체·일본어용 하나뿐이다.
따라서 메모리를 줄이는 수단은 모델 교체가 아니라 **입력 해상도** 뿐이다.

### 1.4 CPU 기준선 측정값

파드 1개, `OCR_MAX_IMAGE_SIDE=640`, 실제 영수증 사진(3024×4032 → 480×640):

| | |
|---|---|
| 인식된 텍스트 | **41건** |
| 신뢰도 | **0.88 ~ 1.00** |
| 추론 시간 | **14,567 ms** |
| 한글 인식 | 정상 (거래정보, 카드승인번호, 닭가슴샐러드덮밥 외 2개, 부가세액 …) |
| 파일명 생성 | `26.09.14(주)우아한형제들_점심_싱싱샐러드` — 네 항목 전부 정확 |

파드 설정: `requests = limits = cpu 1500m / memory 3Gi` (Guaranteed QoS),
`OCR_CPU_THREADS=1`, `OMP_NUM_THREADS=1`.

> 맥(48GB / 12코어, 스레드 2)에서 같은 크기를 쟀을 때는 **1,646 ms** 였다.
> 약 9배 차이는 ① 추론 스레드 2→1 ② CPU 1500m 제한 ③ 코어 성능 차이가 겹친
> 결과로 보인다. 분해해서 재지는 않았다.

### 1.5 메모리 공식 (Capacity Planning의 근거)

맥에서 `resource.getrusage(RUSAGE_SELF).ru_maxrss` 로 해상도를 바꿔 가며 측정:

| 입력 | 피크 RSS | 시간 |
|---|---|---|
| 1200×1600 | 11,247 MB | 6,321 ms |
| 900×1200 | 6,906 MB | 3,994 ms |
| 750×1000 | 5,112 MB | 2,771 ms |
| 600×800 | 3,913 MB | 2,143 ms |
| 480×640 | 2,997 MB | 1,646 ms |
| 360×480 | 2,312 MB | 1,181 ms |

```
피크 ≈ 1,380MB(모델 로드) + 5.1KB × 입력 픽셀 수
```

세 자릿수 범위에서 거의 선형이다. 할당자 캐싱 때문이 아님을 확인했다 —
`FLAGS_use_system_allocator=true` 로 캐싱을 꺼도 12.3GB,
`FLAGS_fraction_of_cpu_memory_to_use=0.2` 로도 11.3GB 였다.

이 식에서 **역산**해 파드 설정을 정했다:

```
쓸 수 있는 픽셀 수 = (limits.memory − 1,380MB) / 5.1KB

파드 8개 목표
  → worker 2대 × 4개
  → allocatable 13.6Gi ÷ 4 = 파드당 3.4Gi 상한
  → limits.memory 3Gi 채택
  → (3,072 − 1,380) / 0.0051 ≈ 332,000px
  → 3:4 비율에서 긴 변 약 665px
  → OCR_MAX_IMAGE_SIDE = 640
```

즉 **"파드 8개"라는 목표가 입력 해상도를 결정했다.** 반대 방향이 아니다.

### 1.6 실패에서 배운 것

클러스터를 한 번 세웠다가 지우고 다시 세웠다. 첫 번째에서 겪은 것들:

**① `limits`를 노드 `allocatable`보다 높게 두면 원인이 읽히지 않는다**

`limits.memory=4Gi`, 노드 allocatable `3,810Mi`. 컨테이너가 자기 한도에 닿기
전에 **노드가 먼저 말랐다.** 그 죽음은 probe 실패를 동반한 SIGTERM/Error로
나타나서 메모리 문제가 아니라 probe 문제처럼 읽혔다. 한도를 allocatable 아래로
내리니 같은 초과가 **깔끔한 `OOMKilled`(exit 137)** 로 드러났다.

> 낮게 틀리면 원인이 읽히고, 높게 틀리면 안 읽힌다.

**② 파드가 노드 CPU를 다 가져가면 kubelet이 멀쩡한 컨테이너를 죽인다**

```
Liveness probe failed: ... context deadline exceeded
Killing: Container ocr failed liveness probe, will be restarted
```

2코어 노드에 `limits.cpu=2`. kubelet·containerd·Calico가 CPU를 못 받아
probe가 타임아웃 났다. 당시엔 파드 `limits`를 줄여서 피했지만, 그건 매니페스트
마다 조심해야 하는 방식이다. **노드 수준에서 `kube-reserved`로 떼어 두는 것이
옳다** — 이번 클러스터에서는 처음부터 그렇게 했다.

**③ 스레드 수가 두 층위에 있다**

`OMP_NUM_THREADS`(OpenMP)와 paddle 자체의 `cpu_threads`는 별개다. 후자의
PaddleOCR 기본값이 **10**이라 컨테이너 CPU limit과 무관하게 스레드 10개가 뜬다.
둘 다 limit에 맞춰야 한다. 추론 스레드가 quota를 다 가져가면 uvicorn 이벤트
루프가 스케줄되지 못해 `/health`가 응답을 못 하고, 그래서 ②가 일어난다.

> 느린 추론보다 응답하는 파드가 값어치 있다.

**④ 같은 태그로 다시 빌드하면 파드가 안 바뀐다**

`imagePullPolicy: IfNotPresent` 라서 노드에 그 태그가 이미 있으면 받아오지
않는다. "재배포했는데 예전 코드가 돈다"로 한참 헤맸다. 벤치마크에서는 **어떤
이미지를 측정했는지 특정할 수 있어야** 하므로, `Always`보다 태그를 올리는 쪽이
맞다.

**⑤ 모델을 바꿔 자원을 줄이려다 품질을 잃었다**

검출을 `PP-OCRv5_mobile_det`로 바꾸니 피크가 11,247MB → 1,598MB로 떨어졌다.
그런데 **한글을 전혀 못 읽었다** — 신뢰도 0.0, 빈 문자열. 더 고약한 것은
`count`가 10으로 똑같았다는 점이다. 검출은 상자를 그대로 찾고 인식만 실패하므로,
"10건 인식됨"만 보는 검증은 이 고장을 통과시킨다.

> 모델을 바꿀 때는 건수가 아니라 **텍스트 내용**을 비교할 것.

**⑥ 환경 쪽에서 반나절을 잃은 것들**

| 증상 | 원인 |
|---|---|
| `net.bridge.*` sysctl이 조용히 무시됨 | `br_netfilter` 모듈 미로드. 키 자체가 없어서 오류도 안 난다 |
| `ImagePullBackOff` (coredns, calico-kube-controllers) | 노드 DNS. `/etc/resolv.conf` 손수정은 `systemd-resolved`가 덮어쓴다. netplan의 `nameservers` + **`dhcp4-overrides.use-dns: false`** 가 필요했다 |
| `kube-reserved` 플래그가 안 먹음 | 1.36에서 전부 DEPRECATED. systemd drop-in의 `KUBELET_EXTRA_ARGS`가 실제로 전달되지 않았다. `/var/lib/kubelet/config.yaml`(KubeletConfiguration)에 넣어야 한다 |
| `ctr pull`이 HTTPS로 붙음 | `ctr`은 CRI 플러그인을 거치지 않는다. `--hosts-dir`가 필요하고, kubelet 경로 검증은 `crictl`로 해야 한다 |

---

## 2. 앞으로 할 것

### 2.1 CPU 기준선 마무리 — 바로 다음

- [ ] **파드 개수 스케일 테스트** `1 → 2 → 4 → 8`

```bash
kubectl -n ocr-bench scale deploy/ocr --replicas=2
kubectl -n ocr-bench get pods -o wide
```

`topologySpreadConstraints(maxSkew: 1)`가 worker 2대에 고르게 가르도록 해 뒀다.
기대 배치:

| replicas | worker1 | worker2 |
|---|---|---|
| 1 | 1 | 0 |
| 2 | 1 | 1 |
| 4 | 2 | 2 |
| 8 | 4 | 4 |

8개가 전부 `Running`이면 1.5절의 역산이 맞았다는 뜻이다. 하나라도 `Pending`
이면 `describe pod`의 `Insufficient cpu` / `Insufficient memory`를 확인한다.

> **아직 검증되지 않음.** 8파드는 계산상 들어가는 것이지 띄워 본 것이 아니다.

- [ ] **부하 생성기** — 브라우저 업로드로는 재현 가능한 측정이 안 된다.
      고정된 영수증 이미지 N장을 정해진 동시성으로 밀어넣고 처리량·지연 분포를
      내는 스크립트가 필요하다. GPU 단계에서도 같은 것을 쓴다
- [ ] **스레드 수 재측정** — 8코어가 됐으니 `OCR_CPU_THREADS` 1 vs 2 vs 4를
      다시 볼 가치가 있다. 14.5초가 줄어들 수 있고, probe가 버티는지가 관건

### 2.2 GPU 노드 추가

사내에 A100 / H100 장비가 있다. **제공 형태를 먼저 확인해야 한다.**

```bash
openstack flavor list --long | grep -i -E 'gpu|a100|h100'
openstack flavor show <GPU flavor>
```

| 방식 | properties | MIG 실험 |
|---|---|---|
| PCI passthrough | `pci_passthrough:alias='a100:1'` | ✅ 게스트가 직접 제어 |
| vGPU (GRID) | `resources:VGPU='1'` | ⚠️ 호스트가 프로파일을 정함 |

MIG 실험을 하려면 **passthrough로 한 장을 통째로** 받아야 한다. 베어메탈
(DGX/HGX)이라면 거기에 직접 올리는 쪽이 낫고, Slurm 얘기까지 붙일 수 있다.

- [ ] GPU flavor 확인 (passthrough / vGPU / 베어메탈)
- [ ] GPU VM 생성 → NVIDIA 드라이버 (**≥ 550.54.14**, CUDA 12.6 빌드 기준)
- [ ] k8s join + 라벨 (`ocr-bench/device=a100` 등)
- [ ] `nvidia-container-toolkit` + containerd runtime 등록
- [ ] `nvidia-device-plugin` (MIG를 쓸 거면 `gpu-operator`)
- [ ] `nvidia-smi`, 그리고 파드 안에서 GPU가 보이는지 확인

### 2.3 GPU 워크로드

- [ ] **`gpu.py` 복원** — `740d9c8^` 에 그대로 있다. `resolve_device()`,
      `cuda_is_usable()`, `get_gpu_info()` 전부 lazy import + 예외 처리가 돼
      있어 CPU 머신에서도 그대로 뜬다
- [ ] `/info` 에 GPU 항목 되살리기 (`cuda_available`, `gpu_name`, VRAM)
- [ ] **GPU 이미지 분리**

```bash
pip install paddlepaddle-gpu==3.3.1 \
  -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
```

```
ocr-workload:0.1.0-cpu     583MB     ← 지금 것, 기준선용으로 유지
ocr-workload:0.1.0-gpu     3GB+      ← CUDA 런타임 포함
```

CPU 기준선 측정에 CUDA 런타임을 끌고 다닐 이유가 없으므로 태그를 나눈다.

- [ ] **`OCR_MAX_IMAGE_SIDE` 재검토** — VRAM이 40~80GB라 1.5절의 메모리 제약이
      사라진다. 원본 3024×4032를 그대로 넣을 수 있고, 그래야 GPU 간 차이가 드러난다

### 2.4 GPU 공유 정책 ★ 핵심

- [ ] **MIG 분할** — `1g.5gb`, `2g.10gb`, `3g.20gb` 등 프로파일별 파드 수용량
- [ ] **time-slicing** — device plugin의 `replicas` 설정, 격리 없음
- [ ] **두 방식 비교** — 같은 부하를 주고 테넌트당 처리량·지연 p50/p99
- [ ] 파드 `1 / 2 / 4 / 7` 로 올리며 측정 (A100 MIG 최대 7조각)

```
A100 1장
   ├─ MIG            하드웨어 분할. SM·L2·메모리 대역폭까지 격리
   ├─ Time-slicing   시분할. 격리 없음, 이웃이 느려지면 나도 느려짐
   └─ MPS            프로세스 컨텍스트 공유
```

**이게 GPUaaS의 핵심 질문이다.** "GPU를 직접 사지 않고 필요한 만큼 쓴다"는
서비스를 만들려면 *한 장을 몇으로 쪼개 팔 수 있는가*를 답해야 한다. CPU는
`1500m`처럼 쪼갤 수 있지만 GPU는 기본적으로 `nvidia.com/gpu: 1` 단위라 파드
하나가 통째로 점유한다.

### 2.5 사용률 분석

- [ ] **DCGM Exporter** (DaemonSet) → Prometheus → Grafana
- [ ] SM 점유율 / VRAM / 메모리 대역폭 / 전력 / SM 클럭
- [ ] MIG 인스턴스별 분리 측정
- [ ] **쪼갤수록 사용률이 어떻게 변하는가** — 1파드일 때 SM이 놀고 있다면
      그만큼 더 팔 수 있다는 뜻이다. 그게 이 측정의 목적

### 2.6 종합

- [ ] **Capacity Planning 문서** — "A100 1장에 OCR 테넌트 몇을 수용하는가"를
      CPU 기준선과 같은 방식(측정 → 공식 → 정책)으로 정리
- [ ] CPU / A100 / H100 × 파드 수 전체 비교표
- [ ] 비용 대비 처리량 (장비 단가를 알 수 있으면)

---

## 3. 열려 있는 질문

| | |
|---|---|
| 파드 8개가 실제로 뜨는가 | 계산은 맞지만 띄워 본 적 없음 |
| A100과 H100이 구별되는가 | 배치 1에서는 커널 실행 오버헤드가 지배해 차이가 측정 오차에 묻힐 수 있다. 입력을 키우거나 동시성을 올려야 드러난다 |
| 14.5초를 줄일 수 있는가 | 스레드 수를 올리면 probe가 버티는지 |
| GPU에서 입력 해상도 상한 | VRAM 제약이 사라지면 어디까지 올리는 것이 의미 있는가 |
| MIG 프로파일 선택 기준 | `1g.5gb` 7개 vs `3g.20gb` 2개 중 OCR에 맞는 쪽 |

---

## 4. 이 프로젝트가 다루지 않는 것

범위를 분명히 해 둔다. 아래는 AI Factory 운영에서 중요하지만 이 프로젝트에
없다.

- **분산 학습** — NCCL, InfiniBand/RDMA, 다중 노드 통신. 이 workload는 추론
  단일 노드다
- **병렬 스토리지** — Lustre/GPFS/WEKA. 모델을 이미지에 구워서 스토리지 I/O가
  경로에 없다
- **배치 스케줄러** — Slurm. 쿠버네티스만 다룬다
- **멀티테넌시의 보안 측면** — 네임스페이스 격리, 네트워크 정책, 과금
