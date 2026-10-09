"""#6 事件窗口（§7.1）逻辑单元：临近重大事件 → 只提醒、不否决、不强制平。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元，**直接吃数据层产物**
（D15 `EventCalendar` 事件日历）+ 阈值集 → 判四态 CheckResult。
不取数、不读 config、不依赖策略聚合层——可单独 import、离线单元测试。

需求源 §7.1 #6（事件窗口）/ §八 D 提醒块：
- **判什么**：候选品种在【提醒窗】内是否临近影响它的重大事件（USDA 供需 / FOMC / 非农 / CPI / OPEC+…）。
  期权卖方怕持仓期间撞事件导致标的跳空、IV 骤升；本项把"临近事件"如实提示，供人自行取舍。
- **只提醒·不否决·不强制平**（用户拍板）：`BLOCKING=False`、**永不 FAIL**——避与不避由人定
  （有人反而想吃事件前的高 IV）。故本单元只产 WARN / PASS / MISSING，绝不淘汰候选。
- **数据源**：D15 `EventCalendar`（长假/到期自动派生 ＋ events.json 可选宏观标注）——`upcoming(today, days, code)` 取窗内命中；
  `codes` 缺省=影响全池、指定则按品种过滤（大小写不敏感，数据层已处理）。
- **阈值**（从注入的 ThresholdSet 读，✔ 已确认、无〔待〕）：`event_warn_days`=5（提醒窗·天）。
- **四态**（§八）：日历缺失/损坏（cal 为 None / not loaded）→ **MISSING**（数据不足不冒充）；
  窗内有影响该品种的事件 → **WARN**（附事件名/日期/几天后/影响范围）；窗内无 → **PASS**；**永不 FAIL**。

分层归属（方案 A）：判定权在本逻辑单元——只用数据层 `EventCalendar.upcoming()` 取命中事件，
自行组装 CheckResult；数据层 `EventCalendar.check()` 是便捷预览，不进策略链路（守三层单向）。

运行自检：``python -m sell_option.checks.c6_event``（加载真实 events.json 按品种评估近期事件）
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

try:  # 包方式（python -m / import）：相对导入
    from .models import CheckResult, Verdict
    from ..thresholds import ThresholdSet
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import CheckResult, Verdict
    from sell_option.thresholds import ThresholdSet

if TYPE_CHECKING:  # 仅类型标注用；运行时靠鸭子类型调用数据层产物方法（不在包加载期 import 数据层重模块）
    from ..fetchers.d15_calendar import EventCalendar

# 规则元数据（§7.1 #6；聚合层汇总用）
RULE_ID = "#6"
RULE_NAME = "事件窗口"
BLOCKING = False                  # 只提醒不否决：本单元永不 FAIL；WARN 也不淘汰候选


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝D15 事件日历产物）
# ---------------------------------------------------------------------------
def evaluate(cal: "EventCalendar | None", th: ThresholdSet, *,
             today: dt.date | None = None, code: str | None = None) -> CheckResult:
    """#6 事件窗口：cal 中 today 起 `event_warn_days` 天内、影响 code 的事件 → WARN；无 → PASS；日历不可用 → MISSING。

    cal：D15 事件日历产物（`EventCalendar`；None / not loaded 即数据缺失）；
    th：注入的阈值集（读 event_warn_days）；today：判定基准日（None→今天）；code：品种（None→按全池口径）。
    非阻断：本单元永不 FAIL，WARN 也不淘汰（只提示、避与不避由人定）。"""
    days = th.event_warn_days
    th_snap = {"event_warn_days": days}
    if cal is None or not getattr(cal, "loaded", False):
        err = getattr(cal, "load_error", None) if cal is not None else "未提供事件日历"
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"事件日历不可用（{err}）→ 无法判 #6，人工核对", BLOCKING,
                           {"hits": [], "today": None, "code": code, "thresholds": th_snap})

    base = today or dt.date.today()
    hits = cal.upcoming(base, days=days, code=code)
    scope = f"品种 {code}" if code else "全池"
    detail = {
        "today": base.isoformat(), "code": code, "window_days": days, "thresholds": th_snap,
        "hits": [{"name": h.event.name, "date": h.event.date.isoformat(),
                  "days_until": h.days_until,
                  "codes": sorted(h.event.codes) if h.event.codes else None} for h in hits],
    }
    if hits:
        reason = "；".join(h.text for h in hits) + "（只提醒·不否决，避与不避由人定）"
        return CheckResult(RULE_ID, RULE_NAME, Verdict.WARN, reason, BLOCKING, detail)
    return CheckResult(RULE_ID, RULE_NAME, Verdict.PASS,
                       f"{days} 天内{scope}无事件提醒 → #6 事件窗口通过", BLOCKING, detail)


# ---------------------------------------------------------------------------
# 实测：加载真实 events.json，按品种评估近期事件
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.d15_calendar import fetch_d15_events
    from sell_option.thresholds import load_thresholds

    today = dt.date.today()
    th = load_thresholds()
    varieties = [v.code for v in fetch_d01()]

    print("=" * 72)
    print(f"#6 事件窗口 · today={today} · 提醒窗 ≤{th.event_warn_days} 天 · BLOCKING={BLOCKING}（永不 FAIL）")

    cal = fetch_d15_events()
    if cal is None or not getattr(cal, "loaded", False):
        err = getattr(cal, "load_error", "未知") if cal is not None else "未提供"
        print(f"  事件日历不可用（{err}）→ #6 不可判")
        print("=" * 72)
        sys.exit(0)

    print(f"  事件日历已加载 · {len(cal.events)} 条事件")
    results = [(code, evaluate(cal, th, today=today, code=code)) for code in varieties]
    dist = {}
    for _, r in results:
        dist[r.verdict.value] = dist.get(r.verdict.value, 0) + 1
    print(f"  [判定分布] {dist}")

    print(f"\n[各品种事件窗口]")
    for code, r in results:
        if r.verdict == Verdict.WARN:
            hits = [f"{h['name']}({h['date']} {h['days_until']}天后)" for h in r.detail["hits"]]
            print(f"  {code:<3} {r.verdict.value:<7} {'; '.join(hits)}")
        else:
            print(f"  {code:<3} {r.verdict.value:<7}")

    pass_codes = [c for c, r in results if r.verdict == Verdict.PASS]
    miss_codes = [c for c, r in results if r.verdict == Verdict.MISSING]
    print(f"\n[PASS] {len(pass_codes)} 品种窗内无事件")
    if miss_codes:
        print(f"[MISSING] {miss_codes}")
    print(f"\n[元数据] RULE_ID={RULE_ID} · BLOCKING={BLOCKING}（只提醒不否决）")
    print("=" * 72)
