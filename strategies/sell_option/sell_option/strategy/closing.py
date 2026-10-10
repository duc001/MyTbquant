"""平仓策略（§7.3 出场规则编排）：装配持仓 → 编排 X1–X4 → 平仓处置汇总。

策略聚合层的一个【具体策略模块】（与 opening / position… 平级，共用 base.combine_results 口径）。职责：
- `ClosingData`：单笔持仓平仓评估的数据装配暂存——持有 D16 持仓 + D09/D10 当前权利金（估值）+ D08 所属标的月
  真实合约现价（破位对照价）+ 判定基准日，按各出场检查需要分发。这是【平仓策略】自己的装配结构。
- `ClosingCheck` / `CLOSING_CHECKS`：出场检查注册表（X1 止盈 → X2 止损 → X3 破位 → X4 到期）。每条＝(rule_id,
  name, blocking, run)；run 把 ClosingData 里该检查需要的数据层产物分发过去 `(data, th) -> CheckResult`。元数据取自
  各逻辑单元模块（单一事实源）；新增出场规则＝append 一条 ClosingCheck。
- `run_closing_checks` / `evaluate_closing`：按 X1→X4 跑检查 + `base.combine_results` 汇总。单条抛异常降级为该条
  MISSING（不崩整轮、不冒充）。
- `CLOSING_LABEL` / `closing_label` / `closing_disposition`：平仓四态 → 中文标签（续持 / 续持·有提示 / 待人工 / 平仓）
  ＋ 从驱动检查提取处置动作（平仓·止盈/止损/破位/到期必平），供 §八 B 块「持仓处置」呈现。

§7.3 定位（逐持仓处置，四态不新增第 5 态——用 Verdict + 注入 labels 表达「平 / 持」）：
- **FAIL → 平仓**（X1 止盈达标 / X2 止损 / X3 破位 / X4 到期必平 任一触发；combine 按 FAIL>MISSING>WARN>PASS 取平仓）；
- **WARN → 续持·有提示**（X1 保底止盈 50–75% / X4 到期 ≤5 提醒——只提示、不强平）；
- **PASS → 续持**（无出场信号）；**MISSING → 待人工**（估值/对照价/DTE 缺失，不冒充）。
- **事件、长假只提示不强平**（§7.3）——不在本策略出场检查内，由 §八 报告 D 块汇总（复用 #6 事件 / D15 长假）。
- 与 opening/position 的区别：closing 逐【持仓】评估（一持仓一 ClosingData），非逐候选；无 market_open 双模式
  （估值 D09/D10、破位 D08 均有盘中/盘后双路，盘后回退日线值可判；无源则该持仓 MISSING）。

运行自检：``python -m sell_option.strategy.closing``（加载真实持仓，展示平仓处置状态）
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

try:  # 包方式（python -m / import）：相对导入
    from ..checks.models import CheckResult, Verdict
    from ..checks import x1_takeprofit, x2_stoploss, x3_breach, x4_expiry
    from ..thresholds import ThresholdSet
    from .base import combine_results
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import CheckResult, Verdict
    from sell_option.checks import x1_takeprofit, x2_stoploss, x3_breach, x4_expiry
    from sell_option.thresholds import ThresholdSet
    from sell_option.strategy.base import combine_results

if TYPE_CHECKING:  # 数据层产物类型仅标注用（聚合层依赖数据层，运行时鸭子类型）
    import datetime as dt

    from ..fetchers.d16_positions import Position
    from ..fetchers.d09_d10_premium import PremiumSet
    from ..fetchers.d08_spot_price import SpotQuote

# 策略元数据（满足 base.Strategy 契约）
STRATEGY_ID = "closing"
STRATEGY_NAME = "平仓处置"


# ---------------------------------------------------------------------------
# 数据装配暂存（平仓策略职责：持有从数据层取回的产物，按检查需要分发）
# ---------------------------------------------------------------------------
@dataclass
class ClosingData:
    """一笔持仓平仓评估从数据层取回的产物集合（平仓策略暂存；随出场规则增多而扩展字段）。

    已接入：X1 止盈 / X2 止损 ← position（R/腿）+ premium（当前平回成本）；X3 破位 ← position（短腿K/结构/品种）
    + spot（所属标的月真实合约现价、非主力月）；X4 到期 ← position（current_dte）+ today（判定基准日）。"""

    position: "Position | None" = None       # D16 持仓（R/max_loss/expiry_date/short_strike/structure/underlying_month）
    premium: "PremiumSet | None" = None      # D09/D10 当前腿权利金（X1/X2 平回成本估值；None → X1/X2 MISSING）
    spot: "SpotQuote | None" = None          # D08 所属标的月真实合约现价（X3 破位对照价；None → X3 MISSING）
    today: "dt.date | None" = None           # 判定基准日（X4 到期；None → date.today()）


# ---------------------------------------------------------------------------
# 出场检查注册表（X1→X4；把 ClosingData 分发成各检查所需的数据层产物）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClosingCheck:
    """一条出场检查的装配规格：元数据取自逻辑单元模块（单一事实源）+ 分发适配器。"""
    rule_id: str
    name: str
    blocking: bool
    run: Callable[[ClosingData, ThresholdSet], CheckResult]


CLOSING_CHECKS: list[ClosingCheck] = [
    ClosingCheck(x1_takeprofit.RULE_ID, x1_takeprofit.RULE_NAME, x1_takeprofit.BLOCKING,   # X1 止盈
                 lambda data, th: x1_takeprofit.evaluate(data.position, data.premium, th)),
    ClosingCheck(x2_stoploss.RULE_ID, x2_stoploss.RULE_NAME, x2_stoploss.BLOCKING,         # X2 止损
                 lambda data, th: x2_stoploss.evaluate(data.position, data.premium, th)),
    ClosingCheck(x3_breach.RULE_ID, x3_breach.RULE_NAME, x3_breach.BLOCKING,               # X3 破位
                 lambda data, th: x3_breach.evaluate(data.position, data.spot, th)),
    ClosingCheck(x4_expiry.RULE_ID, x4_expiry.RULE_NAME, x4_expiry.BLOCKING,               # X4 到期
                 lambda data, th: x4_expiry.evaluate(data.position, data.today, th)),
]

# 平仓四态 → 中文标签（§八 报告用；平仓策略专有词汇，注入 base.combine_results）
CLOSING_LABEL: dict[Verdict, str] = {
    Verdict.PASS: "续持",
    Verdict.WARN: "续持·有提示",
    Verdict.MISSING: "待人工",
    Verdict.FAIL: "平仓",
}


def closing_label(verdict: Verdict) -> str:
    """平仓四态 → 中文标签（续持 / 续持·有提示 / 待人工 / 平仓）。"""
    return CLOSING_LABEL.get(verdict, verdict.value)


def closing_disposition(summary: CheckResult, results: list[CheckResult]) -> str:
    """从汇总结论 + 驱动检查提取【处置动作】（§八 B 块呈现；不改变任何 verdict）。

    FAIL → 「平仓·{驱动检查的 disposition}」（止盈达标 / 止损 / 破位 / 到期必平，多信号并列）；
    WARN → 「续持·{提示项}」（止盈提示 / 到期提醒）；MISSING → 「待人工」；PASS → 「续持」。"""
    def _disps(verdict: Verdict) -> list[str]:
        return [r.detail.get("disposition") for r in results
                if r.verdict == verdict and r.detail.get("disposition")]

    if summary.verdict == Verdict.FAIL:
        d = _disps(Verdict.FAIL)
        return "平仓·" + "/".join(d) if d else "平仓"
    if summary.verdict == Verdict.WARN:
        d = _disps(Verdict.WARN)
        return "续持·" + "/".join(d) if d else "续持·有提示"
    if summary.verdict == Verdict.MISSING:
        return "待人工"
    return "续持"


def run_closing_checks(data: ClosingData, th: ThresholdSet) -> list[CheckResult]:
    """对一笔持仓按 X1→X4 顺序跑已注册出场检查，返回 CheckResult 列表。

    单条抛异常降级为该条 MISSING（不崩整轮、不冒充）。出场检查均有盘中/盘后双路数据，无 opening 的休市跳过逻辑。"""
    results: list[CheckResult] = []
    for spec in CLOSING_CHECKS:
        try:
            results.append(spec.run(data, th))
        except Exception as e:  # noqa: BLE001 - 单检查异常只降级、不崩汇总
            results.append(CheckResult(
                rule_id=spec.rule_id, name="检查异常", verdict=Verdict.MISSING,
                reason=f"检查执行异常（{type(e).__name__}: {e}）→ 降级 MISSING", blocking=spec.blocking))
    return results


def evaluate_closing(data: ClosingData, th: ThresholdSet) -> tuple[CheckResult, list[CheckResult]]:
    """平仓策略入口：跑出场检查 + 按平仓词汇汇总 → (总结论, 逐条结果)。

    总结论四态（FAIL>MISSING>WARN>PASS）：任一出场信号 FAIL → 平仓；估值/对照价缺失 → MISSING（待人工）；
    仅提醒 → WARN（续持·有提示）；全通过 → PASS（续持）。处置动作见 closing_disposition(summary, results)。"""
    results = run_closing_checks(data, th)
    return combine_results(results, labels=CLOSING_LABEL), results


# ---------------------------------------------------------------------------
# 实测：加载真实持仓，展示平仓处置状态
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import datetime as dt
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d16_positions import fetch_d16
    from sell_option.thresholds import load_thresholds

    th = load_thresholds()
    print("=" * 72)
    print(f"strategy.closing · 策略[{STRATEGY_ID}/{STRATEGY_NAME}] · 已注册检查 {len(CLOSING_CHECKS)} 条"
          f"（X1 止盈 + X2 止损 + X3 破位 + X4 到期）· FAIL→平仓")

    book = fetch_d16()
    if not book.loaded:
        print(f"  持仓记录不可用（{book.load_error}）→ 平仓策略不可判")
        print("=" * 72)
        sys.exit(0)

    if not book.positions:
        print("  无持仓 → 平仓策略无需判")
        print("=" * 72)
        sys.exit(0)

    print(f"  持仓 {len(book)} 笔\n")
    today = dt.date.today()
    for p in book.positions:
        dte = p.current_dte(today)
        nc = p.net_credit
        nc_s = f"{nc:.1f}" if nc is not None else "?"
        print(f"  {p.code:<3} {p.short_leg.symbol:<25} R={nc_s:>8}  DTE={dte or '?'}")
        print(f"       平仓评估需实时权利金（盘中 D09/D10）+ 现价（D08）→ 策略运行时自动评估")
    print("=" * 72)
