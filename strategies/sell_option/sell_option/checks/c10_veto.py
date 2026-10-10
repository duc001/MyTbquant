"""#10 否决信号（§7.1）逻辑单元：指数日线 3 危险形态任一命中→否决（硬否决）。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元，**直接吃数据层产物**
（D12 `IndexSeries` 后复权主连日线的原始 OHLC 序列）+ 阈值集 → 判三态 CheckResult。与 #3 同源数据
（同一 D12 IndexSeries）、同 #3 一样：判定权归逻辑单元、**指标计算即判定逻辑的一部分，落在本单元**
（D12 只忠实供原始日线、不含算好的形态）；沿用 #6–#9【方案A】先例，读 `th` 作权威阈值。

需求源 §7.1 #10 + §四 数据口径铁律 + D12 备注：
- **判什么**：3 类"危险形态"，任一命中即否决该候选（均值回归卖方最怕断层 / 临界 / 波动骤升）：
  1. **真实跳空**：最新一根 |今开 − 昨收| / 昨收 ≥ `veto_gap_pct`(2%)——断层风险；
  2. **区间贴边**：最新 close 在近 `_RANGE_WINDOW`(20) 日区间中的【位置】落入顶/底 ≤ `veto_range_edge_pct`(2%)
     （占区间跨度、非占价格——用跨度归一，避免窄幅震荡里 close 天然贴近高低点而误否决、与 #3 偏爱震荡冲突）——临界突破；
  3. **波动扩张**：ATR(14) ≥ `veto_atr_ratio`(1.5)× 滚动基线均值（当前窗之前近 60 日 TR% 均值，TR 以 close 归一、剔除价格水平漂移）——波动骤升 / 换 regime。
  （原"连 N 同向"单向动能形态已上移至 #3 市况作软描述，消除与 #3 趋势判定的语义重叠/冲突。）
- **跳空必须在后复权序列上判**（§7.1 #10 + D12 备注）：后复权（RolloverBackWard）消除换月台阶、只留真实跳空；
  本单元吃 D12 IndexSeries（adjusted=True 后复权）→ 天然满足；绝不喂未复权/绝对价序列。
- **串价铁律**（§四）：#10 全是【相对指标】（形态 / 比率），只建在后复权指数上，绝不碰绝对价——与 D12 定位一致。
- **三态**（§八）：无日线 / D12 state=MISSING / 有效 bar < `_MIN_BARS` / 数值退化（跳空·区间·ATR 全算不出）→
  **MISSING**（不猜、不冒充）；3 形态任一命中 → **FAIL**（否决）；全不命中 → **PASS**。无 WARN（否决信号是硬门槛）。
- **BLOCKING=True**：doc §7.1 #10 未标"非阻断"（对比 #2/#3/#6 均显式标注），与 #1/#4/#5/#7/#8/#9 同列硬否决。
- **needs_live=False**（装配层标记）：指数日线盘后可判（同 #3），休市粗筛也能跑。

运行自检：``python -m sell_option.checks.c10_veto``（接真实数据链从 daily 缓存取 D12 指数日线
OHLC，展示全品种否决信号 3 形态判定）
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

if TYPE_CHECKING:  # 仅类型标注用；运行时靠鸭子类型读 IndexSeries 字段（同 c3，不在包加载期 import 数据层重模块）
    from ..fetchers.d12_index_daily import IndexSeries

# 规则元数据（§7.1 #10；聚合层汇总用）
RULE_ID = "#10"
RULE_NAME = "否决信号"
BLOCKING = True                   # 硬否决：3 危险形态任一命中即淘汰该候选

# 指标窗口（模块常量、同 c3 惯例；非"配置面"、是算法固有窗口，故不进阈值层）
_ATR_PERIOD = 14                  # ATR 窗口（Wilder 惯例 14 日）
_ATR_BASE_WINDOW = 60            # 波动扩张·滚动基线窗：取当前 ATR 窗之前近 N 日 TR% 均值（≈3 月，避免基线含远古 regime 而漂移）
_RANGE_WINDOW = 20                # 区间贴边的近 N 日高低窗口（与 c3 斜率窗口同口径）
_MIN_BARS = 40                    # 最少有效 bar（够 ATR14+区间20、跳空；ATR 另需 ≥74 根见 _atr_expansion）


# ---------------------------------------------------------------------------
# 指标纯函数（离线可测；只吃原始 OHLC 元组，不碰数据层对象）
# bar ＝ (open, high, low, close)
# ---------------------------------------------------------------------------
def _bars(open_, high, low, close) -> list[tuple[float, float, float, float]]:
    """对齐并剔除含 None 的 bar → [(o, h, l, c), ...]（停牌/缺失日忽略，保持序列连续近似）。同 c3._bars。"""
    out: list[tuple[float, float, float, float]] = []
    for o, h, l, c in zip(open_, high, low, close):
        if o is not None and h is not None and l is not None and c is not None:
            out.append((float(o), float(h), float(l), float(c)))
    return out


def _latest_gap_pct(bars: list[tuple[float, float, float, float]]) -> float | None:
    """最新一根相对昨收的跳空幅度 |open[-1] − close[-2]| / |close[-2]|。bar<2 或昨收=0 → None。

    在后复权序列上算（bars 来自 D12 IndexSeries）→ 换月台阶已被抹平、此处只剩真实跳空。"""
    if len(bars) < 2:
        return None
    o = bars[-1][0]
    pc = bars[-2][3]
    if pc == 0:
        return None
    return abs(o - pc) / abs(pc)


def _range_position(bars: list[tuple[float, float, float, float]],
                    window: int = _RANGE_WINDOW) -> float | None:
    """最新 close 在近 window 日区间中的位置：0＝贴底、1＝贴顶、0.5＝居中。区间跨度≤0（死水）→ None。

    用【区间跨度】归一（非价格）：窄幅震荡里 close 居中不触发、趋势/突破把 close 顶到区间边缘才触发。"""
    win = bars[-window:]
    hh = max(b[1] for b in win)
    ll = min(b[2] for b in win)
    c = win[-1][3]
    if hh <= ll:
        return None
    return (c - ll) / (hh - ll)


def _atr_expansion(bars: list[tuple[float, float, float, float]],
                   period: int = _ATR_PERIOD,
                   base_window: int = _ATR_BASE_WINDOW) -> float | None:
    """波动扩张倍数＝当前 ATR(period) / 滚动基线 ATR（TR 以 close 归一为 TR%、剔除价格水平漂移）。

    TR%[i]＝max(h−l, |h−prevC|, |l−prevC|) / close[i]；当前＝末 period 个 TR% 均值；
    基线＝当前窗之前近 base_window 个 TR% 均值（滚动，不随部署时长漂移、不被远古 regime 污染）。
    有效 TR 不足 period+base_window 或基线≤0（死水）→ None。"""
    trs: list[float] = []
    for i in range(1, len(bars)):
        _o, h, l, c = bars[i]
        pc = bars[i - 1][3]
        if c == 0:
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)) / c)
    if len(trs) < period + base_window:
        return None
    atr_now = sum(trs[-period:]) / period
    base = trs[-(period + base_window):-period]
    atr_base = sum(base) / len(base)
    if atr_base <= 0:
        return None
    return atr_now / atr_base


def _fmt_pct(v) -> str:
    return f"{v:.1%}" if isinstance(v, float) else "—"


def _fmt_x(v) -> str:
    return f"{v:.2f}×" if isinstance(v, float) else "—"


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝D12 IndexSeries，同 c3）
# ---------------------------------------------------------------------------
def evaluate(series: "IndexSeries | None", th: ThresholdSet) -> CheckResult:
    """#10 否决信号：从 D12 后复权日线检 3 危险形态，任一命中→FAIL（否决）。

    series：D12 后复权主连日线（相对指标专用、adjusted=True）；None / state=MISSING / 有效 bar < _MIN_BARS
            / 数值退化 → MISSING；
    th：注入阈值集（读 veto_gap_pct / veto_range_edge_pct / veto_atr_ratio）。
    verdict ∈ {PASS, FAIL, MISSING}，永不 WARN。"""
    th_snap = {"veto_gap_pct": th.veto_gap_pct,
               "veto_range_edge_pct": th.veto_range_edge_pct, "veto_atr_ratio": th.veto_atr_ratio}
    if series is None:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           "无指数日线（D12 未取到）→ #10 判不出否决信号", BLOCKING, {"thresholds": th_snap})
    if getattr(series, "state", None) == Verdict.MISSING:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"D12 日线数据层 MISSING（{getattr(series, 'reason', '')}）→ #10 判不出否决信号",
                           BLOCKING, {"thresholds": th_snap})

    bars = _bars(series.open, series.high, series.low, series.close)
    if len(bars) < _MIN_BARS:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"有效日线 {len(bars)} 根 < {_MIN_BARS}（形态窗口不足）→ #10 判不出否决信号",
                           BLOCKING, {**th_snap, "count": len(bars)})

    # 3 形态各自计算（纯函数）
    gap = _latest_gap_pct(bars)
    pos = _range_position(bars)
    atr = _atr_expansion(bars)

    # 数据退化保护：区间位置与 ATR 这两个"形态"信号全算不出（如价格恒定死水、跨度=0/基线=0）→ MISSING
    # （只剩跳空在死水上恒为 0、不足以判形态；绝不冒充 PASS）
    if pos is None and atr is None:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           "日线数值退化（区间位置 / ATR 均算不出）→ #10 数据异常待核",
                           BLOCKING, {**th_snap, "count": len(bars)})

    # 逐形态判命中（阈值来自 th；值为 None 的形态不命中——数据不足不误否决）
    fired: list[str] = []
    if gap is not None and gap >= th.veto_gap_pct:
        fired.append(f"跳空{_fmt_pct(gap)}≥{th.veto_gap_pct:.0%}")
    if pos is not None and (pos <= th.veto_range_edge_pct or pos >= 1 - th.veto_range_edge_pct):
        edge = "贴顶" if pos >= 0.5 else "贴底"
        fired.append(f"区间{edge}(位置{_fmt_pct(pos)})")
    if atr is not None and atr >= th.veto_atr_ratio:
        fired.append(f"波动扩张ATR{_fmt_x(atr)}≥{th.veto_atr_ratio}×")

    detail = {**th_snap, "count": len(bars), "gap_pct": gap,
              "range_position": pos, "atr_ratio": atr, "fired": fired,
              "bar_date": getattr(series, "bar_date", None)}
    if fired:
        reason = "、".join(fired) + " → #10 否决（危险形态）"
        return CheckResult(RULE_ID, RULE_NAME, Verdict.FAIL, reason, BLOCKING, detail)
    reason = (f"3 形态全清（跳空 {_fmt_pct(gap)}<{th.veto_gap_pct:.0%}、"
              f"区间居中(位置 {_fmt_pct(pos)})、ATR {_fmt_x(atr)}<{th.veto_atr_ratio}×）→ #10 通过")
    return CheckResult(RULE_ID, RULE_NAME, Verdict.PASS, reason, BLOCKING, detail)


# ---------------------------------------------------------------------------
# 真实数据自检：从 daily 缓存加载 D12 全品种指数日线，展示否决信号 3 形态判定
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
    print(f"#10 否决信号 · 跳空≥{th.veto_gap_pct:.0%}/"
          f"贴边≤{th.veto_range_edge_pct:.0%}/ATR≥{th.veto_atr_ratio}× · BLOCKING={BLOCKING}")
    print(f"{'品种':<4} {'跳空%':>8} {'区间位置':>8} {'ATR倍':>6}  判定  触发")
    print("-" * 80)
    for code, sym in table.items():
        series = fetch_d12([code], today, index_table=table, cache=cache, verbose=False).get(code)
        r = evaluate(series, th)
        gap = r.detail.get("gap_pct")
        pos = r.detail.get("range_position")
        atr = r.detail.get("atr_ratio")
        gap_s = f"{gap:.1%}" if gap is not None else "N/A"
        pos_s = f"{pos:.1%}" if pos is not None else "N/A"
        atr_s = f"{atr:.2f}×" if atr is not None else "N/A"
        fired = "、".join(r.detail.get("fired", [])) or "—"
        print(f"{code:<4} {gap_s:>8} {pos_s:>8} {atr_s:>6}  {r.verdict.value:<8} {fired}")
    print("=" * 72)
