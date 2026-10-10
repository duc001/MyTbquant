"""本地交易时段判定（彻底去 tbpy.is_trading_time，不再依赖 tbpy 日历）。

时段源：calendar.db.session_hours 表（用户维护，与 manual_calendar 同库）。
    session_hours(exchange TEXT, product TEXT, day TEXT, night TEXT, note TEXT)
      - product='*' ＝ 该交易所默认；否则按品种覆盖（如 rb.SHFE 夜盘 21:00-23:00）。
      - day / night 为 JSON：[{"start":"HH:MM","end":"HH:MM"}, ...]
        - 夜盘跨午夜时 end < start（如铜 21:00-02:30 → {"start":"21:00","end":"02:30"}）。
        - 空数组 = 该时段不存在。
    DB 缺失/异常 → 退回 DEFAULT_SESSIONS 内置默认（兜底，仍不触发 tbpy）。

交易日（是否休市）由 manual_calendar 决定；本模块只判"此刻是否落在某交易时段内"。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

try:  # 包方式
    from .calendar_cache import is_closed_date
except ImportError:  # 脚本直跑
    from sell_option.fetchers.calendar_cache import is_closed_date

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CALENDAR_DB = DEFAULT_DATA_DIR / "calendar.db"

# 内置兜底（DB 不可用时的最后防线，不依赖 tbpy）
_DAY = [("09:00", "10:15"), ("10:30", "11:30"), ("13:30", "15:00")]
DEFAULT_SESSIONS = {
    "DCE":   {"day": _DAY, "night": [("21:00", "23:00")]},
    "CZCE":  {"day": _DAY, "night": [("21:00", "23:30")]},
    "SHFE":  {"day": _DAY, "night": [("21:00", "23:00")]},
    "INE":   {"day": _DAY, "night": [("21:00", "23:00")]},
    "GFEX":  {"day": _DAY, "night": []},
    "CFFEX": {"day": [("09:30", "11:30"), ("13:00", "15:00")], "night": []},
}

_SYM_RE = re.compile(r"^([A-Za-z]+)\d*\.([A-Za-z]+)$")
_SESSIONS_CACHE: dict | None = None


def _as_datetime(when) -> dt.datetime | None:
    """归一 when → datetime；支持 datetime/date/int|float(TBQuant YYYYMMDD.HHMM)/str。"""
    if when is None:
        return dt.datetime.now()
    if isinstance(when, dt.datetime):
        return when
    if isinstance(when, dt.date):
        return dt.datetime(when.year, when.month, when.day)
    if isinstance(when, (int, float)):
        f = float(when)
        ymd = int(f)
        hm = round((f - ymd) * 10000)
        y, md = divmod(ymd, 10000)
        m, d = divmod(md, 100)
        hh, mm = divmod(hm, 100)
        return dt.datetime(y, m, d, hh, mm)
    if isinstance(when, str):
        s = when.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def _hm(s: str) -> int:
    h, mm = s.split(":")
    return int(h) * 60 + int(mm)


def _to_pairs(json_text: str | None) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for item in (json.loads(json_text) if json_text else []):
        out.append((_hm(item["start"]), _hm(item["end"])))
    return out


def _parse_symbol(symbol: str):
    m = _SYM_RE.match(symbol or "")
    if not m:
        return None, None
    return m.group(1), m.group(2).upper()


def _load_sessions() -> dict:
    """读 session_hours 表 → {(exchange,product): {"day":[...], "night":[...]}}；失败用默认。"""
    table: dict = {}
    try:
        db = sqlite3.connect(str(CALENDAR_DB))
        db.execute(
            "CREATE TABLE IF NOT EXISTS session_hours ("
            "exchange TEXT NOT NULL, product TEXT NOT NULL DEFAULT '*', "
            "day TEXT, night TEXT, note TEXT, PRIMARY KEY(exchange, product))"
        )
        for exch, prod, day, night in db.execute(
                "SELECT exchange, product, day, night FROM session_hours"):
            table[(exch, prod)] = {"day": _to_pairs(day), "night": _to_pairs(night)}
        db.close()
    except Exception:  # noqa: BLE001
        table = {}
    if not table:
        table = {(e, "*"): {"day": v["day"], "night": v["night"]}
                 for e, v in DEFAULT_SESSIONS.items()}
    return table


def _sessions() -> dict:
    global _SESSIONS_CACHE
    if _SESSIONS_CACHE is None:
        _SESSIONS_CACHE = _load_sessions()
    return _SESSIONS_CACHE


def is_trading_time(symbol: str, when: object = None) -> bool:
    """本地判定 symbol 在 when（默认此刻）是否处于交易时段；彻底不调用 tbpy。

    休市日（manual_calendar is_open=0）→ 一律 False；周末（Sat/Sun）由本函数 weekday 兜底→False（期货周末从不调市，补班日亦休），与 manual_calendar 双保险。
    跨午夜夜盘归属「起始日」为交易日（如周一 21:00-02:30 属周一）。
    when 须 Python datetime（模块内 _as_datetime 兼容 int/float/str）；任何异常 → False（安全默认）。"""
    judged = _as_datetime(when)
    if judged is None:
        return False
    if judged.weekday() >= 5:  # 5=Sat, 6=Sun：期货周末从不调市（补班日亦休，seed_holidays 已明确）
        return False
    product, exchange = _parse_symbol(symbol)
    if exchange is None:
        return False
    table = _sessions()
    default = DEFAULT_SESSIONS.get(exchange, {"day": _DAY, "night": []})
    sess = (table.get((exchange, product)) or table.get((exchange, "*"))
            or {"day": default["day"], "night": default["night"]})
    t_min = judged.hour * 60 + judged.minute

    for s, e in sess["day"]:
        if s <= t_min <= e and not is_closed_date(judged.date()):
            return True
    for s, e in sess["night"]:
        if e > s:  # 同日夜盘
            if s <= t_min <= e and not is_closed_date(judged.date()):
                return True
        else:  # 跨午夜：当晚 s→24:00 属 judged.date；次日 00:00→e 属 judged.date-1
            if t_min >= s:
                if not is_closed_date(judged.date()):
                    return True
            elif t_min <= e:
                if not is_closed_date(judged.date() - dt.timedelta(days=1)):
                    return True
    return False


# ---------------------------------------------------------------------------
# 播种（供 seed_holidays.py 调用；用户可按券商实际时段改写，彻底去 tbpy 日历）
# ---------------------------------------------------------------------------
def _upsert(exchange: str, product: str, day: list, night: list, note: str = "") -> None:
    db = sqlite3.connect(str(CALENDAR_DB))
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_hours ("
        "exchange TEXT NOT NULL, product TEXT NOT NULL DEFAULT '*', "
        "day TEXT, night TEXT, note TEXT, PRIMARY KEY(exchange, product))"
    )
    db.execute(
        "INSERT INTO session_hours (exchange, product, day, night, note) VALUES (?,?,?,?,?) "
        "ON CONFLICT(exchange, product) DO UPDATE SET day=excluded.day, night=excluded.night, note=excluded.note",
        (exchange, product, json.dumps(day, ensure_ascii=False), json.dumps(night, ensure_ascii=False), note),
    )
    db.commit()
    db.close()


def seed_session_hours() -> None:
    """播种标准交易时段到 calendar.db.session_hours（用户可改，准确时段以券商为准）。"""
    day = [{"start": "09:00", "end": "10:15"}, {"start": "10:30", "end": "11:30"}, {"start": "13:30", "end": "15:00"}]
    _upsert("DCE", "*", day, [{"start": "21:00", "end": "23:00"}], "大连：夜盘默认 21:00-23:00（jd 等个别无夜盘）")
    _upsert("CZCE", "*", day, [{"start": "21:00", "end": "23:30"}], "郑商：夜盘默认 21:00-23:30（AP/CJ 无夜盘，见下）")
    _upsert("SHFE", "*", day, [{"start": "21:00", "end": "23:00"}], "上海：夜盘默认 21:00-23:00（金属见下延长）")
    _upsert("INE", "*", day, [{"start": "21:00", "end": "23:00"}], "能源：夜盘默认 21:00-23:00")
    _upsert("GFEX", "*", day, [], "广期：无夜盘")
    _upsert("CFFEX", "*", [{"start": "09:30", "end": "11:30"}, {"start": "13:00", "end": "15:00"}], [], "中金：无夜盘")
    # 无夜盘品种（避免夜间误判开盘）
    for p in ["AP", "CJ"]:
        _upsert("CZCE", p, day, [], "无夜盘")
    _upsert("DCE", "jd", day, [], "鸡蛋无夜盘")
    _upsert("DCE", "lh", day, [], "生猪无夜盘")
    # SHFE 金属夜盘延长
    for p in ["cu", "al", "zn", "pb", "ni", "sn", "ag", "au", "ss"]:
        _upsert("SHFE", p, day, [{"start": "21:00", "end": "02:30"}], "夜盘 21:00-02:30")
    for p in ["ru", "nr", "sp"]:
        _upsert("SHFE", p, day, [{"start": "21:00", "end": "01:00"}], "夜盘 21:00-01:00")
    print("done: session_hours seeded -> calendar.db.session_hours")
