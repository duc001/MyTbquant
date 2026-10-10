"""#2 持仓量 OI（§7.1）逻辑单元：单合约 OI ≥ oi_min（默认 100，单一事实源 ThresholdSet.oi_min）——盘口够厚（仅提示，不淘汰）。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元，**直接吃数据层产物**
（D14 `OiSet` 合约级持仓量）+ 阈值集 → 判四态 CheckResult。
不取数、不读 config、不依赖策略聚合层——可单独 import、离线单元测试。

需求源 §7.1 #2（原"#1 流动性"按【价格维度=价差】/【数量维度=OI】拆分：价差留 #1 c1_spread，OI 独立为 #2）：
- **判什么**：单合约 OI ≥ `oi_min`(100)——盘口够厚、有足够对手盘进出。卖腿＋保护腿**分别查**。
- **数据源**：OI ← OiSet（D14，唯一源 tbpy get_history_n→position；新浪/openvlab 备源实测不可行已删除，见 §6.3 B）。
  OiSet 由 fetch_oi 对选腿后的各腿逐一产出（拿到→有 oi，tbpy 拿不到→MISSING·oi=None），
  故遍历 `OiSet.all_quotes()` 即覆盖该候选全部腿。
- **阈值**（从注入的 ThresholdSet 读，✔ 已确认、无〔待〕）：`oi_min`=100。
- **四态**（§八）：OI 拿不到（oi is None / state=MISSING）→ **MISSING**；有确切值 < 门槛 → **FAIL**；
  ≥ 门槛 → **PASS**。合并优先级 **FAIL > MISSING > PASS**。无 WARN。
- **BLOCKING=False**（用户拍板）：OI 是"厚度"提示而非硬门槛——FAIL（OI 不足）在聚合层 combine_results
  汇总时**降级为 WARN、不淘汰候选**；但 MISSING（拿不到 OI）不降级、仍如实待人工。

运行自检：``python -m sell_option.checks.c2_oi``（接真实数据链 D06→D07→D08→D14 取
OiSet，展示全品种 ATM 期权持仓量四态判定）
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
    from ..fetchers.d14_contract_oi import OiSet, OiQuote

# 规则元数据（§7.1 #2；聚合层汇总用）
RULE_ID = "#2"
RULE_NAME = "持仓量OI"
BLOCKING = False                  # 仅提示：OI 不足（FAIL）在汇总降级 WARN、不淘汰；MISSING 不降级


# ---------------------------------------------------------------------------
# 子项判定（纯函数，只读数据层产物字段）
# ---------------------------------------------------------------------------
def _oi_subcheck(oq: "OiQuote", th: ThresholdSet) -> tuple[Verdict, str, dict]:
    """单腿 OI 子项：OI ≥ oi_min？OI 拿不到（无值 / state=MISSING）→ MISSING；有值 <门槛 → FAIL；≥ → PASS。"""
    label = oq.role_label
    oi = getattr(oq, "oi", None)
    base = {"symbol": oq.symbol, "oi": oi, "oi_min": th.oi_min}
    if oi is None or getattr(oq, "state", Verdict.MISSING) == Verdict.MISSING:
        return (Verdict.MISSING,
                f"{label}OI 拿不到（唯一源 tbpy position 无值）→ 门槛不可判", base)
    if oi < th.oi_min:
        return Verdict.FAIL, f"{label}OI={oi} < {th.oi_min}（盘口过薄）", base
    return Verdict.PASS, f"{label}OI={oi} ≥ {th.oi_min}", base


def _reason(verdict: Verdict, subs: list[tuple[Verdict, str]]) -> str:
    """按最终四态组织人话结论：非 PASS 列出驱动子项；PASS 给正向摘要。"""
    if verdict == Verdict.PASS:
        return "各腿 OI ≥ 门槛 → #2 持仓量通过"
    suffix = {Verdict.FAIL: "→ #2 提示（盘口偏薄，非阻断不淘汰）",
              Verdict.MISSING: "→ #2 待人工（OI 拿不到，不冒充）",
              Verdict.WARN: "→ #2 提示"}.get(verdict, "")
    driving = [why for v, why in subs if v == verdict]
    return "；".join(driving) + f" {suffix}" if driving else f"#2 {verdict.value} {suffix}"


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝D14 OI 产物）
# ---------------------------------------------------------------------------
def evaluate(oi: "OiSet", th: ThresholdSet) -> CheckResult:
    """#2 持仓量 OI：对 OiSet 中每条腿跑 OI 门槛子项，按 FAIL>MISSING>PASS 合并。

    oi：D14 合约级 OI 产物（按腿提供 oi/state；缺失即该腿 MISSING）；
    th：注入的阈值集（读 oi_min）。非阻断：FAIL 在聚合层降级 WARN、不淘汰。"""
    th_snap = {"oi_min": th.oi_min}
    quotes = oi.all_quotes() if oi is not None else []
    if not quotes:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           "无 OI 数据（D14 未取到任何腿）→ #2 持仓量不可判", BLOCKING,
                           {"legs": [], "thresholds": th_snap})

    subs: list[tuple[Verdict, str]] = []
    leg_details: list[dict] = []
    for oq in quotes:
        ov, owhy, odetail = _oi_subcheck(oq, th)
        subs.append((ov, owhy))
        leg_details.append({"symbol": oq.symbol, "role": oq.role_label,
                            "oi": odetail, "verdict": ov.value})

    verdict = merge_verdicts(v for v, _ in subs)
    return CheckResult(RULE_ID, RULE_NAME, verdict, _reason(verdict, subs), BLOCKING,
                       {"legs": leg_details, "thresholds": th_snap})


# ---------------------------------------------------------------------------
# 真实数据自检：接 D06→D07→D08→D14 全链，展示全品种 ATM 期权持仓量（非阻断）四态判定
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
    from sell_option.fetchers.d14_contract_oi import fetch_oi
    from sell_option.fetchers.d06_option_chain import ChainContract
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
    oi = fetch_oi(atm, today, verbose=False)
    r = evaluate(oi, th)

    print("=" * 72)
    print(f"#2 持仓量 OI · 阈值 oi_min={th.oi_min} · BLOCKING={BLOCKING} · {r.verdict.value}")
    print(f"  {r.reason}")
    dist = oi.distribution()
    print(f"  分布 {dist}")
    print("=" * 72)
