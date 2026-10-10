"""§11.2 命令面（scan / check / exit / collect-oi / refresh / scope / report）：把策略层各模块接成可运行入口。

三层定位：本模块是【策略聚合层】的**命令行编排器**——只把 runner（开仓）/ closing（平仓）/ position（风控）
/ report（日报）/ assemble（选腿装配）与数据层 fetchers 串起来，**自身不做任何判定**（判定仍在 checks/ 与
opening/closing/position）。依赖单向 strategy(cli)→strategy(runner/closing/position/report/assemble)→checks→fetchers，
cli **绝不直接碰 tbpy**（全部经 `default_runtime()` 封装的可注入回调 / assemble 的 FetchBundle）。

命令面（§11.2，`--session open|closed` 可强制覆盖时段、默认逐品种自动探测）：
- `scan`           全池扫描（D01 全品种）：跑 #1–#10 + 平仓处置 + 仓位风控 + 事件/长假提醒 → 生成综合日报（§八 A/B/C/D）；
- `check M [--dual]`  单品种完整检查（--dual 强制评估铁鹰双卖结构）；
- `exit`           持仓平仓扫描（§7.3 X1–X4 出场规则 → 续持/止盈/止损/破位/到期）；
- `collect-oi -c M`   采集该品种候选月期权 OI（实时取数、不落盘），展示分布供复核 #2；
- `refresh [--all]`   刷新主连后复权日线缓存（--all 强制重拉、否则只补缺）；
- `scope`          品种边界表重算〔待建：价格重算逻辑尚未实现（命令面见 §11.2）〕；
- `report`         打印最新一份综合日报。

**可注入运行时 `CliRuntime`**：全部真实取数（探时段 / 装配 / 持仓 / 日历 / OI / 日线 / 平仓取数）都收敛成回调，
`default_runtime()` 绑真实 fetcher（生产用）。

运行自检：``python -m sell_option.strategy.cli``（展示命令面配置 + 可用命令清单）
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

try:  # 包方式（python -m / import）：相对导入
    from ..fetchers.d01_variety_list import fetch_d01
    from ..fetchers.d15_calendar import fetch_d15_events, fetch_d15_holidays
    from ..fetchers.d16_positions import fetch_d16
    from ..fetchers.market_session import MarketSession, probe_session
    from ..thresholds import ThresholdSet, load_thresholds
    from .assemble import default_bundle, make_assemble
    from .closing import ClosingData, evaluate_closing
    from .position import PositionData, evaluate_position
    from .report import REPORTS_DIR, ReportData, make_disposition, write_report
    from .runner import run_candidate, run_pool
    from ..logutil import get_logger, log_call, flow
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.d15_calendar import fetch_d15_events, fetch_d15_holidays
    from sell_option.fetchers.d16_positions import fetch_d16
    from sell_option.fetchers.market_session import MarketSession, probe_session
    from sell_option.thresholds import ThresholdSet, load_thresholds
    from sell_option.strategy.assemble import default_bundle, make_assemble
    from sell_option.strategy.closing import ClosingData, evaluate_closing
    from sell_option.strategy.position import PositionData, evaluate_position
    from sell_option.strategy.report import REPORTS_DIR, ReportData, make_disposition, write_report
    from sell_option.strategy.runner import run_candidate, run_pool
    from sell_option.logutil import get_logger, log_call, flow

if TYPE_CHECKING:  # 数据层产物类型仅标注用（编排层运行时鸭子类型）
    from ..fetchers.d08_spot_price import SpotQuote
    from ..fetchers.d09_d10_premium import PremiumSet
    from ..fetchers.d12_index_daily import IndexDailySet
    from ..fetchers.d14_contract_oi import OiSet
    from ..fetchers.d15_calendar import EventCalendar, HolidayCalendar
    from ..fetchers.d16_positions import Position, PositionBook
    from .assemble import FetchBundle
    from .runner import AssembleFn

__all__ = [
    "CliRuntime", "default_runtime", "build_parser", "main",
    "cmd_scan", "cmd_check", "cmd_exit", "cmd_collect_oi", "cmd_refresh", "cmd_scope", "cmd_report",
]

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 可注入运行时：全部真实取数收敛成回调（生产绑真 fetcher）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CliRuntime:
    """命令面运行时依赖集（可注入）。`default_runtime()` 绑真实 fetcher。

    - assemble_factory(structure, today) -> AssembleFn：装配工厂（structure="dual" 即 --dual 强制铁鹰）；
    - probe(code) -> MarketSession：**按品种**探时段（各品种夜盘不同）；--session 覆盖时不经它；
    - pool_codes() -> [code]：全池品种（D01）；positions/events/holidays：D16/D15 加载器；
    - closing_fetch(position, today, market_open) -> (PremiumSet|None, SpotQuote|None)：平仓估值取数（X1–X3）；
    - collect_oi(code, today) -> OiSet|None：实时采集该品种候选月期权 OI；refresh(codes|None, force) -> IndexDailySet；
    - report_dir：日报落盘目录（默认 data/reports；自检注入 temp）。"""

    th: ThresholdSet
    assemble_factory: Callable[[str | None, dt.date | None], "AssembleFn"]
    probe: Callable[[str], MarketSession]
    pool_codes: Callable[[], list[str]]
    positions: Callable[[], "PositionBook"]
    events: Callable[[], "EventCalendar"]
    holidays: Callable[[], "HolidayCalendar"]
    closing_fetch: Callable[["Position", dt.date, "bool | None"], tuple]
    collect_oi: Callable[[str, dt.date], "OiSet | None"]
    refresh: Callable[[list | None, bool], "IndexDailySet"]
    report_dir: Path = REPORTS_DIR


def _exchange_of(code: str, varieties: list) -> str | None:
    """大小写不敏感比对（D01 代码混用大小写：DCE/SHFE 小写 m/rb、CZCE 大写 AP/SA）。"""
    for v in varieties:
        if (getattr(v, "code", None) or "").upper() == (code or "").upper():
            return getattr(v, "exchange", None)
    return None


_CP_RE = re.compile(r"\d{3,4}-?([CP])-?\d")   # 期权 symbol 月份后紧邻的 C/P（m2611-P-3000 / rb2611C3100）


def _cp_from(position: "Position") -> str:
    """从持仓推断卖腿 C/P：优先 structure（single_call→C / single_put→P），否则解析卖腿 symbol。"""
    st = (position.structure or "").lower()
    if "call" in st:
        return "C"
    if "put" in st:
        return "P"
    m = _CP_RE.search((position.short_leg.symbol or "").upper())
    return m.group(1) if m else "P"   # 解析不出→回落 P（fetch_premium 只用 symbol 取数、cp 仅透传展示）


def _real_closing_fetch(bundle: "FetchBundle", verbose: bool):
    """真实平仓取数：从持仓腿重建 ChainContract 取权利金（D09/D10）+ 建候选月取标的现价（D08）。"""
    from ..fetchers.d07_candidate_months import CandidateMonth, CandidateSet
    from ..fetchers.d09_d10_premium import ROLE_PROTECTION, ROLE_SHORT
    from ..fetchers.models import ChainContract

    def _fetch(position: "Position", today: dt.date, market_open):
        code = position.code or ""
        exchange = position.exchange or _exchange_of(code, bundle.varieties())
        month = position.underlying_month
        legs: list[ChainContract] = []
        roles: dict[str, str] = {}
        cp = _cp_from(position)
        for leg, role in ((position.short_leg, ROLE_SHORT), (position.protection_leg, ROLE_PROTECTION)):
            if leg is None or not leg.symbol:
                continue
            legs.append(ChainContract(code=code, exchange=exchange or "", underlying_month=month or "",
                                      expiry=today, cp=cp, strike=leg.strike or 0.0, symbol=leg.symbol))
            roles[leg.symbol] = role
        premium = bundle.premium(legs, today, roles, market_open) if legs else None
        spot = None
        if code and exchange and month:
            fsym = bundle.templates().futures_symbol(code, exchange, month)
            # is_main 写死 False：平仓链无消费方（估值只看已持仓月）——不值得为此多碰一次 get_main_instrument；
            # 开仓候选链的主力标记已在 assemble._RealFetch.candidates 接线
            cand = CandidateMonth(code=code, exchange=exchange, month4=month, expiry=today, dte=0,
                                  is_main=False, futures_symbol=fsym)
            spot = bundle.spot(CandidateSet({code: (cand,)}), today, market_open).get(code, month)
        return premium, spot

    return _fetch


def _real_collect_oi(bundle: "FetchBundle", th: ThresholdSet, verbose: bool):
    """真实 OI 采集：取该品种候选月的全部期权合约，实时取 OI（D14 get_history_n→position，不使用缓存）。"""
    from ..fetchers.d07_candidate_months import D07Params

    def _collect(code: str, today: dt.date):
        exchange = _exchange_of(code, bundle.varieties())
        if exchange is None:
            return None
        chain = bundle.chain([(code, exchange)], today).for_code(code)
        if not chain:
            return None
        params = D07Params(dte_min=th.dte_min, dte_max=th.dte_max, top_n=th.candidate_months)
        cands = bundle.candidates(chain, today, params, bundle.templates())
        months = {m.month4 for m in cands.months_for(code)}
        contracts = [c for c in chain if c.underlying_month in months]
        if not contracts:
            return None
        return bundle.oi(contracts, today, {}, True)   # market_open=True：显式实时采集（不走过期盘中源跳过逻辑）

    return _collect


def _real_refresh(verbose: bool):
    """真实日线刷新：fetch_d12 后复权主连日线（codes=None→D03 全表；force→use_cache=False 强制重拉）。"""
    from ..fetchers.d03_index_symbol import fetch_d03
    from ..fetchers.d12_index_daily import fetch_d12

    def _refresh(codes: list | None, force: bool):
        return fetch_d12(codes, dt.date.today(), index_table=fetch_d03(),
                         use_cache=not force, verbose=verbose)

    return _refresh


def default_runtime(*, verbose: bool = False) -> CliRuntime:
    """构造真实运行时（生产用）。共享一套 assemble FetchBundle 于 scan/check/exit/collect-oi。"""
    th = load_thresholds()
    bundle = default_bundle(verbose=verbose)
    return CliRuntime(
        th=th,
        assemble_factory=lambda struct, today: make_assemble(th, bundle, structure=struct, today=today),
        probe=lambda code: probe_session(code=code),
        pool_codes=lambda: [v.code for v in fetch_d01()],
        positions=fetch_d16,
        events=lambda: fetch_d15_events(include_expiry=True, include_macro=True),  # 到期(tbpy)+FOMC(联网)自动派生，拉取不到优雅降级
        holidays=lambda: fetch_d15_holidays(long_min=th.long_holiday_min),  # 长假＝读 calendar.db.manual_calendar（is_open=0 闭市日连休分组）
        closing_fetch=_real_closing_fetch(bundle, verbose),
        collect_oi=_real_collect_oi(bundle, th, verbose),
        refresh=_real_refresh(verbose),
    )


# ---------------------------------------------------------------------------
# 编排小工具（纯逻辑，离线可测）
# ---------------------------------------------------------------------------
def _resolve_session(mode: str | None) -> tuple[MarketSession | None, str]:
    """--session 覆盖 → (强制 MarketSession, 报告头时段判定方式文案)；auto/None → (None, 自动探测)。

    返回 session=None 时，runner 逐品种各探时段（各品种夜盘不同）；显式 session 则全池统一（命令覆盖）。"""
    if mode == "open":
        return (MarketSession(True, "命令 --session open 强制开盘（全链路）", "--session:open", None),
                "命令覆盖：--session open（强制开盘·全链路）")
    if mode == "closed":
        return (MarketSession(False, "命令 --session closed 强制休市（粗筛）", "--session:closed", None),
                "命令覆盖：--session closed（强制休市·粗筛）")
    return None, "自动探测（逐品种各探）"


def _position_risk(rt: CliRuntime):
    """C 块：仓位风控（P1 仓位限）——盘后一律可跑（无盘中依赖）。"""
    return evaluate_position(PositionData(book=rt.positions()), rt.th)


def _dispositions(rt: CliRuntime, today: dt.date, session: MarketSession | None) -> tuple[list, bool]:
    """B 块：逐持仓平仓处置（§7.3 X1–X4）。时段：--session 覆盖优先，否则按品种各探。

    持仓簿未加载 → (空列表, False)；平仓估值缺 → evaluate_closing 相应判 MISSING。
    返回 (dispositions, book_loaded) 供 report 区分「无持仓」与「持仓不可用」。"""
    book = rt.positions()
    if not getattr(book, "loaded", False):
        return [], False
    out = []
    for p in book.positions:
        mo = session.open if session is not None else rt.probe(p.code or "").open
        premium, spot = rt.closing_fetch(p, today, mo)
        summary, results = evaluate_closing(ClosingData(position=p, premium=premium, spot=spot, today=today), rt.th)
        out.append(make_disposition(p.position_id, p.code or "?", summary, results))
    return out, True


def _reminders(rt: CliRuntime, today: dt.date) -> tuple[list[str], list[str]]:
    """D 块：事件（#6/D15）+ 长假（§7.3/D15）提醒文本（只标注、不阻断）。日历缺失 → 一条不可用提示。"""
    ev, ho = rt.events(), rt.holidays()
    if getattr(ev, "loaded", False):
        ev_texts = [h.text for h in ev.upcoming(today, days=rt.th.event_warn_days)]
    else:
        ev_texts = [f"⚠ 事件日历不可用（{getattr(ev, 'load_error', '未加载')}）→ 无法判 #6，人工核对"]
    if getattr(ho, "loaded", False):
        ho_texts = [h.text for h in ho.upcoming(today, days=rt.th.holiday_warn_days)]
    else:
        ho_texts = [f"⚠ 长假日历不可用（{getattr(ho, 'load_error', '未加载')}）→ 无法判长假提示，人工核对"]
    return ev_texts, ho_texts


def _build_report(rt: CliRuntime, runs: list, mode: str, today: dt.date,
                  session: MarketSession | None) -> ReportData:
    """把 A（开仓候选）+ B（平仓处置）+ C（仓位风控）+ D（提醒）合成一份综合日报输入（§八）。"""
    pos_summary, pos_results = _position_risk(rt)
    ev_texts, ho_texts = _reminders(rt, today)
    dispositions, book_loaded = _dispositions(rt, today, session)
    note = "" if book_loaded else "持仓记录不可用 → B 块未跑（positions.db 缺失或损坏）"
    return ReportData(
        candidates=runs, dispositions=dispositions, book_loaded=book_loaded, book_note=note,
        position_summary=pos_summary, position_results=pos_results,
        event_reminders=ev_texts, holiday_reminders=ho_texts,
        generated_at=dt.datetime.now(), session_mode=mode,
    )


def _latest_report(dir: Path) -> Path | None:
    """最新一份日报（仅认文件名形如 <ISO date>.md，字典序即时序，取末个）；无则 None。

    显式按日期格式过滤，避免目录里非日报的杂项 .md（如手测产出物）因名字字典序大于日期名而被误选。"""
    d = Path(dir)
    if not d.exists():
        return None
    dated = []
    for f in d.glob("*.md"):
        try:
            dt.date.fromisoformat(f.stem)
        except ValueError:
            continue
        dated.append(f)
    return sorted(dated)[-1] if dated else None


# ---------------------------------------------------------------------------
# 打印小工具（控制台简报；详细见落盘日报）
# ---------------------------------------------------------------------------
def _render_runs(runs: list) -> str:
    """渲染候选简报表（返回字符串，由调用方 print，使日志位置落在触发命令上而非本 helper）。"""
    lines = [f"  {'品种':<6}{'结构':<14}{'时段':<6}{'汇总':<8}标签"]
    for r in runs:
        data = r.data
        struct = getattr(data, "structure", "—") if data is not None else "—"
        sess = "开盘" if r.session.open else "休市"
        lines.append(f"  {r.code:<6}{str(struct):<14}{sess:<6}{r.summary.verdict.value:<8}{r.label}")
    return "\n".join(lines)





# ---------------------------------------------------------------------------
# 各命令实现（吃 CliRuntime + argparse.Namespace；返回进程退出码）
# ---------------------------------------------------------------------------
def _prewarm(rt: "CliRuntime", today: dt.date, verbose: bool = False) -> None:
    """扫描前批量预取现货与指数（盘后/假日）。

    现货/指数按 ≤20/批 + 62s 冷却 汇总成少数几次 get_history_n（约 10 分钟）写入 spot.db/daily.db 缓存；
    run_pool 后续走缓存秒回。
    期权权利金+OI 因选腿依赖各品种自身收盘价、须逐品种取数（47 品种 ≈ 47 次×62s≈48 分钟，
    tbpy 硬限「每 60s 仅 1 次历史查询」决定的下限；首跑写入 premium.db，之后重跑秒回），
    故此处不预取，交由 run_pool 逐品种 combo（命中缓存则跳过）。"""
    from sell_option.strategy.assemble import default_bundle
    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.d07_candidate_months import D07Params, CandidateMonth, CandidateSet

    bundle = default_bundle(verbose=verbose)
    th = rt.th
    varieties = fetch_d01()
    all_codes: list = []
    spot_map: dict = {}
    for v in varieties:
        code = v.code
        exch = v.exchange
        all_codes.append(code)
        chain = bundle.chain([(code, exch)], today).for_code(code)
        if not chain:
            continue
        params = D07Params(dte_min=th.dte_min, dte_max=th.dte_max, top_n=th.candidate_months)
        months = bundle.candidates(chain, today, params, bundle.templates()).months_for(code)
        if not months:
            continue
        for m in months:
            fsym = bundle.templates().futures_symbol(code, exch, m.month4)
            if fsym:
                cm = CandidateMonth(code=code, exchange=exch, month4=m.month4, expiry=today,
                                    dte=0, is_main=False, futures_symbol=fsym)
                spot_map.setdefault(code, []).append(cm)
    if spot_map:
        cand_set = CandidateSet({c: tuple(ms) for c, ms in spot_map.items()})
        n_months = sum(len(ms) for ms in spot_map.values())
        print(f"[prewarm] spot：{n_months} 候选月合约 → 批量取现价（≤20/批 + 62s）…", flush=True)
        bundle.spot(cand_set, today, False)
    print(f"[prewarm] index：{len(all_codes)} 品种 → 批量取指数日线…", flush=True)
    bundle.index_daily(all_codes, today)
    print("[prewarm] 现货/指数预取完成；期权权利金+OI 由 run_pool 逐品种取数（首跑后 premium.db 缓存、重跑秒回）。", flush=True)


@log_call()
def cmd_scan(rt: CliRuntime, args, today: dt.date | None = None) -> int:
    """scan：全池扫描 → 生成综合日报（§八 A/B/C/D）+ 控制台简报。"""
    today = today or dt.date.today()
    t0 = dt.datetime.now()
    session, mode = _resolve_session(getattr(args, "session", None))
    codes = rt.pool_codes()
    logger.info("scan 启动：全池 %d 品种 · 时段=%s", len(codes), mode)
    with flow("scan 全池扫描", 品种数=len(codes), 时段=mode):
        # 盘后/假日：先一次性批量预取（≤20/批 + 62s 冷却），run_pool 后续走缓存秒回
        if not getattr(session, "open", False):
            with flow("预取 现货/指数", 时段=mode):
                try:
                    _prewarm(rt, today, verbose=False)
                except Exception as e:  # noqa: BLE001
                    print(f"[scan] 预取异常，退化为逐品种取数：{type(e).__name__}: {e}", flush=True)
        with flow("run_pool 全池取数+检查", 品种数=len(codes), 时段=mode):
            runs = run_pool(codes, rt.th, assemble=rt.assemble_factory(None, today), session=session, probe=rt.probe)
    print(_render_runs(runs))
    # ---- 早筛闸统计：取期权数据前被便宜阻断检查断定淘汰的品种 → 省掉其 62s 期权取数 ----
    excluded = [(r.code, getattr(r.data, "early_excluded", None))
                for r in runs if getattr(r.data, "early_excluded", None)]
    if excluded:
        from collections import Counter
        by_rule = Counter(rid for _, rid in excluded)
        rule_break = " · ".join(f"{rid}:{n}" for rid, n in sorted(by_rule.items()))
        est = len(excluded) * 62
        print(f"[scan] 早筛淘除 {len(excluded)}/{len(codes)} 品种（{rule_break}）→ "
              f"省去 ≈{est // 60}分{est % 60}秒 期权取数（62s/品种）", flush=True)
        print(f"[scan]   被淘除品种：{', '.join(c for c, _ in excluded)}", flush=True)
    else:
        print("[scan] 早筛未淘除任何品种（全部进入期权取数）", flush=True)
    # ---- 漏斗淘汰统计：机会是被哪个检查卡掉的（先量测再调阈值，别凭感觉改 rw_min/otm_pct/oi_min）----
    from .funnel import build_funnel_stats, render_funnel_text

    print(render_funnel_text(build_funnel_stats(runs)), flush=True)
    data = _build_report(rt, runs, mode, today, session)
    path = write_report(data, date=today, dir=rt.report_dir)
    elapsed = (dt.datetime.now() - t0).total_seconds()
    logger.info("综合日报已生成 → %s（耗时 %d分%d秒）", path, elapsed // 60, elapsed % 60)
    return 0


@log_call()
def cmd_check(rt: CliRuntime, args, today: dt.date | None = None) -> int:
    """check M [--dual]：单品种完整 10 项检查（--dual 强制评估铁鹰双卖）。"""
    today = today or dt.date.today()
    code = args.code
    struct = "dual" if getattr(args, "dual", False) else None
    session, mode = _resolve_session(getattr(args, "session", None))
    print(f"[check] 品种 {code} · 结构={'强制双卖(dual)' if struct else '随 #3 建议'} · 时段={mode}")
    run = run_candidate(code, rt.th, assemble=rt.assemble_factory(struct, today), session=session, probe=rt.probe)
    print(f"  时段：{'开盘' if run.session.open else '休市'}（{run.session.reason}）")
    for r in run.results:
        skipped = "（休市跳过·待开盘复核）" if r.detail.get("skipped") == "closed_session" else ""
        print(f"  - {r.rule_id} {r.name} [{r.verdict.value}]{skipped} {r.reason}")
    print(f"  汇总：{run.summary.verdict.value} → {run.label}（{run.summary.reason}）")
    return 0


@log_call()
def cmd_exit(rt: CliRuntime, args, today: dt.date | None = None) -> int:
    """exit：持仓平仓扫描（§7.3 X1–X4 → 续持/止盈/止损/破位/到期）。"""
    today = today or dt.date.today()
    session, mode = _resolve_session(getattr(args, "session", None))
    book = rt.positions()
    if not getattr(book, "loaded", False):
        print(f"[exit] 持仓记录不可用（{getattr(book, 'load_error', '未加载')}）→ 平仓扫描不可跑")
        return 1
    dispositions, _book_loaded = _dispositions(rt, today, session)
    print(f"[exit] 持仓 {len(dispositions)} 笔 · 时段={mode}")
    for d in dispositions:
        print(f"  - {d.position_id}（{d.code}）：{d.disposition} ← {d.summary.verdict.value}：{d.summary.reason}")
    if not dispositions:
        print("  （无持仓）")
    return 0


def cmd_collect_oi(rt: CliRuntime, args, today: dt.date | None = None) -> int:
    """collect-oi -c M：采集该品种候选月期权 OI（实时取数、不落盘），展示分布供复核 #2。"""
    today = today or dt.date.today()
    code = args.code
    print(f"[collect-oi] 采集 {code} 候选月期权 OI（实时取数、不使用缓存）…")
    oi = rt.collect_oi(code, today)
    if oi is None:
        print(f"[collect-oi] {code}：无从取链 / 品种不在名单 → 未采集（不冒充）")
        return 1
    dist = oi.distribution() if hasattr(oi, "distribution") else {}
    print(f"[collect-oi] {code}：{len(oi)} 个合约 · 分布 {dist or '（无）'} → 复核 #2（门槛见 oi_min）")
    return 0


def cmd_refresh(rt: CliRuntime, args, today: dt.date | None = None) -> int:
    """refresh [--all]：刷新主连后复权日线缓存（--all 强制重拉、否则只补缺）。"""
    force = bool(getattr(args, "all", False))
    print(f"[refresh] 刷新日线缓存（{'--all 强制全量重拉' if force else '只补当日缺口'}）…")
    res = rt.refresh(None, force)
    dist = res.distribution() if hasattr(res, "distribution") else {}
    print(f"[refresh] 完成：{dist or '（无分布信息）'}")
    return 0


def cmd_scope(rt: CliRuntime, args, today: dt.date | None = None) -> int:
    """scope：品种边界表重算〔待建〕——价格重算逻辑尚未实现（命令面见 §11.2）。"""
    try:
        from ..fetchers.boundary_table import load_boundary_table
    except ImportError:  # 脚本直跑
        from sell_option.fetchers.boundary_table import load_boundary_table
    rows = load_boundary_table()
    print(f"[scope] 品种边界表现有 {len(rows)} 行（权威静态表）。")
    print("[scope] 〔待建〕按日线最新收盘重算价格派生列的逻辑尚未实现——当前仅静态读表、不重算、不冒充。")
    return 0


def cmd_report(rt: CliRuntime, args, today: dt.date | None = None) -> int:
    """report：打印最新一份综合日报。"""
    path = _latest_report(rt.report_dir)
    if path is None:
        print(f"[report] {rt.report_dir} 下尚无日报（先跑 scan）")
        return 1
    print(f"[report] 最新日报 {path.name}：\n")
    print(path.read_text(encoding="utf-8"))
    return 0


# ---------------------------------------------------------------------------
# argparse 命令面 + 入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构建 §11.2 命令面解析器（scan/check/exit/collect-oi/refresh/scope/report + 全局 --session）。"""
    p = argparse.ArgumentParser(prog="sellopt", description="期权卖出策略命令面（§11.2）")
    sub = p.add_subparsers(dest="command", required=True)

    def _session(sp):
        sp.add_argument("--session", choices=["open", "closed"], default=None,
                        help="强制时段覆盖（默认逐品种自动探测）：open=全链路 / closed=粗筛")

    sp = sub.add_parser("scan", help="全池扫描 + 生成综合日报（§八）")
    _session(sp); sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("check", help="单品种完整 10 项检查")
    sp.add_argument("code", help="品种代码（大小写不敏感，如 m / SA / AP）")
    sp.add_argument("--dual", action="store_true", help="强制同时评估双卖（铁鹰）结构")
    _session(sp); sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("exit", help="持仓平仓扫描（§7.3）")
    _session(sp); sp.set_defaults(func=cmd_exit)

    sp = sub.add_parser("collect-oi", help="采集该品种候选月期权 OI（实时取数、复核 #2）")
    sp.add_argument("-c", "--code", dest="code", required=True, help="品种代码（大小写不敏感）")
    sp.set_defaults(func=cmd_collect_oi)

    sp = sub.add_parser("refresh", help="刷新主连后复权日线缓存")
    sp.add_argument("--all", action="store_true", help="强制全量重拉（默认只补当日缺口）")
    sp.set_defaults(func=cmd_refresh)

    sp = sub.add_parser("scope", help="品种边界表重算〔待建〕")
    sp.set_defaults(func=cmd_scope)

    sp = sub.add_parser("report", help="打印最新综合日报")
    sp.set_defaults(func=cmd_report)
    return p


def main(argv: list[str] | None = None, *, rt: CliRuntime | None = None,
         today: dt.date | None = None) -> int:
    """命令面入口：解析 argv → 派发子命令（rt/today 可注入供离线测；默认真实运行时 + 今日）。

    进程退出必释放 tbpy 实例：bundle 各源（market_session 等）经 runtime.connect 占用一个
    tbpy 授权槽，原先 sellopt.py 主入口从不释放 → 多次运行/调试多进程会堆积未释放实例撞
    「TBPY实例超限」。atexit 在解释器退出时统一 tbpy.exit()，与命令成功/异常无关。"""
    import atexit
    from runtime import disconnect
    atexit.register(disconnect)
    args = build_parser().parse_args(argv)
    rt = rt or default_runtime()
    return args.func(rt, args, today=today)


# ---------------------------------------------------------------------------
# 实测：展示命令面配置 + 可用命令清单
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底（须在 setup_logging 重定向之前）
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.logutil import setup_logging
    setup_logging()  # 统一日志（幂等）：之后 print 经重定向进日志文件，控制台经 sys.__stderr__，与 sellopt.py 一致

    from sell_option.fetchers.d01_variety_list import fetch_d01

    th = load_thresholds()
    varieties = [v.code for v in fetch_d01()]
    print("=" * 72)
    print("strategy.cli · 命令面配置")
    print(f"  候选品种：{len(varieties)} 个（{', '.join(varieties[:8])}…）")
    print(f"\n  可用命令：")
    print(f"    scan              全池扫描 → 综合日报（A/B/C/D 四块）")
    print(f"    check M [--dual]  单品种完整检查（--dual 强制铁鹰）")
    print(f"    exit              持仓平仓扫描（X1–X4 出场规则）")
    print(f"    collect-oi -c M   采集该品种候选月期权 OI（复核 #2）")
    print(f"    refresh [--all]   刷新主连后复权日线缓存")
    print(f"    scope             品种边界表〔待建〕")
    print(f"    report            打印最新综合日报")
    print(f"\n  时段覆盖：--session open|closed（默认逐品种自动探测）")
    print(f"\n  关键阈值：")
    print(f"    OTM 展示 {th.otm_pct:.0%} · DTE {th.dte_min}–{th.dte_max} · 候选月 {th.candidate_months}")
    print(f"    止盈 {th.tp_floor:.0%}/{th.tp_extend:.0%} · 止损 {th.sl_multiplier:g}×R · 肥瘦 R/W≥{th.rw_min:.2f}")
    print(f"\n  生产入口：python sellopt.py scan（直跑本模块仅自检、不派发命令）")
    print("=" * 72)
