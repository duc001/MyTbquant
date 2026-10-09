"""#3 市况 regime（§7.1）逻辑单元：ADX(14) + 20 日斜率判震荡/趋势 → 映射结构建议（永不 FAIL）。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元，**直接吃数据层产物**
（D12 `IndexSeries` 后复权主连日线的原始 OHLC 序列）+ 阈值集 → 判四态 CheckResult。
D12 只忠实供原始日线（不含算好的指标），故 ADX / 斜率的**计算即判定逻辑的一部分，落在本单元**。
不取数、不读 config、不依赖策略聚合层——可单独 import、离线单元测试。

需求源 §7.1 #3 + §7.2 结构选择：
- **判什么**：震荡 = ADX < `adx_ranging`(25) **且** |20 日斜率| < `slope_ranging`(0.15%/日)；否则为趋势，
  方向由斜率符号定（>0 上涨、<0 下跌）。
- **数据源**：指数日线（D12 后复权主连，相对指标专用；§一 串价铁律——绝不当绝对价）。
- **阈值**（从注入的 ThresholdSet 读，均 ✔ 已确认、无〔待〕）：`adx_ranging`=25.0、`slope_ranging`=0.0015。
- **结构映射**（§7.2；字符串与 D16 持仓 schema `structure` 对齐）：
  震荡→`iron_condor`（双卖铁鹰）；上涨→`single_put`（单卖下方 PUT 价差）；下跌→`single_call`（单卖上方 CALL 价差）。
- **四态**（§八）：判出 regime → **PASS**（detail 带 structure 建议）；数据不足 / 算不出 / 方向不明 → **MISSING**。
  **#3 永不 FAIL、永不 WARN**（BLOCKING=False：只选结构＋建议，不淘汰候选）。ER/Hurst 为升级项（未纳入）。
- **附·软动能**：末尾【连 N 同向 K 线】作软描述（≥5 入 detail/conclusion，仅提示近期动能强度，绝不阻挡）；该单向动能
  形态原属 #10，现上移 #3 以避免与趋势判定语义重叠、更贴合"市况＝趋势"的职责。

指标算法（Wilder 标准，离线纯函数）：
- ADX(14)：TR/+DM/−DM → Wilder 平滑 → +DI/−DI → DX → ADX；首个稳定值需 ~2×period 根 bar。
- 20 日斜率：最近 20 根 close 对序号做最小二乘线性回归，斜率 ÷ 均价 → 归一化为"每日占比"（%/日）。

运行自检：``python -m sell_option.checks.c3_regime``（接真实数据链从 daily 缓存取 D12 指数日线
OHLC，展示全品种市况判定与结构建议）
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

if TYPE_CHECKING:  # 仅类型标注用；运行时靠鸭子类型读 IndexSeries 字段（不在包加载期 import 数据层重模块）
    from ..fetchers.d12_index_daily import IndexSeries

# 规则元数据（§7.1 #3；聚合层汇总用）
RULE_ID = "#3"
RULE_NAME = "市况"
BLOCKING = False                  # 只选结构＋建议，永不淘汰候选（即使 FAIL 也在汇总降级；实际本单元不产 FAIL）

# 指标窗口（Wilder ADX 惯例 14 日；斜率窗口 20 日＝§7.1 口径）
_ADX_PERIOD = 14
_SLOPE_WINDOW = 20
_MIN_BARS = 2 * _ADX_PERIOD       # Wilder ADX 首个稳定值需 ~2×period 根；斜率窗口 20 亦被覆盖

# regime → 结构（§7.2；值与 D16 持仓 schema `structure` 字符串一致）
STRUCTURE_MAP = {"ranging": "iron_condor", "uptrend": "single_put", "downtrend": "single_call"}
STRUCTURE_LABEL = {"iron_condor": "双卖铁鹰", "single_put": "单卖下方 PUT 价差",
                   "single_call": "单卖上方 CALL 价差"}
REGIME_LABEL = {"ranging": "震荡", "uptrend": "上涨趋势", "downtrend": "下跌趋势"}


# ---------------------------------------------------------------------------
# 指标纯函数（离线可测；只吃原始 OHLC，不碰数据层对象）
# ---------------------------------------------------------------------------
def _bars(high, low, close) -> list[tuple[float, float, float]]:
    """对齐并剔除含 None 的 bar → [(h, l, c), ...]（停牌/缺失日忽略，保持序列连续近似）。"""
    out: list[tuple[float, float, float]] = []
    for h, l, c in zip(high, low, close):
        if h is not None and l is not None and c is not None:
            out.append((float(h), float(l), float(c)))
    return out


def _adx(bars: list[tuple[float, float, float]], period: int = _ADX_PERIOD) -> float | None:
    """Wilder ADX(period)：TR/+DM/−DM → Wilder 平滑 → +DI/−DI → DX → ADX。bar 不足→None。"""
    n = len(bars)
    if n < 2 * period:
        return None
    tr_l, pdm_l, ndm_l = [], [], []
    for i in range(1, n):
        h, l, _ = bars[i]
        ph, pl, pc = bars[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up, dn = h - ph, pl - l
        tr_l.append(tr)
        pdm_l.append(up if (up > dn and up > 0) else 0.0)
        ndm_l.append(dn if (dn > up and dn > 0) else 0.0)
    if len(tr_l) < period:
        return None
    # Wilder 首次平滑＝前 period 个之和，其后递推 x - x/period + new
    atr = sum(tr_l[:period])
    pdm_s = sum(pdm_l[:period])
    ndm_s = sum(ndm_l[:period])
    dx_l: list[float] = []
    for i in range(period, len(tr_l)):
        atr = atr - atr / period + tr_l[i]
        pdm_s = pdm_s - pdm_s / period + pdm_l[i]
        ndm_s = ndm_s - ndm_s / period + ndm_l[i]
        if atr <= 0:
            continue
        pdi, ndi = 100.0 * pdm_s / atr, 100.0 * ndm_s / atr
        if pdi + ndi <= 0:
            continue
        dx_l.append(100.0 * abs(pdi - ndi) / (pdi + ndi))
    if len(dx_l) < period:
        return None
    adx = sum(dx_l[:period]) / period
    for dx in dx_l[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def _slope_pct(closes: list[float], window: int = _SLOPE_WINDOW) -> float | None:
    """最近 window 根 close 的最小二乘斜率 ÷ 均价 → 每日占比（%/日）。不足 window 根→None。"""
    vals = closes[-window:] if window > 0 else closes
    if len(vals) < window:
        return None
    n = len(vals)
    xs = list(range(n))
    xbar = sum(xs) / n
    ybar = sum(vals) / n
    den = sum((x - xbar) ** 2 for x in xs)
    if den == 0 or ybar == 0:
        return None
    num = sum((x - xbar) * (y - ybar) for x, y in zip(xs, vals))
    return (num / den) / ybar


def _consecutive_same_dir(open_, close) -> tuple[int, int]:
    """末尾连续同向 K 线：(根数, 方向)；阳 close>open(+1) / 阴 close<open(−1)；doji/异向断链。无→(0,0)。

    原属 #10 的"单向动能"形态，现上移 #3 作软描述（只入 detail/reason，不改硬判定），消除与 #3 趋势语义重叠。"""
    signs: list[int] = []
    for o, c in zip(open_, close):
        if o is None or c is None:
            continue
        signs.append(1 if c > o else (-1 if c < o else 0))
    run, last = 0, 0
    for s in reversed(signs):
        if s == 0:
            break
        if last == 0:
            last, run = s, 1
        elif s == last:
            run += 1
        else:
            break
    return run, last


def _classify(adx_val: float | None, slope_val: float | None,
              th: ThresholdSet) -> tuple[str | None, str]:
    """(ADX, 斜率) → (regime|None, 人话依据)。regime ∈ ranging/uptrend/downtrend；判不出→(None, 原因)。"""
    if adx_val is None or slope_val is None:
        return None, "ADX 或 20 日斜率算不出（有效 bar 不足）→ 判不出市况"
    adx_s, slope_s = f"{adx_val:.1f}", f"{abs(slope_val) * 100:.3f}%"
    if adx_val < th.adx_ranging and abs(slope_val) < th.slope_ranging:
        return "ranging", (f"震荡（ADX {adx_s} < {th.adx_ranging:.0f} 且 |斜率| {slope_s} "
                           f"< {th.slope_ranging * 100:.2f}%/日）")
    if slope_val > 0:
        return "uptrend", f"上涨趋势（斜率 +{slope_val * 100:.3f}%/日，ADX {adx_s}）"
    if slope_val < 0:
        return "downtrend", f"下跌趋势（斜率 {slope_val * 100:.3f}%/日，ADX {adx_s}）"
    return None, f"非震荡（ADX {adx_s}）但 20 日斜率≈0，方向不明 → 判不出市况"


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝D12 IndexSeries）
# ---------------------------------------------------------------------------
def evaluate(series: "IndexSeries | None", th: ThresholdSet) -> CheckResult:
    """#3 市况：从 D12 IndexSeries 的原始 OHLC 算 ADX+20日斜率 → regime → 结构建议。

    series：D12 后复权主连日线（相对指标专用）；None / state=MISSING / 有效 bar < _MIN_BARS → MISSING；
    th：注入的阈值集（读 adx_ranging / slope_ranging）。verdict ∈ {PASS, MISSING}，永不 FAIL/WARN。"""
    th_snap = {"adx_ranging": th.adx_ranging, "slope_ranging": th.slope_ranging}
    if series is None:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           "无指数日线（D12 未取到）→ #3 判不出市况", BLOCKING, {"thresholds": th_snap})
    if getattr(series, "state", None) == Verdict.MISSING:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"D12 日线数据层 MISSING（{getattr(series, 'reason', '')}）→ #3 判不出市况",
                           BLOCKING, {"thresholds": th_snap})

    bars = _bars(series.high, series.low, series.close)
    consec_run, consec_sign = _consecutive_same_dir(series.open, series.close)
    if len(bars) < _MIN_BARS:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"有效日线 {len(bars)} 根 < {_MIN_BARS}（ADX/斜率窗口不足）→ #3 判不出市况",
                           BLOCKING, {**th_snap, "count": len(bars)})

    adx_val = _adx(bars)
    slope_val = _slope_pct([b[2] for b in bars])
    regime, why = _classify(adx_val, slope_val, th)
    detail = {"regime": regime, "adx": adx_val, "slope_pct": slope_val,
              "consecutive_run": consec_run, "consecutive_sign": consec_sign,
              "count": len(bars), "bar_date": getattr(series, "bar_date", None), "thresholds": th_snap}
    if regime is None:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING, why + " → MISSING", BLOCKING, detail)

    structure = STRUCTURE_MAP[regime]
    detail["structure"] = structure
    detail["structure_label"] = STRUCTURE_LABEL[structure]
    run_note = f" · 连{consec_run}同向" if consec_run >= 5 else ""
    reason = f"{REGIME_LABEL[regime]}：{why} → 建议 {STRUCTURE_LABEL[structure]}{run_note}"
    return CheckResult(RULE_ID, RULE_NAME, Verdict.PASS, reason, BLOCKING, detail)


# ---------------------------------------------------------------------------
# 真实数据自检：从 daily 缓存加载 D12 全品种指数日线，展示市况判定与结构建议（永不 FAIL）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d03_index_symbol import fetch_d03
    from sell_option.fetchers.d12_index_daily import fetch_d12, DailyCacheRepo
    from sell_option.thresholds import load_thresholds

    today = __import__("datetime").date.today()
    th = load_thresholds()
    table = fetch_d03()
    cache = DailyCacheRepo()

    print("=" * 72)
    print(f"#3 市况 · ADX<{th.adx_ranging:.0f} · |斜率|<{th.slope_ranging*100:.2f}%/日 · BLOCKING={BLOCKING}")
    print(f"{'品种':<4} {'regime':<12} {'structure':<16} {'ADX':>6} {'斜率%/日':>10}  判定")
    print("-" * 80)
    for code, sym in table.items():
        series = fetch_d12([code], today, index_table=table, cache=cache, verbose=False).get(code)
        r = evaluate(series, th)
        regime = r.detail.get("regime", "?")
        struct = r.detail.get("structure_label", "?")
        adx = r.detail.get("adx")
        slope = r.detail.get("slope_pct")
        adx_s = f"{adx:.1f}" if adx is not None else "N/A"
        slope_s = f"{slope*100:.3f}" if slope is not None else "N/A"
        print(f"{code:<4} {regime:<12} {struct:<16} {adx_s:>6} {slope_s:>10}  {r.verdict.value}")
    print("=" * 72)
