#!/usr/bin/env python3
"""OCR workload 부하 생성기.

의존성이 없다 - 표준 라이브러리만 쓴다. 측정하려는 노드에 pip 로 무언가를
깔기 시작하면 "측정 환경을 만들려고 측정 환경을 바꾸는" 일이 된다. cp 에는
python3 가 이미 있으므로 이 파일 하나만 있으면 돈다.

사용:
    python3 bench/load.py --url http://10.240.0.25:30801 --concurrency 1
    python3 bench/load.py --url http://10.240.0.47:30800 --concurrency 4 --requests 200

측정 원칙 몇 가지가 코드에 박혀 있다:

1. 입력은 bench/sample.png 하나로 고정한다. 요청마다 다른 이미지를 쓰면
   글자 수에 따라 지연이 흔들려서 장치 차이와 구분되지 않는다.
2. warmup 요청은 통계에서 뺀다. 첫 요청은 CUDA 컨텍스트 생성, cuDNN
   자동 튜닝, 메모리 풀 확보를 떠안아서 수십 배 느리다. 그 한 번이 평균에
   들어가면 GPU 가 CPU 보다 느리다는 결론이 나온다.
3. 평균이 아니라 분위수를 본다. 평균은 꼬리를 숨긴다. GPU 는 평균이
   낮아도 커널 실행이 몰릴 때 p99 가 튀는 일이 흔하다.
4. 서버가 보고한 elapsed_ms(순수 추론 시간)와 클라이언트가 잰 왕복
   시간을 둘 다 기록한다. 둘의 차이가 HTTP, 업로드, 큐 대기 몫이다.
   GPU 로 추론이 빨라지면 이 몫이 상대적으로 커지고, 어느 순간 추론이
   아니라 그 주변이 병목이 된다 - 그 전환점이 이 벤치마크의 볼거리다.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_IMAGE = Path(__file__).resolve().parent / "sample.png"


def build_multipart(image_bytes: bytes, filename: str) -> tuple[bytes, str]:
    """multipart/form-data 본문을 손으로 만든다.

    requests 를 쓰면 한 줄이지만, 그러면 측정 노드에 pip install 이 필요해진다.
    본문은 매 요청 같은 바이트열이므로 한 번 만들어 재사용한다 - 인코딩 비용이
    측정에 섞이지 않게.
    """
    boundary = uuid.uuid4().hex
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + image_bytes + tail, f"multipart/form-data; boundary={boundary}"


class Worker(threading.Thread):
    def __init__(self, url, body, content_type, counter, lock, total, results, timeout):
        super().__init__(daemon=True)
        self.url = url
        self.body = body
        self.content_type = content_type
        self.counter = counter
        self.lock = lock
        self.total = total
        self.results = results
        self.timeout = timeout

    def run(self) -> None:
        while True:
            with self.lock:
                if self.counter[0] >= self.total:
                    return
                self.counter[0] += 1

            req = urllib.request.Request(
                self.url,
                data=self.body,
                headers={"Content-Type": self.content_type},
                method="POST",
            )
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read())
                rtt_ms = (time.perf_counter() - started) * 1000
                self.results.append(
                    {
                        "ok": True,
                        "rtt_ms": rtt_ms,
                        "server_ms": payload.get("elapsed_ms"),
                        "count": payload.get("count"),
                    }
                )
            except urllib.error.HTTPError as exc:
                self.results.append(
                    {
                        "ok": False,
                        "rtt_ms": (time.perf_counter() - started) * 1000,
                        "error": f"HTTP {exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self.results.append(
                    {
                        "ok": False,
                        "rtt_ms": (time.perf_counter() - started) * 1000,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )


def pct(values: list[float], p: float) -> float:
    """분위수. statistics.quantiles 는 표본이 적으면 까다로워서 직접 센다."""
    if not values:
        return float("nan")
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]


def fetch_info(base: str, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(f"{base}/info", timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="예: http://10.240.0.25:30801")
    ap.add_argument("--image", default=str(DEFAULT_IMAGE))
    ap.add_argument("-c", "--concurrency", type=int, default=1)
    ap.add_argument("-n", "--requests", type=int, default=100, help="통계에 쓰는 요청 수 (warmup 제외)")
    ap.add_argument("--warmup", type=int, default=5, help="통계에서 뺄 선행 요청 수")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--label", default="", help="결과에 남길 이름 (예: gpu-c1)")
    ap.add_argument("--json-out", default="", help="결과를 이 경로에 JSON 으로 저장")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    endpoint = f"{base}/ocr"

    image_bytes = Path(args.image).read_bytes()
    body, content_type = build_multipart(image_bytes, Path(args.image).name)

    info = fetch_info(base, args.timeout)
    device = info.get("device", "?")
    gpu = info.get("gpu") or {}
    print(f"대상    {base}")
    print(f"device  {device}   gpu={gpu.get('gpu_name') or '-'}   cuda={gpu.get('cuda_available')}")
    print(f"입력    {args.image}  ({len(image_bytes):,} bytes)")
    print(f"설정    concurrency={args.concurrency}  requests={args.requests}  warmup={args.warmup}")
    print()

    def run_phase(total: int, show_progress: bool = False) -> list[dict]:
        counter = [0]
        lock = threading.Lock()
        results: list[dict] = []
        threads = [
            Worker(endpoint, body, content_type, counter, lock, total, results, args.timeout)
            for _ in range(min(args.concurrency, total))
        ]
        t0 = time.perf_counter()
        for t in threads:
            t.start()

        # 진행 표시. CPU 로 돌리면 한 건에 수 초가 걸려서 50건이 5분을 넘는데,
        # 아무것도 찍히지 않으면 멈춘 것과 구별할 수 없다. 실제로 그렇게 보였다.
        # stderr 로 내보내므로 stdout 을 파일로 리다이렉트해도 결과만 깔끔하게 남는다.
        #
        # 별도 스레드로 돌린다. 처음에는 메인 스레드에서 1초마다 살아 있는지
        # 확인하는 루프로 만들었는데, 그러면 마지막 확인과 실제 종료 사이의
        # 최대 1초가 소요 시간에 그대로 들어간다. 실제로 네 수준이 모두
        # "7.00s / 14.28 req/s" 로 나와서 포화된 것처럼 보였다. 처리량은 이
        # 벤치마크의 대표 숫자라 그 양자화가 결론을 바꾼다.
        stop_progress = threading.Event()
        progress_thread = None
        if show_progress and sys.stderr.isatty():
            def _progress() -> None:
                while not stop_progress.is_set():
                    done = len(results)
                    el = time.perf_counter() - t0
                    rate = done / el if el > 0 else 0
                    eta = (total - done) / rate if rate > 0 else float("nan")
                    sys.stderr.write(
                        f"\r  진행 {done:>4}/{total}  경과 {el:6.1f}s  "
                        f"속도 {rate:5.2f} req/s  남은 시간 ~{eta:5.0f}s   "
                    )
                    sys.stderr.flush()
                    stop_progress.wait(0.5)
                sys.stderr.write("\r" + " " * 78 + "\r")
                sys.stderr.flush()
            progress_thread = threading.Thread(target=_progress, daemon=True)
            progress_thread.start()

        for t in threads:
            t.join()
        stop_progress.set()
        if progress_thread is not None:
            progress_thread.join(timeout=2)
        for r in results:
            r["_wall"] = time.perf_counter() - t0
        return results

    if args.warmup > 0:
        warm = run_phase(args.warmup)
        wrtt = [r["rtt_ms"] for r in warm if r["ok"]]
        if wrtt:
            print(f"warmup  {len(wrtt)}건  첫 요청 {wrtt[0]:8.1f} ms   마지막 {wrtt[-1]:8.1f} ms")
            if wrtt[0] > 2 * min(wrtt):
                print("        └ 첫 요청이 눈에 띄게 느리다. GPU 라면 CUDA 컨텍스트 생성 몫이다.")
        else:
            print("warmup  전부 실패 - 아래 오류를 먼저 확인할 것")
            for r in warm[:3]:
                print(f"        {r.get('error')}")
            return 1
        print()

    t0 = time.perf_counter()
    results = run_phase(args.requests, show_progress=True)
    wall = time.perf_counter() - t0

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    rtt = [r["rtt_ms"] for r in ok]
    srv = [r["server_ms"] for r in ok if r.get("server_ms") is not None]

    if not ok:
        print("전부 실패했다.")
        for r in bad[:5]:
            print(f"  {r.get('error')}")
        return 1

    counts = {r.get("count") for r in ok}

    print(f"결과    성공 {len(ok)} / 실패 {len(bad)}   소요 {wall:.2f}s")
    print(f"        처리량  {len(ok) / wall:7.2f} req/s")
    print(f"        인식 텍스트 수 {sorted(c for c in counts if c is not None)}"
          + ("   ← 값이 여러 개면 입력이 고정되지 않았다는 뜻" if len(counts) > 1 else ""))
    print()
    print("                    평균      p50      p90      p95      p99      최대")
    print(f"  왕복(client) {statistics.mean(rtt):8.1f} {pct(rtt,50):8.1f} {pct(rtt,90):8.1f} "
          f"{pct(rtt,95):8.1f} {pct(rtt,99):8.1f} {max(rtt):8.1f}  ms")
    if srv:
        print(f"  추론(server) {statistics.mean(srv):8.1f} {pct(srv,50):8.1f} {pct(srv,90):8.1f} "
              f"{pct(srv,95):8.1f} {pct(srv,99):8.1f} {max(srv):8.1f}  ms")
        overhead = statistics.mean(rtt) - statistics.mean(srv)
        share = overhead / statistics.mean(rtt) * 100
        print()
        print(f"  추론 밖 몫   {overhead:8.1f} ms  (왕복의 {share:.1f}%)"
              "  ← HTTP·업로드·큐 대기")
    if bad:
        print()
        print(f"  실패 {len(bad)}건 중 앞 3개:")
        for r in bad[:3]:
            print(f"    {r.get('error')}")

    if args.json_out:
        out = {
            "label": args.label or device,
            "url": base,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "info": info,
            "config": {
                "concurrency": args.concurrency,
                "requests": args.requests,
                "warmup": args.warmup,
                "image": os.path.basename(args.image),
                "image_bytes": len(image_bytes),
            },
            "summary": {
                "ok": len(ok),
                "failed": len(bad),
                "wall_s": round(wall, 3),
                "throughput_rps": round(len(ok) / wall, 3),
                "rtt_ms": {
                    "mean": round(statistics.mean(rtt), 2),
                    "p50": round(pct(rtt, 50), 2),
                    "p90": round(pct(rtt, 90), 2),
                    "p95": round(pct(rtt, 95), 2),
                    "p99": round(pct(rtt, 99), 2),
                    "max": round(max(rtt), 2),
                },
                "server_ms": {
                    "mean": round(statistics.mean(srv), 2),
                    "p50": round(pct(srv, 50), 2),
                    "p90": round(pct(srv, 90), 2),
                    "p95": round(pct(srv, 95), 2),
                    "p99": round(pct(srv, 99), 2),
                    "max": round(max(srv), 2),
                } if srv else None,
            },
            "raw_rtt_ms": [round(v, 2) for v in rtt],
        }
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print()
        print(f"저장    {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
