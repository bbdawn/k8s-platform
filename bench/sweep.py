#!/usr/bin/env python3
"""동시성 스윕 + GPU/CPU 사용률 동시 기록.

load.py 를 동시성 1/2/4/8 로 차례로 돌리면서, 각 구간 동안 GPU 와 파드 CPU 를
함께 샘플링한다. 처리량만 재면 "포화됐다"까지는 알아도 **무엇이 포화시켰는지**
를 모른다. 이 벤치마크에서 답해야 하는 것은 후자다.

측정하는 것:

1. 처리량과 지연 (load.py 가 돌려주는 JSON)

2. GPU - DCGM Exporter 에서 긁는다.
   cp 에서 파드 IP 로 바로 닿으므로 GPU 노드에 따로 접속하지 않아도 된다.
   DCGM_FI_DEV_GPU_UTIL 은 "커널이 돌고 있다"만 뜻하지 포화를 뜻하지 않는다.
   1% 짜리 커널이 쉬지 않고 돌아도 100% 로 나온다. 진짜 포화 신호는
   DCGM_FI_PROF_SM_OCCUPANCY 와 DCGM_FI_PROF_PIPE_TENSOR_ACTIVE 다.

3. 파드 CPU - 컨테이너의 cgroup 을 직접 읽는다.
   metrics-server 가 없어도 되고, 무엇보다 cpu.stat 에는 kubectl top 이
   보여주지 않는 값이 있다:

       nr_throttled    한도에 걸려 강제로 쉬게 된 횟수
       throttled_usec  그렇게 쉰 시간

   이 둘이 올라가면 파드가 cpu limit(1500m)에 **실제로 막히고 있다**는 직접
   증거다. GPU 워크로드가 GPU 가 아니라 그 옆의 CPU 때문에 못 나가는 상황이
   흔한데, 이 숫자가 그것을 구분해 준다.

사용:
    python3 bench/sweep.py --url http://10.240.0.25:30801
    python3 bench/sweep.py --url http://10.240.0.25:30801 --levels 1,2,4,8 -n 100
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# DCGM Exporter 가 내보내는 것 중 이 벤치마크에서 의미 있는 것들.
# 없으면 조용히 건너뛴다 - 설치 버전에 따라 PROF_ 계열이 빠져 있을 수 있다.
# 이름이 exporter 버전마다 다르다. 이 클러스터의 4.x 는 _RATIO/_BYTES 처럼
# 단위 접미사를 붙이고, 예전 이름은 접미사가 없다. 양쪽을 모두 받아 둔다.
# 실제로 확인한 결과 이 설치본에는 SM_ACTIVE 와 SM_OCCUPANCY 가 없다.
# 그래서 포화 판단은 DRAM/TENSOR 사용률과 FB_USED 로 한다 - 활성(GR_ENGINE)은
# 높은데 그 둘이 낮으면, GPU 는 쉬지 않지만 단위 시간당 하는 일이 거의 없다는 뜻이다.
GPU_METRICS = {
    # 활성 - "커널이 돌고 있다". 포화가 아니다.
    "DCGM_FI_DEV_GPU_UTIL": "gpu_util",
    "DCGM_FI_DEV_GPU_UTIL_RATIO": "gpu_util",
    "DCGM_FI_PROF_GR_ENGINE_ACTIVE": "gr_engine",
    "DCGM_FI_PROF_GR_ENGINE_UTIL_RATIO": "gr_engine",
    # 포화 신호
    "DCGM_FI_PROF_SM_ACTIVE": "sm_active",
    "DCGM_FI_PROF_SM_OCCUPANCY": "sm_occupancy",
    "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE": "tensor",
    "DCGM_FI_PROF_TENSOR_UTIL_RATIO": "tensor",
    "DCGM_FI_PROF_DRAM_ACTIVE": "dram",
    "DCGM_FI_PROF_DRAM_UTIL_RATIO": "dram",
    # 그 외
    "DCGM_FI_DEV_FB_USED": "fb_used",
    "DCGM_FI_DEV_FB_USED_BYTES": "fb_used",
    "DCGM_FI_DEV_POWER_USAGE": "power_w",
    "DCGM_FI_DEV_BOARD_POWER_WATTS": "power_w",
    "DCGM_FI_DEV_SM_CLOCK": "sm_clock",
    "DCGM_FI_DEV_SM_CLOCK_HERTZ": "sm_clock",
    "DCGM_FI_DEV_MEMORY_UTIL_RATIO": "mem_util",
}

METRIC_LINE = re.compile(r"^(DCGM_FI_[A-Z0-9_]+)\{[^}]*\}\s+([0-9.eE+-]+)\s*$")


def sh(cmd: list[str], timeout: float = 20.0) -> str:
    """kubectl 한 번. 실패하면 빈 문자열."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


# --- GPU 쪽 ------------------------------------------------------------------


def find_dcgm_endpoint(namespace: str) -> str | None:
    """DCGM Exporter 주소를 찾는다. 파드 IP 를 먼저 쓴다.

    ClusterIP 도 cp 에서 닿지만 kube-proxy 를 한 단계 더 거친다. 파드 IP 는
    Calico 가 직접 라우팅하므로 샘플링 주기가 1초인 여기서는 그쪽이 낫다.
    """
    ip = sh([
        "kubectl", "-n", namespace, "get", "pod",
        "-l", "app=nvidia-dcgm-exporter",
        "-o", "jsonpath={.items[0].status.podIP}",
    ])
    if ip:
        return f"http://{ip}:9400/metrics"
    ip = sh([
        "kubectl", "-n", namespace, "get", "svc", "nvidia-dcgm-exporter",
        "-o", "jsonpath={.spec.clusterIP}",
    ])
    return f"http://{ip}:9400/metrics" if ip else None


def scrape_gpu(url: str, timeout: float = 3.0) -> dict[str, float]:
    """한 번 긁어서 관심 있는 값만 뽑는다. GPU 가 1장이므로 첫 샘플만 쓴다."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, float] = {}
    for line in text.splitlines():
        m = METRIC_LINE.match(line)
        if not m:
            continue
        key = GPU_METRICS.get(m.group(1))
        if key and key not in out:
            try:
                out[key] = float(m.group(2))
            except ValueError:
                pass
    return out


class GpuSampler(threading.Thread):
    """부하가 도는 동안 1초마다 GPU 를 샘플링한다."""

    def __init__(self, url: str, interval: float = 1.0):
        super().__init__(daemon=True)
        self.url = url
        self.interval = interval
        self.samples: list[dict[str, float]] = []
        # 이름을 _stop 으로 두면 안 된다. threading.Thread 에 같은 이름의 내부
        # 메서드가 있어서 덮어쓰게 되고, join() 이 그것을 호출하는 순간
        # TypeError: 'Event' object is not callable 로 죽는다.
        self._done = threading.Event()

    def run(self) -> None:
        while not self._done.is_set():
            s = scrape_gpu(self.url)
            if s:
                s["_t"] = time.time()
                self.samples.append(s)
            self._done.wait(self.interval)

    def stop(self) -> None:
        self._done.set()

    def summary(self) -> dict:
        if not self.samples:
            return {"samples": 0}
        keys = [k for k in GPU_METRICS.values() if any(k in s for s in self.samples)]
        out: dict = {"samples": len(self.samples)}
        for k in keys:
            vals = [s[k] for s in self.samples if k in s]
            if vals:
                out[k] = {
                    "mean": round(sum(vals) / len(vals), 2),
                    "max": round(max(vals), 2),
                }
        return out


# --- 파드 CPU 쪽 --------------------------------------------------------------


def pod_name(namespace: str, selector: str) -> str | None:
    n = sh([
        "kubectl", "-n", namespace, "get", "pod", "-l", selector,
        "--field-selector=status.phase=Running",
        "-o", "jsonpath={.items[0].metadata.name}",
    ])
    return n or None


def read_cpu_stat(namespace: str, pod: str) -> dict[str, int] | None:
    """컨테이너 cgroup 의 cpu.stat 을 읽는다 (cgroup v2).

    usage_usec     누적 CPU 사용 시간
    nr_throttled   cpu limit 에 걸려 강제로 쉰 횟수
    throttled_usec 그렇게 쉰 누적 시간
    """
    raw = sh([
        "kubectl", "-n", namespace, "exec", pod, "--",
        "cat", "/sys/fs/cgroup/cpu.stat",
    ], timeout=30.0)
    if not raw:
        return None
    out: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out or None


def cpu_delta(before: dict | None, after: dict | None, wall_s: float) -> dict | None:
    if not before or not after or wall_s <= 0:
        return None
    d_usage = after.get("usage_usec", 0) - before.get("usage_usec", 0)
    d_thr_us = after.get("throttled_usec", 0) - before.get("throttled_usec", 0)
    d_thr_n = after.get("nr_throttled", 0) - before.get("nr_throttled", 0)
    d_periods = after.get("nr_periods", 0) - before.get("nr_periods", 0)
    return {
        "cores_used": round(d_usage / 1e6 / wall_s, 3),
        "throttled_count": d_thr_n,
        "throttled_s": round(d_thr_us / 1e6, 3),
        # 스케줄링 주기 중 몇 %에서 한도에 걸렸는가. 이것이 0 보다 크면
        # 파드는 cpu limit 에 실제로 막히고 있다.
        "throttled_period_pct": round(100 * d_thr_n / d_periods, 1) if d_periods else None,
    }


# --- 한 수준 돌리기 -----------------------------------------------------------


def run_level(args, level: int, gpu_url: str | None, ns: str, selector: str) -> dict:
    out_json = Path(args.out_dir) / f"{args.label}-c{level}.json"
    cmd = [
        sys.executable, str(HERE / "load.py"),
        "--url", args.url,
        "-c", str(level),
        "-n", str(args.requests),
        "--warmup", str(args.warmup),
        "--label", f"{args.label}-c{level}",
        "--json-out", str(out_json),
    ]

    pod = pod_name(ns, selector)
    sampler = GpuSampler(gpu_url) if gpu_url else None

    print(f"\n{'='*66}")
    print(f"  동시성 {level}   요청 {args.requests}건   파드 {pod or '?'}")
    print(f"{'='*66}")

    # cpu.stat 은 부하 직전/직후에만 읽는다. kubectl exec 자체가 CPU 를 쓰므로
    # 측정 중에 반복하면 재려는 대상에 영향을 준다.
    cpu_before = read_cpu_stat(ns, pod) if pod else None
    if sampler:
        sampler.start()
    t0 = time.perf_counter()
    rc = subprocess.run(cmd).returncode
    wall = time.perf_counter() - t0
    if sampler:
        sampler.stop()
        sampler.join(timeout=5)
    cpu_after = read_cpu_stat(ns, pod) if pod else None

    load_result = {}
    if out_json.exists():
        try:
            load_result = json.loads(out_json.read_text())
        except Exception:  # noqa: BLE001
            pass

    rec = {
        "concurrency": level,
        "returncode": rc,
        "wall_s": round(wall, 2),
        "load": load_result.get("summary"),
        "gpu": sampler.summary() if sampler else {"samples": 0},
        "cpu": cpu_delta(cpu_before, cpu_after, wall),
    }

    g = rec["gpu"]
    c = rec["cpu"]
    if g.get("samples"):
        bits = []
        for k, lbl in (("gpu_util", "GPU-Util"), ("gr_engine", "GR-Engine"),
                       ("sm_occupancy", "SM-Occ"), ("dram", "DRAM"), ("tensor", "Tensor")):
            if k in g:
                bits.append(f"{lbl} 평균 {g[k]['mean']:.1f} / 최대 {g[k]['max']:.1f}")
        if bits:
            print("  GPU     " + "   ".join(bits))
    if c:
        thr = f"   throttle {c['throttled_count']}회 ({c['throttled_s']:.1f}s)" if c["throttled_count"] else "   throttle 없음"
        print(f"  파드CPU  {c['cores_used']:.2f} 코어 사용{thr}")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", required=True)
    ap.add_argument("--levels", default="1,2,4,8")
    ap.add_argument("-n", "--requests", type=int, default=100)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--label", default="gpu")
    ap.add_argument("--out-dir", default=str(HERE / "results"))
    ap.add_argument("--namespace", default="ocr-bench")
    ap.add_argument("--selector", default="ocr-bench/variant=gpu")
    ap.add_argument("--gpu-namespace", default="gpu-operator")
    ap.add_argument("--no-gpu-metrics", action="store_true")
    args = ap.parse_args()

    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    gpu_url = None
    if not args.no_gpu_metrics:
        gpu_url = find_dcgm_endpoint(args.gpu_namespace)
        if gpu_url:
            probe = scrape_gpu(gpu_url)
            if probe:
                have = sorted(probe)
                print(f"DCGM    {gpu_url}")
                print(f"        수집 가능: {', '.join(have)}")
                if "sm_occupancy" not in probe and "sm_active" not in probe:
                    print("        주의: SM_OCCUPANCY/SM_ACTIVE 가 없다. 포화 판단은 "
                          "DRAM/Tensor 사용률과 FB_USED 로 한다.")
            else:
                print(f"DCGM    {gpu_url} 에서 읽지 못했다. GPU 수집은 건너뛴다.")
                gpu_url = None
        else:
            print("DCGM    exporter 를 찾지 못했다. GPU 수집은 건너뛴다.")

    records = [run_level(args, lv, gpu_url, args.namespace, args.selector) for lv in levels]

    out = {
        "url": args.url,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "requests_per_level": args.requests,
        "levels": records,
    }
    path = Path(args.out_dir) / f"{args.label}-sweep.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))

    # 요약표
    print(f"\n{'='*66}")
    print("  요약")
    print(f"{'='*66}")
    header = f"{'동시성':>5} {'처리량':>10} {'추론p50':>9} {'왕복p50':>9} {'왕복p95':>9} {'밖몫%':>7} {'GR-Eng':>7} {'DRAM':>7} {'Tensor':>7} {'CPU코어':>8} {'throttle':>9}"
    print(header)
    base = None
    for r in records:
        s = r.get("load") or {}
        g = r.get("gpu") or {}
        c = r.get("cpu") or {}
        rps = s.get("throughput_rps")
        if base is None and rps:
            base = rps
        srv = (s.get("server_ms") or {}).get("p50")
        rtt = (s.get("rtt_ms") or {}).get("p50")
        r95 = (s.get("rtt_ms") or {}).get("p95")
        over = None
        if s.get("rtt_ms") and s.get("server_ms"):
            over = 100 * (s["rtt_ms"]["mean"] - s["server_ms"]["mean"]) / s["rtt_ms"]["mean"]
        def f(v, w, d=1):
            return f"{v:>{w}.{d}f}" if isinstance(v, (int, float)) else f"{'-':>{w}}"
        print(
            f"{r['concurrency']:>5} {f(rps,10,2)} {f(srv,9)} {f(rtt,9)} {f(r95,9)} "
            f"{f(over,7)} {f(g.get('gr_engine',{}).get('mean'),7,3)} "
            f"{f(g.get('dram',{}).get('mean'),7,3)} {f(g.get('tensor',{}).get('mean'),7,3)} "
            f"{f(c.get('cores_used'),8,2)} {str(c.get('throttled_count','-')):>9}"
        )
    if base:
        print()
        print("  선형이면 동시성 2배마다 처리량도 2배다. 꺾이는 지점이 포화점이고,")
        print("  그때 DRAM/Tensor 와 CPU코어/throttle 중 어느 쪽이 한계에 붙어")
        print("  있는지가 '무엇이 막았는가'의 답이다.")
    print(f"\n저장    {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
