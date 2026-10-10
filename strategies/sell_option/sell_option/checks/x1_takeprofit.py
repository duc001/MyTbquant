"""X1 止盈（§7.3）逻辑单元：50% 保底止盈 / 结构完好续到 75%——收益率两级阈值 → 平仓落袋 / 提示续持。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元，**吃数据层产物**（D16 `Position` + D09/D10
`PremiumSet`）＋ 阈值集 → 判四态 CheckResult。盈亏估值复用共享底座 `x_pnl.close_pnl`（R / 平回成本 / 收益率），
本单元只负责「收益率 → 四态」的判定（判定权归本单元、读 `th.tp_floor / tp_extend` 作权威阈值＝方案A）。

需求源 §7.3『50% 保底止盈 / 结构完好续到 75%』（阈值已入 ThresholdSet：tp_floor=0.50 / tp_extend=0.75）：
- **收益率 profit_ratio＝(R − 当前平回成本) / R**（占最大权利金比例；R＝开仓净权利金＝最大盈利）。
- **两级止盈**（〔待〕用户校准口径）：profit_ratio ≥ `tp_extend`(75%) → **FAIL 平仓**（止盈达标、落袋为安）；
  `tp_floor`(50%) ≤ ratio < 75% → **WARN 提示**（已达保底止盈，结构完好可续持博 75%）；ratio < 50% → **PASS 续持**。
- **"结构完好续到 75%"的实现**：50–75% 区间只 WARN（提示、不强平）；若此时结构已坏（X3 破位 / X2 止损 / X4
  到期必平 任一 FAIL），closing 汇总按 FAIL>WARN 取「平仓」——故"续持博更高"仅在结构完好（无其它平仓信号）时成立，
  无需本单元显式判"结构完好"（那是 X2/X3/X4 的职责，本单元只管收益率档位）。
- **BLOCKING=True**：止盈达标（FAIL）须真正驱动「平仓」结论（非阻断会被 combine_results 降级 WARN、丢失平仓信号）。
- 不可估值（R/现值缺失）→ **MISSING**（不冒充盈亏）。

运行自检：``python -m sell_option.checks.x1_takeprofit``（加载真实持仓展示止盈评估状态）
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

try:  # 包方式（python -m / import）：相对导入
    from .models import CheckResult, Verdict
    from .x_pnl import close_pnl
    from ..thresholds import ThresholdSet
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import CheckResult, Verdict
    from sell_option.checks.x_pnl import close_pnl
    from sell_option.thresholds import ThresholdSet

if TYPE_CHECKING:  # 数据层产物类型仅标注用（逻辑单元层依赖数据层，运行时鸭子类型读字段）
    from ..fetchers.d16_positions import Position
    from ..fetchers.d09_d10_premium import PremiumSet

# 规则元数据（§7.3 止盈；聚合层汇总用）
RULE_ID = "X1"
RULE_NAME = "止盈"
BLOCKING = True                   # 止盈达标须驱动「平仓」（非阻断会被降级 WARN、丢失平仓信号）


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝D16 Position + D09/D10 PremiumSet）
# ---------------------------------------------------------------------------
def evaluate(position: "Position | None", premium: "PremiumSet | None", th: ThresholdSet) -> CheckResult:
    """X1 止盈：当前收益率是否达 保底止盈(tp_floor) / 止盈达标(tp_extend)？

    position：D16 持仓（取 net_credit=R、腿 symbol）；premium：D09/D10 当前权利金（算平回成本）；
    th：注入阈值集（读 tp_floor / tp_extend，权威阈值）。
    四态：不可估值 → MISSING；ratio ≥ tp_extend → FAIL（平仓落袋）；≥ tp_floor → WARN（保底止盈提示、可续持）；
    否则 → PASS（续持）。"""
    th_snap = {"tp_floor": th.tp_floor, "tp_extend": th.tp_extend}
    pnl = close_pnl(position, premium)
    if not pnl.valued:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"止盈判不出（{pnl.reason}）", BLOCKING, {**th_snap, "valued": False})

    ratio = pnl.profit_ratio
    detail = {**th_snap, "net_credit": pnl.net_credit, "close_cost": pnl.close_cost,
              "profit": pnl.profit, "profit_ratio": ratio}
    if ratio >= th.tp_extend:                       # ≥75% 止盈达标 → 平仓
        detail["disposition"] = "止盈达标"
        return CheckResult(RULE_ID, RULE_NAME, Verdict.FAIL,
                           f"收益率 {ratio:.0%} ≥ 止盈达标线 {th.tp_extend:.0%} → X1 平仓落袋（§7.3）",
                           BLOCKING, detail)
    if ratio >= th.tp_floor:                        # 50–75% 保底止盈 → 提示续持
        detail["disposition"] = "止盈提示"
        return CheckResult(RULE_ID, RULE_NAME, Verdict.WARN,
                           f"收益率 {ratio:.0%} ≥ 保底止盈线 {th.tp_floor:.0%}（未达 {th.tp_extend:.0%}）"
                           f" → X1 提示：结构完好可续持博更高，否则落袋", BLOCKING, detail)
    detail["disposition"] = "续持"
    return CheckResult(RULE_ID, RULE_NAME, Verdict.PASS,
                       f"收益率 {ratio:.0%} < 保底止盈线 {th.tp_floor:.0%} → X1 续持", BLOCKING, detail)


# ---------------------------------------------------------------------------
# 实测：加载真实持仓，展示止盈评估状态
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
    print(f"X1 止盈 · 保底 {th.tp_floor:.0%} 提示 / 达标 {th.tp_extend:.0%} 平仓 · BLOCKING={BLOCKING}")

    book = fetch_d16()
    if not book.loaded:
        print(f"  持仓记录不可用（{book.load_error}）→ X1 不可判")
        print("=" * 72)
        sys.exit(0)

    if not book.positions:
        print("  无持仓 → X1 无需判")
        print("=" * 72)
        sys.exit(0)

    print(f"  持仓 {len(book)} 笔\n")
    today = dt.date.today()
    for p in book.positions:
        dte = p.current_dte(today)
        nc = p.net_credit
        nc_s = f"{nc:.1f}" if nc is not None else "?"
        print(f"  {p.code:<3} {p.short_leg.symbol:<25} R={nc_s:>8}  DTE={dte or '?'}")
        print(f"       止盈需实时权利金（盘中 D09/D10）→ 策略运行时自动评估")
    print("=" * 72)
