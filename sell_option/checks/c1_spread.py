"""#1 盘口价差（§7.1）逻辑单元：买卖价差 ≤ 权利金×10%——盘口够窄（硬否决）。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元，**直接吃数据层产物**
（D09/D10 `PremiumSet` 盘口 bid/ask/last）+ 阈值集 → 判四态 CheckResult。
不取数、不读 config、不依赖策略聚合层——可单独 import、离线单元测试。

需求源 §7.1 #1（原"#1 流动性"按【价格维度=价差】/【数量维度=OI】拆分：价差留在 #1，OI 独立为 #2 c2_oi）：
- **判什么**：买卖价差比 = (ask−bid)/权利金基准 ≤ `spread_ratio_max`(10%)——盘口够窄、成交摩擦成本低。
  卖腿＋保护腿**分别查**，任一腿过宽即否决。
- **数据源**：盘口 bid/ask/last ← PremiumSet（D09/D10）。
- **阈值**（从注入的 ThresholdSet 读，✔ 已确认、无〔待〕）：`spread_ratio_max`=0.10。
- **价差比口径**：`spread = ask − bid`；基准优先 `last`（缺失回退 `mid=(bid+ask)/2`）；`ratio = spread / 基准`。
  这里的"权利金基准"只是把价差归一化的**尺子**（使不同价位合约可比），不衡量收益——收益/风险性价比是 #9 肥瘦。
- **四态**（§八）：休市 / 盘后无盘口（has_book=False 或 bid/ask 缺失，§11.3.5 日线无 bid/ask）→ **MISSING**；
  基准缺失 / 非正 → **MISSING**；任一腿 ratio > 10%（有确切盘口）→ **FAIL**；各腿全达标 → **PASS**。
  合并优先级 **FAIL > MISSING > PASS**（有确切违反即否决；否则有缺数则待人工）。
- **BLOCKING=True**：盘口过宽（FAIL）即淘汰该候选。

运行自检：``python -m sell_option.checks.c1_spread``（接真实数据链 D06→D07→D08→D09/D10 取
PremiumSet，展示全品种 ATM 期权盘口价差四态判定）
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

if TYPE_CHECKING:  # 仅类型标注用；运行时靠鸭子类型调用数据层产物方法（不在包加载期 import 数据层重模块）
    from ..fetchers.d09_d10_premium import PremiumSet, PremiumQuote

# 规则元数据（§7.1 #1；聚合层汇总用）
RULE_ID = "#1"
RULE_NAME = "盘口价差"
BLOCKING = True                   # 盘口过宽即淘汰


# ---------------------------------------------------------------------------
# 子项判定（纯函数，只读数据层产物字段）
# ---------------------------------------------------------------------------
def _spread_subcheck(pq: "PremiumQuote", th: ThresholdSet) -> tuple[Verdict, str, dict]:
    """单腿价差子项：价差比 = (ask−bid)/权利金基准 ≤ spread_ratio_max？

    无盘口（has_book=False / bid|ask 缺失，即休市盘后）→ MISSING；基准缺失/非正 → MISSING。"""
    label = pq.role_label
    base = {"symbol": pq.symbol, "bid": pq.bid, "ask": pq.ask, "last": pq.last,
            "spread_ratio_max": th.spread_ratio_max}
    if not pq.has_book or pq.bid is None or pq.ask is None:
        return (Verdict.MISSING,
                f"{label}无盘口（休市/盘后，D09/D10 日线无 bid/ask）→ 价差不可判",
                {**base, "spread": None, "ratio": None})
    spread = pq.ask - pq.bid
    ref = pq.last if pq.last is not None else (pq.bid + pq.ask) / 2.0
    if ref is None or ref <= 0:
        return (Verdict.MISSING,
                f"{label}权利金基准缺失/非正（last 与 mid 皆不可用）→ 价差比不可判",
                {**base, "spread": spread, "ratio": None})
    ratio = spread / ref
    detail = {**base, "spread": spread, "ref": ref, "ratio": ratio}
    if ratio > th.spread_ratio_max:
        return Verdict.FAIL, f"{label}价差比 {ratio:.1%} > {th.spread_ratio_max:.0%}（盘口过宽）", detail
    return Verdict.PASS, f"{label}价差比 {ratio:.1%} ≤ {th.spread_ratio_max:.0%}", detail


def _reason(verdict: Verdict, subs: list[tuple[Verdict, str]]) -> str:
    """按最终四态组织人话结论：非 PASS 列出驱动子项；PASS 给正向摘要。"""
    if verdict == Verdict.PASS:
        return "各腿盘口价差 ≤ 阈值 → #1 盘口价差通过"
    suffix = {Verdict.FAIL: "→ #1 否决（盘口过宽）",
              Verdict.MISSING: "→ #1 待人工（无盘口，不冒充）",
              Verdict.WARN: "→ #1 提示"}.get(verdict, "")
    driving = [why for v, why in subs if v == verdict]
    return "；".join(driving) + f" {suffix}" if driving else f"#1 {verdict.value} {suffix}"


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝D09/D10 盘口产物）
# ---------------------------------------------------------------------------
def evaluate(premium: "PremiumSet", th: ThresholdSet) -> CheckResult:
    """#1 盘口价差：对 PremiumSet 中每条腿跑价差子项，按 FAIL>MISSING>PASS 合并。

    premium：D09/D10 权利金产物（提供 bid/ask/last/has_book/role）；
    th：注入的阈值集（读 spread_ratio_max）。"""
    th_snap = {"spread_ratio_max": th.spread_ratio_max}
    quotes = premium.all_quotes() if premium is not None else []
    if not quotes:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           "无盘口/权利金数据（D09/D10 未取到）→ #1 盘口价差不可判", BLOCKING,
                           {"legs": [], "thresholds": th_snap})

    subs: list[tuple[Verdict, str]] = []
    leg_details: list[dict] = []
    for pq in quotes:
        sv, swhy, sdetail = _spread_subcheck(pq, th)
        subs.append((sv, swhy))
        leg_details.append({"symbol": pq.symbol, "role": pq.role_label,
                            "spread": sdetail, "verdict": sv.value})

    verdict = merge_verdicts(v for v, _ in subs)
    return CheckResult(RULE_ID, RULE_NAME, verdict, _reason(verdict, subs), BLOCKING,
                       {"legs": leg_details, "thresholds": th_snap})


# ---------------------------------------------------------------------------
# 真实数据自检：接 D06→D07→D08→D09/D10 全链，展示全品种 ATM 期权盘口价差四态判定
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.d02_symbol_template import fetch_d02
    from sell_option.fetchers.d06_option_chain import fetch_d06, OptionIndexRepo
    from sell_option.fetchers.d07_candidate_months import fetch_d07
    from sell_option.fetchers.d08_spot_price import fetch_d08
    from sell_option.fetchers.d09_d10_premium import fetch_premium
    from sell_option.thresholds import load_thresholds

    today = __import__("datetime").date.today()
    th = load_thresholds()
    varieties = [(v.code, v.exchange) for v in fetch_d01()]
    repo = OptionIndexRepo()
    repo.ensure_fresh(today, verbose=False)
    cs_all = fetch_d06(varieties, today, repo=repo, verbose=False)
    templates = fetch_d02()
    cand = fetch_d07(cs_all.all_contracts(), today, symbol_templates=templates)
    spots = fetch_d08(cand, today, verbose=False)

    # 每品种第一候选月 ATM Call
    from sell_option.fetchers.d06_option_chain import ChainContract
    atm: list[ChainContract] = []
    for code, exchange in varieties:
        ms = cand.months_for(code)
        if not ms:
            continue
        month = ms[0]
        spot = spots.price_of(code, month.month4)
        if spot is None:
            continue
        month_cs = [c for c in cs_all.all_contracts()
                    if c.code == code and c.underlying_month == month.month4 and c.cp == "C"]
        if not month_cs:
            continue
        strikes = sorted(set(c.strike for c in month_cs))
        atm_strike = min(strikes, key=lambda s: abs(s - spot))
        atm.extend(c for c in month_cs if c.strike == atm_strike)

    prem = fetch_premium(atm, today, verbose=False)
    r = evaluate(prem, th)

    print("=" * 72)
    print(f"#1 盘口价差 · 阈值 spread_ratio_max={th.spread_ratio_max:.0%} · {r.verdict.value}")
    print(f"  {r.reason}")
    if r.detail.get("legs"):
        for leg in r.detail["legs"][:10]:
            sym = leg["symbol"]
            sr = leg["spread"].get("ratio")
            sr_s = f"{sr:.1%}" if sr is not None else "N/A"
            print(f"  {sym:<24}  价差比={sr_s}")
    print("=" * 72)
