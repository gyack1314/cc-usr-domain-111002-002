"""幂等结果存储与断点续采。

- 合同、批次关系按稳定编号合并，重复采集不产生重复关系。
- 凭证内容按凭证编号只存一份；同编号出现不同摘要时保留冲突（variants）。
- 重采时 interrupted 链允许重跑；已确认的合同/批次关系保留，不回退。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .models import BatchRecord, ContractRecord, ReceiptRef, ReceiptVariant, to_plain

SCHEMA_VERSION = 1


class ResultStore:
    def __init__(self, out_dir: str | Path, run_id: str):
        self.out_dir = Path(out_dir)
        self.shot_dir = self.out_dir / "screenshots"
        self.result_path = self.out_dir / "result.json"
        self.run_id = run_id
        # RLock：flush() 内部调 snapshot()，会在同一线程重入，
        # 普通 Lock 会导致 add_receipt→flush→snapshot 自死锁。
        self._lock = threading.RLock()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.contracts: dict[str, ContractRecord] = {}
        # receipt_id -> {"variants": {digest: ReceiptVariant}}
        self.receipts: dict[str, dict] = {}
        # (contract, batch, receipt, digest) -> ReceiptRef
        self.refs: dict[tuple, ReceiptRef] = {}
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        if not self.result_path.is_file():
            return
        raw = json.loads(self.result_path.read_text(encoding="utf-8"))
        for c in raw.get("contracts", []):
            batches = [BatchRecord(**b) for b in c.pop("batches", [])]
            self.contracts[c["contract_id"]] = ContractRecord(batches=batches, **c)
        self.receipts = raw.get("receipts", {})
        for r in raw.get("receipt_refs", []):
            ref = ReceiptRef(**r)
            self.refs[ref.key()] = ref

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "contracts": [to_plain(c) for c in self.contracts.values()],
                "receipt_refs": [to_plain(r) for r in self.refs.values()],
                "receipts": self.receipts,
            }

    def flush(self) -> None:
        tmp = self.result_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.result_path)

    # ---------- 查询 ----------
    def incomplete_contracts(self) -> set[str]:
        """需要重采的链：未收齐（interrupted）或此前完全不存在。"""
        return {cid for cid, c in self.contracts.items() if c.status != "complete"}

    def is_contract_complete(self, contract_id: str) -> bool:
        c = self.contracts.get(contract_id)
        return c is not None and c.status == "complete"

    # ---------- 写入（幂等合并）----------
    def upsert_contract(self, record: ContractRecord) -> None:
        with self._lock:
            old = self.contracts.get(record.contract_id)
            if old is not None and old.status == "complete" and record.status != "complete":
                # 已确认的合同关系不被一次未完成重采回退。
                return
            if old is None or record.status == "complete":
                self.contracts[record.contract_id] = record
            else:
                # interrupted 重采仍 interrupted：保留较新的部分批次。
                merged_batches = {b.batch_id: b for b in old.batches}
                for b in record.batches:
                    merged_batches[b.batch_id] = b
                record.batches = list(merged_batches.values())
                self.contracts[record.contract_id] = record
            self.flush()

    def add_receipt(self, ref: ReceiptRef, variant: ReceiptVariant) -> None:
        with self._lock:
            # 引用关系按 (合同,批次,凭证,摘要) 幂等；共享凭证保留两条关系。
            self.refs[ref.key()] = ref
            entry = self.receipts.setdefault(ref.receipt_id, {"variants": {}})
            if variant.digest not in entry["variants"]:
                # 同编号不同摘要：保留冲突，不就近覆盖。
                entry["variants"][variant.digest] = to_plain(variant)
            self.flush()

    def receipt_conflicts(self) -> dict[str, list[str]]:
        """返回存在摘要冲突的凭证编号及其全部摘要。"""
        return {
            rid: sorted(entry["variants"].keys())
            for rid, entry in self.receipts.items()
            if len(entry["variants"]) > 1
        }
