"""逻辑单元层（§七）共用模型：检查产物 CheckResult + 四态合并口径：
- **数据层**（§六 `fetchers/`）：`fetch_dXX()` 产出可复用数据产物（PremiumSet / OiSet / SpotQuote…）。
- **逻辑单元层**（§七 本包 `checks/`）：每个检查项一个【纯函数单元】，**直接吃数据层产物 + 阈值**
  → 判四态 CheckResult。只依赖数据层（+ thresholds），**不依赖策略聚合层**，故独立可复用、可离线测。
- **策略聚合层**（`strategy/`）：取数组装、组合逻辑单元、漏斗汇总、输出报告 / 操作结果。

本模块只定义【检查产物结构】与四态合并口径，不含具体判定（判定在各 cX_*.py）。
Verdict 直接复用 `sell_option/fetchers/models.py`（不重复定义，保证与数据层同一口径）。

四态语义（§八，与数据层一脉相承）：
- PASS：数据齐全且达标；FAIL：有确切证据违反阈值（仅 BLOCKING 检查会产生）；
- WARN：达标但时机 / 方向存疑；MISSING：依赖数据缺失——绝不冒充。
合并优先级：**FAIL > MISSING > WARN > PASS**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:  # 包方式（python -m / import）：相对导入（checks → sell_option → fetchers）
    from ..fetchers.models import Verdict
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.models import Verdict

__all__ = ["Verdict", "VERDICT_RANK", "merge_verdicts", "CheckResult"]

# 四态合并优先级：数值越大越"严重"，合并取最大（FAIL > MISSING > WARN > PASS）
VERDICT_RANK: dict[Verdict, int] = {
    Verdict.PASS: 0,
    Verdict.WARN: 1,
    Verdict.MISSING: 2,
    Verdict.FAIL: 3,
}


def merge_verdicts(verdicts: Iterable[Verdict]) -> Verdict:
    """按 FAIL > MISSING > WARN > PASS 优先级合并多个四态结论；空输入→PASS（无违反、无缺数）。

    用于：①单个检查内多子项 / 多腿合并（如 #1 各腿价差子项）；②聚合层多检查合并（§八 漏斗）。"""
    worst = Verdict.PASS
    for v in verdicts:
        if VERDICT_RANK.get(v, 0) > VERDICT_RANK[worst]:
            worst = v
    return worst


@dataclass(frozen=True)
class CheckResult:
    """单条逻辑单元（或聚合汇总）的判定产物——§八 报告的最小单元。

    verdict＝四态结论；reason＝人话结论（直接入报告）；detail＝逐腿 / 逐子项明细（实际值 + 阈值 + 来源）；
    blocking＝该检查 FAIL 是否淘汰候选（#2 持仓量OI / #3 市况 / #6 事件为 False：FAIL 降级 WARN、不淘汰）。"""

    rule_id: str                                     # "#1" .. "#10" / "SUMMARY"
    name: str                                        # "盘口价差" 等
    verdict: Verdict
    reason: str
    blocking: bool = True
    detail: dict[str, Any] = field(default_factory=dict)
