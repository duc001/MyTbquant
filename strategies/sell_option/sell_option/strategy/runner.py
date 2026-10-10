"""单合约 / 全池开仓运行编排（§八·§九 的 check/scan 内核；cli 与 report 复用）。

把用户约定的时序固化成代码——**「时段状态」先设好、再贯穿全程**：
  ① 先探时段、设好状态：`session = probe_session(code=code)`——**按品种探**（各品种夜盘时段不同，
     用该品种主力合约判时段、经 D03 解析）；或由调用方注入 / 命令 `--session` 覆盖；
  ② 按状态取数装配：`data = assemble(code, session)`——装配把 `session.open` 作 `market_open` 下送
     fetch_d08/fetch_premium/fetch_oi：收盘时它们跳过实时盘口 / OI 订阅、走日线 close 回退（省 tbpy 无谓等待），
     开盘时才取实时盘口 / 订阅 OI；
  ③ 跑 check + ④ 策略按状态处理：`evaluate_opening(data, th, market_open=session.open)`
     → 休市粗筛跳过 #1 盘口（needs_live）、#2 OI 盘后仍可取、开盘全链路；再 `classify_opening` 出呈现标签。

依赖方向（守三层单向 strategy→checks→fetchers）：runner（编排）→ market_session（数据层探时段）
+ assemble（数据层取数 / §7.2 选腿装配，注入）+ opening（策略层判定汇总）。判定仍只在 checks/opening，
本模块**只固化时序与状态贯穿**，不含任何判定。

✓ 选腿装配（§7.2：候选月 N、DTE 20–180、短腿近端候选集平值优先、保护腿更虚第 prot_strikes 档、结构随 #3 建议）
  已落地于 `assemble.py`（`assemble_candidate` + `make_assemble`）；本模块以注入接缝固定其契约
  `(code, session) -> OpeningData`。scan/check 直接复用本模块。

运行自检：``python -m sell_option.strategy.runner``（展示运行编排配置 + 检查规则清单）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:  # 包方式（python -m / import）：相对导入
    from ..checks.models import CheckResult, Verdict
    from ..fetchers.market_session import MarketSession, probe_session
    from ..logutil import get_logger, log_call, flow
    from ..thresholds import ThresholdSet
    from .opening import OpeningData, classify_opening, evaluate_opening, funnel_label
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import CheckResult, Verdict
    from sell_option.fetchers.market_session import MarketSession, probe_session
    from sell_option.logutil import get_logger, log_call, flow
    from sell_option.thresholds import ThresholdSet
    from sell_option.strategy.opening import (
        OpeningData, classify_opening, evaluate_opening, funnel_label)

# 装配回调契约：给「品种代码 + 已设好的时段状态」→ 产出该候选的 OpeningData（§7.2 选腿 + 取数，已落地于 assemble.py）。
# 装配内取数须把 session.open 作 market_open 下送 fetch_d08/fetch_premium/fetch_oi（三态标志，见各 fetcher）。
AssembleFn = Callable[[str, MarketSession], OpeningData]

__all__ = ["AssembleFn", "CandidateRun", "run_candidate", "run_pool"]

logger = get_logger(__name__)


def _default_probe(code: str) -> MarketSession:
    """默认时段探针：**按品种**用其主力合约探时段（D03 主连符号）——时段因品种夜盘不同、逐品种各探。"""
    return probe_session(code=code)


@dataclass(frozen=True)
class CandidateRun:
    """一个候选跑完整条流水线的产物：时段状态 + 装配数据 + 汇总 + 逐条 check + 呈现标签。"""
    code: str
    session: MarketSession                 # ① 先设好的时段状态（贯穿取数/check/策略）
    data: "OpeningData | None"             # ② 装配结果（装配异常时 None）
    summary: CheckResult                   # ③④ 漏斗汇总（四态）
    results: list[CheckResult] = field(default_factory=list)   # 逐条 check（休市时仅 #1 盘口为 skipped MISSING；#2 OI 盘后照取）
    label: str = ""                        # classify_opening 呈现标签（可开/次日候选/待人工/淘汰）


@log_call()
def run_candidate(code: str, th: ThresholdSet, *, assemble: AssembleFn,
                  session: MarketSession | None = None,
                  probe: Callable[[str], MarketSession] | None = None) -> CandidateRun:
    """跑【单个合约】的完整开仓流水线（时序见模块 docstring）。

    - `assemble`：取数 / 选腿装配回调（必填；真实装配＝assemble.make_assemble）。
    - `session`：已探好的时段状态；None 则**按本合约品种探一次**（`probe(code)`）。
    - 装配抛异常不崩：降级为一条「取数装配异常」的 MISSING 汇总（如实标注、不冒充）。"""
    # ① 先拿时段、设好状态（显式注入优先；否则按品种 code 探一次——各品种夜盘时段不同）
    if session is None:
        with flow("① 探时段", code=code):
            session = (probe or _default_probe)(code)
    # ② 按状态取数装配（休市可不发起实时取数，由 assemble 依 session.open 决定）
    try:
        with flow("② 取数装配", code=code, session_open=session.open):
            data = assemble(code, session)
    except Exception as e:  # noqa: BLE001 - 装配异常只降级、不崩编排
        summary = CheckResult(
            rule_id="SUMMARY", name="汇总", verdict=Verdict.MISSING,
            reason=f"取数装配异常（{type(e).__name__}: {e}）→ 待人工", blocking=True,
            detail={"label": funnel_label(Verdict.MISSING), "counts": {}, "error": "assemble_failed"})
        logger.error("run_candidate 装配异常 · code=%s · %s: %s", code, type(e).__name__, e)
        return CandidateRun(code, session, None, summary, [], funnel_label(Verdict.MISSING))
    # ③ 跑 check + ④ 策略按状态处理（market_open=session.open 贯穿）
    with flow("③ 跑检查#1-#10", code=code, session_open=session.open):
        summary, results = evaluate_opening(data, th, market_open=session.open)
    with flow("④ 策略汇总", code=code, verdict=summary.verdict.value):
        label = classify_opening(summary, results, data, market_open=session.open)
    logger.info("run_candidate 完成 · %s · 汇总=%s · 标签=%s · 早筛排除=%s",
                code, summary.verdict.value, label, getattr(data, "early_excluded", None))
    return CandidateRun(code, session, data, summary, results, label)


@log_call()
def run_pool(codes: list[str], th: ThresholdSet, *, assemble: AssembleFn,
             session: MarketSession | None = None,
             probe: Callable[[str], MarketSession] | None = None) -> list[CandidateRun]:
    """跑【全池多合约】：**逐品种各探时段**（不同品种夜盘时段不同，不能整池共用一个探针）。

    - `session` 显式注入＝强制全池统一时段（如命令 `--session closed` 覆盖），复用给所有候选；
    - 否则每个候选按自身品种 `probe(code)` 各探一次（is_trading_time 是本地日历判定、无 60s 限流，逐探不贵）。"""
    return [run_candidate(c, th, assemble=assemble, session=session, probe=probe) for c in codes]


# ---------------------------------------------------------------------------
# 实测：展示运行编排配置 + 检查规则清单
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.thresholds import load_thresholds
    from sell_option.fetchers.d01_variety_list import fetch_d01

    th = load_thresholds()
    varieties = [v.code for v in fetch_d01()]
    print("=" * 72)
    print("strategy.runner · 运行编排配置")
    print(f"  候选品种：{len(varieties)} 个（{', '.join(varieties[:8])}…）")
    print(f"\n  流水线时序：")
    print(f"    ① 探时段 probe_session(code) → 按品种各探（夜盘时段因品种而异）")
    print(f"    ② 取数装配 assemble(code, session) → session.open 贯穿 market_open")
    print(f"    ③ 跑检查 #1–#10 evaluate_opening(data, th, market_open)")
    print(f"    ④ 策略汇总 classify_opening → 呈现标签（可开/次日候选/待人工/淘汰）")
    print(f"\n  检查规则（#1–#10）：")
    print(f"    #1 盘口   #2 OI    #3 市况   #4 IV    #5 安全垫")
    print(f"    #6 事件   #7 保证金 #8 预算   #9 肥瘦   #10 否决")
    print(f"\n  关键阈值：")
    print(f"    OTM 展示 {th.otm_pct:.0%} · DTE {th.dte_min}–{th.dte_max} · 候选月 {th.candidate_months}")
    print(f"    止盈保底 {th.tp_floor:.0%} / 达标 {th.tp_extend:.0%} · 止损 {th.sl_multiplier:g}×R")
    print(f"    肥瘦 R/W ≥ {th.rw_min:.2f} · 安全垫 ≥ {th.safety_sigma}σ")
    print(f"\n  运行时由 cli.py 调 make_assemble() → run_pool() 跑全池。")
    print("=" * 72)
