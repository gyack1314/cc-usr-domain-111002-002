"""命令行入口：从合同列表出发的层级采集。

示例：
  python -m collector \
      --base-url http://127.0.0.1:8090 \
      --out ./collected --run run-001 --concurrency 2
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .collector import run_collection


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m collector",
        description="从合同列表采集合同、履约批次与凭证的层级归属关系",
    )
    parser.add_argument("--base-url", required=True, help="门户地址，例如 http://127.0.0.1:8090")
    parser.add_argument("--out", required=True, help="结果目录（result.json 与截图）")
    parser.add_argument("--run", required=True, help="本次采集的运行编号")
    parser.add_argument("--concurrency", type=int, default=2, help="并发详情页数量，每页只处理一条链")
    parser.add_argument("--only", nargs="*", help="只采集指定合同编号，缺省采集列表全部合同")
    parser.add_argument("--timeout-ms", type=int, default=15000, help="单步等待上限，不使用固定 sleep")
    parser.add_argument("--executable-path", help="Chromium 可执行文件路径，缺省读 CHROME_PATH")
    parser.add_argument("--headed", action="store_true", help="有头模式（默认无头）")
    return parser.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    summary = await run_collection(
        base_url=args.base_url.rstrip("/"),
        out_dir=args.out,
        run_id=args.run,
        concurrency=args.concurrency,
        only=args.only,
        timeout_ms=args.timeout_ms,
        executable_path=args.executable_path,
        headless=not args.headed,
    )
    print(f"运行编号: {summary.run_id}")
    print(f"请求合同: {', '.join(summary.requested) or '（无）'}")
    print(f"已收齐: {', '.join(summary.completed) or '（无）'}")
    print(f"未收齐(可重采): {', '.join(summary.interrupted) or '（无）'}")
    print(f"空批次合同: {', '.join(summary.empty_contracts) or '（无）'}")
    print(f"失败批次: {', '.join(summary.error_batches) or '（无）'}")
    print(f"凭证引用关系: {summary.receipt_refs} 条；去重后凭证内容: {summary.receipts} 份")
    if summary.receipt_conflicts:
        print(f"凭证摘要冲突: {summary.receipt_conflicts}")
    if summary.failures:
        print("采集异常:")
        for item in summary.failures:
            print(f"  - {item}")
        return 2
    if summary.interrupted:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
