"""P1 仓位限（§7.4）逻辑单元：总笔数 / 每品种 / 同持品种数 任一超限→FAIL（暂停开新仓、不强平存量）。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元，**直接吃数据层产物**（D16 `PositionBook` 持仓计数）
＋ 阈值集 → 判四态 CheckResult。沿用 #6–#10【方案A】：判定权归本单元、读 `th` 作权威阈值——D16.limit_status
缺省参也已改读 ThresholdSet、旧 D16_* env 常量已删，此处以 `th.max_positions / max_per_code / max_series` 注入为准。

需求源 §7.4 + positions.example.json『_规则』（三值〔待校准〕、以 §6.4 阈值层为准）：
- **判什么**：最多 `max_positions`(6) 笔总持仓 / 每品种 ≤ `max_per_code`(2) 笔 / 同持 ≤ `max_series`(3) 个品种（按 distinct 品种代码计数，无板块归并）。
- **四态**（§八）：D16 未加载（positions.db 缺失 / 损坏）→ **MISSING**（仓位不可知、绝不冒充=0）；任一超限
  → **FAIL**（触线、暂停开新仓；不强平存量，与 #7 一致）；全在上限内 → **PASS**。无 WARN（仓位限是硬门槛）。
- **BLOCKING=True**：触线即汇总 FAIL（「暂停开新仓」提示）；不作开仓流程的程序前置硬闸门（已确认）——
  仅随 scan 入日报 C 块，触线由人按报告执行。

运行自检：``python -m sell_option.checks.p_limits``（加载真实 positions.db 评估当前仓位限）
"""
from __future__ import annotations

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

if TYPE_CHECKING:  # 数据层产物类型仅标注用（逻辑单元层依赖数据层，运行时鸭子类型读字段）
    from ..fetchers.d16_positions import PositionBook

# 规则元数据（§7.4 仓位限；聚合层汇总用）
RULE_ID = "P1"
RULE_NAME = "仓位限"
BLOCKING = True                   # 触线→汇总 FAIL「暂停开新仓」提示（仅入日报 C 块、由人执行，非程序前置闸门）


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝D16 PositionBook，同 c7 吃数据层产物）
# ---------------------------------------------------------------------------
def evaluate(book: "PositionBook | None", th: ThresholdSet) -> CheckResult:
    """P1 仓位限：D16 持仓计数是否触及 总笔数 / 每品种 / 同持品种数（distinct codes）上限？

    book：D16 持仓簿（None / 未加载 → MISSING，仓位不可知不冒充=0）；
    th：注入阈值集（读 max_positions / max_per_code / max_series，权威阈值）。
    四态：D16 不可用 → MISSING；任一超限 → FAIL（暂停开新仓）；全在上限内 → PASS。"""
    th_snap = {"max_positions": th.max_positions, "max_per_code": th.max_per_code,
               "max_series": th.max_series}
    if book is None or not getattr(book, "loaded", False):
        err = getattr(book, "load_error", None) if book is not None else "D16 未取到"
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"持仓记录不可用（{err}）→ P1 仓位限判不出（不冒充=0）",
                           BLOCKING, {"thresholds": th_snap})

    # 以 th 为权威阈值复用 D16 计数（limit_status 是 D16 PositionBook 方法；返回 total/codes/distinct_codes/violations；判定仍归本单元）
    ls = book.limit_status(th.max_positions, th.max_per_code, th.max_series)
    detail = {**th_snap, "total": ls["total"], "codes": ls["codes"],
              "distinct_codes": ls["distinct_codes"], "violations": ls["violations"]}
    if ls["violations"]:
        reason = "、".join(ls["violations"]) + " → P1 触线（暂停开新仓、不强平存量）"
        return CheckResult(RULE_ID, RULE_NAME, Verdict.FAIL, reason, BLOCKING, detail)
    reason = (f"仓位在上限内（总 {ls['total']}/{th.max_positions} 笔 · 同持 {ls['distinct_codes']}/{th.max_series} 品种"
              f" · 每品种≤{th.max_per_code}）→ P1 通过")
    return CheckResult(RULE_ID, RULE_NAME, Verdict.PASS, reason, BLOCKING, detail)


# ---------------------------------------------------------------------------
# 实测：加载真实 positions.db，评估当前仓位限
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import datetime as dt
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d16_positions import fetch_d16
    from sell_option.thresholds import load_thresholds

    th = load_thresholds()
    print("=" * 72)
    print(f"P1 仓位限 · 总≤{th.max_positions} / 每品种≤{th.max_per_code} / 同持≤{th.max_series}品种"
          f" · 触线→FAIL · BLOCKING={BLOCKING}")

    book = fetch_d16()
    if not book.loaded:
        print(f"  持仓记录不可用（{book.load_error}）→ P1 不可判")
        print("=" * 72)
        sys.exit(0)

    r = evaluate(book, th)
    ls = r.detail
    print(f"  持仓 {ls['total']} 笔 · 品种 {ls['distinct_codes']} 个")
    if ls.get("violations"):
        print(f"  违规：{'、'.join(ls['violations'])}")
    print(f"  P1 判定：{r.verdict.value} · {r.reason}")

    if book.positions:
        print(f"\n[持仓明细]")
        for p in book.positions:
            print(f"  {p.code:<3} {p.structure:<15} 卖腿 {p.short_leg.symbol:<25}"
                  f" DTE={p.current_dte(dt.date.today()) or '?'}")
    print("=" * 72)
