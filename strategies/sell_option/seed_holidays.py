"""预写 2026 全年日历到 calendar.db.manual_calendar（替代 tbpy.is_holiday）。

每天一行：manual_calendar(date TEXT PRIMARY KEY, is_open INTEGER, note TEXT)
  - 周末 / 法定节假日          → is_open=0（休市）
  - 其余工作日（含已确认交易日）→ is_open=1（交易日）
tbpy.is_holiday() 不再调用（其假日日历对部分日期误报为假期）。

生成规则：
  1) 周末（Sat/Sun）→ 0；
  2) 在 HOLIDAYS_2026 中的日期 → 0；
  3) 其余工作日 → 1。

⚠ HOLIDAYS_2026 为按公开《2026 年放假安排》的最佳估计，请逐条核对。
⚠ 期货交易所按自身交易日历开收市，与公众调休补班无关：补班日（落在周末）市场仍休市，无需额外开市覆盖。
运行（项目根目录、激活 venv）：  .venv/Scripts/python.exe seed_holidays.py
"""
import sys
from pathlib import Path
_HERE = Path(__file__).resolve().parent
if (_HERE / 'sell_option' / '__init__.py').exists():
    _UNIT = _HERE
else:
    _UNIT = _HERE.parent
_STRATEGIES = _UNIT.parent
_ROOT = _STRATEGIES.parent
for _p in (str(_STRATEGIES), str(_UNIT), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from datetime import date, timedelta
from sell_option.fetchers.calendar_cache import add_calendar_date
from sell_option.fetchers.session_hours import seed_session_hours

# 法定节假日（is_open=0）。仅列日期即可，周末无需列。
HOLIDAYS_2026 = {
    "2026-01-01": "元旦", "2026-01-02": "元旦", "2026-01-03": "元旦",
    "2026-02-16": "春节", "2026-02-17": "春节", "2026-02-18": "春节",
    "2026-02-19": "春节", "2026-02-20": "春节", "2026-02-21": "春节",
    "2026-02-22": "春节",
    "2026-04-04": "清明", "2026-04-05": "清明", "2026-04-06": "清明",
    "2026-05-01": "劳动", "2026-05-02": "劳动", "2026-05-03": "劳动",
    "2026-05-04": "劳动", "2026-05-05": "劳动",
    "2026-06-19": "端午", "2026-06-20": "端午", "2026-06-21": "端午",
    "2026-09-25": "中秋", "2026-09-26": "中秋", "2026-09-27": "中秋",
    "2026-10-01": "国庆", "2026-10-02": "国庆", "2026-10-03": "国庆",
    "2026-10-04": "国庆", "2026-10-05": "国庆", "2026-10-06": "国庆",
    "2026-10-07": "国庆",
}

YEAR = 2026


def main() -> None:
    start = date(YEAR, 1, 1)
    end = date(YEAR, 12, 31)
    d = start
    n_open = n_closed = 0
    while d <= end:
        iso = d.isoformat()
        if d.weekday() >= 5:           # 5=Sat, 6=Sun
            add_calendar_date(iso, False, "周末")
            n_closed += 1
        elif iso in HOLIDAYS_2026:
            add_calendar_date(iso, False, HOLIDAYS_2026[iso])
            n_closed += 1
        else:
            add_calendar_date(iso, True, "交易日")
            n_open += 1
        d += timedelta(days=1)
    print(f"done: {n_open} trading days + {n_closed} closed -> calendar.db.manual_calendar ({YEAR})")


if __name__ == "__main__":
    main()
    seed_session_hours()
