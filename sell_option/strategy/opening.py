"""开仓策略（§7.1 开仓检查编排 + §7.2 结构选择）：装配候选 → 编排 #1–#10 → 漏斗汇总。

策略聚合层的一个【具体策略模块】（与 closing / position… 平级，共用 base.combine_results 口径）。职责：
- `OpeningData`：开仓候选的数据装配暂存——持有从数据层取回的产物（PremiumSet / OiSet / …），按各检查
  需要分发。这是【开仓策略】自己的装配结构（平仓 / 仓位策略各有自己的），不强加给逻辑单元。
- `OpeningCheck` / `OPENING_CHECKS`：开仓检查注册表（#1→#10）。每条＝(rule_id, name, blocking,
  needs_live, run)；run 把 OpeningData 里该检查需要的数据层产物分发过去 `(data, th) -> CheckResult`；
  needs_live 标记该检查是否【依赖盘中实时盘口 bid/ask】——仅 #1 盘口为 True（休市跳过）；#2 OI 走 get_history_n→position，盘后仍可取（needs_live=False）。
  元数据取自各逻辑单元模块（RULE_ID/RULE_NAME/BLOCKING，单一事实源）；新增检查＝append 一条 OpeningCheck。
- `run_opening_checks` / `evaluate_opening`：按 #1→#10 跑检查 + `base.combine_results` 漏斗汇总，
  带 **market_open 双模式**（时段状态由数据层 is_trading() 判定、编排层注入；策略层不碰 tbpy）：
  · market_open=True → **开盘·全链路**：跑全部检查（含实时盘口），产出权威 可开/淘汰/待人工（细）；
  · market_open=False → **休市·粗筛**：needs_live 检查【跳过 + 提醒】（不发起无谓实时取数、记
    skipped MISSING），只跑盘后可判项（#3/#4/#5…），产出「次日候选」粗筛清单（粗）；
  · market_open=None（默认）→ 全跑，呈现层按数据指纹（盘口是否缺失）事后归类，兼容旧调用。
  单条检查抛异常降级为该条 MISSING（不崩整轮、不冒充）。
- `FUNNEL_LABEL` / `funnel_label`：开仓四态 → 中文漏斗标签（可开 / 可开·有提示 / 待人工 / 淘汰）。
- `classify_opening`：四态标签之上细化 MISSING 的【呈现标签】——休市粗筛中「仅盘口项被跳过、
  其余无 FAIL」→「次日候选·待开盘全链路复核」；连盘后可判项都缺 → 仍「待人工」（不改变任何 verdict）。

§7.2 结构选择（已落地）：#3 市况产出 structure 建议（震荡→铁鹰 / 涨→单卖 PUT / 跌→单卖 CALL，
见 c3_regime.STRUCTURE_MAP）；选腿装配（assemble.py）按 DTE 20–180 取前 N=3 近月，走近端候选集
（短腿 平值/虚一/虚二 由近及远、平值优先；保护腿＝更虚第 prot_strikes 档），装配成 OpeningData 后跑 #1–#10。

运行自检：``python -m sell_option.strategy.opening``（本段仅打印引导；开仓评估经 runner.run_pool 全链路编排，请执行 sellopt.py scan）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

try:  # 包方式（python -m / import）：相对导入
    from ..checks.models import CheckResult, Verdict
    from ..checks import c1_spread, c2_oi, c3_regime, c4_iv, c5_safety, c6_event, c7_margin, c8_budget, c9_fatthin, c10_veto
    from ..thresholds import ThresholdSet
    import config
    from ..logutil import get_logger, log_call
    from .base import combine_results
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import CheckResult, Verdict
    from sell_option.checks import c1_spread, c2_oi, c3_regime, c4_iv, c5_safety, c6_event, c7_margin, c8_budget, c9_fatthin, c10_veto
    from sell_option.thresholds import ThresholdSet
    import config
    from sell_option.logutil import get_logger, log_call
    from sell_option.strategy.base import combine_results

logger = get_logger(__name__)

if TYPE_CHECKING:  # 数据层产物类型仅标注用（聚合层依赖数据层，运行时鸭子类型）
    import datetime as dt

    from ..fetchers.d09_d10_premium import PremiumSet
    from ..fetchers.d14_contract_oi import OiSet
    from ..fetchers.d12_index_daily import IndexSeries
    from ..fetchers.d13_iv_snapshot import IvVerdict
    from ..fetchers.d08_spot_price import SpotQuote
    from ..fetchers.d15_calendar import EventCalendar
    from ..fetchers.d17_capital_pool import CapitalPool
    from ..fetchers.d18_budget import BudgetTier

# 策略元数据（满足 base.Strategy 契约）
STRATEGY_ID = "opening"
STRATEGY_NAME = "开仓评估"


# ---------------------------------------------------------------------------
# 数据装配暂存（开仓策略职责：持有从数据层取回的产物，按检查需要分发）
# ---------------------------------------------------------------------------
@dataclass
class OpeningData:
    """一个开仓候选从数据层取回的产物集合（开仓策略暂存；随检查增多而扩展字段）。

    已接入：#1 premium（D09/D10 盘口）；#2 oi（D14 OI）；#3 index_daily（D12 日线）；
    #4 iv（D13 IV 观测）+ structure；#5 spot（D08 现价）+ dte + prot_strike（选腿结果，新口径量 spot→保护腿 缓冲）；
    #6 events（D15 事件日历）+ today（判定基准日）；#7 capital_pool（D17 资金池）+ margin_single
    （拟开新仓占用额，c7_margin.estimate_candidate_margin 估算）+ margin_existing（存量占用，D16 total_margin_occupied）；
    #8 budget_tier（D18 预算档）+ max_loss（单笔最大亏损额，D18.max_loss_cap 按 §三 (W−R)×乘数 折算）；
    #9 net_credit（净权利金 R）+ spread_width（价差宽度 W）——与 #8 同源，选腿装配从 D07 行权价 + D09/D10 权利金算出；
    #10 否决信号复用 index_daily（同 #3 的 D12 后复权序列，检 4 危险形态）——#1–#10 全部接入、无新增字段。"""

    code: str = ""
    structure: str = "single"                # 评估结构 single/dual（§7.2 由 #3 建议 / 命令 --dual 指定；#4 门槛据此）
    premium: "PremiumSet | None" = None      # D09/D10 权利金·盘口（#1 盘口价差）
    oi: "OiSet | None" = None                # D14 合约级 OI（#2 持仓量）
    index_daily: "IndexSeries | None" = None  # D12 后复权主连日线（#3 市况 ADX/斜率 + #10 否决 3 形态，同源复用）
    iv: "IvVerdict | None" = None            # D13 品种级 IV 观测（#4 分位·方向；#5 安全垫取 atmv_current）
    spot: "SpotQuote | None" = None          # D08 候选标的月现价（#5 安全垫）
    dte: int | None = None                   # 到期天数（D13 派生 / D07 候选月；#5 安全垫）
    prot_strike: float | None = None         # 保护腿行权价（§7.2 选腿结果；#5 安全垫新口径：量 spot→保护腿 缓冲）
    events: "EventCalendar | None" = None    # D15 事件日历（#6 事件窗口；None/未加载 → #6 MISSING）
    today: "dt.date | None" = None           # 判定基准日（#6 事件窗 upcoming 用；None→date.today()）
    capital_pool: "CapitalPool | None" = None  # D17 资金池（#7 合计占用；读 .total/.configured；None/未配置 → 合计 MISSING）
    margin_single: float | None = None         # 拟开新仓保证金占用额（c7_margin.estimate_candidate_margin 估算；#7）
    margin_existing: float | None = None       # 存量持仓占用合计（D16 total_margin_occupied()；#7 合计）
    budget_tier: "BudgetTier | None" = None    # D18 预算档（#8 封顶预算；读 .tier/.configured；None/未配置 → MISSING）
    max_loss: float | None = None              # 单笔最大亏损额（D18.max_loss_cap 按 §三 (W−R)×乘数 折算；#8）
    net_credit: float | None = None            # 净权利金 R＝卖腿−保护腿权利金（D09/D10，选腿装配算出；#9 肥瘦，与 #8 同源）
    spread_width: float | None = None          # 价差宽度 W＝短腿−保护腿行权价（D07，选腿装配算出；#9 肥瘦，与 #8 同源）
    early_excluded: str | None = None          # 早筛阻断标记：非空＝取期权数据前已被便宜阻断检查断定淘汰，
                                               # 故跳过昂贵的期权权利金/持仓取数（#1/#2/#8/#9 由 run_opening_checks 标记跳过）
    guard: str | None = None                   # 保护腿护栏观测提示（放行到人工·不据脏数据淘汰）：None＝未触发；
                                               # 非空＝"护栏观测(不阻挡): <side>:保护腿<x>≥短腿<y>×<ratio>"，run_opening_checks 据此把 #8/#9 降级为待人工


# ---------------------------------------------------------------------------
# 开仓检查注册表（#1→#10；把 OpeningData 分发成各检查所需的数据层产物）
# needs_live=True 的检查依赖盘中实时数据（休市粗筛时跳过 + 提醒，不发起无谓取数）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OpeningCheck:
    """一条开仓检查的装配规格：元数据取自逻辑单元模块（单一事实源）+ 是否盘中依赖 + 分发适配器。"""
    rule_id: str
    name: str
    blocking: bool
    needs_live: bool                          # True=盘中实时依赖（#1 盘口 bid/ask）；休市跳过
    run: Callable[[OpeningData, ThresholdSet], CheckResult]
    needs_option: bool = False                # True=依赖昂贵期权取数（权利金/持仓）；早筛已排除时跳过


OPENING_CHECKS: list[OpeningCheck] = [
    OpeningCheck(c1_spread.RULE_ID, c1_spread.RULE_NAME, c1_spread.BLOCKING, True,    # #1 盘口价差：需实时 bid/ask
                 lambda data, th: c1_spread.evaluate(data.premium, th), needs_option=True),
    OpeningCheck(c2_oi.RULE_ID, c2_oi.RULE_NAME, c2_oi.BLOCKING, False,               # #2 持仓量OI：get_history_n→position 盘后/假日可取
                 lambda data, th: c2_oi.evaluate(data.oi, th), needs_option=True),
    OpeningCheck(c3_regime.RULE_ID, c3_regime.RULE_NAME, c3_regime.BLOCKING, False,   # #3 市况：指数日线，盘后可判
                 lambda data, th: c3_regime.evaluate(data.index_daily, th)),
    OpeningCheck(c4_iv.RULE_ID, c4_iv.RULE_NAME, c4_iv.BLOCKING, False,              # #4 IV 分位·方向：openvlab，盘后可判
                 lambda data, th: c4_iv.evaluate(data.iv, data.structure, th)),
    OpeningCheck(c5_safety.RULE_ID, c5_safety.RULE_NAME, c5_safety.BLOCKING, False,   # #5 安全垫：现价回退日线 close，盘后可判
                 lambda data, th: c5_safety.evaluate(data.spot, data.iv, data.dte, data.prot_strike, th)),
    OpeningCheck(c6_event.RULE_ID, c6_event.RULE_NAME, c6_event.BLOCKING, False,       # #6 事件窗口：本地日历，盘后可判、只提醒不否决
                 lambda data, th: c6_event.evaluate(data.events, th, today=data.today, code=data.code)),
    OpeningCheck(c7_margin.RULE_ID, c7_margin.RULE_NAME, c7_margin.BLOCKING, False,    # #7 保证金：占用额估算制、现价可回退日线，盘后可判（显示实际金额供对比）
                 lambda data, th: c7_margin.evaluate(data.margin_single, data.margin_existing, data.capital_pool, th)),
    OpeningCheck(c8_budget.RULE_ID, c8_budget.RULE_NAME, c8_budget.BLOCKING, False,    # #8 封顶预算：最大亏损 (W−R)×乘数，真实价可回退日线，盘后可判
                 lambda data, th: c8_budget.evaluate(data.max_loss, data.budget_tier), needs_option=True),
    OpeningCheck(c9_fatthin.RULE_ID, c9_fatthin.RULE_NAME, c9_fatthin.BLOCKING, False,  # #9 肥瘦：R/W＝净权利金/价差宽度，真实价可回退日线，盘后可判
                 lambda data, th: c9_fatthin.evaluate(data.net_credit, data.spread_width, th), needs_option=True),
    OpeningCheck(c10_veto.RULE_ID, c10_veto.RULE_NAME, c10_veto.BLOCKING, False,         # #10 否决信号：指数日线 3 形态（跳空/贴边/扩张），盘后可判、复用 index_daily
                 lambda data, th: c10_veto.evaluate(data.index_daily, th)),
]

# 开仓四态 → 中文漏斗标签（§八 报告用；开仓策略专有词汇，注入 base.combine_results）
FUNNEL_LABEL: dict[Verdict, str] = {
    Verdict.PASS: "可开",
    Verdict.WARN: "可开·有提示",
    Verdict.MISSING: "待人工",
    Verdict.FAIL: "淘汰",
}


def funnel_label(verdict: Verdict) -> str:
    """开仓四态 → 中文漏斗标签（可开 / 可开·有提示 / 待人工 / 淘汰）。"""
    return FUNNEL_LABEL.get(verdict, verdict.value)


# 盘口/盘中依赖项（needs_live=True 中仅依赖盘口 bid/ask 的项；#2 OI 已改 needs_live=False，盘后 get_history_n→position 可取）
BOOK_DEPENDENT_RULES = frozenset({"#1"})

# 开仓呈现标签（在四态漏斗标签之上细化 MISSING）
LABEL_CANDIDATE = "次日候选·待开盘全链路复核"   # 休市粗筛：仅盘口项待核，其余已定


def _book_absent(data: OpeningData) -> bool:
    """盘口整体缺失＝「取到了权利金数据、但无任何一条腿带 bid/ask 盘口」——休市/盘后的数据指纹
    （§11.3.5 日线无 bid/ask）。仅在 market_open=None（未注入时段）时用作事后归类依据。
    premium 为 None / 空包 → False（那是根本没取到数＝缺数，不是休市）。"""
    ps = data.premium
    if ps is None or len(ps) == 0:
        return False
    return not any(q.has_book for q in ps.all_quotes())


def _is_deferred(r: CheckResult) -> bool:
    """该结果是否为「休市粗筛跳过」的提醒（run_opening_checks 在 closed 模式打的标记）。"""
    return r.detail.get("skipped") == "closed_session"


@log_call(level=logging.DEBUG)
def classify_opening(summary: CheckResult, results: list[CheckResult],
                     data: OpeningData, *, market_open: bool | None = None) -> str:
    """把汇总结论细化成【开仓呈现标签】（§八 报告用；不改变任何 verdict）。

    在 base.combine_results 的四态标签（可开 / 可开·有提示 / 待人工 / 淘汰）之上，把 MISSING 再分两类：
    - **次日候选·待开盘全链路复核**：休市粗筛下，MISSING 全部来自被跳过的盘中依赖项（仅 #1 盘口），
      盘后可判项（#2/#3/#4/#5/#6/#7/#8/#9/#10）无 FAIL —— 机器能定的都已定，只差开盘确认盘口；
    - **待人工**：其它 MISSING（盘后可判项也缺数、数据管道未取到）—— 需真人工判断，不是单纯休市。

    market_open=False 用「跳过标记」判定；market_open=None 回退用数据指纹（盘口整体缺失）事后归类；
    market_open=True（全链路）时 MISSING 均为真缺数 → 待人工。非 MISSING 原样返回 funnel_label。"""
    if summary.verdict != Verdict.MISSING:
        return funnel_label(summary.verdict)
    manuals = [r for r in results if r.verdict == Verdict.MISSING]
    if not manuals:
        return funnel_label(Verdict.MISSING)
    if market_open is False:
        # 休市粗筛：所有 MISSING 都是被跳过的盘中项 → 次日候选；否则（盘后项也缺）→ 待人工
        return LABEL_CANDIDATE if all(_is_deferred(r) for r in manuals) else funnel_label(Verdict.MISSING)
    if market_open is None:
        ids = {r.rule_id for r in manuals}
        if ids <= BOOK_DEPENDENT_RULES and _book_absent(data):
            return LABEL_CANDIDATE
    return funnel_label(Verdict.MISSING)


def _deferred_result(spec: OpeningCheck) -> CheckResult:
    """休市粗筛：needs_live 检查不跑（不发起无谓实时取数），如实记一条「跳过·待开盘复核」的 MISSING。"""
    return CheckResult(
        rule_id=spec.rule_id, name=spec.name, verdict=Verdict.MISSING,
        reason=f"休市跳过·{spec.rule_id}{spec.name} 需盘中实时数据 → 待开盘全链路复核",
        blocking=spec.blocking, detail={"skipped": "closed_session", "needs_live": True})


def run_opening_checks(data: OpeningData, th: ThresholdSet, *,
                       market_open: bool | None = None) -> list[CheckResult]:
    """对一个候选按 #1→#10 顺序跑已注册检查，返回 CheckResult 列表。

    market_open=False（休市粗筛）：needs_live 检查【跳过 + 提醒】（不调用适配器、不发起实时取数），
    其余照常跑；market_open=True/None：全部跑（全链路）。单条抛异常降级为该条 MISSING（不崩整轮、不冒充）。"""
    results: list[CheckResult] = []
    for spec in OPENING_CHECKS:
        if market_open is False and spec.needs_live:
            results.append(_deferred_result(spec))
            continue
        if data.early_excluded is not None and spec.needs_option:
            # 早筛已断定淘汰（便宜阻断检查命中、data.early_excluded 非空）→ 免取期权数据，
            # 依赖期权的检查直接标记跳过（不冒充 MISSING=待人工）。触发条件＝early_excluded 本身；
            # 是否运行早筛阻断由 assemble._early_block 处的 EARLY_EXCLUDE_ENABLED 开关控制（默认 False → 不跑 → early_excluded 为 None）。
            results.append(CheckResult(
                rule_id=spec.rule_id, name=spec.name, verdict=Verdict.MISSING, blocking=spec.blocking,
                reason=f"早筛已排除（{data.early_excluded} 阻断）→ 未取期权数据，跳过",
                detail={"skipped": "early_excluded", "needs_option": True, "excluded_by": data.early_excluded}))
            continue
        try:
            results.append(spec.run(data, th))
        except Exception as e:  # noqa: BLE001 - 单检查异常只降级、不崩汇总
            results.append(CheckResult(
                rule_id=spec.rule_id, name="检查异常", verdict=Verdict.MISSING,
                reason=f"检查执行异常（{type(e).__name__}: {e}）→ 降级 MISSING", blocking=spec.blocking))
    # ---- 保护腿护栏放行：护栏命中(data.guard 非空)＝腿级经济量(R/W、max_loss)被脏/倒挂数据污染，
    #      不据其自动淘汰，放行到人工复核。仅 #8 封顶 / #9 肥瘦 依赖腿经济量会误判；其余检查数据可信、照常。
    if getattr(data, "guard", None):
        overridden = []
        for r in results:
            if r.rule_id in ("#8", "#9"):
                overridden.append(replace(
                    r, verdict=Verdict.MISSING, blocking=False,
                    reason=f"[护栏放行·待人工] {r.reason}｜保护腿数据异常：{data.guard}（交人工核真实数据）"))
            else:
                overridden.append(r)
        results = overridden
        logger.info("run_opening_checks：护栏命中 → #8/#9 腿经济量检查降级为待人工，品种放行到人工（不据脏数据淘汰）")
    return results


@log_call(level=logging.DEBUG)
def evaluate_opening(data: OpeningData, th: ThresholdSet, *,
                     market_open: bool | None = None) -> tuple[CheckResult, list[CheckResult]]:
    """开仓策略入口：跑开仓检查（market_open 双模式）+ 按开仓漏斗词汇汇总 → (总结论, 逐条结果)。

    market_open 由编排层从数据层 is_trading() 取到后注入：True=开盘全链路、False=休市粗筛、None=全跑事后归类。
    呈现标签（可开 / 次日候选 / 待人工 / 淘汰）由 classify_opening(summary, results, data, market_open=…) 给出。"""
    results = run_opening_checks(data, th, market_open=market_open)
    return combine_results(results, labels=FUNNEL_LABEL), results


# ---------------------------------------------------------------------------
# 开仓评估经 runner.run_pool 全链路编排（需 tbpy + 网络）；本段仅打印引导，实际运行请执行 sellopt.py scan
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 72)
    print(f"strategy.opening · 策略[{STRATEGY_ID}/{STRATEGY_NAME}]")
    print("  开仓评估需通过 runner.run_pool() 全链路编排")
    print("  请运行 sellopt.py scan 执行全品种扫描")
    print("=" * 72)
