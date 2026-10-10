"""X2 止损（§7.3）逻辑单元：浮亏 ≥ 2× 净权利金 → 平仓止损（信用价差中段止损、早于封顶最大亏损）。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元，**吃数据层产物**（D16 `Position` + D09/D10
`PremiumSet`）＋ 阈值集 → 判四态 CheckResult。盈亏估值复用共享底座 `x_pnl.close_pnl`（与 X1 止盈同源，
只是阈值方向相反）；判定权归本单元、读 `th.sl_multiplier` 作权威阈值（方案A）。

需求源 §7.3『2× 止损』（阈值已入 ThresholdSet：sl_multiplier=2.0）：
- **止损口径（〔待〕用户校准）**：浮亏 loss＝当前平回成本 − R（R＝开仓净权利金）；loss ≥ `sl_multiplier`(2)×R
  → **FAIL 平仓止损**。即「亏损达净权利金 2 倍」触发——信用价差卖方标准中段止损，早于封顶最大亏损 (W−R)×乘数。
- **四态**：不可估值（R/现值缺失）→ **MISSING**（不冒充盈亏）；loss ≥ 止损线 → **FAIL 平仓**；否则（浮亏未达线
  或浮盈）→ **PASS 续持**。无 WARN（止损是硬线；接近止损线的预警交 X1/呈现层，不在本单元）。
- **BLOCKING=True**：止损（FAIL）须真正驱动「平仓」结论（非阻断会被 combine_results 降级 WARN、丢失平仓信号）。
- 与 X1 互斥：同一持仓不会既止盈达标又止损（收益率不可能同时 ≥75% 且 ≤−200%）；closing 汇总按 FAIL>…取平仓。

运行自检：``python -m sell_option.checks.x2_stoploss``（加载真实持仓展示止损评估状态）
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

# 规则元数据（§7.3 止损；聚合层汇总用）
RULE_ID = "X2"
RULE_NAME = "止损"
BLOCKING = True                   # 止损须驱动「平仓」（非阻断会被降级 WARN、丢失平仓信号）


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝D16 Position + D09/D10 PremiumSet）
# ---------------------------------------------------------------------------
def evaluate(position: "Position | None", premium: "PremiumSet | None", th: ThresholdSet) -> CheckResult:
    """X2 止损：当前浮亏是否 ≥ sl_multiplier × 净权利金？

    position：D16 持仓（取 net_credit=R、腿 symbol）；premium：D09/D10 当前权利金（算平回成本）；
    th：注入阈值集（读 sl_multiplier，权威阈值）。
    四态：不可估值 → MISSING；loss ≥ sl_multiplier×R → FAIL（平仓止损）；否则 → PASS（续持）。"""
    th_snap = {"sl_multiplier": th.sl_multiplier}
    pnl = close_pnl(position, premium)
    if not pnl.valued:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"止损判不出（{pnl.reason}）", BLOCKING, {**th_snap, "valued": False})

    loss = pnl.loss                                  # 平回成本 − R（正＝浮亏、负＝浮盈）
    stop_line = th.sl_multiplier * pnl.net_credit    # 止损线＝2×R
    detail = {**th_snap, "net_credit": pnl.net_credit, "close_cost": pnl.close_cost,
              "loss": loss, "stop_line": stop_line}
    if loss is not None and loss >= stop_line:       # 浮亏达止损线 → 平仓
        detail["disposition"] = "止损"
        return CheckResult(RULE_ID, RULE_NAME, Verdict.FAIL,
                           f"浮亏 {loss:.1f} ≥ 止损线 {stop_line:.1f}（{th.sl_multiplier:g}×净权利金 "
                           f"{pnl.net_credit:.1f}）→ X2 平仓止损（§7.3）", BLOCKING, detail)
    detail["disposition"] = "续持"
    shown = f"浮亏 {loss:.1f}" if (loss is not None and loss > 0) else "无浮亏（浮盈或持平）"
    return CheckResult(RULE_ID, RULE_NAME, Verdict.PASS,
                       f"{shown} < 止损线 {stop_line:.1f}（{th.sl_multiplier:g}×R）→ X2 续持", BLOCKING, detail)


# ---------------------------------------------------------------------------
# 实测：加载真实持仓，展示止损评估状态
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
    print(f"X2 止损 · 浮亏 ≥ {th.sl_multiplier:g}×净权利金 → FAIL 平仓 · BLOCKING={BLOCKING}")

    book = fetch_d16()
    if not book.loaded:
        print(f"  持仓记录不可用（{book.load_error}）→ X2 不可判")
        print("=" * 72)
        sys.exit(0)

    if not book.positions:
        print("  无持仓 → X2 无需判")
        print("=" * 72)
        sys.exit(0)

    print(f"  持仓 {len(book)} 笔\n")
    today = dt.date.today()
    for p in book.positions:
        dte = p.current_dte(today)
        nc = p.net_credit
        nc_s = f"{nc:.1f}" if nc is not None else "?"
        sl = f"{th.sl_multiplier * nc:.1f}" if nc is not None else "?"
        print(f"  {p.code:<3} {p.short_leg.symbol:<25} R={nc_s:>8}  止损线={sl:>8}  DTE={dte or '?'}")
        print(f"       止损需实时权利金（盘中 D09/D10）→ 策略运行时自动评估")
    print("=" * 72)
