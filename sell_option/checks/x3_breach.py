"""X3 破位（§7.3）逻辑单元：现价朝不利方向侵蚀到距短腿行权价仅破位档 bp 的安全垫线（保垫侵蚀）→ 强制平仓（价格型止损，与 X2 盈亏型互补）。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元，**吃数据层产物**（D16 `Position` + D08 `SpotQuote`）
＋ 阈值集 → 判四态 CheckResult。判定权归本单元、读 `th.breach_pct(code)` 作权威阈值（方案A；D16 不含破位判定）。

需求源 §7.3『破位止损 · 破位档：C 7%、M·SR 5% · 〔待〕其余品种』（阈值已入 ThresholdSet：breach_default=0.07 /
breach_m=0.05 / breach_sr=0.05，`th.breach_pct(code)` 按品种取值、未列走 default）：
- **破位对照价＝持仓期权所属标的月真实合约现价（D08），不是主力月**（§7.3 串价铁律）——ClosingData.spot 须为该持仓
  `underlying_month` 的真实合约 SpotQuote（由平仓运行编排按持仓标的月取数、精确对齐，绝不用主力月价冒充）。
- **破位口径（保垫侵蚀·已定稿）**：以短腿行权价 K 为参照、现价到 K 的距离为安全垫，朝不利方向被侵蚀到距 K 不足 bp（品种档值）即破位——**未越 K 先平**：
  · 卖 PUT（single_put，怕跌）：现价 ≤ K×(1+bp) → **破位**；
  · 卖 CALL（single_call，怕涨）：现价 ≥ K×(1−bp) → **破位**。
  档线设在虚值侧的理由：若按「已越过 K 再达 bp」口径，10% 虚值短腿的触发点已落在封顶亏损区（X2 2× 止损早该打出），X3 永远空转；保垫口径下档值（7%/5%）＝该品种最小安全垫，越线即出、先于穿 K，与 X2 盈亏型止损真正互补。
- **四态**：破位对照价缺失（spot None / price None / state≠PASS）或短腿 K 缺失 → **MISSING**（不冒充破位判定）；
  iron_condor 无需单独口径（§7.3：铁鹰落库拆成 single_put＋single_call 两笔、各自走本单腿判定）；若表内仍出现
  iron_condor/未知结构行＝录入异常 → **MISSING**（不误判、不猜）；越线 → **FAIL 平仓**；未越线 → **PASS 续持**。无 WARN（破位是硬线）。
- **BLOCKING=True**：破位（FAIL）须真正驱动「平仓」结论。

运行自检：``python -m sell_option.checks.x3_breach``（接真实数据链从 positions.db 取 D16 持仓 + D08 现价，展示已卖持仓破位判定）
"""
from __future__ import annotations

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
    from ..fetchers.d08_spot_price import SpotQuote

# 规则元数据（§7.3 破位；聚合层汇总用）
RULE_ID = "X3"
RULE_NAME = "破位"
BLOCKING = True                   # 破位须驱动「平仓」（非阻断会被降级 WARN、丢失平仓信号）


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝D16 Position + D08 SpotQuote）
# ---------------------------------------------------------------------------
def evaluate(position: "Position | None", spot: "SpotQuote | None", th: ThresholdSet) -> CheckResult:
    """X3 破位（保垫侵蚀）：现价是否朝不利方向侵蚀到距短腿 K 仅破位档 bp 的安全垫线？

    position：D16 持仓（取 short_leg.strike=K、structure 定向、code 取破位档）；
    spot：D08 所属标的月真实合约现价（破位对照价，非主力月）；th：注入阈值集（breach_pct(code) 权威）。
    四态：对照价/短腿K缺失 → MISSING；iron_condor/未知结构行（铁鹰应拆两笔单腿录入）→ MISSING；越线 → FAIL（平仓）；未越线 → PASS（续持）。"""
    if position is None:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING, "持仓缺失 → X3 破位判不出", BLOCKING, {})
    bp = th.breach_pct(position.code)
    k = position.short_leg.strike if position.short_leg else None
    if k is None or k <= 0:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           "短腿行权价缺失 → X3 破位判不出（不冒充）", BLOCKING, {"breach_pct": bp})
    if spot is None or getattr(spot, "price", None) is None or getattr(spot, "state", None) != Verdict.PASS:
        why = "破位对照价缺失（无 D08 所属标的月真实合约现价）" if spot is None else f"现价不可用（{getattr(spot, 'reason', '')}）"
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"{why} → X3 破位判不出（绝不用主力月价冒充）", BLOCKING,
                           {"breach_pct": bp, "short_strike": k})

    struct = position.structure
    price = spot.price
    if struct == "single_put":                       # 卖 PUT：怕跌，安全垫被侵蚀到 K 上方 bp 线以内即破（未越 K）
        threshold, direction, breached = k * (1 + bp), "跌破", price <= k * (1 + bp)
    elif struct == "single_call":                    # 卖 CALL：怕涨，安全垫被侵蚀到 K 下方 bp 线以内即破（未越 K）
        threshold, direction, breached = k * (1 - bp), "涨破", price >= k * (1 - bp)
    else:                                            # 铁鹰应拆两笔单腿录入（§7.3）；iron_condor/未知结构＝录入异常，不猜
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"结构 {struct} 非单腿价差（铁鹰应拆两笔录入，§7.3）→ X3 判不出（不误判、不冒充）", BLOCKING,
                           {"breach_pct": bp, "short_strike": k, "structure": struct})

    detail = {"breach_pct": bp, "short_strike": k, "spot": price, "structure": struct,
              "threshold": threshold, "direction": direction}
    if breached:
        detail["disposition"] = "破位"
        return CheckResult(RULE_ID, RULE_NAME, Verdict.FAIL,
                           f"现价 {price:g} {direction}短腿 K={k:g} 的安全垫线（破位档 {bp:.0%}·阈值 {threshold:g}）"
                           f" → X3 平仓止损（§7.3）", BLOCKING, detail)
    detail["disposition"] = "续持"
    return CheckResult(RULE_ID, RULE_NAME, Verdict.PASS,
                       f"现价 {price:g} 未{direction}短腿 K={k:g} 的安全垫线（破位档 {bp:.0%}·阈值 {threshold:g}）→ X3 续持",
                       BLOCKING, detail)


# ---------------------------------------------------------------------------
# 真实数据自检：从 positions.db 加载 D16 持仓 + D08 现价，展示破位判定
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d16_positions import fetch_d16
    from sell_option.fetchers.d08_spot_price import fetch_d08
    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.d02_symbol_template import fetch_d02
    from sell_option.fetchers.d06_option_chain import fetch_d06, OptionIndexRepo
    from sell_option.fetchers.d07_candidate_months import fetch_d07
    from sell_option.thresholds import load_thresholds

    today = __import__("datetime").date.today()
    th = load_thresholds()
    book = fetch_d16()

    print("=" * 72)
    print(f"X3 破位 · M档={th.breach_pct('M'):.0%} C档={th.breach_pct('C'):.0%} · BLOCKING={BLOCKING}")
    if not book.loaded or not book.positions:
        print(f"  持仓未加载或为空 → 无法判定")
    else:
        varieties = [(v.code, v.exchange) for v in fetch_d01()]
        repo = OptionIndexRepo()
        repo.ensure_fresh(today, verbose=False)
        cs_all = fetch_d06(varieties, today, repo=repo, verbose=False)
        templates = fetch_d02()
        cand = fetch_d07(cs_all.all_contracts(), today, symbol_templates=templates)
        spots = fetch_d08(cand, today, verbose=False)
        for pos in book.positions:
            spot = spots.get(pos.code, pos.underlying_month)  # Position property：从卖腿 symbol 提取标的月（None→get 落空→MISSING）
            r = evaluate(pos, spot, th)
            print(f"  {pos.code} {pos.structure} K={pos.short_leg.strike} "
                  f"→ {r.verdict.value} {r.detail.get('disposition', '?')}")
    print("=" * 72)
