"""合同—履约批次—凭证层级采集器。

只通过浏览器可见页面操作取得资料，不调用门户的内部运维、运行时或
数据文件入口；采集端对这些内部路径保持零引用（由测试静态约束保证）。
"""
from .errors import CollectorError, StaleFrameError, ChainInterrupted
from .models import BatchRecord, ContractRecord, ReceiptRef, ReceiptVariant
from .store import ResultStore
from .collector import run_collection

__all__ = [
    "CollectorError",
    "StaleFrameError",
    "ChainInterrupted",
    "BatchRecord",
    "ContractRecord",
    "ReceiptRef",
    "ReceiptVariant",
    "ResultStore",
    "run_collection",
]
