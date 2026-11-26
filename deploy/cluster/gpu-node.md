# GPU 노드 추가 절차

H100 VM 1대를 기존 클러스터(cp + worker1 + worker2)에 워커로 붙이는 순서.
2026-11-26에 작성했다.

CPU 워커를 붙일 때와 **공통인 부분은 `bootstrap.md` 1절을 그대로 따른다**
(hostname / swap / 커널 모듈 / sysctl / DNS / containerd / kubeadm 패키지).
이 문서는 **GPU 때문에 추가로 해야 하는 것만** 다룬다:

| 단계 | 하는 일 | 왜 |
|---|---|---|
| 2 | NVIDIA 드라이버 | 호스트 커널이 GPU를 모른다 |
| 3 | NVIDIA Container Toolkit | 컨테이너 안으로 `/dev/nvidia*`와 드라이버 라이브러리를 넣어 준다 |
| 6 | NVIDIA device plugin | kubelet에게 `nvidia.com/gpu` 라는 자원을 알려 준다 |

세 개가 **직렬로 의존한다.** 드라이버가 없으면 toolkit이 동작하지 않고,
toolkit이 없으면 device plugin 파드가 뜨지 못하고, device plugin이 없으면
`nvidia.com/gpu: 1` 을 요청하는 파드가 영원히 `Pending`이다.
**한 단계씩 확인하고 넘어가야 어디서 틀어졌는지 알 수 있다.**

---

## 0. [gpu 노드] 시작 전 확인 — 이 결과가 뒤의 선택을 가른다

드라이버를 깔기 **전에** 네 가지를 본다. 특히 0.1은 ROADMAP 2.4절(GPU 분할
실험)이 가능한지를 결정한다.

### 0.1 GPU가 VM에 어떤 방식으로 붙어 있나 — passthrough vs vGPU

```bash
lspci -nn | grep -i nvidia
```

| 출력 | 방식 | MIG | 의미 |
|---|---|---|---|
| `3D controller [0302]: NVIDIA Corporation GH100 [10de:2330]` 같은 **물리 칩 이름** | **PCI passthrough** | **게스트에서 켤 수 있다** | GPU 한 장이 통째로 이 VM 것이다 |
| `GRID H100-...` / 장치 이름에 vGPU 표기 | **vGPU (mediated)** | **게스트에서 못 켠다** | 하이퍼바이저가 이미 쪼개 놓은 조각을 받은 것 |

- **passthrough면** MIG(하드웨어 분할)와 time-slicing(소프트웨어 분할)을 **둘 다**
  실험할 수 있다. ROADMAP 2.4절이 그대로 성립한다.
- **vGPU면** 분할 정책이 OpenStack 쪽에서 이미 정해진 상태라 게스트에서 바꿀 수
  없다. 이때는 time-slicing만 비교 축으로 쓰고, MIG는 "왜 못 했는지"를 문서에
  남기는 쪽이 정직하다.

OpenStack 쪽에서도 같이 확인하면 확실하다:

```bash
# [맥 또는 OpenStack CLI 가 되는 곳]
openstack flavor show <flavor-name> -f json | grep -i -E 'pci|vgpu|resources'
```

`pci_passthrough:alias` → passthrough / `resources:VGPU` → vGPU.

### 0.2 Secure Boot

```bash
mokutil --sb-state
```

`SecureBoot enabled` 면 서명되지 않은 커널 모듈을 못 올린다. NVIDIA 드라이버는
DKMS로 빌드되므로 **설치는 되는데 `modprobe`에서 막혀 `nvidia-smi`가
`NVIDIA-SMI has failed...`로 실패한다.** 끄거나(OpenStack 이미지 속성) MOK에
키를 등록해야 한다. `disabled` 또는 `command not found`면 그냥 진행한다.

### 0.3 디스크 여유

```bash
df -h /var/lib/containerd /var/lib
nproc; free -h
```

**GPU 이미지는 CPU 이미지보다 훨씬 크다.** `paddlepaddle-gpu`는 cuDNN·cuBLAS 등
CUDA 라이브러리를 끌고 들어와서, 모델까지 구우면 **8~12GB** 가 된다(CPU 이미지는
2GB대). 드라이버까지 합쳐 **최소 60GB는 비어 있어야** 한다. 모자라면 빌드가
중간에 `no space left on device`로 죽는데, 15분쯤 기다린 뒤에 나와서 아깝다.

### 0.4 기존 노드와 같은 서브넷인지

```bash
ip -4 addr show scope global | grep inet
```

`10.240.0.x` 가 아니면 보안 그룹 규칙(`bootstrap.md` 0.1절)을 새 서브넷에도
열어 줘야 한다. VXLAN(4789/udp)과 10250/tcp가 특히 중요하다.

---

## 1. [gpu 노드] 공통 노드 준비

`bootstrap.md` **1.1 ~ 1.7절을 그대로** 실행한다. 바뀌는 것은 호스트 이름뿐이다.

```bash
sudo hostnamectl set-hostname k8s-gpu
```

### 1.1 /etc/hosts — 이번엔 **4대 모두** 고쳐야 한다

기존 3대에도 새 노드를 알려 줘야 한다. 한 대만 고치면 그 한 대만 안다.

```bash
# [4대 모두] GPU 노드 줄을 추가. 기존 3줄이 이미 있으면 이 줄만.
GPU_IP=<새 VM 의 IP>
echo "$GPU_IP  k8s-gpu" | sudo tee -a /etc/hosts
```

```bash
# 확인 - 4대 모두에서
getent hosts k8s-cp k8s-worker1 k8s-worker2 k8s-gpu
```

### 1.2 DNS — 먼저 확인할 것

첫 클러스터에서 **가장 많이 시간을 뺏긴 것이 이것이다.** `/etc/resolv.conf`는
`stub-resolv.conf` 심볼릭 링크라서 손으로 고치면 덮어써진다. 드라이버를
apt로 받아야 하므로 DNS가 안 되면 2절에서 바로 막힌다.

```bash
getent hosts developer.download.nvidia.com
```

안 나오면 netplan으로 고친다. **`dhcp4-overrides`가 핵심이다** — 이게 없으면
DHCP가 주는 DNS가 이겨서 설정이 무시된다.

```bash
IFACE=$(ip -4 route show default | awk '{print $5}' | head -1); echo $IFACE

sudo tee /etc/netplan/99-dns.yaml >/dev/null <<EOF
network:
  version: 2
  ethernets:
    $IFACE:
      dhcp4: true
      dhcp4-overrides:
        use-dns: false
      nameservers:
        addresses: [8.8.8.8, 8.8.4.4]
EOF

sudo chmod 600 /etc/netplan/99-dns.yaml
sudo netplan try          # 120초 안에 Enter 를 눌러야 유지된다
resolvectl status | grep -A2 'DNS Servers'
```

### 1.3 kubeadm 패키지는 **기존 노드와 같은 버전으로**

```bash
apt-cache madison kubeadm | head -5
K8S_VER=1.36.6-1.1        # ← 기존 3대와 동일해야 한다
sudo apt-get install -y kubelet=$K8S_VER kubeadm=$K8S_VER kubectl=$K8S_VER
sudo apt-mark hold kubelet kubeadm kubectl
```

버전이 어긋나면 join 자체는 되는데 나중에 조용히 이상하게 동작한다.

---

## 2. [gpu 노드] NVIDIA 드라이버

### 2.1 왜 Ubuntu 기본 저장소가 아니라 NVIDIA 저장소인가

Ubuntu 저장소의 `nvidia-driver-*-server`도 동작하지만 버전이 뒤처진다.
H100(Hopper)은 비교적 새 아키텍처라 **NVIDIA의 CUDA 저장소에서 받는 쪽이
안전하다.** 그리고 우리는 뒤에서 `paddlepaddle-gpu` (CUDA 12.6 빌드)를 쓸
예정이라 **드라이버가 550.54.14 이상이어야 한다.**

> CUDA는 minor version compatibility가 있어서 드라이버가 12.x 계열 최소
> 버전만 넘으면 12.6 런타임이 돈다. 그래도 최신 data center 브랜치를 쓰면
> 고민할 일이 없다.

### 2.2 설치

```bash
sudo apt-get update
sudo apt-get install -y build-essential linux-headers-$(uname -r)
```

커널 헤더가 없으면 DKMS가 모듈을 빌드하지 못한다. **설치는 성공한 것처럼
끝나고 재부팅 뒤에야 `nvidia-smi`가 실패한다.**

```bash
cd /tmp
curl -fsSLO https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update
```

```bash
# 어떤 드라이버가 있는지 먼저 본다
apt-cache search '^nvidia-open' ; apt-cache policy nvidia-open
```

```bash
sudo apt-get install -y nvidia-open
```

- `nvidia-open` = **open GPU kernel module** 방식. Hopper(H100) 이후 세대는
  NVIDIA가 이쪽을 권장하고, 신규 기능도 여기에 먼저 들어온다.
- 구형 proprietary 모듈이 필요하면 `cuda-drivers`를 대신 쓴다. H100에서는
  `nvidia-open`을 먼저 시도할 것.
- nouveau 블랙리스트는 패키지가 알아서 넣는다.

```bash
sudo reboot
```

**재부팅이 필요하다.** 커널 모듈을 바꿨기 때문에 그냥 `modprobe`로 끝나지 않는다.

### 2.3 확인

```bash
nvidia-smi
```

```
+-----------------------------------------------------------------------------+
| NVIDIA-SMI 5xx.xx       Driver Version: 5xx.xx     CUDA Version: 12.x        |
|   0  NVIDIA H100 ...              On  |   00000000:00:05.0 Off |         0   |
|  N/A   32C    P0    70W / 350W |      0MiB / 81559MiB |      0%      Default |
+-----------------------------------------------------------------------------+
```

**이 출력을 기록해 둘 것.** 포트폴리오에 "어떤 하드웨어에서 쟀는가"를 적어야
하는데, 메모리 용량(80GB / 94GB)과 폼팩터(SXM / PCIe)가 성능을 크게 가른다.

```bash
# 기록용 - 한 줄로
nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap,pcie.link.gen.max \
           --format=csv
```

안 나올 때:

| 증상 | 원인 |
|---|---|
| `NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver` | 모듈 미적재. `lsmod \| grep nvidia`, `dmesg \| grep -i nvidia`. Secure Boot(0.2절) 의심 |
| `command not found` | 설치 자체가 안 됨. `dpkg -l \| grep nvidia` |
| `No devices were found` | VM에 GPU가 안 붙음. 0.1절의 `lspci` 부터 |

---

## 3. [gpu 노드] NVIDIA Container Toolkit

드라이버가 있어도 **컨테이너 안에서는 GPU가 안 보인다.** 컨테이너는 자기만의
`/dev`를 보기 때문이다. toolkit이 컨테이너를 만들 때 `/dev/nvidia*` 장치와
호스트의 드라이버 라이브러리(`libcuda.so` 등)를 주입해 준다.

### 3.1 설치

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null

sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
nvidia-ctk --version
```

### 3.2 containerd에 등록

```bash
sudo cp /etc/containerd/config.toml /etc/containerd/config.toml.bak
sudo nvidia-ctk runtime configure --runtime=containerd
```

`nvidia` 라는 런타임 핸들러가 `config.toml`에 추가된다. 기본 런타임은 `runc`
그대로 둔다 — **`--set-as-default`를 쓰지 않는다.** 모든 파드가 nvidia 런타임을
타게 되면 GPU를 안 쓰는 파드까지 영향을 받고, 측정 대상이 아닌 쪽에 변수가
생긴다. device plugin이 알아서 GPU 파드에만 붙여 준다.

### 3.3 ★ cgroup driver 다시 확인 — 놓치기 쉬운 곳

`nvidia-ctk`가 추가한 **nvidia 런타임 블록에도 `SystemdCgroup = true`가 있어야
한다.** 여기가 `false`면 GPU 파드만 무작위로 죽거나 노드가 흔들리는데,
`runc` 쪽은 멀쩡하니 원인을 엉뚱한 데서 찾게 된다.

```bash
grep -n -B8 'SystemdCgroup' /etc/containerd/config.toml
```

nvidia 블록 아래에 없으면 넣는다:

```bash
sudo sed -i '/runtimes\.nvidia\.options/,/^\[/ s/^\(\s*\)SystemdCgroup = false/\1SystemdCgroup = true/' \
  /etc/containerd/config.toml

# 그래도 없으면 손으로. containerd 2.x 기준 경로:
#   [plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.nvidia.options]
#     SystemdCgroup = true
```

```bash
sudo systemctl restart containerd
sudo systemctl is-active containerd
```

### 3.4 레지스트리 설정이 남아 있는지 확인

`nvidia-ctk`가 `config.toml`을 다시 쓰면서 `bootstrap.md` 7.2절의 certs.d
설정을 날렸을 수 있다. **날아가면 `ImagePullBackOff`가 난다.**

```bash
grep -n 'config_path' /etc/containerd/config.toml
```

없으면 다시 넣는다 (containerd 2.x):

```bash
CP_IP=10.240.0.18

sudo mkdir -p /etc/containerd/certs.d/$CP_IP:30500
sudo tee /etc/containerd/certs.d/$CP_IP:30500/hosts.toml >/dev/null <<EOF
server = "http://$CP_IP:30500"

[host."http://$CP_IP:30500"]
  capabilities = ["pull", "resolve", "push"]
  skip_verify = true
EOF

grep -q 'config_path' /etc/containerd/config.toml || \
sudo tee -a /etc/containerd/config.toml >/dev/null <<'EOF'

[plugins.'io.containerd.cri.v1.images'.registry]
  config_path = '/etc/containerd/certs.d'
EOF

sudo systemctl restart containerd
```

> containerd 2.2.1은 `config_path = '/etc/containerd/certs.d:/etc/docker/certs.d'`
> 가 **기본값**이라 이미 들어 있을 수 있다. 있으면 건드리지 않는다.

### 3.5 k8s 없이 먼저 확인 — 여기서 걸러야 한다

device plugin을 올리기 전에, **containerd 수준에서 GPU가 컨테이너에 들어가는지**
본다. 이게 되면 뒤는 쉽고, 안 되면 3절로 돌아온다.

```bash
sudo ctr -n k8s.io images pull docker.io/nvidia/cuda:12.6.0-base-ubuntu24.04

sudo ctr -n k8s.io run --rm --gpus 0 \
  docker.io/nvidia/cuda:12.6.0-base-ubuntu24.04 gputest nvidia-smi
```

컨테이너 **안에서** `nvidia-smi`가 H100을 보여 주면 3절은 끝이다.

---

## 4. [gpu 노드] 클러스터에 join

```bash
# [cp] join 명령을 새로 만든다 (기존 토큰은 24시간이면 만료된다)
kubeadm token create --print-join-command
```

```bash
# [gpu 노드] 위 출력을 그대로
sudo kubeadm join 10.240.0.18:6443 --token <토큰> \
  --discovery-token-ca-cert-hash sha256:<해시>
```

```bash
# [cp]
kubectl get nodes -o wide
# k8s-gpu 이 보이고, Calico 가 뜨면 Ready 가 된다 (1~2분)
```

Calico DaemonSet이 새 노드에 자동으로 파드를 띄운다. 따로 할 일은 없다.

```bash
kubectl -n kube-system get pods -o wide | grep k8s-gpu
```

---

## 5. [gpu 노드] kube-reserved

CPU 워커와 같은 이유다. **systemd drop-in의 `KUBELET_EXTRA_ARGS`는 동작하지
않는다** — 1.36에서 `--kube-reserved` 류는 deprecated고, kubeadm이 설치한
kubelet은 `--config`만 읽는다. `config.yaml`에 직접 넣어야 한다.

```bash
sudo tee -a /var/lib/kubelet/config.yaml >/dev/null <<'EOF'
kubeReserved:
  cpu: "500m"
  memory: "512Mi"
systemReserved:
  cpu: "500m"
  memory: "1Gi"
evictionHard:
  memory.available: "500Mi"
  nodefs.available: "10%"
EOF

sudo systemctl restart kubelet
sudo systemctl is-active kubelet
```

```bash
# [cp] 확인 - capacity 보다 줄어 있어야 한다
kubectl describe node k8s-gpu | grep -A8 Allocatable
```

> **GPU 메모리는 여기서 다루지 않는다.** `nvidia.com/gpu`는 개수 단위 정수
> 자원이라 kube-reserved의 대상이 아니다. GPU 메모리를 나누는 것은 6절 이후의
> MIG / time-slicing 이야기다.

---

## 6. [cp] 노드 라벨과 taint

### 6.1 라벨

```bash
kubectl label node k8s-gpu ocr-bench/role=gpu
kubectl label node k8s-gpu ocr-bench/accelerator=h100

kubectl get nodes -L ocr-bench/role,ocr-bench/accelerator
```

`accelerator` 라벨을 따로 두는 이유: 나중에 A100 노드를 추가하면 **같은
매니페스트에 `nodeSelector`만 바꿔서** CPU / A100 / H100 비교가 된다. 머신
이름을 박으면 매번 매니페스트를 고치게 된다 (`bootstrap.md` 6절과 같은 이유).

### 6.2 taint — GPU 노드를 비싸게 지킨다

```bash
kubectl taint node k8s-gpu nvidia.com/gpu=present:NoSchedule
```

H100은 클러스터에서 가장 비싼 자원이다. **아무 파드나 올라오면 측정이
오염된다.** taint를 걸면 명시적으로 `tolerations`를 쓴 파드만 올라온다.

지금 돌고 있는 CPU 워크로드는 `nodeSelector: ocr-bench/role=target`이라 어차피
안 올라오지만, 시스템 DaemonSet이나 나중에 추가할 것들까지 막아 준다.
device plugin은 이 taint를 견디도록 되어 있다.

> 이건 GPUaaS 운영에서 실제로 쓰는 패턴이다. GPU 노드 풀을 taint로 격리하고,
> GPU를 요청하는 워크로드만 toleration을 달아 들어오게 한다.

---

## 7. [cp] NVIDIA device plugin

kubelet은 CPU와 메모리만 안다. **`nvidia.com/gpu` 라는 자원이 존재한다는 것을
알려 주는 것이 device plugin이다.** DaemonSet으로 돌면서 노드의 GPU를 세어
kubelet에 등록한다.

### 7.1 설치

```bash
DP=$(curl -s https://api.github.com/repos/NVIDIA/k8s-device-plugin/releases/latest \
     | grep -o '"tag_name": *"[^"]*"' | cut -d'"' -f4)
echo "device plugin $DP"

kubectl create -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/$DP/deployments/static/nvidia-device-plugin.yml
```

> **gpu-operator를 쓰지 않는 이유.** operator는 드라이버·toolkit·plugin·DCGM을
> 한 번에 깔아 주지만, 각 조각이 무엇을 하는지가 가려진다. 지금은 **배우고
> 설명하는 것이 목적**이므로 손으로 쌓는다. 나중에 노드가 늘면 operator로
> 바꾸는 편이 맞고, 그 판단 근거를 아는 상태가 된다.

### 7.2 확인 — 세 단계로

```bash
# 1) 파드가 떴나
kubectl -n kube-system get pods -l name=nvidia-device-plugin-ds -o wide
kubectl -n kube-system logs -l name=nvidia-device-plugin-ds --tail=30
```

로그에 `Registered device plugin for 'nvidia.com/gpu' with Kubelet` 이 나와야 한다.

```bash
# 2) 노드 자원에 올라왔나  ← 가장 중요한 확인
kubectl describe node k8s-gpu | grep -E 'nvidia.com/gpu'
```

```
  nvidia.com/gpu:     1      ← Capacity
  nvidia.com/gpu:     1      ← Allocatable
```

```bash
# 3) 실제로 파드가 GPU 를 쓰나
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: gpu-check
spec:
  restartPolicy: Never
  nodeSelector:
    ocr-bench/accelerator: h100
  tolerations:
    - key: nvidia.com/gpu
      operator: Exists
      effect: NoSchedule
  containers:
    - name: cuda
      image: nvidia/cuda:12.6.0-base-ubuntu24.04
      command: ["nvidia-smi"]
      resources:
        limits:
          nvidia.com/gpu: 1
EOF

kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/gpu-check --timeout=300s
kubectl logs gpu-check
kubectl delete pod gpu-check
```

**`kubectl logs`에 H100이 나오면 GPU 노드는 완성이다.**

파드가 `Pending`에서 안 움직이면:

```bash
kubectl describe pod gpu-check | tail -20
```

| Events 메시지 | 원인 |
|---|---|
| `Insufficient nvidia.com/gpu` | device plugin이 자원을 등록 못 함 → 7.2의 1) 2) |
| `had untolerated taint {nvidia.com/gpu: present}` | `tolerations` 빠짐 |
| `didn't match Pod's node affinity/selector` | 라벨 오타 (6.1절) |
| `ImagePullBackOff` | DNS 또는 certs.d (1.2 / 3.4절) |

---

## 8. 여기까지 끝나면 나오는 상태

```bash
kubectl get nodes -o wide -L ocr-bench/role,ocr-bench/accelerator
```

| 노드 | vCPU | Memory | 역할 |
|---|---|---|---|
| `k8s-cp` | 4 | 8GB | control plane + 레지스트리 |
| `k8s-worker1` | 8 | 16GB | CPU 측정 대상 |
| `k8s-worker2` | 8 | 16GB | CPU 측정 대상 |
| `k8s-gpu` | ? | ? | **GPU 측정 대상 (H100 x1)** |

**아직 OCR은 GPU에서 돌지 않는다.** 지금 이미지는 `paddlepaddle` (CPU 빌드)라
GPU 노드에 올려도 CPU로 추론한다. 다음 문서(`gpu-workload.md`)에서:

1. `gpu.py` 복원 — `OCR_DEVICE` 로 device를 고르는 코드 (`git show 740d9c8^:workloads/inference/app/gpu.py`)
2. `Dockerfile.gpu` — `paddlepaddle-gpu==3.3.1` (cu126 인덱스)
3. `ocr-workload:0.1.0-gpu` 빌드 → 레지스트리 push
4. `deploy/k8s/overlays/gpu/` — nodeSelector + `nvidia.com/gpu: 1`
5. 같은 영수증으로 CPU vs H100 측정

그 다음이 ROADMAP 2.4절 — **GPU 분할(MIG / time-slicing)로 파드 수를 늘리는
실험.** 0.1절에서 passthrough로 확인됐다면 MIG까지 간다.

---

## 9. 문제가 생겼을 때 보는 순서

| 증상 | 먼저 볼 것 |
|---|---|
| `nvidia-smi` 실패 | `lsmod \| grep nvidia`, `dmesg \| grep -i nvidia`, Secure Boot (0.2) |
| 컨테이너 안에서만 GPU 안 보임 | 3.5절의 `ctr run --gpus 0`. toolkit 설정 문제 |
| GPU 파드만 무작위로 죽음 | nvidia 런타임의 `SystemdCgroup` (3.3절) |
| `nvidia.com/gpu` 가 0 | device plugin 로그 (7.2) |
| 파드 `Pending` | `describe pod` Events — taint / 라벨 / 자원 (7.2 표) |
| `ImagePullBackOff` | `config_path` 가 날아갔는지 (3.4절) |
| 재부팅 후 전부 깨짐 | netplan DNS (1.2), `swapon --show`, `nvidia-smi` 순 |

### 노드 떼어 내기

```bash
# [cp]
kubectl drain k8s-gpu --ignore-daemonsets --delete-emptydir-data
kubectl delete node k8s-gpu

# [gpu 노드]
sudo kubeadm reset -f
sudo rm -rf /etc/cni/net.d /var/lib/cni
```
