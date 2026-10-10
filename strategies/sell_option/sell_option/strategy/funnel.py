"""漏斗淘汰统计（诊断用）：定位「机会枯竭」的真凶。

把全池扫描结果（list[CandidateRun]）聚合成：
- 每个检查（#1–#10）各**阻断**了多少品种（n_elim，卡掉）；
- 其中谁是**第一个**阻断项（n_first，首要杀手 —— 真凶排序依据）；
- 装配异常 / 无腿可评（选腿失败、凑不出保护档）的品种清单；
- WARN（不阻断，仅提示）与 skipped（休市跳过 / 早筛排除，不占期权取数）分开计。

用法：机会变少时**先量测再调阈值**——不要凭感觉改 rw_min / otm_pct / oi_min。
纯统计，不取数、不含判定；判定仍在 checks/opening。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

try:  # 包方式（python -m / import）：相对导入
    from ..checks.models import Verdict
    from .opening import LABEL_CANDIDATE
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import Verdict
    from sell_option.strategy.opening import LABEL_CANDIDATE

__all__ = ["RuleStat", "FunnelStats", "build_funnel_stats",
           "render_funnel_text", "render_funnel_md"]

# 阻断性判否：FAIL / MISSING 且 blocking=True 才算「卡掉」；WARN 不阻断
_ELIM_VERDICTS = (Verdict.FAIL, Verdict.MISSING)
# 这两个标签代表「通过了漏斗」，不应归入「无腿可评」
_PASS_LABELS = ("可开", LABEL_CANDIDATE)


def _rule_order(rule_id: str) -> tuple:
    """排序键：#9 → (9, '#9')；无数字的兜到最后。"""
    digits = "".join(ch for ch in rule_id if ch.isdigit())
    return (int(digits), rule_id) if digits else (99, rule_id)


@dataclass
class RuleStat:
    """单项检查的漏斗计数。"""
    rule_id: str
    name: str = ""
    n_elim: int = 0        # 该检查阻断性判否（FAIL/MISSING）的品种数
    n_first: int = 0       # 作为「首个阻断项」的品种数 → 首要杀手排序依据
    n_warn: int = 0        # WARN（不阻断）
    n_skip: int = 0        # skipped（休市跳过 / 早筛排除）
    codes_elim: list[str] = field(default_factory=list)
    codes_first: list[str] = field(default_factory=list)


@dataclass
class FunnelStats:
    total: int = 0
    assemble_failed: list[str] = field(default_factory=list)  # 装配异常（data=None）
    no_legs: list[str] = field(default_factory=list)          # 无腿可评（选腿失败/无保护档）
    guard_anomaly: list[str] = field(default_factory=list)   # 保护腿护栏观测告警（检测·不淘汰；run_opening_checks 据此把 #8/#9 降级待人工·放行到人工）
    rules: dict[str, RuleStat] = field(default_factory=dict)
    labels: Counter = field(default_factory=Counter)
    passed: list[str] = field(default_factory=list)           # 标签＝可开

    def sorted_rules(self) -> list[RuleStat]:
        return [self.rules[k] for k in sorted(self.rules, key=_rule_order)]

    def top_killers(self) -> list[RuleStat]:
        """按「首要杀手」次数降序（同次数按规则号）。"""
        return sorted((r for r in self.rules.values() if r.n_first),
                      key=lambda r: (-r.n_first, _rule_order(r.rule_id)))


def build_funnel_stats(runs) -> FunnelStats:
    """聚合一批 CandidateRun → 漏斗统计（纯函数，不取数、不判否）。"""
    st = FunnelStats(total=len(runs))
    for run in runs:
        code = getattr(run, "code", "?")
        label = getattr(run, "label", "") or ""
        if getattr(run, "data", None) is None:          # 装配异常 → 无数据可评
            st.assemble_failed.append(code)
            continue
        # 保护腿护栏结果（检测·不淘汰；下游据其把 #8/#9 降级待人工·放行到人工）：仅登记告警
        g = getattr(run.data, "guard", None)
        if g:
            st.guard_anomaly.append(code)
        st.labels[label or "?"] += 1
        if label == "可开":
            st.passed.append(code)
        results = getattr(run, "results", None) or []
        if not results:                                  # 连检查都没跑起来
            st.no_legs.append(code)
            continue
        first = None
        for res in results:
            rid = getattr(res, "rule_id", "?")
            rs = st.rules.get(rid)
            if rs is None:
                rs = st.rules[rid] = RuleStat(rule_id=rid, name=getattr(res, "name", "") or "")
            det = getattr(res, "detail", None) or {}
            if det.get("skipped"):
                rs.n_skip += 1                           # 休市跳过 / 早筛排除：不算卡掉
                continue
            v = getattr(res, "verdict", None)
            if v is Verdict.WARN:
                rs.n_warn += 1                           # 提示，不阻断
                continue
            if getattr(res, "blocking", False) and v in _ELIM_VERDICTS:
                rs.n_elim += 1
                rs.codes_elim.append(code)
                if first is None:                        # 只记第一个阻断项＝首要杀手
                    first = rs
                    rs.n_first += 1
                    rs.codes_first.append(code)
        if first is None and label not in _PASS_LABELS:
            st.no_legs.append(code)                      # 判为淘汰/待人工却找不到阻断项 → 数据侧问题
    return st


def render_funnel_text(st: FunnelStats) -> str:
    """控制台简报（scan 打印）。"""
    L = ["=" * 72,
         f"【漏斗淘汰统计】全池 {st.total} 品种 —— 定位机会是被谁卡掉的",
         "=" * 72]
    if st.assemble_failed:
        L.append(f"  装配异常（无数据）       {len(st.assemble_failed):>3}   {', '.join(st.assemble_failed[:12])}")
    if st.no_legs:
        L.append(f"  无腿可评（选腿/保护档）  {len(st.no_legs):>3}   {', '.join(st.no_legs[:12])}")
    if st.guard_anomaly:
        L.append(f"  保护腿护栏观测告警(不阻挡)  {len(st.guard_anomaly):>3}   {', '.join(st.guard_anomaly[:12])}")
    L.append("-" * 72)
    L.append("  检查\t\t卡掉\t首要\tWARN\t跳过\t被卡掉的品种（前 10）")
    for r in st.sorted_rules():
        if not (r.n_elim or r.n_warn or r.n_skip):
            continue
        L.append(f"  {r.rule_id} {r.name}\t{r.n_elim}\t{r.n_first}\t{r.n_warn}\t{r.n_skip}\t"
                 f"{', '.join(r.codes_elim[:10])}")
    L.append("-" * 72)
    kill = st.top_killers()
    if kill:
        L.append("  首要杀手（第一个阻断项）排名：")
        for r in kill[:6]:
            L.append(f"    {r.rule_id} {r.name}\t{r.n_first:>3}  → {', '.join(r.codes_first[:12])}")
    else:
        L.append("  首要杀手：无（没有被任何检查阻断的品种）")
    L.append("-" * 72)
    L.append("  结果分布：" + " · ".join(f"{k}:{v}" for k, v in st.labels.most_common()))
    L.append(f"  可开 {len(st.passed)}"
             + (f"（{', '.join(st.passed)}）" if st.passed else ""))
    L.append("=" * 72)
    return "\n".join(L)


def render_funnel_md(st: FunnelStats) -> list[str]:
    """日报小节（markdown）。"""
    L = ["## 漏斗淘汰统计（诊断：机会是被谁卡掉的）", "",
         "> `卡掉`＝该检查**阻断性**判否（FAIL/MISSING）的品种数；"
         "`首要`＝它是该品种**第一个**阻断项的次数（真凶排序依据）；"
         "`WARN` 不阻断、仅提示；`跳过`＝休市跳过或早筛排除（不占期权取数）。", "",
         "| 检查 | 卡掉 | 首要 | WARN | 跳过 | 被卡掉的品种 |",
         "|---|---:|---:|---:|---:|---|"]
    for r in st.sorted_rules():
        if not (r.n_elim or r.n_warn or r.n_skip):
            continue
        L.append(f"| {r.rule_id} {r.name} | {r.n_elim} | {r.n_first} | {r.n_warn} | {r.n_skip} "
                 f"| {', '.join(r.codes_elim[:14])} |")
    if st.assemble_failed:
        L.append(f"| 装配异常（无数据） | {len(st.assemble_failed)} | - | - | - "
                 f"| {', '.join(st.assemble_failed[:14])} |")
    if st.no_legs:
        L.append(f"| 无腿可评（选腿/保护档） | {len(st.no_legs)} | - | - | - "
                 f"| {', '.join(st.no_legs[:14])} |")
    if st.guard_anomaly:
        L.append(f"| 保护腿护栏观测告警（不阻挡） | {len(st.guard_anomaly)} | - | - | - "
                 f"| {', '.join(st.guard_anomaly[:14])} |")
    L.append("")
    kill = st.top_killers()
    if kill:
        L.append("**首要杀手排名**（调阈值优先看这里）：")
        L.append("")
        for r in kill[:6]:
            L.append(f"1. **{r.rule_id} {r.name}** —— 首要阻断 {r.n_first} 个"
                     f"（{', '.join(r.codes_first[:14])}）")
        L.append("")
    L.append("**结果分布**：" + " · ".join(f"{k} {v}" for k, v in st.labels.most_common())
             + f"　→　**可开 {len(st.passed)}**"
             + (f"（{', '.join(st.passed)}）" if st.passed else ""))
    L.append("")
    return L


if __name__ == "__main__":
    # 自检：用假数据验证统计口径（不取数、不联网）
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    from sell_option.checks.models import CheckResult

    def _r(rid, name, verdict, blocking=False, skipped=None):
        return CheckResult(rule_id=rid, name=name, verdict=verdict, reason="",
                           blocking=blocking, detail={"skipped": skipped} if skipped else {})

    class _Run:
        def __init__(self, code, results, label="淘汰", data=object()):
            self.code, self.results, self.label, self.data = code, results, label, data

    runs = [
        _Run("m", [_r("#1", "盘口", Verdict.MISSING, False, "closed_session"),
                   _r("#9", "肥瘦", Verdict.FAIL, True)], "淘汰"),
        _Run("AP", [_r("#1", "盘口", Verdict.MISSING, False, "closed_session"),
                    _r("#2", "OI", Verdict.FAIL, True),
                    _r("#9", "肥瘦", Verdict.FAIL, True)], "淘汰"),
        _Run("CF", [_r("#1", "盘口", Verdict.MISSING, False, "closed_session"),
                    _r("#9", "肥瘦", Verdict.FAIL, True)], "淘汰"),
        _Run("rb", [_r("#1", "盘口", Verdict.MISSING, False, "closed_session"),
                    _r("#4", "IV", Verdict.WARN)], LABEL_CANDIDATE),
        _Run("i", [], "待人工"),
    ]
    print(render_funnel_text(build_funnel_stats(runs)))
