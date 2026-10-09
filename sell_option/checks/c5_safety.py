"""#5 安全垫（§7.1）逻辑单元：预期移动 < 现价→保护腿 缓冲（|保护腿−现价|）——该缓冲 ≥ N σ 才够安全（硬否决）。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元，吃【数据层产物 + 选腿/装配标量】+ 阈值 → 判四态：
- 数据层产物：D08 `SpotQuote`（候选标的月真实合约现价·绝对价）+ D13 `IvVerdict`（主力月 ATM IV 现值）；
- 装配标量：`dte`（到期天数，D06 到期日 → D07 候选月派生）、`prot_strike`（保护腿行权价，§7.2/§6.3D 选腿结果；#5 新口径量 spot→保护腿 缓冲）
  ——这两者是【结构选择/选腿阶段】的产物，不是数据层取数项，故以标量入参（逻辑单元对"从哪算来"无感知）。
不取数、不读 config、不依赖策略聚合层——可单独 import、离线单元测试。

需求源 §7.1 #5 + §6.3D 选腿 + §6.3E IV 源：
- **判什么**：预期移动（1σ）＝ 现价 × IV × √(DTE/365)；现价→保护腿 缓冲 ＝ |保护腿行权价 − 现价|；
  安全垫 σ 倍数 ＝ 缓冲 / 预期移动；σ ≥ `safety_sigma`(1.0) 才够安全。
- **阈值**（从注入 ThresholdSet 读，✔ 已确认、无〔待〕）：`safety_sigma`=1.0。
- **IV 口径（§6.3E）**：候选月逐月 IV 走 openvlab chart/light（连通/覆盖实现期待验证）；**当前回退 D13 主力月
  ATM IV（`atmv_current`，百分比）近似**，detail 标注 `iv_source=主力月ATM近似`。atmv_current 是百分数（18.5→0.185）。
- **四态**（§八）：数据齐全且 σ ≥ 门槛 → **PASS**；齐全但 σ < 门槛 → **FAIL**（硬否决，安全垫不足）；
  现价 / IV / DTE / 保护腿行权价任一缺失或异常 → **MISSING**（不猜、不冒充）。无 WARN（安全垫是硬门槛）。
- **BLOCKING=True**：FAIL（安全垫不足）即淘汰该候选。
- **ATR 仅对账**：§7.1 注明 ATR 不参与判定（报告层对照用），本单元不纳入。

运行自检：``python -m sell_option.checks.c5_safety``（接真实数据链 D06→D07→D08→D13 取
SpotQuote/IvVerdict，展示全品种 ATM 期权安全垫 σ 倍数判定）
"""
from __future__ import annotations

import math
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

if TYPE_CHECKING:  # 仅类型标注用；运行时靠鸭子类型读产物字段（不在包加载期 import 数据层重模块）
    from ..fetchers.d08_spot_price import SpotQuote
    from ..fetchers.d13_iv_snapshot import IvVerdict

# 规则元数据（§7.1 #5；聚合层汇总用）
RULE_ID = "#5"
RULE_NAME = "安全垫"
BLOCKING = True                   # 硬否决：安全垫不足（FAIL）即淘汰该候选

_IV_SOURCE_LABEL = "主力月ATM近似(§6.3E)"   # 当前 IV 口径标注（候选月 chart/light 未落地）


# ---------------------------------------------------------------------------
# 计算纯函数（离线可测）
# ---------------------------------------------------------------------------
def _iv_decimal(iv) -> float | None:
    """从 D13 IV 观测取年化波动率【小数】：atmv_current 是百分数（18.5 → 0.185）。缺失/非正→None。"""
    pct = getattr(iv, "atmv_current", None) if iv is not None else None
    if pct is None:
        return None
    try:
        v = float(pct)
    except (TypeError, ValueError):
        return None
    return v / 100.0 if v > 0 else None


def _expected_move(price: float, iv_dec: float, dte: int) -> float:
    """1σ 预期移动 ＝ 现价 × IV(小数) × √(DTE/365)。"""
    return price * iv_dec * math.sqrt(dte / 365.0)


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝D08 现价 + D13 IV + DTE + 短腿行权价）
# ---------------------------------------------------------------------------
def evaluate(spot: "SpotQuote | None", iv: "IvVerdict | None", dte: int | None,
             prot_strike: float | None, th: ThresholdSet) -> CheckResult:
    """#5 安全垫（新口径·spot→保护腿 缓冲）：|保护腿−现价| ÷ 预期移动(1σ) ≥ safety_sigma？

    spot：D08 候选标的月真实合约现价（取 price）；iv：D13 主力月 ATM IV（取 atmv_current，近似）；
    dte：到期天数；prot_strike：保护腿(买入腿)行权价（选腿结果，垂直价差最大亏损边界）；
    th：注入阈值集（读 safety_sigma）。对平值短腿（短腿=现价）这等于价差宽度，即真实的抗移动缓冲。"""
    th_snap = {"safety_sigma": th.safety_sigma, "iv_source": _IV_SOURCE_LABEL}

    # ---- 逐项取输入，任一缺失/异常 → MISSING（不猜、不冒充）----
    price = getattr(spot, "price", None) if spot is not None else None
    if price is None or price <= 0 or getattr(spot, "state", None) == Verdict.MISSING:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           "现价缺失（D08 候选标的月真实合约未取到）→ #5 不可判", BLOCKING, th_snap)

    iv_pct = getattr(iv, "atmv_current", None) if iv is not None else None
    iv_dec = _iv_decimal(iv)
    if iv_dec is None:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           "IV 缺失（D13 主力月 ATM IV 未取到；§6.3E 候选月 IV 未落地）→ #5 不可判",
                           BLOCKING, {**th_snap, "price": price, "iv_pct": iv_pct})

    if dte is None or dte <= 0:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"DTE 缺失/非正（{dte}，D06 到期日未取到）→ #5 不可判", BLOCKING,
                           {**th_snap, "price": price, "iv_pct": iv_pct})

    if prot_strike is None:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           "保护腿行权价缺失（§7.2 选腿未产出）→ #5 不可判", BLOCKING,
                           {**th_snap, "price": price, "iv_pct": iv_pct, "dte": dte})

    # ---- 计算安全垫（新口径：现价 → 保护腿 的缓冲，即垂直价差最大亏损边界距离）----
    exp_move = _expected_move(price, iv_dec, dte)
    prot_buffer = abs(prot_strike - price)
    if exp_move <= 0:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"预期移动≤0（{exp_move:.2f}，IV/DTE 异常）→ #5 不可判", BLOCKING,
                           {**th_snap, "price": price, "iv_pct": iv_pct, "dte": dte})
    sigma = prot_buffer / exp_move

    detail = {**th_snap, "price": price, "iv_pct": iv_pct, "iv_dec": iv_dec, "dte": dte,
              "prot_strike": prot_strike, "expected_move": exp_move,
              "prot_buffer": prot_buffer, "sigma_multiple": sigma}

    if sigma >= th.safety_sigma:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.PASS,
                           f"安全垫 {sigma:.2f}σ ≥ {th.safety_sigma:.1f}σ（保护腿缓冲 {prot_buffer:.1f} "
                           f"≥ 预期移动 {exp_move:.1f}；IV {iv_pct:.1f}% 主力月近似）", BLOCKING, detail)
    return CheckResult(RULE_ID, RULE_NAME, Verdict.FAIL,
                       f"安全垫 {sigma:.2f}σ < {th.safety_sigma:.1f}σ（保护腿缓冲 {prot_buffer:.1f} "
                       f"< 预期移动 {exp_move:.1f}，硬否决；IV {iv_pct:.1f}% 主力月近似）", BLOCKING, detail)


# ---------------------------------------------------------------------------
# 真实数据自检：接 D06→D07→D08→D13 全链，展示全品种 ATM 期权安全垫 σ 倍数判定
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
    from sell_option.fetchers.d13_iv_snapshot import fetch_d13 as fetch_iv
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
    iv_set = fetch_iv(varieties, today, verbose=False)

    print("=" * 72)
    print(f"#5 安全垫 · 阈值 safety_sigma={th.safety_sigma} · BLOCKING={BLOCKING}")
    print(f"{'品种':<4} {'现价':>8} {'保护腿K':>8} {'DTE':>4} {'IV%':>6} {'σ倍数':>6}  判定")
    print("-" * 70)
    for code, exchange in varieties[:15]:  # 展示前 15 品种
        ms = cand.months_for(code)
        if not ms:
            continue
        month = ms[0]
        spot_price = spots.price_of(code, month.month4)
        if spot_price is None:
            continue
        iv = iv_set.get(code)
        if iv is None or iv.atmv_current is None:
            continue
        dte = month.dte
        if dte is None or dte <= 0:
            continue
        # 约 3% OTM Put 短腿 + 下方 4 档(40点)保护腿（新口径：#5 量 spot→保护腿 缓冲）
        short_k = round(spot_price * 0.97 / 10) * 10
        prot_k = short_k - 40
        r = evaluate(spots.get(code, month.month4), iv, dte, prot_k, th)
        spot_s = f"{spot_price:g}"
        strike_s = f"{prot_k:g}"
        iv_s = f"{iv.atmv_current:.1f}" if iv.atmv_current else "N/A"
        sigma_s = f"{r.detail.get('sigma_multiple', 0):.2f}" if r.detail.get("sigma_multiple") is not None else "N/A"
        print(f"{code:<4} {spot_s:>8} {strike_s:>8} {dte:>4} {iv_s:>6} {sigma_s:>6}  {r.verdict.value}")
    print("=" * 72)
