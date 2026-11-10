"""采集结果数据模型。

层级：合同（contract）→ 履约批次（batch）→ 批次凭证（receipt）。
凭证内容按 receipt_id 去重保存一份；批次与凭证之间保留独立引用关系，
同一凭证被两个批次引用时会产生两条 ReceiptRef，不会被全局去重吞掉。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ReceiptVariant:
    """同一凭证编号下的一种摘要版本；同号不同摘要保留冲突。"""

    digest: str
    text: str
    frame_url: str
    receipt_ref: str
    evidence: dict[str, Any]
    seen_at: str


@dataclass
class ReceiptRef:
    """批次 → 凭证 的一条引用关系（不是凭证内容本身）。"""

    contract_id: str
    batch_id: str
    receipt_id: str
    digest: str
    frame_url: str
    receipt_ref: str
    evidence: dict[str, Any]
    seen_at: str

    def key(self) -> tuple[str, str, str, str]:
        # 同编号但不同摘要属于不同关系，digest 进入键以避免就近覆盖。
        return self.contract_id, self.batch_id, self.receipt_id, self.digest


@dataclass
class BatchRecord:
    """一条履约批次记录。

    status:
      - ok: 成功读到凭证；
      - empty: 合同下没有批次是合同级结果，批次本身不会出现 empty；
      - error: 批次 frame 明确给出 data-ready=error（加载失败）；
      - interrupted: 页面在该批次未完成时关闭，不算成功也不算空。
    """

    batch_id: str
    contract_id: str
    status: str
    frame_url: str
    frame_locator: str
    owner_id: str | None
    ready: str | None
    panel_locator: str | None = None
    section_locator: str | None = None
    receipt_ref: str | None = None
    receipt_id: str | None = None
    digest: str | None = None
    number_text: str | None = None
    panel_text: str | None = None
    download_controls: int | None = None
    screenshot: str | None = None
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    seen_at: str = ""


@dataclass
class ContractRecord:
    """一条合同父级链的采集结果。"""

    contract_id: str
    name: str
    status: str  # complete | interrupted
    detail_frame_url: str
    frame_locator: str
    owner_id: str | None
    ready: str | None
    number_text: str | None
    download_controls: int | None
    batch_buttons: list[str]
    empty: bool
    batches: list[BatchRecord] = field(default_factory=list)
    screenshot: str | None = None
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    seen_at: str = ""


def to_plain(obj: Any) -> Any:
    """把 dataclass 结果转成可 JSON 序列化的普通结构。"""
    return asdict(obj)
