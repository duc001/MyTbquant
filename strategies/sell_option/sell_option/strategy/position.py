"""仓位风控策略（§7.4 仓位限）：装配组合状态 → 编排 P1 → 风控汇总。

策略聚合层的一个【具体策略模块】（与 opening / closing… 平级，共用 base.combine_results 口径）。职责：
- `PositionData`：仓位风控的数据装配暂存——持有从数据层取回的 D16 持仓簿（P1 计数）。
- `PositionCheck` / `POSITION_CHECKS`：仓位风控检查注册表（P1）。每条＝(rule_id, name, blocking, run)；
  run 把 PositionData 里该检查需要的数据层产物分发过去 `(data, th) -> CheckResult`。元数据取自各逻辑单元模块
  （RULE_ID/RULE_NAME/BLOCKING，单一事实源）；新增风控项＝append 一条 PositionCheck。
- `run_position_checks` / `evaluate_position`：按 P1 跑检查 + `base.combine_results` 风控汇总。单条检查抛异常
  降级为该条 MISSING（不崩整轮、不冒充）。仓位风控全部基于本地 DB（positions.db），
  **盘后可判、无盘中实时依赖**，故无 opening 的 needs_live / market_open 双模式。
- `RISK_LABEL` / `risk_label`：风控四态 → 中文标签（可开新仓 / 有提示 / 待人工 / 触线暂停开仓）。

§7.4 定位（组合维度风控，非单标的评估）：opening 判「这一个候选能不能开」，position 判「当前组合还让不让开新仓」。
P1 触线（总笔数 / 每品种 / 同持品种数超限）→ **暂停开新仓、不强平存量**（与 #7 一致）。
落地口径（已确认）：P1 **不作开仓流程的程序前置硬闸门**——runner 不跑 evaluate_position，仅在 scan 时算一次
入日报 C 块，触线「暂停开新仓」由人按报告执行。

运行自检：``python -m sell_option.strategy.position``（接真实数据链从 positions.db 取 D16 持仓簿，展示 编排 → 风控四态）
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

try:  # 包方式（python -m / import）：相对导入
    from ..checks.models import CheckResult, Verdict
    from ..checks import p_limits
    from ..thresholds import ThresholdSet
    from .base import combine_results
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import CheckResult, Verdict
    from sell_option.checks import p_limits
    from sell_option.thresholds import ThresholdSet
    from sell_option.strategy.base import combine_results

if TYPE_CHECKING:  # 数据层产物类型仅标注用（聚合层依赖数据层，运行时鸭子类型）
    from ..fetchers.d16_positions import PositionBook

# 策略元数据（满足 base.Strategy 契约）
STRATEGY_ID = "position"
STRATEGY_NAME = "仓位风控"


# ---------------------------------------------------------------------------
# 数据装配暂存（仓位风控策略职责：持有从数据层取回的产物，按检查需要分发）
# ---------------------------------------------------------------------------
@dataclass
class PositionData:
    """仓位风控从数据层取回的产物集合（本策略暂存；随风控项增多而扩展字段）。

    已接入：P1 book（D16 持仓簿 → 总笔数 / 每品种 / 同持品种数（distinct codes）计数）。
    book 为 None / 未加载 → P1 判 MISSING。"""

    book: "PositionBook | None" = None       # D16 持仓簿（P1 仓位限计数；None/未加载 → P1 MISSING）


# ---------------------------------------------------------------------------
# 仓位风控检查注册表（P1；把 PositionData 分发成各检查所需的数据层产物）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PositionCheck:
    """一条仓位风控检查的装配规格：元数据取自逻辑单元模块（单一事实源）+ 分发适配器。"""
    rule_id: str
    name: str
    blocking: bool
    run: Callable[[PositionData, ThresholdSet], CheckResult]


POSITION_CHECKS: list[PositionCheck] = [
    PositionCheck(p_limits.RULE_ID, p_limits.RULE_NAME, p_limits.BLOCKING,   # P1 仓位限：D16 计数
                  lambda data, th: p_limits.evaluate(data.book, th)),
]

# 风控四态 → 中文标签（§八 报告用；仓位风控策略专有词汇，注入 base.combine_results）
RISK_LABEL: dict[Verdict, str] = {
    Verdict.PASS: "风控通过·可开新仓",
    Verdict.WARN: "风控·有提示",
    Verdict.MISSING: "待人工·数据缺失",
    Verdict.FAIL: "触线·暂停开仓",
}


def risk_label(verdict: Verdict) -> str:
    """风控四态 → 中文标签（可开新仓 / 有提示 / 待人工 / 触线暂停开仓）。"""
    return RISK_LABEL.get(verdict, verdict.value)


def run_position_checks(data: PositionData, th: ThresholdSet) -> list[CheckResult]:
    """按 P1 顺序跑已注册风控检查，返回 CheckResult 列表。

    单条抛异常降级为该条 MISSING（不崩整轮、不冒充）。仓位风控无盘中依赖、盘后一律可跑（不同于 opening 的休市跳过）。"""
    results: list[CheckResult] = []
    for spec in POSITION_CHECKS:
        try:
            results.append(spec.run(data, th))
        except Exception as e:  # noqa: BLE001 - 单检查异常只降级、不崩汇总
            results.append(CheckResult(
                rule_id=spec.rule_id, name="检查异常", verdict=Verdict.MISSING,
                reason=f"检查执行异常（{type(e).__name__}: {e}）→ 降级 MISSING", blocking=spec.blocking))
    return results


def evaluate_position(data: PositionData, th: ThresholdSet) -> tuple[CheckResult, list[CheckResult]]:
    """仓位风控策略入口：跑 P1 检查 + 按风控词汇汇总 → (总结论, 逐条结果)。

    总结论四态（FAIL>MISSING>WARN>PASS）：任一触线 FAIL → 暂停开新仓（不强平存量）；数据缺失 → MISSING（待人工）；
    全通过 → PASS（可开新仓）。detail.counts 给各态计数、detail.label 给命中标签。"""
    results = run_position_checks(data, th)
    return combine_results(results, labels=RISK_LABEL), results


# ---------------------------------------------------------------------------
# 真实数据自检：从 positions.db 取 D16 持仓簿，展示 编排 → 风控四态
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d16_positions import fetch_d16
    from sell_option.thresholds import load_thresholds

    th = load_thresholds()
    book = fetch_d16()

    data = PositionData(book=book)
    summary, results = evaluate_position(data, th)

    print("=" * 72)
    print(f"strategy.position · 策略[{STRATEGY_ID}/{STRATEGY_NAME}]")
    print(f"  汇总：{summary.verdict.value} → {summary.detail.get('label', '?')}")
    for r in results:
        print(f"  {r.rule_id} {r.verdict.value}：{r.reason}")
    print("=" * 72)
