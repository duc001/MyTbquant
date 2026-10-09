"""取数归属交易日解析（共享工具）。

所有 fetcher 的缓存查询统一口径：**先问 D15 日历"最近交易日是哪天"，再严格按该交易日
查缓存 / 写库**，避免"按 today 模糊回退(`load_latest`)"把非交易日、或恰好库里最新的脏
日期当真用（纠正旧 `repo.load(sym, today) or repo.load_latest(sym, today)` 模式）。

设计要点：
- 读 D15 日历走 ``calendar.db``（manual_calendar 表），**不触发 tbpy 调用**；
- 日历缓存不可用（如离线首次）→ closed=None → 朴素跳过周末兜底；
- 上限 400 天防呆。
"""
from __future__ import annotations

import datetime as dt


def resolve_pull_date(today: dt.date, closed: set[dt.date] | None = None) -> dt.date:
    """取数归属交易日（pull_date）：今天若是交易日→今天；否则回退【最近交易日】。

    closed＝闭市日集合（来自 D15 日历磁盘缓存 calendar.db，不含 tbpy 调用）；
    closed 为 None（日历缓存不可用）→ 朴素跳过周末兜底。上限 400 天防呆。"""
    d = today
    for _ in range(400):
        if closed is not None:
            if d not in closed:
                return d
        else:
            if d.weekday() < 5:        # 朴素兜底：跳过周六日
                return d
        d -= dt.timedelta(days=1)
    return today


def _load_holiday_closed(today: dt.date) -> set[dt.date] | None:
    """读 manual_calendar（calendar.db）闭市日集合（不触发 tbpy）。

    返回闭市日 set（今天往前 400 天窗口，覆盖 resolve_pull_date 防呆上限）；
    无数据 / 读取异常 → None（交由 resolve_pull_date 朴素兜底）。"""
    try:
        from .calendar_cache import get_closed_dates
        start = today - dt.timedelta(days=400)
        return set(get_closed_dates(start, today))
    except Exception:  # noqa: BLE001 - 日历读取异常→降级朴素兜底，绝不影响取数
        return None


def pull_date_for(today: dt.date) -> dt.date:
    """一键：读 D15 日历闭市日集合 + 解析归属交易日（pull_date）。"""
    return resolve_pull_date(today, _load_holiday_closed(today))
