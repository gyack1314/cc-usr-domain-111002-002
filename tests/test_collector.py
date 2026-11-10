"""采集器真实浏览器端到端检查。

覆盖：
  1. 全量层级采集（合同→批次→凭证，含共享凭证两条引用）
  2. 空批次合同与失败批次是不同结果
  3. 快速切换父项不串资料（合同层、批次层各一次）
  4. 页面中途关闭的链不被标记收齐，重跑可续采且已确认关系幂等
  5. 同编号不同摘要保留冲突
  6. 同名“编号/下载”控件作用域限定
  7. 只经可见页面操作，不触碰门户内部接口
  8. CLI 端到端可用
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright  # noqa: E402

from collector.collector import run_collection  # noqa: E402
from collector.engine import ChainScraper  # noqa: E402
from collector.store import ResultStore  # noqa: E402
from portal.__main__ import running  # noqa: E402

CHROME = os.environ.get("CHROME_PATH")


def _post_control(base: str, payload: dict) -> None:
    req = urllib.request.Request(
        base + "/__control",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).read()


class CollectorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server_cm = running()
        self.base, self.state = self.server_cm.__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.server_cm.__exit__(None, None, None)

    def _run(self, **kw):
        kw.setdefault("base_url", self.base)
        kw.setdefault("out_dir", self.out)
        kw.setdefault("run_id", "test-run")
        kw.setdefault("executable_path", CHROME)
        return asyncio.run(run_collection(**kw))

    def _load_result(self) -> dict:
        return json.loads(Path(self.out, "result.json").read_text(encoding="utf-8"))

    # 1. 全量层级 + 共享凭证
    def test_full_hierarchy_and_shared_receipt(self):
        summary = self._run(concurrency=2)
        self.assertEqual([], summary.failures)
        self.assertEqual(set(summary.completed), {"CT-17", "CT-29"})
        data = self._load_result()

        contracts = {c["contract_id"]: c for c in data["contracts"]}
        c17 = contracts["CT-17"]
        self.assertEqual("complete", c17["status"])
        self.assertFalse(c17["empty"])
        self.assertEqual(["BT-11", "BT-12"], c17["batch_buttons"])
        self.assertTrue(Path(c17["screenshot"]).is_file())
        # frame 地址证据：detail 与 batch 两级都有
        self.assertIn("/detail.html?id=CT-17", c17["detail_frame_url"])
        b11 = next(b for b in c17["batches"] if b["batch_id"] == "BT-11")
        self.assertEqual("ok", b11["status"])
        self.assertIn("/batch.html?contract=CT-17&id=BT-11", b11["frame_url"])
        self.assertEqual("BT-11", b11["owner_id"])
        self.assertEqual("true", b11["ready"])

        # 共享凭证：两份独立引用关系 + 一份凭证内容
        refs = data["receipt_refs"]
        shared = [r for r in refs if r["receipt_id"] == "RC-shared"]
        self.assertEqual(2, len(shared))
        parents = {(r["contract_id"], r["batch_id"]) for r in shared}
        self.assertEqual({("CT-17", "BT-11"), ("CT-29", "BT-21")}, parents)
        self.assertEqual(3, len(refs))
        self.assertEqual({"RC-shared", "RC-12"}, set(data["receipts"].keys()))
        self.assertEqual("共享凭证", data["receipts"]["RC-shared"]["variants"]["共享凭证"]["digest"])

    # 2a. 空批次合同：正常完成的空，不是失败
    def test_empty_contract_is_complete_empty(self):
        _post_control(self.base, {"phase": "empty-contract"})
        summary = self._run(run_id="empty-run")
        self.assertIn("CT-17", summary.empty_contracts)
        data = self._load_result()
        c17 = next(c for c in data["contracts"] if c["contract_id"] == "CT-17")
        self.assertEqual("complete", c17["status"])
        self.assertTrue(c17["empty"])
        self.assertEqual([], c17["batches"])
        self.assertEqual("true", c17["ready"])

    # 2b. 失败批次：error 落到对应父批次
    def test_failed_batch_is_error_not_empty(self):
        _post_control(self.base, {"phase": "failed-batch"})
        summary = self._run(run_id="fail-run")
        self.assertIn("CT-17/BT-11", summary.error_batches)
        data = self._load_result()
        c17 = next(c for c in data["contracts"] if c["contract_id"] == "CT-17")
        self.assertEqual("complete", c17["status"])  # 链本身处理完
        self.assertFalse(c17["empty"])
        b11 = next(b for b in c17["batches"] if b["batch_id"] == "BT-11")
        self.assertEqual("error", b11["status"])
        self.assertEqual("error", b11["ready"])
        self.assertIn("失败", b11["panel_text"])
        self.assertIsNone(b11["receipt_id"])
        # 失败批次不产生凭证引用
        self.assertEqual([], [r for r in data["receipt_refs"] if r["batch_id"] == "BT-11"])

    # 3a. 快速切换合同：旧 frame 文本未就绪时不读取，归属不串
    def test_rapid_contract_switch_does_not_mix(self):
        async def scenario():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True, executable_path=CHROME,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                page = await browser.new_page()
                await page.goto(self.base)
                await page.locator("#contracts [data-contract]").first.wait_for()
                scraper = ChainScraper(page, self.base, Path(self.out), 15000)
                # 连续快速点击两个合同，再按顺序采集
                await page.locator('[data-contract="CT-17"]').click()
                await page.locator('[data-contract="CT-29"]').click()
                c29 = await scraper._read_contract("CT-29")
                self.assertEqual("CT-29", c29.owner_id)
                self.assertEqual("true", c29.ready)
                self.assertEqual(["BT-21"], c29.batch_buttons)
                # frame 地址必须指向 CT-29，而不是先点开的 CT-17
                self.assertIn("id=CT-29", c29.detail_frame_url)
                # 回到 CT-17 再读一次，确认重新解析后仍是正确父项
                await page.locator('[data-contract="CT-17"]').click()
                c17 = await scraper._read_contract("CT-17")
                self.assertEqual(["BT-11", "BT-12"], c17.batch_buttons)
                self.assertIn("id=CT-17", c17.detail_frame_url)
                await browser.close()
        asyncio.run(scenario())

    # 3b. 快速切换批次：批次 frame 重建后归属不串
    def test_rapid_batch_switch_does_not_mix(self):
        async def scenario():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True, executable_path=CHROME,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                page = await browser.new_page()
                await page.goto(self.base)
                await page.locator("#contracts [data-contract]").first.wait_for()
                scraper = ChainScraper(page, self.base, Path(self.out), 15000)
                contract = await scraper.open_contract("CT-17")
                detail = page.frame_locator("#detail")
                # 快速连点两个批次
                await detail.locator('[data-batch="BT-11"]').click()
                await detail.locator('[data-batch="BT-12"]').click()
                b12 = await scraper._read_one_batch("CT-17", "BT-12")
                self.assertEqual("ok", b12.status)
                self.assertEqual("RC-12", b12.receipt_id)
                self.assertIn("id=BT-12", b12.frame_url)
                # 再点回 BT-11：必须拿到共享凭证，而不是上一帧的 RC-12
                await detail.locator('[data-batch="BT-11"]').click()
                b11 = await scraper._read_one_batch("CT-17", "BT-11")
                self.assertEqual("RC-shared", b11.receipt_id)
                self.assertIn("id=BT-11", b11.frame_url)
                await browser.close()
        asyncio.run(scenario())

    # 4. 页面中途关闭 → interrupted；重跑续采且幂等
    def test_interrupted_chain_then_resume_idempotent(self):
        calls = {"n": 0}

        async def fault(cid, page):
            # 第二次进入链时关闭页面，模拟详情页崩溃/被替换后失联
            calls["n"] += 1
            if calls["n"] >= 2:
                await page.close()

        summary1 = self._run(run_id="resume", concurrency=1, fault=fault)
        self.assertEqual(1, len(summary1.interrupted))
        data1 = self._load_result()
        interrupted = next(c for c in data1["contracts"] if c["status"] == "interrupted")
        # 未收齐的链绝不能伪装成 complete
        self.assertNotEqual("complete", interrupted["status"])

        # 不带故障重跑：已 complete 的链跳过，interrupted 链补采
        summary2 = self._run(run_id="resume", concurrency=1)
        self.assertEqual([], summary2.interrupted)
        self.assertEqual([], summary2.failures)
        data2 = self._load_result()
        self.assertTrue(all(c["status"] == "complete" for c in data2["contracts"]))

        # 再跑一次：完全幂等，引用关系不翻倍
        summary3 = self._run(run_id="resume", concurrency=2)
        data3 = self._load_result()
        self.assertEqual(3, len(data3["receipt_refs"]))
        self.assertEqual(2, len(data3["receipts"]))

    # 5. 同编号不同摘要 → 保留冲突，不覆盖
    def test_same_receipt_id_different_digest_keeps_conflict(self):
        async def scenario():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True, executable_path=CHROME,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                page = await browser.new_page()
                await page.goto(self.base)
                await page.locator("#contracts [data-contract]").first.wait_for()
                scraper = ChainScraper(page, self.base, Path(self.out), 15000)
                store = ResultStore(self.out, "conflict-run")
                contract = await scraper.open_contract("CT-17")
                contract = await scraper.read_batches(contract)
                for b in contract.batches:
                    if b.status == "ok":
                        ref, variant = scraper.build_receipt(b)
                        store.add_receipt(ref, variant)

                # 手工注入同编号、不同摘要的第二版本（模拟页面冲突数据）
                from collector.models import ReceiptRef, ReceiptVariant
                store.add_receipt(
                    ReceiptRef("CT-17", "BT-11", "RC-shared", "冲突摘要B",
                               "http://x/batch.html", "CT-17/BT-11/RC-shared", {}, "t"),
                    ReceiptVariant("冲突摘要B", "编号 BT-11 | 冲突摘要B",
                                   "http://x/batch.html", "CT-17/BT-11/RC-shared", {}, "t"),
                )
                conflicts = store.receipt_conflicts()
                self.assertIn("RC-shared", conflicts)
                self.assertEqual({"共享凭证", "冲突摘要B"}, set(conflicts["RC-shared"]))
                # 两条引用关系都在
                refs = [r for r in store.snapshot()["receipt_refs"] if r["receipt_id"] == "RC-shared"]
                self.assertEqual(2, len(refs))
                await browser.close()
        asyncio.run(scenario())

    # 6. 同名控件作用域：三层“下载”、两层“编号”各归各的父记录
    def test_same_named_controls_are_scope_limited(self):
        self._run(run_id="scope-run")
        data = self._load_result()
        c17 = next(c for c in data["contracts"] if c["contract_id"] == "CT-17")
        # 合同层只统计自己文档里的下载按钮（1 个），不含批次层
        self.assertEqual(1, c17["download_controls"])
        b11 = next(b for b in c17["batches"] if b["batch_id"] == "BT-11")
        ev = b11["evidence"]
        # 批次 frame 文档级 1 个 + Shadow section 内 1 个，被分别计数
        self.assertEqual(1, ev["document_download_controls"])
        self.assertEqual(1, ev["shadow_download_controls"])
        # 编号文本来自当前 section，且 owner 双属性指向当前批次
        self.assertIn("BT-11", b11["number_text"])
        self.assertEqual("BT-11", ev["owner_attribute"])
        self.assertEqual("true", ev["ready_attribute"])

    # 7. 采集端只经可见页面操作：不访问内部接口
    def test_visible_only_no_internal_endpoints(self):
        before = len(self.state["actions"])
        self._run(run_id="visible-run")
        # 内部接口确实存在且可用（证明我们不是因为端点不存在才“没访问”）
        with urllib.request.urlopen(self.base + "/__state", timeout=10) as resp:
            self.assertEqual(200, resp.status)
        # 采集期间页面只通过可见点击触发 open-contract 动作，
        # 没有任何 human-confirm 之类的内部授权/调度动作被采集端借用。
        during = self.state["actions"][before:]
        self.assertTrue(during, "应至少记录到页面点击触发的 open-contract")
        self.assertTrue(
            all(a.get("name") == "open-contract" for a in during),
            f"出现非页面点击的内部动作: {during}",
        )
        # 静态约束再兜一道：采集代码里不得出现内部入口或数据文件字符串。
        forbidden = ("/__control", "/__state", "/api/runtime", "data.json")
        src = Path(__file__).resolve().parents[1] / "collector"
        for py in src.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, f"{py} 不得直接使用内部入口 {token}")

    # 8. CLI 端到端
    def test_cli_end_to_end(self):
        from collector.__main__ import main
        code = main([
            "--base-url", self.base,
            "--out", self.out,
            "--run", "cli-run",
            "--concurrency", "2",
            "--executable-path", CHROME or "",
        ])
        self.assertEqual(0, code)
        data = self._load_result()
        self.assertEqual(2, len(data["contracts"]))


if __name__ == "__main__":
    unittest.main()
