"""采集编排：多详情页并发、单页单链、断点续采与幂等合并。"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

from playwright.async_api import async_playwright

from .engine import ChainScraper
from .errors import ChainInterrupted, CollectorError
from .models import ContractRecord
from .store import ResultStore

DEFAULT_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


@dataclass
class CollectionSummary:
    run_id: str
    requested: list[str]
    completed: list[str] = field(default_factory=list)
    interrupted: list[str] = field(default_factory=list)
    empty_contracts: list[str] = field(default_factory=list)
    error_batches: list[str] = field(default_factory=list)
    receipt_refs: int = 0
    receipts: int = 0
    receipt_conflicts: dict = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


async def run_collection(
    base_url: str,
    out_dir: str,
    run_id: str,
    concurrency: int = 2,
    only: list[str] | None = None,
    timeout_ms: int = 15000,
    executable_path: str | None = None,
    headless: bool = True,
    fault=None,
) -> CollectionSummary:
    """从合同列表出发的完整采集。

    并发可以打开多个详情页，但每个页面只处理一条父级链；
    页面关闭导致未完成的链保留 interrupted，绝不标记为已收齐。
    重跑时已 complete 的链默认跳过（幂等），interrupted 链允许重采。
    """
    store = ResultStore(out_dir, run_id)
    summary = CollectionSummary(run_id=run_id, requested=[])
    launch_args = list(DEFAULT_LAUNCH_ARGS)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            executable_path=executable_path or os.environ.get("CHROME_PATH"),
            args=launch_args,
        )
        try:
            # 一个“门户页”负责读取合同列表（主页面层级）。
            list_context = await browser.new_context(
                viewport={"width": 1280, "height": 900}
            )
            list_page = await list_context.new_page()
            await list_page.goto(base_url, wait_until="domcontentloaded")
            await list_page.locator("#contracts [data-contract]").first.wait_for(
                timeout=timeout_ms
            )
            contracts = await ChainScraper(
                list_page, base_url, store.shot_dir, timeout_ms
            ).list_contracts()
            await list_context.close()

            targets = contracts
            if only:
                wanted = set(only)
                targets = [c for c in contracts if c["contract_id"] in wanted]
            summary.requested = [c["contract_id"] for c in targets]

            semaphore = asyncio.Semaphore(max(1, concurrency))
            names = {c["contract_id"]: c["name"] for c in contracts}

            async def worker(contract: dict[str, str]) -> None:
                async with semaphore:
                    cid = contract["contract_id"]
                    if store.is_contract_complete(cid):
                        # 已确认的合同幂等跳过；其关系保留在 store 中。
                        summary.completed.append(cid)
                        return
                    context = await browser.new_context(
                        viewport={"width": 1280, "height": 900}
                    )
                    page = await context.new_page()
                    try:
                        await page.goto(base_url, wait_until="domcontentloaded")
                        await page.locator("#contracts [data-contract]").first.wait_for(
                            timeout=timeout_ms
                        )
                        scraper = ChainScraper(
                            page,
                            base_url,
                            store.shot_dir,
                            timeout_ms,
                            fault=fault,
                        )
                        record: ContractRecord = await scraper.open_contract(cid)
                        record.name = names.get(cid, contract["name"])
                        try:
                            record = await scraper.read_batches(record)
                        except ChainInterrupted as exc:
                            # 页面关闭：已拿到的批次保留，链标记未收齐。
                            record.status = "interrupted"
                            record.error = str(exc)
                            store.upsert_contract(record)
                            summary.interrupted.append(cid)
                            return

                        if record.empty:
                            # 空批次是正常结果，与加载失败严格区分。
                            summary.empty_contracts.append(cid)
                        for b in record.batches:
                            if b.status == "error":
                                summary.error_batches.append(
                                    f"{cid}/{b.batch_id}"
                                )
                            elif b.status == "ok":
                                ref, variant = scraper.build_receipt(b)
                                # 共享凭证：每条批次引用独立保存，
                                # 凭证内容同编号只存一份（冲突另存 variants）。
                                store.add_receipt(ref, variant)
                        store.upsert_contract(record)
                        summary.completed.append(cid)
                    except ChainInterrupted as exc:
                        summary.interrupted.append(cid)
                        store.upsert_contract(
                            ContractRecord(
                                contract_id=cid,
                                name=names.get(cid, contract["name"]),
                                status="interrupted",
                                detail_frame_url="",
                                frame_locator="page#detail",
                                owner_id=None,
                                ready=None,
                                number_text=None,
                                download_controls=None,
                                batch_buttons=[],
                                empty=False,
                                error=str(exc),
                                seen_at="",
                            )
                        )
                    except CollectorError as exc:
                        summary.failures.append(f"{cid}: {exc}")
                    finally:
                        await context.close()

            await asyncio.gather(*(worker(c) for c in targets))
        finally:
            await browser.close()

    store.flush()
    snap = store.snapshot()
    summary.receipt_refs = len(snap["receipt_refs"])
    summary.receipts = len(snap["receipts"])
    summary.receipt_conflicts = store.receipt_conflicts()
    return summary
