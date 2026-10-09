"""综合日报合成（§八 输出）：把 opening/closing/position 已算好的结果渲染成一份 Markdown 日报。

策略聚合层的【渲染模块】（与 opening/closing/position/runner 平级）。**只渲染、不取数、不判定**——
所有四态结论、处置动作、风控标签均由上游策略算好后传入，本模块把它们组织成人读日报并落盘
（守三层单向 strategy→checks→fetchers：report 只依赖同层 runner/opening/closing/position 的产物 +
数据层只读类型标注，绝不反向触发取数或重算判定）。

§八 四块（一份 Markdown、盘后一次性生成、落 `data/reports/<date>.md`）：
- **A 开仓机会**（← runner.CandidateRun / opening）：品种 × 结构 × 10 项检查漏斗（可开 / 可开·有提示 /
  待人工 / 淘汰及原因），逐品种明细，**真实价（D08 绝对价）与指数口径（D12 后复权主连·相对指标）分列**
  （§二 串价铁律：混用即缺陷）。双模式由 CandidateRun.session 带入、report 只读不重探。
- **B 持仓处置**（← closing）：逐持仓 → 续持 / 止盈 / 止损 / 破位 / 到期提醒（§7.3），处置动作取
  closing_disposition 预计算结果。
- **C 组合风控**（← position）：笔数 / 每品种 / 同持品种数是否触线（§7.4）。
- **D 提醒与待人工**：事件（#6）/ 长假（只标注、不阻断）＋**全部 MISSING 项汇总清单**（自动从 A/B/C 各
  results 收集；休市跳过项标注「待开盘复核」以别于真缺数）。

**时段状态字段（贯穿报告、必备，§八）**：报告头记生成时刻 + 时段判定方式（自动探测 / --session 覆盖）+
整池状态（一致→单一状态；混合→「逐品种见明细」）；A 块逐候选行记 market_open + MarketSession.reason +
探针合约 + 判定时刻（直接由 CandidateRun.session 带入，report 只读取、不重探）。

运行自检：``python -m sell_option.strategy.report``（展示日报配置 + 已有日报列表）
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

try:  # 包方式（python -m / import）：相对导入
    from ..checks.models import CheckResult, Verdict
    from .runner import CandidateRun
    from .opening import OpeningData, funnel_label
    from .closing import closing_disposition, closing_label
    from .position import risk_label
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import CheckResult, Verdict
    from sell_option.strategy.runner import CandidateRun
    from sell_option.strategy.opening import OpeningData, funnel_label
    from sell_option.strategy.closing import closing_disposition, closing_label
    from sell_option.strategy.position import risk_label

if TYPE_CHECKING:  # 数据层产物类型仅标注用（渲染层运行时鸭子类型读字段）
    from ..fetchers.market_session import MarketSession

# 报告落盘目录（data/ 已 git 忽略；与其他 fetcher 的 parents[1]/data 同范式）
REPORTS_DIR = Path(__file__).resolve().parents[1] / "data" / "reports"

__all__ = [
    "REPORTS_DIR", "ReportData", "PositionDisposition", "make_disposition",
    "render_report", "save_report", "write_report",
]

# 四态 → 报告用文字标记（不用重 emoji，与 checks 四态词汇一致；D 块提醒文本自带 ⚠ 由 EventHit/HolidayHit 生成）
_VERDICT_TAG = {
    Verdict.PASS: "PASS",
    Verdict.WARN: "WARN",
    Verdict.FAIL: "FAIL",
    Verdict.MISSING: "MISSING",
}

# ---------------------------------------------------------------------------
# 早筛短回路(FastFail) 开关态：报告文案动态反映 config.EARLY_EXCLUDE_ENABLED，
# 避免硬编码「已关闭」（claim M：开关打开时说明应与实际行为一致）。
# ---------------------------------------------------------------------------
try:
    import config as _config
except Exception:  # config 不可用时保守按默认（关闭）处理
    _config = None


def _early_exclude_state() -> str:
    """返回『已开启』/『已关闭』，反映当前 EARLY_EXCLUDE_ENABLED。"""
    return "已开启" if bool(getattr(_config, "EARLY_EXCLUDE_ENABLED", False)) else "已关闭"

# 表内紧凑标记（A 块矩阵表用）
_VERDICT_CELL = {Verdict.PASS: "✓", Verdict.WARN: "⚠", Verdict.FAIL: "✗", Verdict.MISSING: "缺"}

# （遗留常量：A 块现由下方 COLUMNS 渲染，下列仅作 #1–#10 规范序备查、不参与出表）
_A_CHECK_IDS = ("#1", "#2", "#3", "#4", "#5", "#6", "#7", "#8", "#9", "#10")
_A_CHECK_NAMES = ("盘口", "OI", "市况", "IV", "安全垫", "事件", "保证金", "预算", "肥瘦", "形态")


# ---------------------------------------------------------------------------
# 输入结构（渲染层契约：上游策略算好的产物，report 只读）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PositionDisposition:
    """B 块一行：一笔持仓的平仓评估产物（处置动作由 closing_disposition 预计算、report 只读）。"""
    position_id: str                       # 持仓标识（如短腿 symbol / 自定义 id）
    code: str                              # 品种
    summary: CheckResult                   # closing 汇总（四态）
    results: list[CheckResult] = field(default_factory=list)   # X1–X4 逐条
    disposition: str = ""                  # closing_disposition 结果（平仓·止盈达标 / 续持·到期提醒 …）


def make_disposition(position_id: str, code: str, summary: CheckResult,
                     results: list[CheckResult]) -> PositionDisposition:
    """便捷装配：从 closing 的 (summary, results) 算出处置动作，组成 B 块行（供 cli/编排层调用）。"""
    return PositionDisposition(position_id, code, summary, results,
                               closing_disposition(summary, results))


@dataclass
class ReportData:
    """一份综合日报的全部输入（渲染层暂存；各块由上游策略算好后注入）。

    - candidates：A 块（runner.run_pool 产物，逐候选带 session/data/summary/results/label）；
    - dispositions：B 块（逐持仓平仓处置）；
    - position_summary / position_results：C 块（position.evaluate_position 产物）；
    - event_reminders / holiday_reminders：D 块提醒文本（来自 #6 事件 / D15 长假，只标注不阻断）；
    - generated_at / session_mode：报告头（生成时刻 + 时段判定方式）。"""

    candidates: list[CandidateRun] = field(default_factory=list)
    dispositions: list[PositionDisposition] = field(default_factory=list)
    book_loaded: bool = True                     # D16 持仓簿是否加载（False → B 块显示不可用提示）
    book_note: str = ""                          # 持仓簿不可用时的人话原因（供 B 块渲染）
    position_summary: CheckResult | None = None
    position_results: list[CheckResult] = field(default_factory=list)
    event_reminders: list[str] = field(default_factory=list)
    holiday_reminders: list[str] = field(default_factory=list)
    generated_at: dt.datetime = field(default_factory=dt.datetime.now)
    session_mode: str = "自动探测（逐品种各探）"


# ---------------------------------------------------------------------------
# 渲染小工具（纯函数，离线可测）
# ---------------------------------------------------------------------------
def _tag(verdict: Verdict) -> str:
    return _VERDICT_TAG.get(verdict, str(verdict))


def _fmt_date(when) -> str:
    """判定/生成时刻的人话渲染（None＝「此刻」）。"""
    if when is None:
        return "此刻"
    if isinstance(when, dt.datetime):
        return when.strftime("%Y-%m-%d %H:%M:%S")
    return str(when)


def _short_session(session: "MarketSession") -> str:
    """时段状态短标（A 块表格用）：开盘 / 休市。"""
    return "开盘" if getattr(session, "open", False) else "休市"


def _pool_session_status(candidates: list[CandidateRun]) -> str:
    """整池时段状态：全一致→单一状态；混合→「逐品种见明细」（§八 报告头）。"""
    if not candidates:
        return "无候选（未跑开仓扫描）"
    opens = {c.session.open for c in candidates}
    if len(opens) == 1:
        o = next(iter(opens))
        return "开盘·全链路（权威结论）" if o else "休市·粗筛（次日候选，盘口待开盘复核）"
    return "混合（部分品种开盘、部分休市 → 逐品种见 A 块明细）"


def _caliber_lines(data: "OpeningData | None") -> list[str]:
    """A 块「真实价与指数口径分列」（§二 串价铁律：绝对价只取真实合约、相对指标只建在指数上）。"""
    if data is None:
        return ["行情口径：装配失败 · 无行情数据（见汇总原因）"]
    lines: list[str] = []
    spot = data.spot                                    # 真实价（绝对价·当下真实合约 D08）
    if spot is not None and spot.price is not None:
        lines.append(f"真实价（绝对价·D08）：{spot.price:g} ＠ {spot.futures_symbol or '?'}"
                     f"（源 {spot.source}·{ _fmt_date(spot.bar_date) }）")
    else:
        lines.append("真实价（绝对价·D08）：—（缺，绝对价不冒充）")
    idx = data.index_daily                              # 指数口径（相对指标·后复权主连 D12）
    last_close = next((c for c in reversed(idx.close) if c is not None), None) if idx is not None else None
    if idx is not None and last_close is not None:
        lines.append(f"指数口径（相对指标·D12 后复权主连）：{last_close:g} ＠ {idx.index_symbol or '?'}"
                     f"（{idx.count} 根·{ _fmt_date(idx.bar_date) }）※ 仅供 #3/#4/#10 相对指标，非绝对价")
    else:
        lines.append("指数口径（相对指标·D12 后复权主连）：—（缺，长窗口指标不可判）")
    return lines


def _check_line(r: CheckResult) -> str:
    """逐条检查渲染（仿 HTML 紧凑单行）：`[PASS] #1 盘口价差：原因`。"""
    skipped = "（休市跳过·待开盘复核）" if r.detail.get("skipped") == "closed_session" else ""
    return f"  - [{_tag(r.verdict)}] {r.rule_id} {r.name}：{r.reason}{skipped}"


def _collect_manual(data: ReportData) -> list[tuple[str, CheckResult]]:
    """D 块「全部 MISSING 项汇总清单」：自动从 A/B/C 各 results 收集（含装配失败的 A 汇总）。"""
    out: list[tuple[str, CheckResult]] = []
    for c in data.candidates:                           # A：逐候选 results + 装配失败时的 summary
        if c.data is None and c.summary.verdict == Verdict.MISSING:
            out.append((f"A·{c.code}", c.summary))
        for r in c.results:
            if r.verdict == Verdict.MISSING:
                out.append((f"A·{c.code}", r))
    for d in data.dispositions:                         # B：逐持仓 results
        for r in d.results:
            if r.verdict == Verdict.MISSING:
                out.append((f"B·{d.position_id}", r))
    for r in data.position_results:                     # C：风控 results
        if r.verdict == Verdict.MISSING:
            out.append((f"C·{r.rule_id}", r))
    return out


# ---------------------------------------------------------------------------
# 各块渲染（返回 Markdown 行列表；render_report 拼接）
# ---------------------------------------------------------------------------
def _render_header(data: ReportData) -> list[str]:
    return [
        "# 期权卖出策略 · 综合日报",
        "",
        f"- **生成时刻**：{data.generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **时段判定方式**：{data.session_mode}",
        f"- **整池时段状态**：{_pool_session_status(data.candidates)}",
        f"- **规模**：开仓候选 {len(data.candidates)} · 持仓处置 {len(data.dispositions)}"
        f" · 待人工 {len(_collect_manual(data))} 项",
        "",
        "> 只读决策支持：本报告不接账户、不下单；「可开」仅机械检查通过，下单前须人工核对盘口与保证金实数。",
        "",
    ]


def _vcell(verdict: Verdict) -> str:
    """A 块矩阵表内紧凑标记。"""
    return _VERDICT_CELL.get(verdict, "?")

# 评估口径结构（single/dual）→ 中文标签（§7.2：dual=铁鹰双卖、single=单腿价差）
_STRUCT_LABEL = {"single": "单腿", "dual": "双卖铁鹰"}

def _struct_label(s: str) -> str:
    """结构列：single/dual 内部枚举 → 可读中文（未知值原样返回）。"""
    return _STRUCT_LABEL.get(s, s)


def _margin_cell(r: CheckResult) -> str:
    """#7 保证金专用单元格：显示实际估算金额（元）+ 判定符号，供品种间资金占用对比。

    用 single_verdict（单笔判定）决定符号，因为合计判定可能因存量持仓缺失而 MISSING，但单笔金额仍有参考价值。
    PASS → `✓ {金额}元`；FAIL → `✗ {金额}元超`；MISSING/缺数 → `?`。"""
    sm = r.detail.get("single_margin")
    if sm is None:
        return "?"
    val = f"{sm:,.0f}"
    single_v = r.detail.get("single_verdict")
    if single_v == "FAIL":
        return f"✗{val}元超"
    if single_v == "PASS":
        return f"✓{val}元"
    if single_v == "WARN":
        return f"⚠{val}元"
    return "?"


def _check_cell(rid: str, r: "CheckResult | None") -> str:
    """A 块矩阵单元：判定符号 + 关键数值（仿 #7 保证金『✓8,411元』的『符号+值』风格）。

    #4 IV→附 IV 百分位；#5 安全垫→附 σ 倍数；#7 保证金→专用 _margin_cell；
    #8 预算→附最大亏损；#9 肥瘦→附 R/W；#2 OI→卖腿/保护腿数值；#3 市况→regime+ADX；
    #6 事件→有/无（具体内容见 D 块）。#1/#10 无头条数值，仅符号。"""
    if r is None:
        return "—"
    sym = _vcell(r.verdict)
    d = r.detail or {}
    if rid == "#7":
        return _margin_cell(r)
    if rid == "#4":
        p = d.get("percentile")
        return f"{sym}{_fmt_num(p, 'ppct')}" if p is not None else sym
    if rid == "#5":
        s = d.get("sigma_multiple")
        return f"{sym}{_fmt_num(s, 'sigma')}σ" if s is not None else sym
    if rid == "#8":
        m = d.get("max_loss")
        return f"{sym}{_fmt_num(m)}" if m is not None else sym
    if rid == "#9":
        rw = d.get("rw_ratio")
        return f"{sym}{_fmt_num(rw, 'pct')}" if rw is not None else sym
    if rid == "#2":
        legs = d.get("legs") or []
        ois = []
        for lg in legs:
            if not isinstance(lg, dict):
                continue
            o = lg.get("oi")                       # 结构：leg["oi"] 可能是 {...} 或裸数值
            if isinstance(o, dict):
                o = o.get("oi")
            if isinstance(o, (int, float)):
                ois.append(o)
        if ois:
            return f"{sym}{'/'.join(_fmt_num(o) for o in ois)}"
        return sym
    if rid == "#3":                                   # 市况：regime + ADX（+ 连N同向软动能，原 #10 形态上移）
        regime = d.get("regime")
        if regime is None:
            return sym
        label = {"ranging": "震荡", "uptrend": "上涨", "downtrend": "下跌"}.get(regime, regime)
        adx = d.get("adx")
        s = f"{label} ADX{adx:.1f}" if isinstance(adx, (int, float)) else label
        run = d.get("consecutive_run")
        sign = d.get("consecutive_sign")
        if isinstance(run, int) and run >= 5 and sign in (1, -1):
            s += f"·连{run}{'↑' if sign > 0 else '↓'}"
        return f"{sym}{s}"
    if rid == "#6":                                   # 事件：A 块只标「有/无」，具体内容见 D 块
        if r.verdict == Verdict.MISSING:
            return "缺"
        hits = d.get("hits") or []
        return "有" if hits else "无"
    if rid == "#10":                                  # 形态：✗ 时附触发的危险形态
        fired = d.get("fired") or []
        if fired:
            s = fired[0].split("≥")[0].strip()        # 去阈值比较后缀，留形态名+数值
            return f"{sym}{s}"
        return sym
    if rid == "#1":                                   # 盘口：附最宽腿价差比（≤10% 过关）
        legs = d.get("legs") or []
        ratios = []
        for lg in legs:
            if isinstance(lg, dict):
                sp = lg.get("spread")
                if isinstance(sp, dict) and isinstance(sp.get("ratio"), (int, float)):
                    ratios.append(sp["ratio"])
        if ratios:
            return f"{sym}{_fmt_num(max(ratios), 'pct')}"
        return sym
    return sym


def _result_map(results: list[CheckResult]) -> dict[str, CheckResult]:
    """把 results 列表转成 {rule_id: CheckResult} 字典（A 块矩阵取数用）。"""
    return {r.rule_id: r for r in results}


def _fmt_num(x, kind: str = "money") -> str:
    """数值→表格单元格文本：None/非数→'—'；money 千分位、pct 小数百分比(0–1→%)、ppct 已是百分比(0–100→%)、sigma 两位小数。

    注意量纲：'pct' 用于 0–1 小数（如肥瘦 R/W=0.06 → 6.0%）；'ppct' 用于 openvlab 直接给出的
    0–100 量表（如 IV 百分位=75.83 → 75.8%），二者不可混用（错用会二次 ×100 成 7583% 的显示 bug）。"""
    if x is None or isinstance(x, bool):
        return "—"
    if kind == "money":
        return f"{x:,.0f}"
    if kind == "pct":
        return f"{x:.1%}"
    if kind == "ppct":
        return f"{x:.1f}%"
    if kind == "sigma":
        return f"{x:.2f}"
    return str(x)


def _early_excluded(c: "CandidateRun") -> str | None:
    """取该候选的早筛阻断标记：取期权/OI 数据前已被便宜检查断定淘汰（assemble 早筛闸 short-circuit）。"""
    data = getattr(c, "data", None)
    return getattr(data, "early_excluded", None) if data is not None else None


def _conclusion_label(c: "CandidateRun") -> str:
    """结论单元格：漏斗标签（＋若早筛短回路开启时被阻断的规则标记；FastFail 默认关闭，一般只显漏斗标签）。"""
    label = c.label or funnel_label(c.summary.verdict)
    excl = _early_excluded(c)
    return f"{label}·早筛{excl}" if excl else label


def _exclusion_note(c: "CandidateRun") -> str | None:
    """逐品种明细『取数状态』一句话：完整取数 / 取数后淘汰 / 待人工，便于核对数据齐备性。"""
    excl = _early_excluded(c)
    if excl:
        return (f"早筛阻断（{excl}）→ 取期权/OI 前已淘汰，故 #2/#8/#9 显「缺」为设计内省额"
                f"（非数据缺失；市场里该品种 OI 真实存在）")
    v = c.summary.verdict
    if v == Verdict.FAIL:
        note = "取数后淘汰（已过早筛·期权/OI 已取，被 #9 肥瘦等后期规则否决；数据齐备）"
        econ = getattr(c, "data", None)
        nc = getattr(econ, "net_credit", None) if econ is not None else None
        if nc is not None and abs(nc) < 0.5:
            note += "；净信用≈0（极薄价差：两腿权利金近乎互抵，非真·取数失败）"
        return note
    if v in (Verdict.PASS, Verdict.WARN):
        return "完整取数（早筛通过·期权/OI 已取）"
    return None  # MISSING：交由明细 reason 说明（休市跳过 / 真缺数）


def _render_a(candidates: list[CandidateRun]) -> list[str]:
    exc = _early_exclude_state()
    lines = ["## A · 开仓机会（← opening 漏斗）", "",
             f"> ✓通过 · ⚠提醒 · ✗否决 · 缺=休市跳过/真缺；早筛短回路(FastFail)={exc}；"
             "列序：品种→盘口/OI/市况/形态/事件→保证金/最大亏/σ/IV%→真实价/R净/W宽/肥瘦→结论", ""]
    if not candidates:
        lines += ["_（无开仓候选：未跑 scan/check，或全池为空。）_", ""]
        return lines
    # 合并总表：一行一品种，列按「决策先后 + 列间关系」排序（§五 漏斗顺序）：
    #   A 标识时段 → B 市场可交易性(#1/#2/#3/#10形态/#6，#10 紧邻 #3 市况，二者同读指数日线) →
    #   C 资金风险(#7/#8/#5/#4) → D 收益结构(真实价/R净/W宽/肥瘦，肥瘦=R净÷W宽 派生) → E 决策(结论，最右)
    COLUMNS = [
        ("品种", "code"), ("结构", "struct"), ("时段", "session"),
        ("盘口", ("check", "#1")), ("OI", ("check", "#2")),
        ("市况", ("check", "#3")), ("形态", ("check", "#10")), ("事件", ("check", "#6")),
        ("保证金", ("check", "#7")), ("最大亏", ("check", "#8")),
        ("σ", ("check", "#5")), ("IV%", ("check", "#4")),
        ("真实价", "raw_price"), ("R净", "raw_credit"), ("W宽", "raw_width"),
        ("肥瘦", ("check", "#9")),
        ("结论", "conclusion"),
    ]

    hdr = "| " + " | ".join(name for name, _ in COLUMNS) + " |"
    ncols = len(COLUMNS)
    lines += [hdr, "|" + "---|" * ncols]

    def _gv(rmap, rid, key):
        r = rmap.get(rid)
        return r.detail.get(key) if r is not None else None

    for c in candidates:
        d = c.data
        struct = _struct_label(getattr(d, "structure", "?")) if d is not None else "—"
        label = _conclusion_label(c)
        rmap = _result_map(c.results)
        session = _short_session(c.session)
        price = getattr(getattr(d, "spot", None), "price", None) if d is not None else None
        cells = []
        for _name, spec in COLUMNS:
            if spec == "code":
                cells.append(c.code)
            elif spec == "struct":
                cells.append(struct)
            elif spec == "session":
                cells.append(session)
            elif spec == "conclusion":
                cells.append(label)
            elif spec == "veto":
                cells.append(_check_cell("#10", rmap.get("#10")))
            elif spec == "raw_price":
                cells.append(_fmt_num(price))
            elif spec == "raw_credit":
                cells.append(_fmt_num(_gv(rmap, "#9", "net_credit")))
            elif spec == "raw_width":
                cells.append(_fmt_num(_gv(rmap, "#9", "spread_width")))
            elif isinstance(spec, tuple) and spec[0] == "check":
                cells.append(_check_cell(spec[1], rmap.get(spec[1])))
            else:
                cells.append("—")
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    # 逐品种明细（候选给完整明细；淘汰信息已在总表 + 漏斗统计中体现，不单列）
    actionable = [c for c in candidates if (c.label or funnel_label(c.summary.verdict)) != "淘汰"]
    lines += ["### 候选明细（次日候选 / 待人工）", ""]
    if actionable:
        for c in actionable:
            lines += _render_candidate_detail(c)
    else:
        lines += ["_（无候选 / 待人工）_", ""]
    return lines


# HTML 模式二 用中文「提醒」替代 WARN；其余沿用英文（与总表一致）
_TAG_COLOR = {
    "PASS":    ("#dafbe1", "#1a7f37"),
    "WARN":    ("#fff1e5", "#bc4c00"),
    "MISSING": ("#fff8c5", "#9a6700"),
    "FAIL":    ("#ffebe9", "#cf222e"),
}


def _cand_legs(d) -> str | None:
    """从期权链抽取选腿（role@strike），用于明细「选腿」行。"""
    ps = getattr(d, "premium", None) if d is not None else None
    qs = getattr(ps, "quotes", None) if ps is not None else None
    if not qs:
        return None
    parts = []
    for q in qs.values():
        role = getattr(q, "role", "")
        strike = getattr(q, "strike", None)
        cp = getattr(q, "cp", "")
        if role and strike is not None:
            st = int(strike) if isinstance(strike, float) else strike
            parts.append(f"{role}{cp}@{st}")
    return " · ".join(parts) if parts else None


def _cand_conclusion(c: CandidateRun) -> str:
    results = c.results or []
    n_pass = sum(1 for r in results if r.verdict == Verdict.PASS)
    n_warn = sum(1 for r in results if r.verdict == Verdict.WARN)
    n_fail = sum(1 for r in results if r.verdict == Verdict.FAIL)
    n_miss = sum(1 for r in results if r.verdict == Verdict.MISSING)
    blockers = [r.rule_id for r in results if r.verdict in (Verdict.FAIL, Verdict.MISSING)]
    parts = [f"{n_pass}过", f"{n_warn}提醒", f"{n_fail}否", f"{n_miss}缺"]
    if blockers:
        parts.append("阻断=" + ",".join(blockers))
    return " · ".join(parts)


# HTML 模式二 用中文「提醒」替代 WARN；其余沿用英文（与总表一致）
def _disp_tag(verdict: str) -> str:
    """固定宽度显示标签（保证 <pre> 内对齐）。"""
    return {"PASS": "[PASS]", "WARN": "[WARN]",
            "MISSING": "[MISS]", "FAIL": "[FAIL]"}.get(verdict, f"[{verdict}]")


def _disp_w(s: str) -> int:
    """显示宽度：CJK 计 2，其余计 1。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


_NAME_W = 8  # 名称列显示宽度（CJK 计 2），不足补空格使『：』对齐


def _cand_check_html(r: CheckResult) -> str:
    """候选明细逐条：彩色状态块 + 对齐（原始 HTML <pre>，预览下等宽对齐）。"""
    v = _tag(r.verdict)
    bg, fg = _TAG_COLOR.get(v, ("#eaeef2", "#57606a"))
    skipped = "（休市跳过·待开盘复核）" if r.detail.get("skipped") == "closed_session" else ""
    name_pad = r.name + " " * max(0, _NAME_W - _disp_w(r.name))
    chip = (f'<span style="background:{bg};color:{fg};padding:1px 6px;'
            f'border-radius:4px;font-weight:700;">{_disp_tag(v)}</span>')
    return f'{chip}{r.rule_id:>4} {name_pad}：{r.reason}{skipped}'


def _render_candidate_detail(c: CandidateRun) -> list[str]:
    """候选(次日候选/待人工) 明细：彩色状态块 + 等宽对齐（原始 HTML <pre> 块）。"""
    struct = _struct_label(getattr(c.data, "structure", "?")) if c.data is not None else "—"
    label = _conclusion_label(c)
    price = getattr(getattr(c.data, "spot", None), "price", None) if c.data is not None else None
    bar = getattr(getattr(c.data, "spot", None), "bar_date", None) if c.data is not None else None
    if isinstance(bar, dt.date):
        date = bar.isoformat()
    elif bar:
        date = str(bar)
    else:
        date = dt.date.today().isoformat()
    lines = [
        f"#### {c.code} · {struct} · {label}",
        "",
        f"标的 {_fmt_num(price)} · 模式={struct} · 数据截至 {date}",
    ]
    legs = _cand_legs(c.data)
    if legs:
        lines.append(f"选腿 {legs}")
    g = getattr(c.data, "guard", None)
    if g:
        lines.append(f"护栏 {g}")
    lines.append("")
    pre: list[str] = [
        '<pre style="font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;'
        ' font-size:13px; line-height:1.7; background:#f6f8fa; padding:10px 12px;'
        ' border:1px solid #d0d7de; border-radius:6px; overflow-x:auto;">',
    ]
    if c.results:
        for r in c.results:
            pre.append(_cand_check_html(r))
    else:
        pre.append("（无检查明细）")
    pre.append("</pre>")
    lines += pre
    lines.append("")
    lines.append(f"**结论**：{label}（{_cand_conclusion(c)}）")
    lines.append("")
    return lines


def _render_b(dispositions: list[PositionDisposition],
              book_loaded: bool = True, book_note: str = "") -> list[str]:
    lines = ["## B · 持仓处置（← closing §7.3）", "",
             "> 逐持仓四态：**FAIL→平仓**（止盈达标/止损/破位/到期必平）；**WARN→续持·有提示**；"
             "**PASS→续持**；**MISSING→待人工**（估值/对照价/DTE 缺失，不冒充）。事件·长假只提示不强平（见 D 块）。", ""]
    if not dispositions:
        if book_loaded is False:
            lines += [f"_（{book_note or '持仓记录不可用 → 平仓扫描未跑'}）_", ""]
        else:
            lines += ["_（无持仓：positions.db 为空或未跑 exit 扫描。）_", ""]
        return lines
    lines += ["| 持仓 | 品种 | 处置 | 汇总 |", "|---|---|---|---|"]
    for d in dispositions:
        lines.append(f"| {d.position_id} | {d.code} | {d.disposition or closing_label(d.summary.verdict)}"
                     f" | [{_tag(d.summary.verdict)}] |")
    lines.append("")
    for d in dispositions:
        lines += [
            f"### {d.position_id}（{d.code}）· {d.disposition or closing_label(d.summary.verdict)}",
            f"- 汇总：[{_tag(d.summary.verdict)}] {d.summary.reason}",
            f"- 出场检查（{len(d.results)}）：",
        ]
        lines += [_check_line(r) for r in d.results] if d.results else ["  - _（无检查明细）_"]
        lines.append("")
    return lines


def _render_c(summary: CheckResult | None, results: list[CheckResult]) -> list[str]:
    lines = ["## C · 组合风控（← position §7.4）", "",
             "> 笔数 / 每品种 / 同持品种数是否触线：**FAIL→触线·暂停开仓**；**WARN→有提示**；"
             "**MISSING→待人工**（D16 缺失）；**PASS→风控通过·可开新仓**。", ""]
    if summary is None:
        lines += ["_（未跑仓位风控：无 D16 持仓，或 skip。）_", ""]
        return lines
    lines += [f"**风控汇总**：[{_tag(summary.verdict)}] {risk_label(summary.verdict)} · {summary.reason}", ""]
    if results:
        lines += ["| 检查项 | 状态 | 原因 |", "|---|---|---|"]
        for r in results:
            lines.append(f"| {r.rule_id} {r.name} | [{_tag(r.verdict)}] | {r.reason} |")
        lines.append("")
    return lines


def _render_d(data: ReportData) -> list[str]:
    lines = ["## D · 提醒与待人工", ""]
    # 事件提醒表
    lines += ["### 事件提醒（#6，只标注·不阻断）", ""]
    if data.event_reminders:
        lines += ["| 提醒 |", "|---|"]
        lines += [f"| {t} |" for t in data.event_reminders]
        lines.append("")
    else:
        lines += ["_（近期无事件提醒）_", ""]
    # 长假提醒表
    lines += ["### 长假提醒（§7.3，只提示·不强制平）", ""]
    if data.holiday_reminders:
        lines += ["| 提醒 |", "|---|"]
        lines += [f"| {t} |" for t in data.holiday_reminders]
        lines.append("")
    else:
        lines += ["_（近期无长假节前提醒）_", ""]
    # MISSING 汇总：按检查项分组统计（取代逐条列表）
    manuals = _collect_manual(data)
    lines += [f"### 待人工（MISSING）汇总（{len(manuals)} 项）",
              "> 数据不足一律 MISSING、绝不用代理数据冒充；休市跳过 ≠ 真缺数。", ""]
    if manuals:
        # 按 rule_id 分组计数 + 按来源(A/B/C)分组
        from collections import Counter
        by_check: Counter = Counter()
        by_source: Counter = Counter()
        skip_count = 0
        for tag, r in manuals:
            by_check[f"{r.rule_id} {r.name}"] += 1
            by_source[tag.split("·")[0]] += 1
            if r.detail.get("skipped") == "closed_session":
                skip_count += 1
        lines += ["| 检查项 | MISSING 次数 |", "|---|---|"]
        for check, cnt in by_check.most_common():
            lines.append(f"| {check} | {cnt} |")
        lines.append("")
        if skip_count:
            lines.append(f"_其中 {skip_count} 项为休市跳过（待开盘复核），非真实数据缺失。_")
            lines.append("")
    else:
        lines.append("_（无 MISSING：全部检查数据齐备）_")
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# 对外入口：渲染 + 落盘
# ---------------------------------------------------------------------------
def _render_funnel(candidates: list[CandidateRun]) -> list[str]:
    """漏斗淘汰统计（诊断）：定位机会是被哪个检查卡掉的（先量测再调阈值）。"""
    from .funnel import build_funnel_stats, render_funnel_md

    return render_funnel_md(build_funnel_stats(candidates))


def render_report(data: ReportData) -> str:
    """把 ReportData 渲染成一份完整的综合日报 Markdown 文本（§八 四块 + 报告头 + 时段状态字段）。"""
    lines: list[str] = []
    lines += _render_header(data)
    lines += _render_a(data.candidates)
    lines += _render_funnel(data.candidates)
    lines += _render_b(data.dispositions, data.book_loaded, data.book_note)
    lines += _render_c(data.position_summary, data.position_results)
    lines += _render_d(data)
    return "\n".join(lines).rstrip() + "\n"


def _is_date_stem(stem: str) -> bool:
    """文件名主干是否为 ISO 日期（``YYYY-MM-DD``）——用于从 reports/ 中只筛真正的日报。"""
    try:
        dt.date.fromisoformat(stem)
    except ValueError:
        return False
    return True


def save_report(md: str, date: dt.date | dt.datetime | None = None,
                dir: Path | str = REPORTS_DIR) -> Path:
    """把日报文本落盘到 `data/reports/<date>.md`（目录不存在则建；返回写入路径）。"""
    d = date or dt.date.today()
    if isinstance(d, dt.datetime):
        d = d.date()
    out_dir = Path(dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{d.isoformat()}.md"
    path.write_text(md, encoding="utf-8")
    return path


def write_report(data: ReportData, date: dt.date | dt.datetime | None = None,
                 dir: Path | str = REPORTS_DIR) -> Path:
    """便捷入口：render_report + save_report 一步到位（cli `scan`/`report` 用）。"""
    return save_report(render_report(data), date=date, dir=dir)


# ---------------------------------------------------------------------------
# 实测：展示日报配置 + 已有日报列表
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 72)
    print("strategy.report · 日报配置")
    print(f"  日报落盘目录：{REPORTS_DIR}")
    print(f"\n  日报四块：")
    print(f"    A · 开仓机会（品种×10检查矩阵 + 关键指标对比）")
    print(f"    B · 持仓处置（续持 / 止盈 / 止损 / 破位 / 到期）")
    print(f"    C · 组合风控（仓位限）")
    print(f"    D · 提醒与待人工（事件 / 长假 / MISSING 汇总）")

    # 列出已有日报（仅认文件名形如 <ISO date>.md，排除杂项 .md）
    if REPORTS_DIR.exists():
        reports = sorted(
            f for f in REPORTS_DIR.glob("*.md")
            if _is_date_stem(f.stem)
        )
        if reports:
            print(f"\n  已有日报 {len(reports)} 份：")
            for r in reports[-5:]:
                print(f"    {r.name}（{r.stat().st_size:,} 字节）")
            if len(reports) > 5:
                print(f"    …（更早 {len(reports) - 5} 份）")
        else:
            print(f"\n  尚无日报（跑 scan 命令生成）")
    else:
        print(f"\n  日报目录不存在（首次 scan 时自动创建）")
    print("=" * 72)
