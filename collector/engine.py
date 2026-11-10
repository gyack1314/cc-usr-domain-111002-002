"""真实浏览器采集引擎。

层级与作用域规则：
  主页面（合同列表）
    └ iframe#detail（合同详情 frame，切换合同时整个 iframe 被替换重建）
        └ iframe#batch（批次 frame，切换批次时整个 iframe 被替换重建）
            └ receipt-panel 的开放 Shadow DOM（凭证面板，可被整体替换）

所有等待都要求 data-owner-id 与 data-ready 同时匹配当前父项：
旧文本仍可见但新请求未完成（ready!=true）时一律不读取。
frame 重建或面板替换后，frame_locator/locator 全部重新解析，
不缓存任何已经失效的元素句柄（Playwright locator 本身是即时解析的）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import (
    Error as PWError,
    Page,
    TimeoutError as PWTimeoutError,
)

from .errors import ChainInterrupted, StaleFrameError
from .models import BatchRecord, ContractRecord, ReceiptRef, ReceiptVariant

DETAIL_FRAME = "#detail"
BATCH_FRAME = "#batch"
PANEL = "receipt-panel"

# 页面/上下文/浏览器被关闭时，Playwright 抛出的异常名集合。
_CLOSED_ERROR_NAMES = {
    "TargetClosedError",
    "BrowserClosedError",
    "BrowserContextClosedError",
    "PageClosedError",
}


def _is_closed_error(exc: BaseException) -> bool:
    return type(exc).__name__ in _CLOSED_ERROR_NAMES or (
        isinstance(exc, PWError) and "closed" in str(exc).lower()
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChainScraper:
    """一个浏览器页面只处理一条父级链（合同→批次→凭证）。"""

    def __init__(
        self,
        page: Page,
        base_url: str,
        shot_dir: Path,
        timeout_ms: int = 15000,
        fault=None,
    ):
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.shot_dir = Path(shot_dir)
        self.timeout_ms = timeout_ms
        # 测试用故障注入：在合同详情打开后回调（例如关闭页面）。
        self.fault = fault

    # ---------- 通用工具 ----------
    async def _guard(self, action, label: str):
        """把等待/读取动作包一层：失效即抛 StaleFrameError，页面关闭即中断。"""
        try:
            return await action()
        except PWTimeoutError as exc:
            raise StaleFrameError(f"{label} 等待超时，frame 可能已被替换") from exc
        except Exception as exc:  # noqa: BLE001 - 浏览器侧异常归类
            if _is_closed_error(exc):
                raise ChainInterrupted(f"页面在链未完成时关闭：{label}") from exc
            raise

    async def list_contracts(self) -> list[dict[str, str]]:
        """只从主页面可见的合同按钮读取列表，限定主页面层级。"""
        buttons = self.page.locator("#contracts [data-contract]")
        count = await buttons.count()
        result = []
        for i in range(count):
            item = buttons.nth(i)
            result.append({
                "contract_id": await item.get_attribute("data-contract"),
                "name": (await item.inner_text()).strip(),
            })
        return result

    async def _shot(self, name: str) -> str | None:
        try:
            path = self.shot_dir / name
            await self.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:  # noqa: BLE001 - 证据截图失败不应抹掉已采集数据
            return None

    # ---------- 合同层 ----------
    async def open_contract(self, contract_id: str) -> ContractRecord:
        # 选择器限定在主页面 #contracts 容器内的当前合同按钮，
        # 不使用“页面第一个同名按钮”。
        button = self.page.locator(
            f'#contracts [data-contract="{contract_id}"]'
        )
        await self._guard(
            lambda: button.wait_for(state="visible", timeout=self.timeout_ms),
            f"合同按钮 {contract_id}",
        )
        await self._guard(button.click, f"点击合同 {contract_id}")

        if self.fault is not None:
            outcome = self.fault(contract_id, self.page)
            if asyncio.iscoroutine(outcome):
                await outcome

        record = await self._read_contract(contract_id)
        return record

    async def _read_contract(self, contract_id: str) -> ContractRecord:
        # iframe 可能刚被替换：循环内每次都重新取 frame_locator 与 src。
        last_err: Exception | None = None
        for _attempt in range(5):
            detail = self.page.frame_locator(DETAIL_FRAME)
            body = detail.locator(
                f'body[data-owner-id="{contract_id}"][data-ready="true"]'
            )
            try:
                await self._guard(
                    lambda: body.wait_for(state="attached", timeout=self.timeout_ms),
                    f"合同详情就绪 {contract_id}",
                )
                return await self._guard(
                    lambda: self._extract_contract(detail, contract_id),
                    f"读取合同详情 {contract_id}",
                )
            except ChainInterrupted:
                # 页面关闭不可重试，直接上抛，由编排层把链标记为未收齐。
                raise
            except StaleFrameError as exc:
                # 旧合同文本（CT-STALE 或上一份合同）仍可见但未就绪：丢弃重解析。
                last_err = exc
                continue
        raise StaleFrameError(f"合同 {contract_id} 详情 frame 重新解析失败") from last_err

    async def _extract_contract(self, detail, contract_id: str) -> ContractRecord:
        frame_url = await self.page.locator(DETAIL_FRAME).get_attribute("src")
        owner = await detail.locator("#owner").inner_text()
        owner_id = await detail.locator("body").get_attribute("data-owner-id")
        ready = await detail.locator("body").get_attribute("data-ready")
        # 双匹配复核：owner-id 与 ready 必须同时指向当前父项。
        if owner_id != contract_id or ready != "true" or owner.strip() != contract_id:
            raise StaleFrameError(
                f"详情 frame 归属不匹配：owner={owner_id} ready={ready} 期望={contract_id}"
            )
        batch_buttons = detail.locator("#batches [data-batch]")
        batch_ids = [
            (await batch_buttons.nth(i).get_attribute("data-batch"))
            for i in range(await batch_buttons.count())
        ]
        # 同名“下载”控件：限定合同 frame 的 body 直接子按钮，
        # 不碰批次 frame 与 Shadow DOM 里的同名按钮。
        download_controls = await detail.locator("body > button").count()
        return ContractRecord(
            contract_id=contract_id,
            name="",
            status="interrupted",  # 批次处理完后再置 complete
            detail_frame_url=frame_url or "",
            frame_locator=f"page{DETAIL_FRAME}",
            owner_id=owner_id,
            ready=ready,
            number_text=owner.strip(),
            download_controls=download_controls,
            batch_buttons=batch_ids,
            empty=not batch_ids,
            batches=[],
            evidence={
                "owner_attribute": owner_id,
                "ready_attribute": ready,
                "frame_src": frame_url,
                "scoped_selector": f'{DETAIL_FRAME} body[data-owner-id="{contract_id}"]',
            },
            seen_at=_now(),
        )

    # ---------- 批次 + 凭证层 ----------
    async def read_batches(
        self, contract: ContractRecord
    ) -> ContractRecord:
        detail = self.page.frame_locator(DETAIL_FRAME)
        for batch_id in contract.batch_buttons:
            contract.batches.append(
                await self._read_one_batch(contract.contract_id, batch_id)
            )
        contract.status = "complete"
        contract.screenshot = await self._shot(
            f"contract-{contract.contract_id}.png"
        )
        return contract

    async def _read_one_batch(self, contract_id: str, batch_id: str) -> BatchRecord:
        # 每次切换批次，#batch iframe 都会重建，locator 在循环内重新解析。
        detail = self.page.frame_locator(DETAIL_FRAME)
        # 批次按钮限定在当前合同 frame 的 #batches 容器内。
        batch_button = detail.locator(f'#batches [data-batch="{batch_id}"]')
        await self._guard(
            lambda: batch_button.wait_for(state="visible", timeout=self.timeout_ms),
            f"批次按钮 {batch_id}",
        )
        await self._guard(batch_button.click, f"点击批次 {batch_id}")

        last_err: Exception | None = None
        for _attempt in range(5):
            batch = detail.frame_locator(BATCH_FRAME)
            try:
                base = await self._guard(
                    lambda: self._extract_batch_terminal(detail, batch, batch_id, contract_id),
                    f"读取批次终态 {batch_id}",
                )
                if base.status == "error":
                    return base
                return await self._guard(
                    lambda: self._extract_receipt(batch, batch_id, base),
                    f"读取凭证面板 {batch_id}",
                )
            except ChainInterrupted:
                # 页面关闭不可重试，链交由编排层标记未收齐。
                raise
            except StaleFrameError as exc:
                # frame/面板可能被替换：丢弃 locator，下一轮重新解析整个 frame。
                last_err = exc
                continue
        raise StaleFrameError(f"批次 {batch_id} 凭证面板重新解析失败") from last_err

    async def _extract_batch_terminal(
        self, detail, batch, batch_id: str, contract_id: str
    ) -> BatchRecord:
        # #batch 元素位于详情 frame 的文档内，必须经由 FrameLocator 查找，
        # 不能在主文档里链式定位（普通 locator 不穿透 frame 边界）。
        frame_url = await detail.locator(BATCH_FRAME).get_attribute("src")
        ok_body = batch.locator(
            f'body[data-owner-id="{batch_id}"][data-ready="true"]'
        )
        err_body = batch.locator(
            f'body[data-owner-id="{batch_id}"][data-ready="error"]'
        )
        # 成功与失败是两种终态；未完成（无 data-ready 或 false）会超时重试。
        try:
            await ok_body.or_(err_body).wait_for(
                state="attached", timeout=self.timeout_ms
            )
        except PWTimeoutError as exc:
            raise StaleFrameError(f"批次 {batch_id} 终态等待超时") from exc

        owner_id = await batch.locator("body").get_attribute("data-owner-id")
        ready = await batch.locator("body").get_attribute("data-ready")
        if owner_id != batch_id:
            raise StaleFrameError(
                f"批次 frame 归属不匹配：owner={owner_id} 期望={batch_id}"
            )

        base = BatchRecord(
            batch_id=batch_id,
            contract_id=contract_id,
            status="",
            frame_url=frame_url or "",
            frame_locator=f"page{DETAIL_FRAME} {BATCH_FRAME}",
            owner_id=owner_id,
            ready=ready,
            evidence={
                "owner_attribute": owner_id,
                "ready_attribute": ready,
                "frame_src": frame_url,
                "scoped_selector": f'{BATCH_FRAME} body[data-owner-id="{batch_id}"]',
            },
            seen_at=_now(),
        )

        if ready == "error":
            # 加载失败落到对应父批次，绝不把空数组当成功。
            base.status = "error"
            base.panel_text = (
                await batch.locator(PANEL).inner_text()
            ).strip()
            base.panel_locator = f"{BATCH_FRAME} {PANEL}"
            base.screenshot = await self._shot(f"batch-{batch_id}-error.png")
            base.error = "批次凭证加载失败（data-ready=error）"
        return base

    async def _extract_receipt(self, batch, batch_id: str, base: BatchRecord) -> BatchRecord:
        # 进入开放 Shadow DOM，选择器全部限定在当前批次的
        # section（data-owner-id 与 data-ready 双匹配）之下。
        try:
            section = batch.locator(PANEL).locator(
                f'section[data-owner-id="{batch_id}"][data-ready="true"]'
            )
            await section.wait_for(state="attached", timeout=self.timeout_ms)
        except PWTimeoutError as exc:
            raise StaleFrameError(f"批次 {batch_id} 凭证面板等待超时") from exc
        number_p = section.locator("p").first
        receipt_p = section.locator("[data-receipt]")
        number_text = (await number_p.inner_text()).strip()
        receipt_id = await receipt_p.get_attribute("data-receipt")
        digest = await receipt_p.get_attribute("data-digest")
        # Shadow 内的“下载”按钮限定在当前 section；
        # 另统计文档级同名按钮，证明两者作用域不同、未混用。
        shadow_downloads = await section.locator("button").count()
        doc_downloads = await batch.locator("body > button").count()
        base.status = "ok"
        base.panel_locator = f"{BATCH_FRAME} {PANEL} >> shadow"
        base.section_locator = (
            f'{PANEL} section[data-owner-id="{batch_id}"]'
        )
        base.receipt_ref = f"{base.contract_id}/{batch_id}/{receipt_id}"
        base.receipt_id = receipt_id
        base.digest = digest
        base.number_text = number_text
        base.download_controls = shadow_downloads
        base.evidence.update({
            "receipt_attribute": receipt_id,
            "digest_attribute": digest,
            "shadow_download_controls": shadow_downloads,
            "document_download_controls": doc_downloads,
            "section_number_text": number_text,
            "shadow_scope": "open",
        })
        return base

    # ---------- 引用与凭证内容 ----------
    @staticmethod
    def build_receipt(batch: BatchRecord) -> tuple[ReceiptRef, ReceiptVariant]:
        ref = ReceiptRef(
            contract_id=batch.contract_id,
            batch_id=batch.batch_id,
            receipt_id=batch.receipt_id or "",
            digest=batch.digest or "",
            frame_url=batch.frame_url,
            receipt_ref=batch.receipt_ref or "",
            evidence=batch.evidence,
            seen_at=batch.seen_at,
        )
        variant = ReceiptVariant(
            digest=batch.digest or "",
            text=(batch.number_text or "") + " | " + (batch.digest or ""),
            frame_url=batch.frame_url,
            receipt_ref=batch.receipt_ref or "",
            evidence=batch.evidence,
            seen_at=batch.seen_at,
        )
        return ref, variant
