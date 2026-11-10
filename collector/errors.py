"""采集异常类型。"""


class CollectorError(Exception):
    """采集过程中的可预期失败（含层级与父项标识）。"""


class StaleFrameError(CollectorError):
    """frame 已被重建或面板被替换，需要丢弃旧 locator 重新解析。"""


class ChainInterrupted(CollectorError):
    """页面在父级链未完成时关闭，该链不得标记为已收齐。"""
