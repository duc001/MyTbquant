"""策略聚合层通用机制（策略无关）：Strategy 契约 + 漏斗合并 + 四态复用。

三层定位：本模块是【策略聚合层 strategy/】的共享底座。聚合层职责（§七 → §八）＝组合逻辑单元层
（checks/）的可复用检查 → 漏斗汇总 → 输出分析 / 操作结果。本模块【不绑定任何具体策略】，只提供
各策略共用的合并口径与契约；具体"装配什么数据、编排哪些检查、用什么标签词汇"在各策略模块里。

- `Strategy`：策略模块元数据契约（STRATEGY_ID / STRATEGY_NAME；evaluate 数据入参各策略不同、不固定签名）。
- `combine_results`：把多条 CheckResult 按统一口径合成一个总结论——合并优先级 **FAIL > MISSING > WARN > PASS**；
  非阻断项（blocking=False，如 #2 持仓量OI / #3 市况 / #6 事件）即便 FAIL 也降级 WARN（只提示 / 建议、永不淘汰）。
  标签词汇由调用方注入 `labels`，故 opening（可开 / 淘汰）、closing（平 / 持）等策略共用本机制。

本包内【一策略一模块】（依赖单向：策略模块 → base → checks → fetchers），均已落地：
- `opening.py`  开仓策略（§7.1 检查编排 #1–#10 + §7.2 结构选择）；
- `assemble.py` §7.2 选腿装配（取数→定结构→选腿→算经济性→填 OpeningData；runner 注入接缝的实体）；
- `runner.py`   开仓运行编排（check/scan 内核：时段状态贯穿 取数→check→策略）；
- `closing.py`  平仓策略（§7.3 出场规则编排 X1 止盈 / X2 止损 / X3 破位 / X4 到期）；
- `position.py` 仓位风控策略（§7.4 仓位限 P1）；
- `report.py`   §八 综合日报合成（A 开仓 / B 持仓处置 / C 组合风控 / D 提醒待人工，纯渲染层）；
- `cli.py`      §11.2 命令面（scan / check / exit / collect-oi / refresh / scope / report；入口 sellopt.py）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

try:  # 包方式（python -m / import）：相对导入
    from ..checks.models import CheckResult, Verdict, merge_verdicts
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import CheckResult, Verdict, merge_verdicts

__all__ = ["Strategy", "combine_results", "CheckResult", "Verdict", "merge_verdicts"]


@runtime_checkable
class Strategy(Protocol):
    """策略模块的元数据契约（模块对象即满足：具备下列模块级属性）。

    各策略的 evaluate 数据入参不同（开仓吃候选装配、平仓吃持仓 + 行情），故只约束元数据、
    不固定 evaluate 签名——与逻辑单元层 checks.base.CheckUnit 同一思路。"""

    STRATEGY_ID: str
    STRATEGY_NAME: str


def combine_results(results: list[CheckResult], *, labels: dict[Verdict, str],
                    summary_id: str = "SUMMARY", summary_name: str = "汇总") -> CheckResult:
    """把多条 CheckResult 合成一个总结论（各策略共用的漏斗口径；标签词汇由 labels 注入）。

    - 非阻断项（blocking=False）的 FAIL 降级为 WARN（永不淘汰）；其余按 FAIL>MISSING>WARN>PASS 合并。
    - reason：PASS → "{标签}：全部 N 项检查通过"；否则 → "{标签}：{驱动结论的检查名}"。
    - detail：{results 逐条摘要, counts 各态计数, label 命中标签}。
    - 空结果 → MISSING（不可汇总、不冒充）。"""
    if not results:
        return CheckResult(summary_id, summary_name, Verdict.MISSING,
                           "无检查结果 → 不可汇总", True, {"results": [], "counts": {}})

    effective: list[Verdict] = []
    for r in results:
        v = r.verdict
        if v == Verdict.FAIL and not r.blocking:
            v = Verdict.WARN          # 非阻断项永不淘汰：FAIL 降级 WARN
        effective.append(v)
    verdict = merge_verdicts(effective)

    driving = [r for r, v in zip(results, effective) if v == verdict]
    label = labels.get(verdict, verdict.value)
    if verdict == Verdict.PASS:
        reason = f"{label}：全部 {len(results)} 项检查通过"
    else:
        names = "、".join(f"{r.rule_id}{r.name}" for r in driving)
        reason = f"{label}：{names}"
    counts: dict[str, int] = {}
    for v in effective:
        counts[v.value] = counts.get(v.value, 0) + 1
    detail = {
        "results": [{"rule_id": r.rule_id, "name": r.name,
                     "verdict": r.verdict.value, "reason": r.reason} for r in results],
        "counts": counts,
        "label": label,
    }
    return CheckResult(summary_id, summary_name, verdict, reason, True, detail)
