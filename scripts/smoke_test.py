"""容器/部署冒烟测试：API 健康检查 + 依赖检查 + 一份完整日报。

用法:
    python scripts/smoke_test.py [--base http://127.0.0.1:8000] [--full]
    docker compose exec agent-api python scripts/smoke_test.py --base http://agent-api:8000

--full 会执行一次 /report（真实 LLM + 真实数据，耗时约 1-3 分钟）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

DEFAULT_QUERY = "给我生成一份关于 Pilbara 锂矿的今日简报"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--full", action="store_true", help="执行完整 /report")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    ok = True

    # 1. /health
    try:
        r = httpx.get(f"{base}/health", timeout=10)
        print(f"[health] {r.status_code} {r.text}")
        ok = ok and r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        print(f"[health] FAIL {exc}")
        return 1

    # 2. /health/dependencies
    try:
        r = httpx.get(f"{base}/health/dependencies", timeout=60)
        payload = r.json()
        print(f"[dependencies] {json.dumps(payload, ensure_ascii=False)[:800]}")
        ok = ok and payload.get("status") in ("ok", "degraded")
    except Exception as exc:  # noqa: BLE001
        print(f"[dependencies] FAIL {exc}")
        return 1

    # 3. /report（可选，完整链路）
    if args.full:
        t0 = time.time()
        try:
            r = httpx.post(
                f"{base}/report",
                json={"query": DEFAULT_QUERY, "report_date": "2026-10-08"},
                timeout=600,
            )
            payload = r.json()
            elapsed = time.time() - t0
            print(
                f"[report] status={payload.get('status')} "
                f"elapsed_ms={payload.get('elapsed_ms')} wall={elapsed:.0f}s"
            )
            markdown = payload.get("report_markdown", "")
            print(
                f"[report] markdown {len(markdown)} chars, sources={len(payload.get('sources', []))}"
            )
            print(
                f"[report] missing_data={len(payload.get('missing_data', []))} warnings={len(payload.get('warnings', []))}"
            )
            ok = ok and payload.get("status") in ("success", "partial")
            if payload.get("status") == "error":
                print(f"[report] error={payload.get('error')}")
        except Exception as exc:  # noqa: BLE001
            print(f"[report] FAIL {exc}")
            ok = False

    print("SMOKE " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
