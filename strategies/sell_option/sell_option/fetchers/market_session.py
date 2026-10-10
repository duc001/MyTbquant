"""市场时段探针（数据层【平台能力】，非 D 编号数据项）：本地日历+时段表判「现在 / 指定时刻是否开盘」（彻底不调 tbpy 日历）。

用途：opening 双模式（§八 A / §九）的 `market_open` 信号源。策略层不碰 tbpy（守三层依赖方向
strategy→checks→fetchers），由【编排层】调本模块取到 market_open 再注入 `evaluate_opening(data, th, market_open=…)`：
    from sell_option.fetchers.market_session import is_market_open
    summary, results = evaluate_opening(data, th, market_open=is_market_open(code="m"))

为什么不复用 OI/premium 源的 is_trading()：
- 源级 is_trading() 语义是「**这个源现在能不能出数**」——新浪 / openvlab 是 HTTP 全天候，恒 True，
  并不代表交易所开盘。交易所真实交易时段由本模块本地 session_hours 表反映（与任一数据项解耦），不依赖 tbpy。
  「市场是否开盘」是独立的平台能力，故抽到本模块。
- 判据＝本地日历（calendar.db.manual_calendar，is_open=0 休市）∧ 本地时段表（calendar.db.session_hours）；两者皆为本地 DB，彻底不调 tbpy。
- **按品种探（重要）**：交易时段因品种而异——夜盘有的到 23:00、有的到 01:00/02:30、有的【无夜盘】(如苹果 AP)。
  故探针须用【该品种自己的主力合约】：传 `code` 时经 D03 解析主连符号(m→m9888.DCE、AP→AP888.CZCE)。
  is_trading_time 现为本模块本地实现（读 session_hours），不再经 tbpy；AP 无夜盘已由 session_hours 标记（21:30→False）。
  切勿用单一合约套全池——会把无夜盘品种在夜盘时段误判成「开盘」而错走全链路。

实测坑（2026-09-30 实调坐实，见 `_as_datetime` 防呆）：
- `is_trading_time(symbol, when)` 的 when **须传 Python datetime 对象**（模块内 `_as_datetime` 已把 int/float/str 归一，但传 datetime 最稳）；
  早年 tbpy 版传 TBQuant 公式 YYYYMMDD.HHMM 浮点会被静默误解析，现已杜绝。
- 法定休市日取自 calendar.db.manual_calendar（手动维护，is_open=0 休市 / 1 交易日）；周末（Sat/Sun）由 is_trading_time 的 weekday 兜底→False（期货周末从不调市，补班日亦休），非时段由 session_hours 时段表自然返回 False 兜住；不再调用 tbpy.is_holiday()。
- 拿不到本地日历/时段表（DB 缺失/异常）/ 判定异常 → **一律按「休市」返回 open=False**（安全默认：宁走粗筛、绝不在无实时
  盘口时冒充「可开」，守 §二「下单前人工复核盘口实数」）。

运行自检：``python -m sell_option.fetchers.market_session``（本地日历+时段表，逐品种探全 47 品种交易时段，无需 tbpy）
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

__all__ = ["DEFAULT_PROBE", "MarketSession", "resolve_probe_symbol", "probe_session", "is_market_open"]

# 无 code / probe_symbol 时的兜底探针（豆粕主连，含日盘+夜盘、具代表性；主连符号不会到期）。
# ⚠ 正常应【按品种探】（传 code，经 D03 解析各品种主连）——夜盘时段因品种而异，单一探针套全池会误判无夜盘品种。
DEFAULT_PROBE = "m9888.DCE"

_D03 = None    # 进程内缓存的 D03 主连映射表（懒加载，只读边界表、不碰 tbpy）

logger = logging.getLogger(__name__)

# 本地权威交易日历源：calendar.db.manual_calendar 表（用户预写的逐日判定，替代 tbpy.is_holiday）。
#   manual_calendar(date TEXT PRIMARY KEY, is_open INTEGER, note TEXT)
#   - is_open=0 → 该日休市（法定节假日等）；is_open=1 → 该日确为交易日（覆盖 tbpy 误报）。
#   - 表中【无记录】的日期 → 默认视为交易日（open），由 is_trading_time 判定具体时段；
#     weekend / 非时段仍由 is_trading_time 自然返回 False 兜住。
# tbpy.is_holiday() 不再调用（其假日日历对部分日期误报为假期，会把真实交易日误判休市）。
# 统一读写逻辑见 calendar_cache 模块（不依赖 tbpy，D08/D09/D10/D14/D15/_trading_day 共用）。
try:
    from .calendar_cache import is_closed_date, add_calendar_date
except ImportError:  # 脚本直跑兜底
    from sell_option.fetchers.calendar_cache import is_closed_date, add_calendar_date
try:
    from .session_hours import is_trading_time
except ImportError:  # 脚本直跑兜底
    from sell_option.fetchers.session_hours import is_trading_time


@dataclass(frozen=True)
class MarketSession:
    """时段探针产物：open＝是否开盘（→ market_open）；reason＝人话依据（入报告 / 排障）；judged_at＝被判定的时刻。"""
    open: bool
    reason: str
    probe_symbol: str
    judged_at: dt.datetime | None = None     # None＝判「此刻」；否则＝判定的指定时刻


def _as_datetime(when: object) -> dt.datetime | None:
    """把 when 归一成 datetime（本地判定只认 datetime，见模块 docstring 实测坑）。

    None→None（判此刻）；datetime→原样；date→当日 00:00；数值→按 TBQuant TimeStamp YYYYMMDD.HHMM 解析
    （如 20240102.1000→2024-01-02 10:00、整数 20240102→当日 00:00）。其余类型→TypeError。"""
    if when is None or isinstance(when, dt.datetime):
        return when
    if isinstance(when, dt.date):
        return dt.datetime(when.year, when.month, when.day)
    if isinstance(when, (int, float)) and not isinstance(when, bool):
        ts = float(when)
        ymd = int(ts)
        hhmm = round((ts - ymd) * 10000)          # .1000→1000、.2130→2130（round 兜浮点误差）
        y, mo, d = ymd // 10000, (ymd // 100) % 100, ymd % 100
        hh, mm = divmod(hhmm, 100)
        return dt.datetime(y, mo, d, hh, mm)
    raise TypeError(f"when 须为 datetime/date/数值 TimeStamp，得到 {type(when).__name__}")


def resolve_probe_symbol(code: str) -> str:
    """品种代码 → 该品种主力合约（D03 主连符号，如 m→m9888.DCE、AP→AP888.CZCE），作 is_trading_time 探针。

    懒加载并缓存 D03 表（只读边界表、不碰 tbpy）；品种不在边界表 → KeyError（调用方按安全默认处理）。"""
    global _D03
    if _D03 is None:
        try:  # 包方式：相对导入
            from .d03_index_symbol import fetch_d03
        except ImportError:  # 脚本直跑
            from sell_option.fetchers.d03_index_symbol import fetch_d03
        _D03 = fetch_d03()
    return _D03.get(code)


def probe_session(probe_symbol: str | None = None, when: object = None, *,
                  code: str | None = None) -> MarketSession:
    """判定在 when（默认此刻）是否处于交易时段——**按品种探**（时段因品种夜盘不同而异）。

    探针符号优先级：显式 `probe_symbol` > `code`（经 D03 解析该品种主力合约）> `DEFAULT_PROBE` 兜底。
    open＝非休市日（manual_calendar）∧ 在盘中时段（session_hours）；两者皆本地 DB，彻底不调 tbpy。
    任何拿不到 / 解析失败 / 异常 → open=False（按休市，安全默认）。"""
    judged_at = _as_datetime(when)
    if probe_symbol is None:                       # 解析探针符号：优先按品种（code→D03 主连）
        if code is not None:
            try:
                probe_symbol = resolve_probe_symbol(code)
            except Exception as e:  # noqa: BLE001 - 品种无主连符号 → 无法判时段，安全默认休市
                return MarketSession(False, f"品种 {code!r} 无主力合约符号（D03 解析失败：{e}）→ 按休市处理",
                                     f"?{code}", judged_at)
        else:
            probe_symbol = DEFAULT_PROBE
    try:
        # 休市日：本地 calendar.db.manual_calendar（is_open=0）；时段：本地 session_hours。
        # 两者皆为本地 DB，彻底不调 tbpy 日历调用。
        date = judged_at.date() if judged_at is not None else dt.date.today()
        if is_closed_date(date):
            return MarketSession(False, "法定节假日（本地日历）→ 休市", probe_symbol, judged_at)
        trading = is_trading_time(probe_symbol, judged_at)
    except Exception as e:  # noqa: BLE001 - 判定异常只降级、不崩编排
        return MarketSession(False, f"时段判定异常（{type(e).__name__}: {e}）→ 按休市处理", probe_symbol, judged_at)
    if trading:
        return MarketSession(True, f"开盘（{probe_symbol} 在交易时段）", probe_symbol, judged_at)
    return MarketSession(False, f"非交易时段（{probe_symbol} 不在盘中）→ 休市", probe_symbol, judged_at)


def is_market_open(probe_symbol: str | None = None, when: object = None, *,
                   code: str | None = None) -> bool:
    """便捷入口：直接返回 market_open（True=开盘走全链路 / False=休市走粗筛）。

    **按品种探**：传 `code`（is_market_open(code='SA')）用该品种主力合约判时段。"""
    return probe_session(probe_symbol, when, code=code).open


# ---------------------------------------------------------------------------
# 实测：本地日历+时段表，探全品种交易时段
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d01_variety_list import fetch_d01

    varieties = [v.code for v in fetch_d01()]
    print("=" * 72)
    print(f"market_session 实测 · 品种 {len(varieties)} 个 · 本地日历+时段表（无需 tbpy）")

    open_count = 0
    closed_count = 0
    err_count = 0
    results = []
    for code in varieties:
        s = probe_session(code=code)
        results.append((code, s))
        if s.open:
            open_count += 1
        elif "异常" in s.reason:
            err_count += 1
        else:
            closed_count += 1

    print(f"  开盘 {open_count} · 休市 {closed_count} · 异常 {err_count}\n")
    for code, s in results:
        mark = "✓" if s.open else "×"
        print(f"  {mark} {code:<3} open={s.open!s:<5} · {s.reason}")

    errs = [(c, s) for c, s in results if "异常" in s.reason or "无主力" in s.reason]
    if errs:
        print(f"\n[异常/解析失败]")
        for c, s in errs:
            print(f"  {c:<3} {s.reason}")
    print("=" * 72)
