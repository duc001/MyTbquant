"""D15 · 事件日历 ＋ 长假日历（全部自动派生，人工标注可选）。

需求源 §6.1 D15 / §7.1 #6 事件窗口 / §7.3 事件·长假提示 / §八 四态：
- **D15 长假日历**（来源＝calendar.db.manual_calendar）：从 manual_calendar 表读取 is_open=0 的闭市日，
  连续分组 → 连续 ≥N 闭市日＝长假。**不再调用 tbpy、不读 holidays.json**，日历由用户预写进 manual_calendar 表。
- **D15 到期事件**（自动派生）：期权到期日本就由交易所规则决定，从 D07 候选月已算好的 `expiry`
  元数据直接生成"X 月期权到期"事件（亦可传任意含 `.code/.month4/.expiry` 的候选）。不再手填。
- **D15 宏观事件·FOMC**（联网自动拉取）：美联储官网 FOMC 议息日程实时拉取 → 自动生成事件
  （codes=None→影响全池）。拉取失败（网络/超时/被墙/解析异常）→ 返回空、**优雅降级"拉取不到就算了"**，
  不冒充、不阻断 #6；USDA/CPI 等其它宏观如需可后续同法接入。
- **events.json（可选人工标注层）**：仅作你特别想标注的事的覆盖/附加层，缺失不报 MISSING；
  自动派生已覆盖长假/到期/FOMC，无人工维护负担。
- **只提醒、不否决、不强制平**（§7.1 #6 / §7.3）——四态映射：
  临近提醒 → **WARN**；近期无提醒 → **PASS**；日历文件/数据源均不可用无法判 → **MISSING**（数据不足不冒充）；
  **永不 FAIL**（事件/长假不否决策略、不强制平仓，避与不避由人定）。

运行自检：``python -m sell_option.fetchers.d15_calendar``（读 manual_calendar 长假 + 联网拉取 FOMC + 读 events.json）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:  # 包方式（python -m / import）：相对导入
    from .models import Verdict, as_date
    from .market_session import probe_session
    from ..thresholds import load_thresholds
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.models import Verdict, as_date
    from sell_option.fetchers.market_session import probe_session
    from sell_option.thresholds import load_thresholds

# 人工维护日历目录（data/ 已 git 忽略）
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
EVENTS_FILE = DEFAULT_DATA_DIR / "events.json"
# HOLIDAYS_FILE 已移除：长假日历改由 calendar.db.manual_calendar 提供，不再使用 JSON。
CALENDAR_CACHE_DB = DEFAULT_DATA_DIR / "calendar.db"   # D15 日历缓存（长假＋FOMC，统一 sqlite3）

# 长假派生探针：用某品种主力合约探"当日 10:00 是否开盘"。所有期货均有日盘，10:00 在盘中即=交易日；
# 周末/法定节假日→非交易时段。默认用豆粕主连（含日盘+夜盘、具代表性；主连符号不会到期）。
DEFAULT_HOLIDAY_PROBE_CODE = "m"
# 向前扫描天数（覆盖约 13 个月，足以捕获任意长假段与其节前提醒窗）
HOLIDAY_SCAN_DAYS = int(os.getenv("SELLOPT_D15_HOLIDAY_SCAN_DAYS", "400"))
# 扫描起点向过去回溯天数：捕获"今天之前"的近期假期（如国庆 10-01 在 10-02 扫描时会被漏掉），
# 供下游"取数归属交易日"正确回退到真正最后交易日（manual_calendar 已含完整日历，无需更长回溯）。
HOLIDAY_SCAN_PAST_DAYS = int(os.getenv("SELLOPT_D15_HOLIDAY_SCAN_PAST", "30"))

# 事件/长假规则阈值（#6 提醒窗、长假认定/节前窗）旧 D15_* env 常量已删，
# 单一事实源＝ThresholdSet（§6.4）：event_warn_days / long_holiday_min / holiday_warn_days，缺省参运行时读 load_thresholds()。
# HOLIDAY_CACHE_MAX_AGE_DAYS 已移除：长假日历改由 manual_calendar 提供，不再有 tbpy 磁盘缓存。
FOMC_CACHE_MAX_AGE_DAYS = int(os.getenv("SELLOPT_D15_FOMC_CACHE_MAX_AGE", "7"))         # FOMC 磁盘缓存复用时效（天内）
# _HORIZON_DAYS 已移除：不再调用 tbpy.is_holiday()，无远期误报窗口概念。


# ---------------------------------------------------------------------------
# 共享工具（离线可测）：安全读 JSON + 安全解析日期 + 连休分组
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> tuple[dict | None, str | None]:
    """读 JSON → (data, error)；文件缺失/损坏返回 (None, 原因) 交上层判 MISSING。"""
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"文件不存在：{path}"
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败：{e}"
    except OSError as e:
        return None, f"读取失败：{type(e).__name__}: {e}"


def _safe_date(value) -> dt.date | None:
    """安全归一化日期（复用 models.as_date 兼容 YYYY-MM-DD / YYYY/MM/DD / YYYYMMDD）；坏值→None。"""
    try:
        return as_date(value)
    except (ValueError, TypeError):
        return None


def _group_runs(dates: list[dt.date], long_min: int) -> list["HolidayRun"]:
    """把已排序去重的闭市自然日按【连续】分组为 HolidayRun 列表（纯函数，离线可测）。

    相邻两日差 1 天即同一连休段；每段 length＝闭市天数、last_trading_day＝首闭市日前一天。"""
    runs: list[HolidayRun] = []
    if not dates:
        return runs
    start = prev = dates[0]
    for d in dates[1:]:
        if (d - prev).days == 1:
            prev = d
        else:
            runs.append(_make_run(start, prev, long_min))
            start = prev = d
    runs.append(_make_run(start, prev, long_min))
    return runs


def _make_run(start: dt.date, end: dt.date, long_min: int) -> "HolidayRun":
    length = (end - start).days + 1
    return HolidayRun(start=start, end=end, length=length,
                      last_trading_day=start - dt.timedelta(days=1),
                      is_long=length >= long_min)


# ===========================================================================
# D15 · 事件日历（宏观事件=可选标注层 ＋ 到期事件=自动派生）
# ===========================================================================
@dataclass(frozen=True)
class Event:
    """一条市场事件（events.json 的 events[] 元素，或自动派生的到期事件）。

    codes 缺省（None/空）＝影响全池；指定时只对这些品种生效（大小写不敏感，统一大写比对）。"""

    date: dt.date
    name: str
    codes: frozenset[str] | None = None

    def affects(self, code: str | None) -> bool:
        """该事件是否影响给定品种：codes 缺省→全池 True；否则 code 大写命中才 True。"""
        if not self.codes:            # 缺省＝全池
            return True
        if code is None:              # 未指定品种查询→视为全池，命中任何限定事件
            return True
        return code.upper() in self.codes


@dataclass(frozen=True)
class EventHit:
    """一次事件临近命中（供 #6 提醒 / 报告展示）。"""

    event: Event
    days_until: int                   # event.date - today（0＝当天，>0＝未来天数）

    @property
    def text(self) -> str:
        when = "今天" if self.days_until == 0 else f"{self.days_until} 天后"
        scope = "全池" if not self.event.codes else "/".join(sorted(self.event.codes))
        return f"⚠ {when}（{self.event.date.isoformat()}）{self.event.name}｜影响：{scope}"


@dataclass
class EventCalendar:
    """fetch_d15_events 产物：事件日历 ＋ #6 提醒判定。

    loaded=False（events.json 缺失/损坏 且 无自动派生事件）→ check 判 MISSING（数据不足不冒充）。
    source 记录数据来源口径（manual-events.json / auto-expiry / 二者合并 / missing）。"""

    events: list[Event] = field(default_factory=list)
    loaded: bool = False
    load_error: str | None = None
    source: str = ""

    def upcoming(self, today: dt.date, days: int | None = None,
                 code: str | None = None) -> list[EventHit]:
        """today 起 days 天内（0 ≤ days_until ≤ days）、影响 code 的事件，按临近排序；days=None→读 th.event_warn_days。"""
        if days is None:
            days = load_thresholds().event_warn_days
        hits: list[EventHit] = []
        for ev in self.events:
            du = (ev.date - today).days
            if 0 <= du <= days and ev.affects(code):
                hits.append(EventHit(event=ev, days_until=du))
        hits.sort(key=lambda h: (h.days_until, h.event.date, h.event.name))
        return hits

    def check(self, today: dt.date, code: str | None = None,
              days: int | None = None) -> tuple[Verdict, str]:
        """#6 事件窗口四态：数据源均不可用→MISSING；days 内有影响事件→WARN；否则 PASS（永不 FAIL）；days=None→读 th.event_warn_days。"""
        if days is None:
            days = load_thresholds().event_warn_days
        if not self.loaded:
            return Verdict.MISSING, f"事件日历不可用（{self.load_error}）→ 无法判 #6，人工核对"
        hits = self.upcoming(today, days=days, code=code)
        if hits:
            return Verdict.WARN, "；".join(h.text for h in hits)
        scope = f"品种 {code}" if code else "全池"
        return Verdict.PASS, f"{days} 天内{scope}无事件提醒"

    def __len__(self) -> int:
        return len(self.events)


def derive_expiration_events(candidates: Iterable) -> list[Event]:
    """从候选月（含 .code/.month4/.expiry，或 .underlying_month 替代 .month4）自动生成"期权到期"事件。

    纯函数、离线可测；候选缺 expiry 自动跳过。codes 设为该品种（大小写不敏感由 Event 统一大写）。
    这是"到期"事件自动派生的唯一入口——不再依赖手填 events.json 里的交割日/到期日。"""
    out: list[Event] = []
    for c in (candidates or []):
        expiry = getattr(c, "expiry", None)
        if expiry is None:
            continue
        code = getattr(c, "code", None)
        month4 = getattr(c, "month4", None) or getattr(c, "underlying_month", None)
        name = f"{code} {month4} 期权到期" if code else "期权到期"
        out.append(Event(date=expiry, name=name,
                         codes=frozenset({code.upper()}) if code else None))
    return out


# 全池到期事件缓存（按判定日，避免同进程重复拉 D06/D07）
_POOL_EXP_CACHE: dict[str, list[Event]] = None  # type: ignore[assignment]

# 长假日历内存缓存（同进程避免重复扫 manual_calendar）
_HOLIDAY_CACHE: dict[str, "HolidayCalendar"] | None = None  # type: ignore[assignment]


def _derive_pool_expirations(today: dt.date, months_ahead: int = 6) -> list[Event]:
    """走 D01+D07 拉全池候选月的 expiry，自动生成到期事件；任一环节拿不到→返回 []（优雅降级，不冒充）。

    months_ahead 控制向前生成范围（默认 6 个月，覆盖所有在交易窗口内的候选月）。"""
    try:
        from .d01_variety_list import fetch_d01
        from .d02_symbol_template import fetch_d02
        from .d06_option_chain import OptionIndexRepo, fetch_d06
        from .d07_candidate_months import D07Params, fetch_d07
    except Exception:  # noqa: BLE001 - 任何导入失败（如离线/缺模块）→ 无自动到期事件
        return []
    try:
        varieties = [(v.code, v.exchange) for v in fetch_d01()]
        repo = OptionIndexRepo()
        repo.ensure_fresh(today, verbose=False)
        cs = fetch_d06(varieties, today, repo=repo, verbose=False)
        templates = fetch_d02()
        cand = fetch_d07(cs.all_contracts(), today,
                         params=D07Params(dte_min=1, dte_max=months_ahead * 30, top_n=99),
                         symbol_templates=templates)
        cands: list = []
        for code in cand.codes():
            cands.extend(cand.months_for(code))
        return derive_expiration_events(cands)
    except Exception:  # noqa: BLE001 - tbpy/取数异常 → 不冒充，返回空（由 MISSING/PASS 兜底）
        return []


def _pool_expirations_cache(today: dt.date) -> list[Event]:
    global _POOL_EXP_CACHE
    if _POOL_EXP_CACHE is None:
        _POOL_EXP_CACHE = {}
    key = today.isoformat()
    if key not in _POOL_EXP_CACHE:
        _POOL_EXP_CACHE[key] = _derive_pool_expirations(today)
    return _POOL_EXP_CACHE[key]


# ---------------------------------------------------------------------------
# FOMC 宏观事件自动拉取（美联储官网；优雅降级：拉取不到就算了）
# ---------------------------------------------------------------------------
_FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_FOMC_CACHE: dict[str, list[Event]] | None = None


class FomcCacheRepo:
    """data/calendar.db FOMC 缓存表：主键 pull_date，存 updated + events_json。

    与 manual_calendar 共用同一 DB 文件（calendar.db），统一 D15 日历缓存管理。"""

    def __init__(self, db_path: Path = CALENDAR_CACHE_DB):
        self.db_path = db_path
        self._migrate()

    def _migrate(self):
        # 老库迁移：cache_date -> pull_date（列名重构，ALTER 安全、幂等）
        try:
            with sqlite3.connect(self.db_path) as cn:
                cols = {r[1] for r in cn.execute("PRAGMA table_info(fomc_cache)")}
                if "cache_date" in cols and "pull_date" not in cols:
                    cn.execute("ALTER TABLE fomc_cache RENAME COLUMN cache_date TO pull_date")
        except Exception:
            pass

    def _conn(self):
        cn = sqlite3.connect(self.db_path)
        cn.execute(
            "CREATE TABLE IF NOT EXISTS fomc_cache ("
            "pull_date TEXT NOT NULL PRIMARY KEY, updated TEXT, events_json TEXT)")
        return cn

    def save(self, today: dt.date, events: list[Event]) -> None:
        """幂等写入 FOMC 缓存；落盘失败静默不阻断。"""
        try:
            events_json = json.dumps(
                [{"date": e.date.isoformat(), "name": e.name,
                  "codes": sorted(e.codes) if e.codes else None}
                 for e in events], ensure_ascii=False)
            with self._conn() as cn:
                cn.execute(
                    "INSERT OR REPLACE INTO fomc_cache (pull_date, updated, events_json) "
                    "VALUES (?,?,?)",
                    (today.isoformat(), today.isoformat(), events_json))
        except Exception:  # noqa: BLE001 - 落盘失败不阻断
            pass

    def load_latest(self, today: dt.date) -> dict | None:
        """取最新的 FOMC 缓存；缺失/损坏→None。"""
        try:
            with self._conn() as cn:
                row = cn.execute(
                    "SELECT updated, events_json FROM fomc_cache "
                    "WHERE pull_date<=? ORDER BY pull_date DESC LIMIT 1",
                    (today.isoformat(),)).fetchone()
            if row is None:
                return None
            updated = dt.date.fromisoformat(row[0]) if row[0] else None
            events_raw = json.loads(row[1])
            events = [Event(date=dt.date.fromisoformat(d["date"]), name=d.get("name", ""),
                            codes=frozenset(d["codes"]) if d.get("codes") else None)
                      for d in events_raw]
            return {"updated": updated, "events": events}
        except Exception:  # noqa: BLE001 - 损坏即视为无缓存
            return None


_fomc_repo = FomcCacheRepo()


def _save_fomc_db(events: list[Event], today: dt.date | None = None) -> None:
    """FOMC 日程落盘 calendar.db（全集，供后续复用）；落盘失败静默不阻断。"""
    _fomc_repo.save(today or dt.date.today(), events)


def _load_fomc_db() -> dict | None:
    """读取 calendar.db FOMC 缓存 → {"updated": date|None, "events": list[Event]}；缺失/损坏→None。"""
    return _fomc_repo.load_latest(dt.date.today())


# 议息决议日编码在声明/发布会链接的文件名里：.../monetaryYYYYMMDD 或 .../fomcpressconfYYYYMMDD
# （该日期＝会议第二天声明发布日＝市场关注的“FOMC 议息”日；
#  正文 “(Released Month DD, YYYY)” 是会后纪要发布日，约晚 3 周，绝不能用来当议息日）
_FOMC_LINK_RE = re.compile(r"(?:monetary|fomcpressconf)(\d{8})")

# 维护中的 FOMC 决议日（会议第二天＝声明发布日）。美联储提前约一年公布全年排期，
# 但官网仅对已发生的会议暴露可抓取的 monetaryYYYYMMDD 链接；当年 Q4 与次年级的暂定会议
# 不在本页暴露，故以本维护表作为前瞻来源。⚠ 需随美联储官方日历定期核对更新。
_FOMC_SCHEDULE: list[tuple[int, int, int]] = [
    (2026, 10, 28), (2026, 12, 16),
    (2027, 1, 27), (2027, 3, 17), (2027, 4, 28), (2027, 6, 16),
    (2027, 7, 28), (2027, 9, 22), (2027, 10, 27), (2027, 12, 15),
    (2028, 1, 26),
]


def _parse_fomc_html(html: str, *, since: dt.date) -> list[dt.date]:
    """从 FOMC 日历页 HTML 抽取议息决议日（YYYYMMDD，来自声明/发布会链接）。

    纯函数、离线可测。返回去重排序后的 date 列表（不包 Event 包装，便于与维护表合并）；since 之前忽略。"""
    out: list[dt.date] = []
    seen: set[dt.date] = set()
    for m in _FOMC_LINK_RE.finditer(html):
        ymd = m.group(1)
        try:
            d = dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        except ValueError:
            continue
        if d < since or d in seen:
            continue
        seen.add(d)
        out.append(d)
    out.sort()
    return out


def _curated_fomc_events(today: dt.date) -> list[Event]:
    """维护表中的前瞻 FOMC 议息事件（含官网未暴露的当年 Q4/次年级暂定会议）。"""
    since = today - dt.timedelta(days=7)
    out: list[Event] = []
    for y, mo, d in _FOMC_SCHEDULE:
        ev_date = dt.date(y, mo, d)
        if ev_date >= since:
            out.append(Event(date=ev_date, name=f"FOMC 议息（{y}-{mo:02d}）", codes=None))
    out.sort(key=lambda e: e.date)
    return out


def fetch_fomc_events(today: dt.date | None = None) -> list[Event]:
    """自动拉取 FOMC 议息日程 → 事件列表（codes=None＝影响全池）。

    来源＝维护表(前瞻，含官网未暴露的当年 Q4/次年级暂定会议) ∪ 联网抓取(官网已发生会议的
    monetaryYYYYMMDD 声明链接，作校验/补全)。联网失败→仅用维护表；维护表为空→空（优雅降级）。
    按判定日缓存【全集】，返回时再过滤 since 之前的过期事件，避免缓存被 since 误删未来会议。"""
    base = today or dt.date.today()
    key = base.isoformat()
    since = base - dt.timedelta(days=7)
    global _FOMC_CACHE
    if _FOMC_CACHE is None:
        _FOMC_CACHE = {}
    if key in _FOMC_CACHE:
        return _FOMC_CACHE[key]
    # 1) 复用磁盘缓存（全集，够新即返回；过期→重抓）
    rec = _load_fomc_db()
    if rec and rec["updated"] and (base - rec["updated"]).days <= FOMC_CACHE_MAX_AGE_DAYS:
        events = [e for e in rec["events"] if e.date >= since]
        _FOMC_CACHE[key] = events
        return events
    # 2) 维护表(前瞻) + 联网抓取(校验/补全) 合并
    by_date: dict[dt.date, Event] = {e.date: e for e in _curated_fomc_events(base)}
    try:
        import urllib.request
        req = urllib.request.Request(_FOMC_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", "ignore")
        for d in _parse_fomc_html(html, since=dt.date(2000, 1, 1)):
            by_date[d] = Event(date=d, name=f"FOMC 议息（{d.year}-{d.month:02d}）", codes=None)
    except Exception:  # noqa: BLE001 - 联网失败不阻断，维护表兜底
        pass
    all_events = sorted(by_date.values(), key=lambda e: e.date)
    # 3) 落盘全集（供后续复用）；返回 >= since
    _save_fomc_db(all_events)
    out = [e for e in all_events if e.date >= since]
    _FOMC_CACHE[key] = out
    return out


def fetch_d15_events(path: Path = EVENTS_FILE, candidates: Iterable | None = None,
                     include_expiry: bool = False, include_macro: bool = False,
                     today: dt.date | None = None) -> EventCalendar:
    """加载事件日历 → EventCalendar。

    三大事件源，全部自动派生为主、人工可选标注为辅：
    - **到期事件**（自动）：include_expiry=True 时，candidates 优先；未传则自动拉全池候选
      （_pool_expirations_cache，tbpy 不可用→空，不影响）。
    - **宏观事件·FOMC**（自动）：include_macro=True 时，从美联储官网拉取议息日程（fetch_fomc_events，
      网络/被墙→空，优雅降级"拉取不到就算了"）；codes=None→影响全池。
    - **人工标注**（events.json，可选）：文件存在则载入作为附加层；缺失不强制 MISSING。
    三者合并按日期排序。loaded = 有手动数据 ∨ 任一自动源被启用 ∨ 合并后有事件；
    即只要启用自动源（cli 默认启用），空结果也判 PASS（不冒充、不报不可用），符合"拉取不到就算了"。"""
    base = today or dt.date.today()
    data, err = _read_json(path)
    manual: list[Event] = []
    has_manual = data is not None
    if has_manual:
        for raw in (data.get("events") or []):
            if not isinstance(raw, dict):
                continue
            d = _safe_date(raw.get("date"))
            if d is None:
                continue
            name = str(raw.get("name") or "").strip() or "（未命名事件）"
            codes_raw = raw.get("codes")
            codes = frozenset(str(c).upper() for c in codes_raw) if codes_raw else None
            manual.append(Event(date=d, name=name, codes=codes))

    sources: list[str] = []
    if has_manual:
        sources.append("manual-events.json")
    events: list[Event] = list(manual)

    if include_expiry:
        exp = derive_expiration_events(
            _pool_expirations_cache(base) if candidates is None else candidates)
        if exp:
            events.extend(exp)
            sources.append("auto-expiry")

    if include_macro:
        fomc = fetch_fomc_events(base)
        if fomc:
            events.extend(fomc)
            sources.append("auto-fomc")

    events.sort(key=lambda e: (e.date, e.name))
    loaded = has_manual or include_expiry or include_macro or bool(events)
    load_error = err if not loaded else None
    return EventCalendar(events=events, loaded=loaded, load_error=load_error,
                         source=" + ".join(sources))


# ===========================================================================
# D15 · 长假日历（来源＝calendar.db.manual_calendar）
# ===========================================================================
@dataclass(frozen=True)
class HolidayRun:
    """一段连续闭市日（长假判定单元）。

    length＝闭市自然日天数；is_long＝length ≥ th.long_holiday_min（默认 5）；
    last_trading_day＝首闭市日前一天（节前最后交易日，7.3 清仓提醒锚点）。"""

    start: dt.date                    # 首个闭市日
    end: dt.date                      # 末个闭市日
    length: int                       # 连续闭市天数
    last_trading_day: dt.date         # start - 1（节前最后交易日）
    is_long: bool                     # 是否达长假阈值


@dataclass(frozen=True)
class HolidayHit:
    """一次长假临近命中（供 7.3 提醒 / 报告展示）。"""

    run: HolidayRun
    days_until: int                   # 距节前最后交易日天数（0＝今天即节前最后交易日）

    @property
    def text(self) -> str:
        r = self.run
        when = "今天即节前最后交易日" if self.days_until == 0 else f"距节前最后交易日 {self.days_until} 天"
        return (f"⚠ {when}（{r.last_trading_day.isoformat()}）→ 长假 {r.start.isoformat()}"
                f"~{r.end.isoformat()}（连休 {r.length} 日），建议节前清仓（只提示·不强制）")


@dataclass
class HolidayCalendar:
    """fetch_d15_holidays 产物：长假闭市日历 ＋ 7.3 长假提醒判定。

    loaded=False（manual_calendar 读取失败）→ check 判 MISSING。
    source 记录来源口径（manual_calendar(db) / missing）。"""

    closed_dates: list[dt.date] = field(default_factory=list)   # 已排序去重
    runs: list[HolidayRun] = field(default_factory=list)
    loaded: bool = False
    load_error: str | None = None
    source: str = ""

    def is_closed(self, date: dt.date) -> bool:
        """某自然日是否闭市。"""
        return date in self._closed_set

    @property
    def _closed_set(self) -> set[dt.date]:
        # 惰性构建闭市集（is_closed 频繁查询用）；每次基于 closed_dates 现算，避免额外状态
        return set(self.closed_dates)

    def long_holidays(self) -> list[HolidayRun]:
        """全部长假段（length ≥ 阈值），按开始日排序。"""
        return [r for r in self.runs if r.is_long]

    def upcoming(self, today: dt.date, days: int | None = None,
                 long_only: bool = True) -> list[HolidayHit]:
        """today 起 days 天内即将进入节前最后交易日的长假（last_trading_day ≥ today 且 ≤ days）。

        long_only=True 只报长假（默认，7.3 口径）；已进入假期（last_trading_day < today）不再提醒；days=None→读 th.holiday_warn_days。"""
        if days is None:
            days = load_thresholds().holiday_warn_days
        hits: list[HolidayHit] = []
        for r in self.runs:
            if long_only and not r.is_long:
                continue
            du = (r.last_trading_day - today).days
            if 0 <= du <= days:
                hits.append(HolidayHit(run=r, days_until=du))
        hits.sort(key=lambda h: h.days_until)
        return hits

    def check(self, today: dt.date, days: int | None = None) -> tuple[Verdict, str]:
        """7.3 长假四态：数据源均不可用→MISSING；days 内有长假临近→WARN；否则 PASS（永不 FAIL）；days=None→读 th.holiday_warn_days。"""
        if days is None:
            days = load_thresholds().holiday_warn_days
        if not self.loaded:
            return Verdict.MISSING, f"长假日历不可用（{self.load_error}）→ 无法判长假提示，人工核对"
        hits = self.upcoming(today, days=days)
        if hits:
            return Verdict.WARN, "；".join(h.text for h in hits)
        return Verdict.PASS, f"{days} 天内无长假节前提醒"

    def __len__(self) -> int:
        return len(self.closed_dates)


# derive_closed_dates 已移除：长假日历改由 manual_calendar 表提供，不再用 tbpy 探针自动派生。


# HolidayCacheRepo 已移除：长假日历改由 manual_calendar 表提供，holiday_cache 磁盘缓存不再写入。


def fetch_d15_holidays(long_min: int | None = None,
                       code: str = DEFAULT_HOLIDAY_PROBE_CODE,
                       scan_days: int = HOLIDAY_SCAN_DAYS, start: dt.date | None = None,
                       auto: bool = True, _probe=None) -> HolidayCalendar:
    """加载长假闭市日历（来源＝calendar.db.manual_calendar，不再调用 tbpy / 不读 holidays.json）。

    manual_calendar(date, is_open, note)：is_open=0 → 闭市。取 [start - PAST, start + scan_days]
    区间内的闭市日，连续分组为 runs；连续 ≥ long_min 闭市日＝长假。区间内 manual_calendar 无记录的
    日期默认视为交易日，不会造成假长假（但亦不会预警超出日历覆盖范围的远期假期，需确保日历已覆盖）。
    long_min=None→读 th.long_holiday_min（单一事实源）。auto/_probe 仅为签名兼容保留，已不再使用 tbpy。"""
    if long_min is None:
        long_min = load_thresholds().long_holiday_min
    today = start or dt.date.today()
    # 内存缓存（同进程避免重复扫库）
    global _HOLIDAY_CACHE
    if _HOLIDAY_CACHE is None:
        _HOLIDAY_CACHE = {}
    im_key = f"{today.isoformat()}|{scan_days}|{code}"
    if im_key in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[im_key]
    start_scan = today - dt.timedelta(days=HOLIDAY_SCAN_PAST_DAYS)
    end_scan = today + dt.timedelta(days=scan_days)
    try:
        try:
            from .calendar_cache import get_closed_dates
        except ImportError:
            from sell_option.fetchers.calendar_cache import get_closed_dates
        dates = get_closed_dates(start_scan, end_scan)
        cal = HolidayCalendar(closed_dates=dates,
                              runs=_group_runs(dates, long_min),
                              loaded=True, load_error=None,
                              source="manual_calendar(db)")
    except Exception as e:  # noqa: BLE001
        cal = HolidayCalendar(closed_dates=[], runs=[], loaded=False,
                              load_error=f"manual_calendar 读取失败：{e}", source="missing")
    _HOLIDAY_CACHE[im_key] = cal
    return cal


# ===========================================================================
# 自检（连真实 tbpy + 联网拉取 FOMC）
# ===========================================================================


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    today = dt.date.today()
    print("=" * 72)
    print(f"[D15 自检 · {today.isoformat()}]（manual_calendar 长假 + 联网 FOMC）")

    # 1) 长假：读 manual_calendar
    print("\n[长假·manual_calendar]")
    hol = fetch_d15_holidays(auto=True)
    v, why = hol.check(today)
    print(f"  source={hol.source} · 闭市日 {len(hol)} 个 / 长假段 {len(hol.long_holidays())} 个")
    print(f"  → {v.value}：{why}")
    for r in hol.long_holidays():
        print(f"    {r.start.isoformat()}~{r.end.isoformat()}（连休 {r.length} 日）")

    # 2) 到期事件 + FOMC + events.json：全源合并
    print("\n[事件日历·全源合并]")
    ev = fetch_d15_events(include_expiry=True, include_macro=True, today=today)
    v2, why2 = ev.check(today)
    print(f"  source={ev.source} · 事件 {len(ev)} 条")
    print(f"  → {v2.value}：{why2}")
    # 展示近期事件明细
    upcoming = ev.upcoming(today, days=30)
    if upcoming:
        print(f"  近 30 天事件：")
        for h in upcoming:
            print(f"    {h.text}")

    # 3) FOMC 单独展示
    print("\n[FOMC·联网拉取]")
    fomc = fetch_fomc_events(today)
    print(f"  拉到 {len(fomc)} 条议息日程")
    for e in fomc:
        print(f"    {e.date.isoformat()} {e.name}")

    # 4) events.json 人工标注层
    print("\n[events.json·人工标注层]")
    if EVENTS_FILE.exists():
        ev_manual = fetch_d15_events(path=EVENTS_FILE)
        print(f"  载入 {len(ev_manual)} 条（仅人工标注）")
    else:
        print(f"  文件不存在（{EVENTS_FILE}），跳过（非必须）")

    print("=" * 72)
