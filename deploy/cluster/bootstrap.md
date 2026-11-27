# 클러스터 구축 절차

OpenStack VM 4대(일반 3 + GPU 1)에 kubeadm으로 클러스터를 세우는 순서.
2026-11-26에 **다른 OpenStack 환경에서 세 번째로** 만들면서 정리했다. 앞선 두
클러스터에서 실제로 걸렸던 것들을 주석으로 남겨 뒀다.

| VM | Node | IP | 역할 |
|---|---|---|---|
| vm-1 | `k8s-cp` | `10.240.0.18` | control plane + 레지스트리 + 모니터링 |
| vm-2 | `k8s-worker1` | `10.240.0.47` | CPU 측정 대상 |
| vm-3 | `k8s-worker2` | `10.240.0.132` | CPU 측정 대상 |
| gpu | `k8s-gpu` | `10.240.0.25` | **GPU 측정 대상 (H100)** |

GPU 노드는 이 문서의 1절(공통 준비)과 3절(join)만 따르고, 드라이버와
device plugin은 **`gpu-node.md`** 를 따른다. 순서가 중요하다 - 드라이버 설치에
재부팅이 필요하므로 **join 전에** 끝내야 한다.

- Ubuntu 24.04 LTS / amd64
- Kubernetes **v1.36** — 지난번 v1.34는 2026-10에 지원이 끝났다. Calico v3.32가
  v1.36에 맞춰 나와서 짝이 분명하다.
- CNI: Calico, `manifests/calico.yaml` — 기본값이 **IPIP Always** 라 파드 패킷이
  항상 캡슐화된다. OpenStack에서는 이게 중요하다. 아래 0.2절 참고.
- 컨테이너 런타임: containerd 2.x (Docker apt 저장소의 `containerd.io`)

각 절의 머리에 **어느 노드에서 실행하는지** 적어 뒀다. `[전체]`는 4대 모두.

---

## 0. 시작 전에 — OpenStack 쪽 설정

**이걸 빼먹으면 노드는 Ready가 되는데 파드끼리 통신이 안 된다.** 증상이
CNI 문제처럼 보여서 찾는 데 오래 걸린다.

### 0.1 보안 그룹 (Security Group)

```
# control plane 으로 들어오는 것
6443/tcp        Kubernetes API server        ← 워커와 내 맥에서
2379-2380/tcp   etcd                        ← cp 자신만
10257/tcp       kube-controller-manager
10259/tcp       kube-scheduler

# 워커로 들어오는 것
10250/tcp       kubelet API                 ← cp 에서 (logs, exec, probe)
30000-32767/tcp NodePort 서비스             ← 내 맥에서

# 노드끼리 (3대 서로 전부 허용)
4789/udp        VXLAN - Calico 파드 네트워크
179/tcp         BGP - Calico (VXLAN만 쓰면 없어도 되지만 열어 두면 편하다)
```

가장 쉬운 방법은 **VM 3대가 속한 보안 그룹 자신을 remote group으로 하는 allow-all
규칙 하나**를 넣는 것이다. 노드 사이는 전부 열고, 바깥에서는 6443과 NodePort만
연다.

### 0.2 포트 보안 (Port Security) — OpenStack 고유의 함정

OpenStack Neutron은 기본적으로 **VM 포트에 할당된 IP가 아닌 출발지 IP를 가진
패킷을 버린다**(anti-spoofing). 파드 IP(`192.168.x.x`)는 VM의 IP가 아니므로,
Calico가 캡슐화 없이 파드 패킷을 그대로 내보내면 **네트워크가 조용히 먹어 버린다.**

두 가지 해법 중 하나를 쓴다:

1. **Calico를 VXLAN always로 설정한다** (아래 4절에서 그렇게 한다). 파드 패킷이
   노드 IP를 출발지로 하는 UDP 4789 안에 들어가므로 anti-spoofing에 걸리지 않는다.
   **이쪽을 추천한다** — OpenStack 설정을 건드릴 필요가 없다.
2. VM 포트마다 `allowed_address_pairs`에 파드 CIDR을 등록하거나 포트 보안을 끈다.
   OpenStack 관리자 권한이 필요하다.

**설치 방식에 따라 기본값이 다르니 확인할 것:**

| 설치 방식 | 캡슐화 기본값 | anti-spoofing |
|---|---|---|
| `manifests/calico.yaml` | `CALICO_IPV4POOL_IPIP="Always"` | **안 걸린다** (항상 IPIP 캡슐화) |
| tigera-operator + `custom-resources.yaml` | `encapsulation: VXLANCrossSubnet` | **걸린다** (같은 서브넷에서 캡슐화 생략) |

아래 4절은 `calico.yaml`을 쓴다. 첫 번째 클러스터에서 이 OpenStack 환경에
실제로 동작한 방식이고, 기본값이 이미 항상 캡슐화라 따로 손댈 것이 없다.

### 0.3 노드 IP 적어 두기

이후 모든 절에서 쓴다. 아래는 2026-11-25에 만든 VM 기준이고, 다시 만들면
바뀌므로 `ip -4 addr`로 확인할 것.

```bash
# [전체] 각 노드에서 자기 IP 확인
ip -4 addr show scope global | grep inet
```

```
CP_IP=10.240.0.18
W1_IP=10.240.0.47
W2_IP=10.240.0.132
GPU_IP=10.240.0.25
```

노드 서브넷은 `10.240.0.0/24` 다. 2절에서 주는 파드 CIDR `192.168.0.0/16` 과
겹치지 않는다 - 겹치면 라우팅이 조용히 깨진다.

---

## 1. [전체] 공통 사전 작업

4대 모두에서 똑같이 실행한다.

### 1.1 호스트 이름과 /etc/hosts

```bash
# 각 노드에서 자기 이름으로. OpenStack이 이미 넣어 줬으면 확인만 하고 넘어간다.
sudo hostnamectl set-hostname k8s-cp        # 또는 k8s-worker1 / k8s-worker2
hostname
```

```bash
# [전체] 4대 모두 같은 내용. 자기 자신을 포함해서 네 줄 다 넣는다.
sudo tee -a /etc/hosts <<'EOF'
10.240.0.18   k8s-cp
10.240.0.47   k8s-worker1
10.240.0.132  k8s-worker2
10.240.0.25   k8s-gpu
EOF
```

`/etc/hosts`는 그 머신 안에서만 쓰는 이름표라서, 한 대에 넣어도 다른 세 대는
모른다. 그래서 4대 모두에 같은 내용을 넣는다.

**클러스터가 도는 데 반드시 필요한 것은 아니다.** kubelet은 자기 IP를 노드에
등록하고 API server도 그 IP로 접속하므로, 이름 해석 없이도 동작한다. 그래도 넣는
이유는 세 가지다:

- 노드가 **자기 호스트 이름**을 해석하지 못하면 kubeadm과 일부 도구가 경고를
  내거나 실패한다. Ubuntu 클라우드 이미지가 `127.0.1.1 <hostname>`을 넣어 주긴
  하지만, 그건 루프백이라 다른 노드가 쓸 수 없다.
- `ssh k8s-worker1`, `scp ... k8s-cp:` 처럼 노드 사이를 오갈 때 IP를 외우지
  않아도 된다. 이미지 tar를 옮기거나 로그를 가져올 때 계속 쓴다.
- 오류 메시지에 IP 대신 이름이 나와서 읽기 쉽다.

```bash
# 확인 - 4대 모두에서 서로를 이름으로 찾을 수 있어야 한다
getent hosts k8s-cp k8s-worker1 k8s-worker2 k8s-gpu
```

> cloud-init이 `/etc/hosts`를 관리하도록 설정돼 있으면 재부팅 때 덮어쓸 수 있다.
> `grep manage_etc_hosts /etc/cloud/cloud.cfg` 가 `true`면 거기에 넣거나
> `false`로 바꿀 것. Ubuntu 클라우드 이미지 기본값은 `false`라 보통은 문제없다.

### 1.2 swap 끄기

kubelet은 swap이 켜져 있으면 뜨지 않는다(기본 설정 기준).

```bash
sudo swapoff -a
sudo sed -i '/\sswap\s/s/^/#/' /etc/fstab
free -h | grep -i swap        # Swap 이 0B 여야 한다
```

### 1.3 커널 모듈과 sysctl

```bash
sudo tee /etc/modules-load.d/k8s.conf <<'EOF'
overlay
br_netfilter
EOF
sudo modprobe overlay
sudo modprobe br_netfilter

sudo tee /etc/sysctl.d/99-k8s.conf <<'EOF'
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sudo sysctl --system >/dev/null

# 확인 - 셋 다 1 이어야 한다
sysctl net.bridge.bridge-nf-call-iptables net.ipv4.ip_forward
```

### 1.4 containerd 설치

**먼저 이미 깔려 있는지 본다. 환경마다 다르다.**

- 이전 OpenStack 이미지에는 containerd 2.2.1 이 구워져 있었다 -> 이 절을 건너뛴다.
- 2026-11-26 의 새 OpenStack 이미지에는 **없었다** -> 아래대로 설치한다.

어느 쪽이든 **1.5절은 반드시 해야 한다.** 이미 깔려 있더라도 CRI 가 꺼져
있을 수 있다.

```bash
containerd --version
```

없을 때만 설치한다. Ubuntu 24.04 기본 저장소의 containerd는 1.7.x이므로, 2.x가
필요하면 Docker 저장소에서 받는다. (k8s 자체는 1.7.x로도 잘 돈다 — 1.5절의
cgroup driver와 7.2절의 설정 키 이름만 버전에 맞추면 된다.)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
     -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt-get update
sudo apt-get install -y containerd.io
containerd --version          # 2.x 가 나와야 한다
```

### 1.5 containerd 설정 — CRI 활성화 + cgroup driver

**두 가지를 동시에 고친다. 이 절을 건너뛰면 `kubeadm init` 이 preflight 에서
죽는다.**

#### 함정 1 — Docker 저장소 패키지는 CRI 를 꺼 놓고 배포한다

`containerd.io` 패키지가 설치하는 `/etc/containerd/config.toml` 에는 이 줄이
들어 있다:

```toml
disabled_plugins = ["cri"]
```

Docker 데몬용으로 쓰라는 기본값이다. 쿠버네티스는 **CRI 로 containerd 와
대화**하므로, 이 상태면 `kubeadm init` 이 이렇게 죽는다:

```
[ERROR CRI]: could not connect to the container runtime:
  rpc error: code = Unimplemented desc = unknown service runtime.v1.RuntimeService
```

`containerd --version` 은 멀쩡히 나오고 `systemctl is-active` 도 `active` 라서,
"containerd 는 도는데 왜 안 되지" 가 된다. **2026-11-26 새 환경에서 실제로
걸렸다.** 이전 환경은 이미지에 CRI 가 켜진 containerd 가 구워져 있어서
드러나지 않았던 차이다.

#### 함정 2 — cgroup driver

`SystemdCgroup = false` 로 두면 kubelet 과 containerd 가 서로 다른 cgroup
driver 를 써서, 노드가 `NotReady` 에 머물거나 파드가 무작위로 죽는다.

#### 해결 — 기본 설정을 통째로 다시 만든다

`containerd config default` 가 만드는 설정에는 `disabled_plugins` 가 비어
있다. 그래서 한 번에 둘 다 해결된다.

```bash
sudo mkdir -p /etc/containerd
containerd config default | sudo tee /etc/containerd/config.toml >/dev/null

# containerd 2.x 는 config version 3 이고 plugin 이름이 io.containerd.cri.v1.runtime 이다
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml

sudo systemctl restart containerd
sudo systemctl enable containerd
```

```bash
# 확인 - 세 가지를 전부 본다
grep -n 'disabled_plugins\|SystemdCgroup' /etc/containerd/config.toml
#   disabled_plugins = []        <- 비어 있어야 한다
#   SystemdCgroup = true

sudo ctr plugin ls | grep -i 'cri '
#   io.containerd.grpc.v1.cri ... ok

sudo crictl --runtime-endpoint unix:///run/containerd/containerd.sock version
#   RuntimeName: containerd
```

`crictl` 은 `kubeadm` 패키지가 의존성으로 끌고 온다(1.6절). 아직 안 깔았으면
앞의 두 줄만 봐도 된다.

### 1.6 kubeadm / kubelet / kubectl 설치

세 패키지의 역할이 다르다. 셋 다 **4대 모두**에 깐다.

| 패키지 | 하는 일 | 어디서 도나 |
|---|---|---|
| `kubelet` | 노드마다 떠서 파드를 실제로 띄우는 데몬 | **systemd 서비스** |
| `kubeadm` | 클러스터를 세우고(`init`) 붙이는(`join`) 일회성 도구 | 사람이 실행 |
| `kubectl` | API server 에 명령을 보내는 클라이언트 | 사람이 실행 |

워커에는 `kubectl` 이 없어도 되지만, 문제가 생겼을 때 그 노드에서 바로 보려면
있는 편이 낫다. 셋을 같이 깐다.

#### 1) apt 키 디렉터리

```bash
sudo mkdir -p -m 755 /etc/apt/keyrings
```

`-m 755` 는 apt 가 키를 읽을 수 있게 하는 권한이다. 기본 umask 로 만들면
`700` 이 되는 환경이 있어서 명시한다. docker 저장소 설정(1.4절)에서 이미
만들었으면 아무 일도 일어나지 않는다 - `-p` 라서 오류도 안 난다.

#### 2) 저장소 등록

```bash
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key \
  | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
```

```bash
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.36/deb/ /' \
  | sudo tee /etc/apt/sources.list.d/kubernetes.list
```

**★ 주소에 마이너 버전(`v1.36`)이 박혀 있다.** 쿠버네티스 apt 저장소는
마이너 버전마다 주소가 따로 있다. `v1.34` 주소를 그대로 두고 1.36 을 찾으면
**오류가 아니라 "그런 패키지 없음"** 으로 나와서 한참 헤맨다. 나중에 1.37 로
올릴 때도 이 줄을 먼저 바꿔야 한다.

`gpg --dearmor` 는 사람이 읽는 ASCII 키를 apt 가 읽는 바이너리로 바꾸는 것이다.

#### 3) 버전 고르기 - 설치 전에 반드시 본다

```bash
sudo apt-get update
apt-cache madison kubeadm | head -5
```

```
kubeadm | 1.36.7-1.1 | https://pkgs.k8s.io/core:/stable:/v1.36/deb  Packages
kubeadm | 1.36.6-1.1 | ...
```

맨 위가 최신이다. **이 값을 4대에 똑같이 쓴다.**

#### 4) 버전을 박아서 설치

```bash
K8S_VER=1.36.7-1.1        # <- 3) 에서 본 값
sudo apt-get install -y kubelet=$K8S_VER kubeadm=$K8S_VER kubectl=$K8S_VER
```

`apt-get install -y kubelet kubeadm kubectl` 처럼 버전 없이 깔면 **노드마다
설치 시점에 따라 다른 패치 버전이 들어간다.** 며칠 걸쳐 노드를 추가하면
실제로 어긋난다. join 은 그래도 되는 경우가 많아서, 한참 뒤에 이상한 증상으로
나타난다.

#### 5) 버전 고정

```bash
sudo apt-mark hold kubelet kubeadm kubectl
```

`apt upgrade` 가 멋대로 올리지 못하게 막는다. 쿠버네티스 업그레이드는
`kubeadm upgrade` 로 control plane -> 워커 순서를 지켜야 하는 작업이다.
패키지 매니저가 kubelet 만 먼저 올려 버리면 버전 스큐 정책을 넘겨 노드가
떨어져 나간다.

푸는 것은 `sudo apt-mark unhold kubelet kubeadm kubectl`.

#### 6) 확인

```bash
kubeadm version -o short
kubelet --version
kubectl version --client -o yaml | grep gitVersion
```

4대 모두 **`v1.36.7`** 로 같아야 한다.

```bash
apt-mark showhold          # 세 패키지가 나와야 한다
```

> **kubelet 이 계속 재시작해도 정상이다.** 이 시점에는 설정 파일
> (`/var/lib/kubelet/config.yaml`) 이 없어서 `kubeadm init` / `join` 을
> 기다리는 상태다. `systemctl status kubelet` 에 `activating (auto-restart)`
> 가 보여도 넘어간다. init/join 뒤에 `active (running)` 이 된다.

#### GPU 노드도 똑같이 한다

GPU 노드는 **이 절을 빼는 것이 아니라 더하는 것**이다. 1절 전체를 그대로 하고,
드라이버와 toolkit(`gpu-node.md` 2·3절)을 추가로 한 뒤에 join 한다.

### 1.7 [전체] 여기까지 확인

```bash
sudo systemctl is-active containerd    # active
sudo swapon --show                     # 아무것도 안 나와야 함
kubeadm version -o short               # 3대 모두 같은 버전
```

---

## 2. [cp] control plane 초기화

```bash
CP_IP=10.240.0.18

sudo kubeadm init \
  --apiserver-advertise-address=$CP_IP \
  --pod-network-cidr=192.168.0.0/16 \
  --kubernetes-version=v1.36.x
```

- `--pod-network-cidr=192.168.0.0/16` — Calico 기본값. VM 서브넷
  (`10.240.0.0/24`)과 겹치지 않는다. **겹치면 라우팅이 깨지므로 반드시 확인할 것.**
- `--apiserver-advertise-address` — OpenStack VM은 NIC가 여러 개일 수 있어서
  명시하는 편이 안전하다.

성공하면 마지막에 **`kubeadm join ...` 한 줄이 출력된다. 복사해 둘 것.**
잃어버렸으면 3절 끝에서 다시 만들 수 있다.

### 2.1 kubectl 설정

지난번엔 매번 `export KUBECONFIG=/etc/kubernetes/admin.conf`를 쳤다. 이번엔
홈에 깔아 두고 그럴 필요 없게 한다.

```bash
mkdir -p $HOME/.kube
sudo cp /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config

kubectl get nodes
# k8s-cp   NotReady   control-plane   ...
# NotReady 가 정상이다. CNI 가 아직 없다.
```

### 2.2 kubectl 자동완성 (선택)

```bash
echo 'source <(kubectl completion bash)' >> ~/.bashrc
echo 'alias k=kubectl'                   >> ~/.bashrc
echo 'complete -o default -F __start_kubectl k' >> ~/.bashrc
source ~/.bashrc
```

---

## 3. [워커] 클러스터에 join

**worker1, worker2 각각에서** 2절에서 복사한 명령을 그대로 실행한다.

```bash
sudo kubeadm join 10.240.0.18:6443 \
  --token <토큰> \
  --discovery-token-ca-cert-hash sha256:<해시>
```

join 명령을 잃어버렸으면 **cp에서** 새로 만든다:

```bash
# [cp]
kubeadm token create --print-join-command
```

### 3.1 [cp] 확인

```bash
kubectl get nodes
# 3대 모두 보이고, 모두 NotReady 여야 한다 (CNI 가 아직 없으므로 정상)
```

워커가 안 보이면 보안 그룹의 6443을 먼저 의심한다.

---

## 4. [cp] Calico 설치

`manifests/calico.yaml` 한 장으로 설치한다. operator 방식보다 단순하고, **첫 번째
클러스터에서 이 환경에 실제로 동작한 방식이다.**

### 4.1 버전 고르기

Calico와 Kubernetes는 짝이 있다 — v3.32가 k8s v1.36에 맞춰 나왔다.
[릴리스 목록](https://github.com/projectcalico/calico/releases)에서 설치한 k8s
마이너 버전에 맞는 태그를 고른다.

```bash
CALICO=v3.32.0          # ← k8s v1.36 기준. 릴리스 목록에서 최신 패치로.
```

### 4.2 설치

```bash
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/$CALICO/manifests/calico.yaml
```

**CIDR이 `kubeadm init`과 맞는지 확인할 것.** `calico.yaml`의
`CALICO_IPV4POOL_CIDR`은 주석 처리돼 있어서 Calico 기본값 `192.168.0.0/16`을
쓴다. 2절에서 `--pod-network-cidr=192.168.0.0/16`을 줬으므로 일치한다. 한쪽만
바꾸면 파드가 라우팅되지 않는다.

```bash
# 실제로 어떤 캡슐화를 쓰는지 확인 - IPIP=Always 여야 한다
kubectl -n kube-system get ds calico-node \
  -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="CALICO_IPV4POOL_IPIP")]}{.name}={.value}{"\n"}{end}'
```

> **operator 방식을 쓸 거라면** `custom-resources.yaml`을 그대로 적용하지 말 것.
> 그쪽 기본값은 `VXLANCrossSubnet`이라 같은 서브넷에서 캡슐화를 생략하고, 0.2절의
> anti-spoofing에 걸린다. `encapsulation: VXLAN`(always)으로 바꿔야 한다.

### 4.3 확인

```bash
# calico-node 3개(노드당 1개)가 Running 이 될 때까지 몇 분 걸린다
kubectl -n kube-system get pods -l k8s-app=calico-node -w
```

```bash
kubectl get nodes
# 3대 모두 Ready
```

**여기서 파드 네트워크가 실제로 되는지 반드시 확인한다.** 노드가 Ready인 것과
파드끼리 통신이 되는 것은 별개다 — 0.2절의 함정은 노드를 Ready로 두고 통신만
끊으므로, `get nodes`만 보고 넘어가면 한참 뒤에 엉뚱한 데를 파게 된다.

```bash
# 서로 다른 노드에 파드 2개를 띄우고 핑을 쏜다
kubectl run net-a --image=busybox:1.36 --restart=Never -- sleep 3600
kubectl run net-b --image=busybox:1.36 --restart=Never -- sleep 3600
kubectl wait --for=condition=Ready pod/net-a pod/net-b --timeout=120s
kubectl get pod net-a net-b -o wide      # NODE 가 서로 다른지 확인

B_IP=$(kubectl get pod net-b -o jsonpath='{.status.podIP}')
kubectl exec net-a -- ping -c3 $B_IP     # ← 이게 되어야 한다

# DNS 도 확인
kubectl exec net-a -- nslookup kubernetes.default

kubectl delete pod net-a net-b
```

`ping`이 100% 패킷 유실이면 **VXLAN(4789/udp)이 보안 그룹에서 막혔거나**
`encapsulation`이 `VXLAN`이 아닌 것이다.

---

## 5. [cp] kube-reserved — 시스템 몫 떼어 두기

첫 번째 클러스터에서 **파드가 노드의 CPU를 전부 가져가 kubelet·containerd·Calico를
굶겼다.** probe가 타임아웃 나고 kubelet이 멀쩡한 컨테이너를 죽였다:

```
Liveness probe failed: ... context deadline exceeded
Killing: Container ocr failed liveness probe, will be restarted
```

지난번엔 파드의 `limits`를 줄여서 피했는데, 그건 매니페스트마다 조심해야 하는
방식이다. **노드 수준에서 떼어 두는 쪽이 옳다.**

```bash
# [워커 2대에서] - cp 는 어차피 taint 로 워크로드가 안 올라오니 생략해도 된다
sudo tee /etc/systemd/system/kubelet.service.d/20-reserved.conf <<'EOF'
[Service]
Environment="KUBELET_EXTRA_ARGS=--kube-reserved=cpu=500m,memory=512Mi \
--system-reserved=cpu=500m,memory=1Gi \
--eviction-hard=memory.available<500Mi,nodefs.available<10%"
EOF

sudo systemctl daemon-reload
sudo systemctl restart kubelet
```

```bash
# [cp] allocatable 이 줄었는지 확인
kubectl describe node k8s-worker1 | grep -A6 Allocatable
```

**이 숫자가 매니페스트의 `resources`를 정한다.** 16GB 노드에서 위 설정이면
allocatable memory는 대략 13.5~14Gi가 된다.

---

## 6. [cp] 벤치마크용 노드 라벨

매니페스트에 머신 이름을 박지 않기 위해 역할 라벨을 쓴다. 지난번에 이것 때문에
배포할 때마다 `deployment.yaml`을 고쳤다.

```bash
kubectl label node k8s-worker1 ocr-bench/role=target
kubectl label node k8s-worker2 ocr-bench/role=target

kubectl get nodes -l ocr-bench/role=target
```

이번엔 worker **2대 모두**에 붙인다. 파드를 8개까지 퍼뜨리려면 양쪽을 다 써야 한다.

GPU 노드는 라벨을 **다르게** 붙인다. 같은 `role=target` 을 붙이면 CPU 측정
파드가 GPU 노드로 새어 들어가 측정이 오염된다.

```bash
kubectl label node k8s-gpu ocr-bench/role=gpu
kubectl label node k8s-gpu ocr-bench/accelerator=h100

# GPU 노드는 taint 로 격리한다. 명시적으로 toleration 을 단 파드만 들어온다.
kubectl taint node k8s-gpu nvidia.com/gpu=present:NoSchedule

kubectl get nodes -o wide -L ocr-bench/role,ocr-bench/accelerator
```

`accelerator` 를 따로 두는 이유: 나중에 A100 노드를 붙이면 **매니페스트의
`nodeSelector` 한 줄만 바꿔서** CPU / A100 / H100 비교가 된다.

---

## 7. [cp] 로컬 레지스트리

지난번엔 레지스트리가 없어서 이미지를 빌드한 노드 한 대에만 두고 파드를 거기
고정했다. 파드를 양쪽 워커에 퍼뜨리려면 **이미지가 두 노드 모두에 있어야 한다.**

### 7.1 레지스트리 띄우기

cp에 고정하고 hostPath에 저장한다. `registry:2`는 Docker Hub에서 받아 오므로
닭-달걀 문제가 없다.

```bash
kubectl create -f - <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: registry
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: registry
  namespace: registry
spec:
  replicas: 1
  selector:
    matchLabels: {app: registry}
  template:
    metadata:
      labels: {app: registry}
    spec:
      # cp 에 고정한다. hostPath 를 쓰므로 노드가 바뀌면 이미지가 사라진다.
      nodeSelector:
        node-role.kubernetes.io/control-plane: ""
      tolerations:
        - key: node-role.kubernetes.io/control-plane
          operator: Exists
          effect: NoSchedule
      containers:
        - name: registry
          image: registry:2
          ports:
            - containerPort: 5000
          volumeMounts:
            - name: data
              mountPath: /var/lib/registry
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits:   {cpu: "1",  memory: 1Gi}
      volumes:
        - name: data
          hostPath:
            path: /var/lib/registry
            type: DirectoryOrCreate
---
apiVersion: v1
kind: Service
metadata:
  name: registry
  namespace: registry
spec:
  type: NodePort
  selector: {app: registry}
  ports:
    - port: 5000
      targetPort: 5000
      nodePort: 30500
EOF

kubectl -n registry rollout status deploy/registry --timeout=180s
curl -s http://$CP_IP:30500/v2/_catalog     # {"repositories":[]}
```

### 7.2 [전체] containerd에 insecure registry 등록

TLS 없이 평문 HTTP로 쓰므로, 노드마다 알려 줘야 한다. **4대 모두에서** 실행한다.
GPU 노드는 `nvidia-ctk` 가 `config.toml` 을 다시 쓰면서 이 설정을 날릴 수 있으니,
`gpu-node.md` 3.4절에서 한 번 더 확인한다.

**설정 키 이름이 containerd 버전마다 다르다.** 먼저 확인할 것:

```bash
containerd --version
```

```bash
CP_IP=10.240.0.18

# hosts.toml 은 버전과 무관하게 같다
sudo mkdir -p /etc/containerd/certs.d/$CP_IP:30500
sudo tee /etc/containerd/certs.d/$CP_IP:30500/hosts.toml >/dev/null <<EOF
server = "http://$CP_IP:30500"

[host."http://$CP_IP:30500"]
  capabilities = ["pull", "resolve", "push"]
  skip_verify = true
EOF
```

`config.toml` 에 certs.d 를 보라고 알려 준다. **이것을 빼먹으면 조용히
무시되고 `ImagePullBackOff` 가 난다.**

#### ★ 버전마다 다르다 - 반드시 현재 값을 먼저 본다

| containerd | `config_path` 기본값 |
|---|---|
| 1.7.x | 없음 (키 이름도 다르다) |
| 2.2.1 | `'/etc/containerd/certs.d:/etc/docker/certs.d'` - **이미 들어 있다** |
| **2.4.2** | **`''` (빈 문자열) - 넣어야 한다** |

2026-11-27 의 2.4.2 환경에서 실제로 비어 있었다. 2.2.1 때의 기억으로
"이미 있겠지" 하고 넘어가면 나중에 파드가 안 뜬다.

```bash
grep -n -B10 "config_path = ''" /etc/containerd/config.toml \
  | grep -E "^\s*[0-9]+[-:]\s*\[|config_path"
```

```
30-    [plugins.'io.containerd.cri.v1.images'.registry]
31:      config_path = ''                                  <- 이것
250-  [plugins.'io.containerd.transfer.v1.local']
255:    config_path = ''                                   <- 건드리지 말 것
```

**`config_path` 는 여러 군데 있다.** `transfer.v1.local` 쪽을 고치면 아무
일도 일어나지 않는다. `cri.v1.images.registry` 아래 것이어야 한다.

#### ★ GPU 노드는 따옴표가 다르다

`nvidia-ctk runtime configure` 를 돌린 노드는 **`config.toml` 의 TOML 표기가
큰따옴표로 정규화된다.** 드롭인(`conf.d/99-nvidia.toml`)만 쓰는 줄 알았는데
본 파일의 표기도 바꿔 놓는다.

```
# 일반 노드 (containerd config default 가 만든 그대로)
[plugins.'io.containerd.cri.v1.images'.registry]

# GPU 노드 (nvidia-ctk 가 다시 쓴 뒤)
[plugins."io.containerd.cri.v1.images".registry]
```

작은따옴표로만 찾는 스크립트는 GPU 노드에서 `ValueError: substring not found`
로 죽는다. **아래 스크립트는 따옴표를 가리지 않는다.**

#### 고치는 법 - 섹션을 찾아서 값만 바꾼다

`tee -a` 로 섹션을 통째로 덧붙이면 **TOML 중복 키 오류로 containerd 가 아예
뜨지 않는다.** 이미 그 섹션이 존재하기 때문이다. 줄 번호도 노드마다 다를 수
있으므로 섹션을 찾아서 고친다.

```bash
sudo python3 - <<'EOF'
import re
p = "/etc/containerd/config.toml"
s = open(p).read()
m = re.search(r"""\[plugins\.['"]io\.containerd\.cri\.v1\.images['"]\.registry\]""", s)
if not m:
    print("registry 섹션 없음 - config.toml 을 다시 생성해야 한다")
else:
    j = s.index("config_path", m.end())
    k = s.index("\n", j)
    print("변경 전:", s[j:k].strip())
    s = s[:j] + 'config_path = "/etc/containerd/certs.d"' + s[k:]
    open(p, "w").write(s)
    print('변경 후: config_path = "/etc/containerd/certs.d"')
EOF
```

섹션 자체가 없는 1.7.x 라면 그때만 덧붙인다:

```bash
# containerd 1.7.x 전용 - 키 이름이 다르다
grep -q 'config_path' /etc/containerd/config.toml || \
sudo tee -a /etc/containerd/config.toml >/dev/null <<'EOF'

[plugins."io.containerd.grpc.v1.cri".registry]
  config_path = "/etc/containerd/certs.d"
EOF
```

```bash
grep -n -A3 "images'.registry\]" /etc/containerd/config.toml
sudo systemctl restart containerd
```

```bash
# 확인 - 레지스트리에서 받아지는지
sudo ctr -n k8s.io images pull --hosts-dir /etc/containerd/certs.d \
  $CP_IP:30500/nothing:latest 2>&1 | tail -2
# "not found" 면 통신은 된 것이다. "connection refused" 나
# "http: server gave HTTP response to HTTPS client" 면 설정이 안 먹은 것이다.
```

**`--hosts-dir` 가 필요한 이유:** `ctr` 은 CRI 플러그인을 거치지 않아서
`config.toml` 의 `config_path` 를 읽지 않는다. 직접 알려 줘야 한다. kubelet 은
CRI 를 통하므로 `config.toml` 설정만으로 충분하다. 지난 클러스터에서 이것을
모르고 "설정했는데 왜 안 되지" 로 시간을 썼다.

> **GPU 노드에서 containerd 를 재시작할 때.** 그 노드의 컨테이너가 모두 잠깐
> 끊긴다. gpu-operator 파드들이 다시 뜨는 동안 `nvidia.com/gpu` 가 몇 초간
> 0 이 될 수 있다. 직후에 GPU 파드를 띄우면 `Insufficient nvidia.com/gpu` 로
> Pending 이 난다. 재시작 뒤 cp 에서 한 번 확인하고 넘어간다:
>
> ```bash
> kubectl -n gpu-operator get pods -o wide
> kubectl describe node k8s-gpu | grep 'nvidia.com/gpu'
> ```

---

## 8. [worker1] 이미지 빌드와 push

8코어가 되었으니 지난번(2코어)보다 훨씬 빠르다.

### 8.1 buildkit 설치

containerd가 있어도 이미지를 **빌드**할 수는 없다(`ctr`은 빌더가 아니다).
docker나 `nerdctl-full`은 자체 containerd를 끌고 들어와서 클러스터의 런타임과
충돌할 수 있으므로, buildkit만 따로 쓴다.

```bash
BK=$(curl -s https://api.github.com/repos/moby/buildkit/releases/latest \
     | grep -o '"tag_name": *"[^"]*"' | cut -d'"' -f4)
echo "buildkit $BK"

curl -sSL -o /tmp/buildkit.tar.gz \
  https://github.com/moby/buildkit/releases/download/$BK/buildkit-$BK.linux-amd64.tar.gz
sudo tar -C /usr/local -xzf /tmp/buildkit.tar.gz
buildctl --version
```

### 8.2 buildkitd 실행

```bash
sudo mkdir -p /etc/buildkit
sudo tee /etc/buildkit/buildkitd.toml <<'EOF'
[worker.containerd]
  enabled = true
  # kubelet 이 보는 네임스페이스와 같아야 한다. 기본값(buildkit)에 넣으면
  # 빌드는 성공하는데 파드는 ErrImageNeverPull 로 죽는다.
  namespace = "k8s.io"

[worker.oci]
  enabled = false
EOF

sudo systemd-run --unit=buildkitd --description="buildkit daemon" \
  /usr/local/bin/buildkitd --config /etc/buildkit/buildkitd.toml

sudo systemctl status buildkitd --no-pager | head -5
sudo buildctl debug workers -v
```

### 8.3 소스 받고 빌드

```bash
git clone https://github.com/bbdawn/k8s-platform.git
cd k8s-platform
```

```bash
CP_IP=10.240.0.18
IMAGE=$CP_IP:30500/ocr-workload:0.1.0-cpu

sudo buildctl build \
  --frontend dockerfile.v0 \
  --local context=workloads/inference \
  --local dockerfile=workloads/inference \
  --output type=image,name=$IMAGE,push=true,registry.insecure=true
```

- **`| tail -20`을 붙이지 말 것.** 진행 상황이 안 보여서 멈춘 것처럼 보인다.
  지난번에 이걸로 한참 헤맸다.
- 모델을 이미지에 굽는 단계에서 HuggingFace에서 수백 MB를 받으므로 **5~15분**
  걸린다. 조용해 보여도 기다릴 것.

```bash
# push 확인
curl -s http://$CP_IP:30500/v2/_catalog
curl -s http://$CP_IP:30500/v2/ocr-workload/tags/list
```

---

## 9. [cp] 워크로드 배포

```bash
cd ~/k8s-platform    # 없으면 clone
git pull
```

`deploy/k8s/deployment.yaml`의 `image:`를 레지스트리 주소로 바꾸고
`imagePullPolicy`를 `IfNotPresent`로 둔다. (매니페스트 쪽 수정은 5절의
allocatable 값을 보고 함께 확정한다.)

```bash
kubectl apply -k deploy/k8s
kubectl -n ocr-bench rollout status deploy/ocr --timeout=600s
```

CPU에서 모델 로드 + 워밍업에 수십 초가 걸린다. startup probe가
`failureThreshold: 30 × periodSeconds: 10` = 최대 5분을 버틴다.

### 9.1 확인

```bash
kubectl -n ocr-bench get pods -o wide
kubectl -n ocr-bench logs deploy/ocr | tail -20
# "PaddleOCR ready on cpu" 와 "warm-up inference finished" 가 보여야 한다
```

```bash
# 엔진이 실제로 올라왔는지
curl -s http://$W1_IP:30800/info
# {"paddle_version":"3.3.1","device":"cpu","ocr_ready":true,"error":null}
```

맥 브라우저에서 `http://<워커IP>:30800/` 을 열면 업로드 화면이 나온다.

---

## 10. 문제가 생겼을 때 보는 순서

| 증상 | 먼저 볼 것 |
|---|---|
| 노드가 `NotReady` | `sudo journalctl -u kubelet -n 50` — cgroup driver, swap |
| 워커가 `get nodes`에 안 보임 | 보안 그룹 6443, `kubeadm join` 출력 |
| 파드 간 ping 실패 | 4789/udp, `encapsulation: VXLAN` (0.2절) |
| `ErrImageNeverPull` / `ImagePullBackOff` | `certs.d` 설정(7.2), 레지스트리에 태그가 있는지 |
| 파드 `Pending` | `kubectl describe pod` 의 Events — `Insufficient cpu/memory`, `didn't match node selector` |
| `OOMKilled` (exit 137) | `limits.memory`와 `OCR_MAX_IMAGE_SIDE`. README 7장의 계산식 |
| `Error` / exit 143 + probe 실패 | `limits.memory`가 노드 allocatable보다 **큰** 경우. 낮춰서 깔끔한 OOMKilled로 만들 것 |
| 재배포했는데 예전 코드가 돌아감 | 이미지 태그가 같으면 파드가 안 바뀐다. `rollout restart` 또는 태그를 바꿀 것 |

### 처음부터 다시

```bash
# [해당 노드]
sudo kubeadm reset -f
sudo rm -rf /etc/cni/net.d /var/lib/cni $HOME/.kube
sudo iptables -F && sudo iptables -t nat -F && sudo iptables -t mangle -F && sudo iptables -X
sudo systemctl restart containerd kubelet
```
