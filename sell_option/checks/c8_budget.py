"""#8 封顶预算 —— §7.1 第 8 项检查（逻辑单元层）。

判"单笔最大亏损封顶可控"：最大亏损 =（价差宽度 W − 净权利金 R）× 乘数（§三，真实价 tbpy），
须 ≤ 预算档（D18，config 人工确认，1500 起 / 3000）。封差价差不做裸卖，亏损天然封顶，#8 只校它是否
落在可承受预算内。

设计（§6.4 判定权归逻辑单元、方案A 同 #6/#7）：c8 自己判，直接读 D18 BudgetTier 的预算档（tier）——
  D18 的 check_budget / check_structure 降为便捷预览、不进策略链路；#8 【无 ThresholdSet 阈值字段】
  （预算档本身即 D18 config 权威源，直接比绝对额、无比例），故 evaluate 不吃 th。
最大亏损额（max_loss）由上游【选腿装配】按 §三 (W−R)×乘数 算好后传入——D18.max_loss_cap 是该折算的
  纯函数实现（W=短腿/保护腿行权价差、R=卖腿−保护腿权利金、乘数=D11）；c8 只做判定、不重复折算（DRY）。

四态：预算档未配置(D18 tier 待人工确认)/最大亏损额缺失→MISSING；> 预算档→FAIL；≤→PASS（BLOCKING，硬否决）。
needs_live=False（开盘/休市都可判）：最大亏损依赖真实价 W/R，盘后可用日线 close 回退（同 #5/#7）。

运行自检：``python -m sell_option.checks.c8_budget``（加载真实 config.BUDGET_TIER 展示当前配置态）
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

try:  # 包方式（python -m / import）：相对导入
    from .models import CheckResult, Verdict
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import CheckResult, Verdict

if TYPE_CHECKING:
    from ..fetchers.d18_budget import BudgetTier


RULE_ID = "#8"
RULE_NAME = "封顶预算"
BLOCKING = True                   # 硬否决：单笔最大亏损 > 预算档（FAIL）即淘汰


def _to_float(value) -> float | None:
    """安全转 float：None/空串/非数/bool→None（不猜值）。同 c7/d23 惯例。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def evaluate(max_loss, budget_tier: "BudgetTier | None") -> CheckResult:
    """判单笔最大亏损是否 ≤ 预算档（§7.1 #8，BLOCKING，c8 自己判、方案A）。

    参数（均为已备好的数据层产物/标量，纯函数、不取数）：
      max_loss：单笔最大亏损额（元/组），上游按 §三 (W−R)×乘数 算好（D18.max_loss_cap）；
                None/非数→MISSING（数据缺失，不冒充）。
      budget_tier：D18 BudgetTier；读其 .tier（预算档金额）/.configured（是否人工确认）。
                   None 或 configured=False → 预算档未配置 → MISSING。

    四态：预算档未配置 / 最大亏损缺失 → MISSING；max_loss > tier → FAIL（硬否决，亏损封顶不可控）；
          ≤ tier → PASS（含边界相等，余量入 reason）。"""
    tier = _to_float(getattr(budget_tier, "tier", None)) if budget_tier is not None else None
    configured = bool(getattr(budget_tier, "configured", False)) if budget_tier is not None else False
    th_snap = {"budget_tier": tier, "configured": configured}
    ml = _to_float(max_loss)

    if not configured:
        verdict = Verdict.MISSING
        reason = "预算档未配置（D18 档位数值待人工确认）→ #8 封顶预算不可判"
    elif ml is None:
        verdict = Verdict.MISSING
        reason = "单笔最大亏损额缺失（W/R/乘数不全，见 §三 公式）→ #8 无法判定"
    elif ml > tier:
        verdict = Verdict.FAIL
        reason = f"单笔最大亏损 {ml:,.0f} > 预算档 {tier:,.0f} → #8 否决（亏损封顶不可控）"
    else:
        verdict = Verdict.PASS
        reason = f"单笔最大亏损 {ml:,.0f} ≤ 预算档 {tier:,.0f}（余量 {tier - ml:,.0f}）→ #8 通过"

    detail = {**th_snap, "max_loss": ml}
    return CheckResult(RULE_ID, RULE_NAME, verdict, reason, BLOCKING, detail)


# ---------------------------------------------------------------------------
# 实测：加载真实 D18 预算档配置，展示当前状态
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d18_budget import fetch_d18, max_loss_cap

    print("=" * 72)
    print(f"#8 封顶预算 · BLOCKING={BLOCKING}")

    bt = fetch_d18()
    if bt.configured:
        print(f"  D18 预算档 = {bt.tier:,.0f} 元/笔 · 候选档 = {bt.options}")
        for ml in (800, 1200, 1500, 2000):
            r = evaluate(ml, bt)
            print(f"  最大亏损 {ml:>6,} → #8 {r.verdict.value}（{r.reason}）")
    else:
        print(f"  D18 预算档 = 未配置 → #8 封顶预算将判 MISSING")
        print(f"  候选档 = {bt.options}")
        print(f"  启用：$env:SELLOPT_BUDGET_TIER=\"1500\"（或改 config.py 默认值）后重跑即生效")

    print(f"\n[折算示例] max_loss_cap(W=200, R=60, 乘数=10) = {max_loss_cap(200, 60, 10):,.0f} 元")
    print("=" * 72)
