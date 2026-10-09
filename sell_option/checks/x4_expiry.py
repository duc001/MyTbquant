"""X4 到期（§7.3）逻辑单元：距到期 ≤3 天必平 / ≤5 天提醒——DTE 两级阈值 → 平仓 / 提醒移仓。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元，**吃数据层产物**（D16 `Position`）＋ 阈值集 →
判四态 CheckResult。判定权归本单元、读 `th.expiry_must_dte / expiry_warn_dte` 作权威阈值（方案A；D16.expiry_alerts
缺省参也已改读 ThresholdSet、旧 D16_* env 常量已删）。DTE 由 `Position.current_dte(today)` 从持仓入库的【到期日 expiry_date】现算。

需求源 §7.3『到期 ≤5 提醒、≤3 必平』（阈值已入 ThresholdSet：expiry_warn_dte=5 / expiry_must_dte=3）：
- **当前 DTE**＝`(expiry_date − today).days` 现算（到期日为不变量、随 D16 入库）；缺 expiry_date → DTE 不可知。
- **两级到期**：DTE ≤ `expiry_must_dte`(3) → **FAIL 必平**（临近交割、gamma/指派风险，强制平仓）；
  `expiry_must_dte` < DTE ≤ `expiry_warn_dte`(5) → **WARN 提醒**（建议移仓/平仓、不强制）；DTE > 5 → **PASS 续持**。
- **四态**：DTE 不可知（缺 expiry_date）→ **MISSING**（不冒充到期判定）；DTE≤3 → FAIL；≤5 → WARN；否则 PASS。
- **BLOCKING=True**：到期必平（FAIL）须真正驱动「平仓」结论（提醒 WARN 不受阻断影响、照常呈现）。
- 交割月/临近到期不并入"事件"（§7.3）：本单元是到期兜底；事件/长假只提示不强平、由报告 D 块汇总（非平仓检查项）。

运行自检：``python -m sell_option.checks.x4_expiry``（接真实数据链从 positions.db 取 D16 持仓，展示到期必平/提醒/续持判定）
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

try:  # 包方式（python -m / import）：相对导入
    from .models import CheckResult, Verdict
    from ..thresholds import ThresholdSet
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import CheckResult, Verdict
    from sell_option.thresholds import ThresholdSet

if TYPE_CHECKING:  # 数据层产物类型仅标注用（逻辑单元层依赖数据层，运行时鸭子类型读字段）
    from ..fetchers.d16_positions import Position

# 规则元数据（§7.3 到期；聚合层汇总用）
RULE_ID = "X4"
RULE_NAME = "到期"
BLOCKING = True                   # 到期必平须驱动「平仓」（非阻断会被降级 WARN、丢失平仓信号）


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝D16 Position + 判定基准日）
# ---------------------------------------------------------------------------
def evaluate(position: "Position | None", today: "dt.date | None", th: ThresholdSet) -> CheckResult:
    """X4 到期：当前 DTE 是否达 提醒(expiry_warn_dte) / 必平(expiry_must_dte)？

    position：D16 持仓（取 current_dte(today)）；today：判定基准日（None→date.today()）；
    th：注入阈值集（读 expiry_warn_dte / expiry_must_dte，权威阈值）。
    四态：DTE 不可知 → MISSING；DTE ≤ must → FAIL（必平）；≤ warn → WARN（提醒）；否则 → PASS（续持）。"""
    th_snap = {"expiry_warn_dte": th.expiry_warn_dte, "expiry_must_dte": th.expiry_must_dte}
    if position is None:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING, "持仓缺失 → X4 到期判不出", BLOCKING, th_snap)

    base = today or dt.date.today()
    dte = position.current_dte(base)
    if dte is None:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           "当前 DTE 不可知（持仓缺 expiry_date）→ X4 判不出（不冒充到期）",
                           BLOCKING, th_snap)

    detail = {**th_snap, "dte": dte, "today": base.isoformat()}
    if dte <= th.expiry_must_dte:                    # ≤3 必平（含已到期 DTE≤0）
        detail["disposition"] = "到期必平"
        edge = "（已到期/交割）" if dte <= 0 else ""
        return CheckResult(RULE_ID, RULE_NAME, Verdict.FAIL,
                           f"距到期 {dte} 天 ≤ 必平线 {th.expiry_must_dte}{edge} → X4 强制平仓（§7.3）",
                           BLOCKING, detail)
    if dte <= th.expiry_warn_dte:                    # ≤5 提醒（> must）
        detail["disposition"] = "到期提醒"
        return CheckResult(RULE_ID, RULE_NAME, Verdict.WARN,
                           f"距到期 {dte} 天 ≤ 提醒线 {th.expiry_warn_dte}（未达必平 {th.expiry_must_dte}）"
                           f" → X4 提醒移仓/平仓", BLOCKING, detail)
    detail["disposition"] = "续持"
    return CheckResult(RULE_ID, RULE_NAME, Verdict.PASS,
                       f"距到期 {dte} 天 > 提醒线 {th.expiry_warn_dte} → X4 续持", BLOCKING, detail)


# ---------------------------------------------------------------------------
# 真实数据自检：从 positions.db 加载 D16 持仓，展示到期必平/提醒/续持判定
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d16_positions import fetch_d16
    from sell_option.thresholds import load_thresholds

    today = __import__("datetime").date.today()
    th = load_thresholds()
    book = fetch_d16()

    print("=" * 72)
    print(f"X4 到期 · today={today} · ≤{th.expiry_must_dte}必平 / ≤{th.expiry_warn_dte}提醒 · BLOCKING={BLOCKING}")
    if not book.loaded or not book.positions:
        print(f"  持仓未加载或为空 → 无法判定")
    else:
        for pos in book.positions:
            r = evaluate(pos, today, th)
            dte = r.detail.get("dte", "?")
            disp = r.detail.get("disposition", "?")
            print(f"  {pos.code} {pos.structure} DTE={dte} → {r.verdict.value} {disp}")
    print("=" * 72)
