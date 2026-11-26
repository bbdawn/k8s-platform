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

### 2.0 ★ 먼저 이미 깔려 있는지 본다

**2026-11-26 환경에서는 이미 깔려 있었다.** OpenStack 이미지가 GPU 용으로
준비된 것이어서 드라이버도 DCGM 도 들어 있었다. 모르고 `nvidia-open` 을 깔려
하면 이렇게 충돌한다:

```
libnvidia-compute-570 : Conflicts: libnvidia-compute
E: Error, pkgProblemResolver::Resolve generated breaks
```

**이 오류를 보면 purge 부터 하지 말 것.** 멀쩡한 드라이버가 이미 돌고 있을
가능성이 높다. 먼저 확인한다:

```bash
nvidia-smi
dpkg -l | grep -i nvidia | awk '{print $2, $3}'
dkms status
```

| 결과 | 해야 할 일 |
|---|---|
| `nvidia-smi` 가 GPU 를 보여 준다 | **아무것도 안 한다.** 3절로 간다 |
| 패키지는 있는데 `nvidia-smi` 실패 | 커널 모듈만 없다 -> `nvidia-driver-<버전>-open` 설치 |
| 아무것도 없다 | 2.1 ~ 2.3 대로 설치 |

버전이 높다고 좋은 것이 아니다. **이미 깔린 드라이버가 요구치를 넘으면 그대로
쓴다.** 그 커널에서 이미 검증된 조합이고, 재부팅을 한 번 아낀다.

요구치: `paddlepaddle-gpu` (CUDA 12.6 빌드)는 **드라이버 550.54.14 이상**.
2026-11-26 환경은 **570.211.01** 이라 그대로 쓴다.

```
NVIDIA-SMI 570.211.01   Driver Version: 570.211.01   CUDA Version: 12.8
0  NVIDIA H100 NVL   00000000:00:06.0   0MiB / 95830MiB   MIG M.: Disabled
```

- `CUDA Version: 12.8` 은 이 드라이버가 **받아 줄 수 있는 최대** 런타임이다.
  컨테이너가 12.6 을 들고 와도 된다.
- `MIG M.: Disabled` 는 **`N/A` 가 아니다.** 지원하는데 꺼져 있다는 뜻이고,
  MIG 실험이 가능하다는 신호다.

### 2.0.1 드라이버와 커널을 고정한다

`apt upgrade` 가 커널을 올리면 DKMS 모듈과 어긋나 `nvidia-smi` 가 죽는다.

```bash
sudo apt-mark hold nvidia-driver-570 nvidia-dkms-570 \
                   linux-image-generic linux-headers-generic
apt-mark showhold
```

### 2.1 Secure Boot 확인

```bash
mokutil --sb-state
```

`EFI variables are not supported on this system` 이면 **레거시 BIOS 부팅**이라
Secure Boot 자체가 적용되지 않는다(2026-11-26 환경이 이랬다). 걱정할 것 없다.
`SecureBoot enabled` 면 서명되지 않은 모듈이 안 올라가므로 끄거나 MOK 에 키를
등록해야 한다.

### 2.2 없을 때만 - 설치

```bash
sudo apt-get update
sudo apt-get install -y build-essential linux-headers-$(uname -r)
dpkg -l | grep "linux-headers-$(uname -r)"      # <- 반드시 확인
```

커널 헤더가 없으면 DKMS 가 모듈을 빌드하지 못한다. **설치는 성공한 것처럼
끝나고 재부팅 뒤에야 `nvidia-smi` 가 실패한다.** 가장 흔한 실패다.

```bash
cd /tmp
curl -fsSLO https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update
apt-cache policy nvidia-open
sudo apt-get install -y nvidia-open
```

- `nvidia-open` = open GPU kernel module. Hopper(H100) 이후 세대는 NVIDIA 가
  이쪽을 권장한다.
- **`cuda-toolkit` 은 깔지 않는다.** 호스트에는 드라이버만 있으면 된다. CUDA
  런타임(cuDNN, cuBLAS)은 컨테이너 이미지가 자기 것을 들고 온다. 호스트에
  깔면 5GB 넘게 먹고 버전이 어긋나 혼란만 생긴다.

```bash
dkms status        # nvidia/<버전>, <커널>, x86_64: installed
sudo reboot
```

### 2.3 확인

```bash
nvidia-smi
nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv
nvidia-smi -q | grep -i -A2 "MIG Mode"
```

**이 출력을 기록해 둘 것.** 포트폴리오에 "어떤 하드웨어에서 쟀는가" 를 적어야
한다.

재부팅했으므로 **앞서 한 설정이 살아 있는지도 같이 본다**:

```bash
hostname                                       # k8s-gpu
free -h | grep -i Swap                         # 0B
lsmod | grep -E 'overlay|br_netfilter'         # 둘 다
resolvectl status | grep -A2 'DNS Servers'     # 8.8.8.8
grep -c 'disabled_plugins = \[\]' /etc/containerd/config.toml   # 1
```

안 나올 때:

| 증상 | 원인 |
|---|---|
| `NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver` | 모듈 미적재. `lsmod \| grep nvidia`, `dmesg \| grep -i nvidia`, Secure Boot |
| `command not found` | 설치 자체가 안 됨. `dpkg -l \| grep nvidia` |
| `No devices were found` | VM 에 GPU 가 안 붙음. 0.1절의 `lspci` 부터 |

### 2.4 덤 - DCGM 이 이미 있을 수 있다

```bash
dpkg -l | grep datacenter-gpu-manager
```

2026-11-26 환경에는 `datacenter-gpu-manager-4-core 4.6.1` 이 들어 있었다.
ROADMAP 2.5절(GPU 사용률 수집)에서 DCGM Exporter 를 붙일 때 한 단계 줄어든다.

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

### 3.2 containerd에 등록 - containerd 2.x 는 드롭인에 쓴다

```bash
sudo cp /etc/containerd/config.toml /etc/containerd/config.toml.bak
sudo nvidia-ctk runtime configure --runtime=containerd
```

**containerd 2.4.2 에서는 `config.toml` 이 바뀌지 않는다.** 출력을 잘 읽을 것:

```
INFO Using config version 4
INFO Using CRI runtime plugin name "io.containerd.cri.v1.runtime"
INFO Wrote updated config to /etc/containerd/conf.d/99-nvidia.toml
```

`config.toml` 을 grep 하면 `runtimes.nvidia` 가 **안 나온다.** 설정이 드롭인
파일에 들어갔기 때문이다. 이걸 모르면 "설정이 안 됐나" 하고 같은 명령을
반복하게 된다.

### 3.3 ★ 확인할 것 두 가지

#### (1) `imports` 가 있어야 드롭인이 읽힌다

```bash
grep -n 'imports' /etc/containerd/config.toml
# imports = ["/etc/containerd/conf.d/*.toml"]
```

**이 줄이 없으면 `99-nvidia.toml` 은 디스크에 놓인 파일일 뿐이다.** 오류 없이
조용히 무시되고, 파드는 `Insufficient nvidia.com/gpu` 로 Pending 에 머문다.
`containerd config default` 가 만드는 설정에는 들어 있다(2.4.2 기준). 없으면:

```bash
grep -q '^imports' /etc/containerd/config.toml || \
  sudo sed -i "1i imports = ['/etc/containerd/conf.d/*.toml']" /etc/containerd/config.toml
```

#### (2) nvidia 런타임에도 `SystemdCgroup = true`

```bash
grep -c 'SystemdCgroup = true' /etc/containerd/conf.d/99-nvidia.toml
# 2  <- runc 블록과 nvidia 블록 각각
```

`nvidia-ctk` 는 기존 설정을 읽어서 옮겨 주므로, **1.5절에서 `SystemdCgroup =
true` 를 해 뒀으면 자동으로 따라온다.** 안 해 뒀다면 여기서 `false` 가 되고,
**GPU 파드만** 무작위로 죽는다 - CPU 워크로드는 `runc` 를 타니 멀쩡해서 원인을
엉뚱한 데서 찾게 된다.

`default_runtime_name = "runc"` 인 것이 맞다. `--set-as-default` 를 쓰지
않았으므로 GPU 를 요청한 파드만 nvidia 런타임을 탄다.

```bash
sudo systemctl restart containerd
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

> `crictl info` 로도 확인할 수 있지만 **`crictl` 은 `kubeadm` 의 의존성이
> 아니다.** 1.36.7 에서 깔리는 것은 `kubeadm / kubectl / kubelet /
> kubernetes-cni` 넷뿐이다. 필요하면 따로 깐다:
>
> ```bash
> sudo apt-get install -y cri-tools
> sudo tee /etc/crictl.yaml >/dev/null <<'EOF'
> runtime-endpoint: unix:///run/containerd/containerd.sock
> image-endpoint: unix:///run/containerd/containerd.sock
> timeout: 10
> EOF
> ```
>
> 워커에는 kubeconfig 가 없어서 `kubectl` 이 안 된다. 그 노드의 컨테이너를
> 들여다보는 도구는 `crictl` 뿐이므로 깔아 두는 편이 낫다.

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
