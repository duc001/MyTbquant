"""本地交易日历缓存（calendar.db · manual_calendar 表）。

替代 tbpy.is_holiday()：tbpy 假日日历对部分日期误报为假期，会把真实交易日误判休市。
表 manual_calendar(date TEXT PRIMARY KEY, is_open INTEGER, note TEXT)
  - is_open=0 → 休市；is_open=1 → 交易日；缺失 → 默认交易日（由 is_trading_time 判具体时段）。

本模块不依赖 tbpy，供 market_session / D08 / D09/D10 / D14 / D15 / _trading_day 统一读取写入。
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CALENDAR_DB = DEFAULT_DATA_DIR / "calendar.db"
_TABLE = "manual_calendar"
_CACHE: dict[dt.date, bool] | None = None


def _load_calendar() -> dict[dt.date, bool]:
    """读取 manual_calendar，返回 {date: is_open}（进程内缓存一次）。

    表不存在/损坏 → 返回空 dict + 记一次警告（退化为仅 is_trading_time 判定）。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    cal: dict[dt.date, bool] = {}
    try:
        db = sqlite3.connect(str(CALENDAR_DB))
        db.execute(
            f"CREATE TABLE IF NOT EXISTS {_TABLE} "
            f"(date TEXT PRIMARY KEY, is_open INTEGER, note TEXT)"
        )
        for ds, fl in db.execute(f"SELECT date, is_open FROM {_TABLE}"):
            try:
                cal[dt.date.fromisoformat(str(ds))] = bool(fl)
            except Exception:  # noqa: BLE001 - 单条脏数据跳过
                pass
        db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("manual_calendar 读取失败（%s）→ 退化为仅 is_trading_time 判定", e)
    _CACHE = cal
    return cal


def is_closed_date(d: dt.date) -> bool:
    """某自然日是否休市：表中显式 is_open=0 → True；无记录 → False（默认交易日）。"""
    return not _load_calendar().get(d, True)


def get_closed_dates(start: dt.date, end: dt.date) -> list[dt.date]:
    """返回 [start, end] 区间内所有休市日（is_open=0），按日期升序去重。

    区间内 manual_calendar 无记录的日期默认视为交易日，不会列入（不会造成假长假）。"""
    cal = _load_calendar()
    out: list[dt.date] = []
    cur = start
    one = dt.timedelta(days=1)
    while cur <= end:
        if cal.get(cur, True) is False:
            out.append(cur)
        cur += one
    return out


def add_calendar_date(date_str: str, is_open: bool, note: str = "") -> None:
    """写入/覆盖一条逐日判定（幂等，已存在则更新）。"""
    d = dt.date.fromisoformat(date_str)
    db = sqlite3.connect(str(CALENDAR_DB))
    db.execute(
        f"CREATE TABLE IF NOT EXISTS {_TABLE} "
        f"(date TEXT PRIMARY KEY, is_open INTEGER, note TEXT)"
    )
    db.execute(
        f"INSERT INTO {_TABLE} (date, is_open, note) VALUES (?, ?, ?) "
        f"ON CONFLICT(date) DO UPDATE SET is_open=excluded.is_open, note=excluded.note",
        (d.isoformat(), 1 if is_open else 0, note),
    )
    db.commit()
    db.close()
    global _CACHE
    _CACHE = None  # 失效缓存，下次探测重载
