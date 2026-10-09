"""#7 保证金（§7.1）逻辑单元：单笔占用 ≤1万 且（存量+新仓）合计 ≤100%×资金池（硬否决）。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元。判定入口 `evaluate` **只吃【已算好的占用额标量】**
＋ D17 `CapitalPool`（资金池金额）＋ 阈值集 → 判四态 CheckResult（与 c1–c6 一致：判定权归逻辑单元、
读 `th` 作权威阈值；D17 的 `check_single_margin`/`check_open_margin` 降为便捷预览、**不进策略链路**——
沿用 #6 事件窗口【方案A】先例）。
另附 `estimate_candidate_margin()` **共享估算器**（纯函数）：串 D04×D05×D11×现价 → 拟开新仓占用额，
**由装配/上游调用**（不进 evaluate），算好喂给 `OpeningData.margin_single`。

需求源 §7.1 #7 + §6.1 D04/D05/D11/D16/D17：
- **判什么**：
  · 单笔：拟开新仓保证金占用 ≤ `margin_per_trade`（1 万）；
  · 合计：存量持仓占用（D16 各持仓 `margin` 之和）＋ 拟开新仓占用 ≤ `margin_pool_ratio`（100%）× 资金池（D17 total）。
- **占用额估算制**（tbpy 无保证金字段，标注『估算』）：单腿占用＝现价×乘数(D11)×保证金率(D04)×手数；
  组合占用＝单腿占用×组合优惠系数(D05)。
- **资金池只在开仓决策时控制**（用户口径）：超限只否决【拟开的这笔新仓】、**绝不因占用超限强平存量持仓**、
  不用于 §7.3 平仓扫描 / 盘后监控（本工具只读决策支持、不接账户不自动交易）。
- **阈值权威源＝th**（`margin_per_trade` / `margin_pool_ratio`）；`pool` 只提供资金池【金额】（`.total`/`.configured`）。
  D17 的 env 比例与 th 同值冗余，本单元统一读 th（将来 D17 重构收敛后仍一致）。
- **四态**（§八）：单笔或合计超限 → **FAIL**（硬否决）；资金池未配置 / 占用额缺失 → **MISSING**（不猜、不冒充）；
  均未超 → **PASS**。无 WARN（保证金是硬门槛）。
- **BLOCKING=True**：FAIL（保证金超限）即淘汰该候选。
- **needs_live=False**（装配层标记）：占用额估算依赖现价，但现价可回退日线 close（同 #5 安全垫），盘后粗筛也能估。

运行自检：``python -m sell_option.checks.c7_margin``（加载真实 D04/D05/D11 表 + 资金池配置演示保证金估算）
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

try:  # 包方式（python -m / import）：相对导入
    from .models import CheckResult, Verdict, merge_verdicts
    from ..thresholds import ThresholdSet
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import CheckResult, Verdict, merge_verdicts
    from sell_option.thresholds import ThresholdSet

if TYPE_CHECKING:  # 仅类型标注用；运行时靠鸭子类型读产物字段（不在包加载期 import 数据层重模块）
    from ..fetchers.d17_capital_pool import CapitalPool

# 规则元数据（§7.1 #7；聚合层汇总用）
RULE_ID = "#7"
RULE_NAME = "保证金"
BLOCKING = True                   # 硬否决：单笔或合计占用超限（FAIL）即淘汰该候选


# ---------------------------------------------------------------------------
# 纯函数工具（离线可测）
# ---------------------------------------------------------------------------
def _to_float(value) -> float | None:
    """安全转 float：None/空串/非数/bool→None（不猜值）；占用额允许 0（无存量持仓）。"""
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


# ---------------------------------------------------------------------------
# 共享估算器（由装配/上游调用；不进 evaluate）
# ---------------------------------------------------------------------------
def estimate_candidate_margin(spot, code: str, exchange: str, *, d04, d05, d11,
                              volume: int = 1) -> float | None:
    """估算【拟开新仓】保证金占用额（元）＝现价 × 乘数 × 保证金率 × 手数 × 组合优惠系数。

    串数据层估算原语：D11（乘数）→ D04（单腿占用＝现价×乘数×率×手数）→ D05（×组合优惠系数）。
    **由装配/上游调用**（#7 上游算好占用额，evaluate 只判定），结果标注『估算』（tbpy 无保证金字段）。
    任一原料缺失/非正/品种或交易所不在表内 → **None**（诚实降级，绝不猜值冒充）。

    spot：标的现价（D08 tick / 日线 close）；code：品种；exchange：交易所（D05 系数按所）；
    volume：手数（默认 1）；d04/d05/d11：数据层估算原语表（MarginRateTable/ComboDiscountTable/MultiplierSet）。"""
    price = _to_float(spot)
    if price is None or price <= 0:
        return None
    if not isinstance(volume, int) or volume <= 0:
        return None
    try:
        mult = d11.contract_unit(code)               # D11 乘数（缺品种→KeyError）
        single = d04.estimate_leg_margin(price, code, mult, volume)   # D04 单腿占用
        return d05.estimate_combined(single, exchange)                # D05 组合优惠
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝已算好的占用额标量 + 资金池）
# ---------------------------------------------------------------------------
def evaluate(single_margin, existing_occupied, pool: "CapitalPool | None", th: ThresholdSet) -> CheckResult:
    """#7 保证金：单笔占用 ≤ margin_per_trade 且（存量＋新仓）合计 ≤ margin_pool_ratio × 资金池？

    single_margin：拟开新仓单笔保证金占用额（`estimate_candidate_margin` 产物）；
    existing_occupied：存量持仓占用合计（D16 `PositionBook.total_margin_occupied()`）；
    pool：D17 `CapitalPool`（只读 `.total` 资金池金额 / `.configured`）；th：注入阈值集（读两阈值）。
    单笔判定独立于资金池；合计判定需 资金池已配置 + 存量 + 新仓 三者齐全，任一缺 → 该子项 MISSING。
    两子项按 FAIL>MISSING>WARN>PASS 合并（`merge_verdicts`）。"""
    per_cap = th.margin_per_trade
    ratio = th.margin_pool_ratio
    pool_total = _to_float(getattr(pool, "total", None)) if pool is not None else None
    configured = bool(getattr(pool, "configured", False)) if pool is not None else False
    th_snap = {"per_trade_cap": per_cap, "pool_ratio": ratio, "pool_total": pool_total,
               "pool_configured": configured, "estimated": True}

    sm = _to_float(single_margin)
    ex = _to_float(existing_occupied)

    # ---- 单笔判定（独立于资金池：只看拟开这一笔占用）----
    if sm is None:
        single_v = Verdict.MISSING
        single_why = "拟开新仓保证金占用额缺失 → #7 单笔不可判"
    elif sm > per_cap:
        single_v = Verdict.FAIL
        single_why = f"单笔保证金 {sm:,.0f} > 上限 {per_cap:,.0f}（估算）→ 否决"
    else:
        single_v = Verdict.PASS
        single_why = f"单笔保证金 {sm:,.0f} ≤ 上限 {per_cap:,.0f}（估算）"

    # ---- 合计判定（存量＋新仓 ≤ ratio×资金池；需资金池已配置 + 两占用额齐全）----
    total_occ = limit = None
    if not configured:
        total_v = Verdict.MISSING
        total_why = "资金池未配置（D17）→ #7 合计不可判"
    elif sm is None or ex is None:
        total_v = Verdict.MISSING
        miss = "拟开新仓占用" if sm is None else "存量持仓占用（D16 margin）"
        total_why = f"{miss}缺失 → #7 合计不可判"
    else:
        total_occ = ex + sm
        limit = pool_total * ratio
        if total_occ > limit:
            total_v = Verdict.FAIL
            total_why = (f"合计占用 {total_occ:,.0f}（存量 {ex:,.0f}＋新仓 {sm:,.0f}）> 上限 "
                         f"{limit:,.0f}（{ratio:.0%}×资金池 {pool_total:,.0f}）→ 否决该新仓")
        else:
            total_v = Verdict.PASS
            total_why = (f"合计占用 {total_occ:,.0f}（存量 {ex:,.0f}＋新仓 {sm:,.0f}）≤ 上限 "
                         f"{limit:,.0f}（{ratio:.0%}×资金池，余量 {limit - total_occ:,.0f}）")

    verdict = merge_verdicts([single_v, total_v])
    detail = {**th_snap, "single_margin": sm, "existing_occupied": ex,
              "total_occupied": total_occ, "limit": limit,
              "single_verdict": single_v.value, "total_verdict": total_v.value}
    reason = f"单笔：{single_why}；合计：{total_why}"
    return CheckResult(RULE_ID, RULE_NAME, verdict, reason, BLOCKING, detail)


# ---------------------------------------------------------------------------
# 实测：加载真实 D04/D05/D11 表 + 资金池配置，演示保证金估算
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d04_margin_rate import fetch_d04
    from sell_option.fetchers.d05_combo_discount import fetch_d05
    from sell_option.fetchers.d11_contract_multiplier import fetch_d11
    from sell_option.fetchers.d17_capital_pool import fetch_d17
    from sell_option.thresholds import load_thresholds

    th = load_thresholds()
    pool = fetch_d17()

    print("=" * 72)
    print(f"#7 保证金 · 单笔≤{th.margin_per_trade:,.0f} · 合计≤{th.margin_pool_ratio:.0%}×资金池 · BLOCKING={BLOCKING}")

    d04 = fetch_d04()
    d05 = fetch_d05()
    d11 = fetch_d11()
    print(f"  D04 保证金率表：{len(d04._data) if hasattr(d04, '_data') else '已加载'}")
    print(f"  D05 组合优惠表：{len(d05._data) if hasattr(d05, '_data') else '已加载'}")
    print(f"  D11 乘数表：已加载")
    print(f"  D17 资金池：total={pool.total:,.0f}" if pool.configured else "  D17 资金池：未配置")

    def _est(code, exchange, spot):
        est = estimate_candidate_margin(spot, code, exchange, d04=d04, d05=d05, d11=d11)
        if est is not None:
            r = evaluate(est, 0, pool, th)
            print(f"  {code:<3} 现价{spot:>10,.0f} → 估算占用 {est:>10,.0f} 元 · #7 {r.verdict.value}")
        else:
            print(f"  {code:<3} 现价{spot:>10,.0f} → 估算不出（品种/交易所不在表）")

    print(f"\n[候选品种保证金估算]（存量=0，新仓 1 手）")
    _est("m", "DCE", 3000.0)
    _est("rb", "SHFE", 3100.0)
    _est("AP", "CZCE", 7500.0)
    _est("lc", "GFEX", 150000.0)
    print("=" * 72)
