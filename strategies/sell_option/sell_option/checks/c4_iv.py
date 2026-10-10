"""#4 IV 分位·方向（§7.1）逻辑单元：卖权利金须 IV 高位 + 拐头向下（硬否决，保护卖点质量）。

三层定位 + 与 D13 的分层约定：
- 本模块是【逻辑单元层】#4 检查单元，吃数据层 **D13 产物 `IvVerdict`** 的【观测量】——
  分位 `atmv_percentile` + 方向 `direction`（方向由 `D13._direction` 依 1 日日变算好，复用不重写）
  —— + 阈值集 → 判四态 CheckResult。不取数、不读 config、不依赖策略聚合层。
- **判定唯一归逻辑单元层**：D13 已降级为纯观测供给（只取 IV 观测 + 算方向，不判定），本单元以【统一阈值源
  `ThresholdSet.iv_pct_single/double`】判四态，是 #4 的唯一权威。

需求源 §7.1 #4（用户确认：硬否决 + 60 单 / 70 双 + 方向回落才 PASS）：
- **判什么**：分位 ≥ 门槛（`structure`＝single→`iv_pct_single`60 / dual→`iv_pct_double`70）为"高位"；
  高位 **且** IV 正在回落（direction=falling）才 PASS——水平（贵）+ 方向（拐）两条 AND 缺一不可。
- **数据源**：openvlab 主力月 IV（D13）；tbpy 反解仅对账、不判定。品种级、主力月口径（一品种一门槛，不随月翻倍）。
- **阈值**（从注入的 ThresholdSet 读，均 ✔ 已确认、无〔待〕）：`iv_pct_single`=60、`iv_pct_double`=70。
- **四态**（§八）：分位 < 门槛（含分位=0 真值）→ **FAIL**（硬否决，太便宜不卖）；高位＋falling → **PASS**；
  高位＋rising → **WARN**（等回落再入）；高位＋flat（有日变数据但无方向）→ **WARN**（暂不入场）；
  高位＋方向不明（atmv_1dchg 缺失）→ **MISSING**（数据不足不可判，不冒充软提示）；无 IV / 分位字段缺失 → **MISSING**。
- **BLOCKING=True**：FAIL（低位）即淘汰该候选；WARN 不淘汰（汇总为"可开·有提示"）。

运行自检：``python -m sell_option.checks.c4_iv``（连 openvlab 取全品种 IV + ThresholdSet 门槛判 #4）
"""
from __future__ import annotations

import warnings
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

if TYPE_CHECKING:  # 仅类型标注用；运行时靠鸭子类型读 IvVerdict 字段（不在包加载期 import 数据层重模块）
    from ..fetchers.d13_iv_snapshot import IvVerdict

# 规则元数据（§7.1 #4；聚合层汇总用）
RULE_ID = "#4"
RULE_NAME = "IV分位"
BLOCKING = True                   # 硬否决：IV 分位过低（FAIL）即淘汰该候选

_DIR_LABEL = {"falling": "回落", "rising": "上升", "flat": "走平", "unknown": "不明"}


# ---------------------------------------------------------------------------
# 判定纯函数（门槛按 structure 取；只用 ThresholdSet，不碰 D13 的 state）
# ---------------------------------------------------------------------------
def _threshold(structure: str, th: ThresholdSet) -> float:
    """按结构取 IV 分位门槛：dual（双卖/铁鹰）→ iv_pct_double(70)；其余（单卖）→ iv_pct_single(60)。"""
    return th.iv_pct_double if structure == "dual" else th.iv_pct_single


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝D13 IvVerdict + 结构）
# ---------------------------------------------------------------------------
def evaluate(iv: "IvVerdict | None", structure: str, th: ThresholdSet) -> CheckResult:
    """#4 IV 分位·方向：从 D13 IvVerdict 观测（分位 + 方向）按 ThresholdSet 门槛判四态。

    iv：D13 品种级 IV 观测（用 atmv_percentile / direction；D13 只取数不判定）；
    structure：'single'（门槛 60）/ 'dual'（门槛 70），由 §7.2 结构选择 / 命令 --dual 决定；
    th：注入的阈值集（读 iv_pct_single / iv_pct_double）。"""
    thr = _threshold(structure, th)
    struct_label = "双卖" if structure == "dual" else "单卖"
    if structure not in ("single", "dual"):
        warnings.warn(f"c4_iv: 未预期的 structure={structure!r}（预期 single/dual），已回落 single 门槛", stacklevel=2)
    th_snap = {"structure": structure, "threshold": thr,
               "iv_pct_single": th.iv_pct_single, "iv_pct_double": th.iv_pct_double}

    if iv is None:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"无 IV 数据（D13 openvlab 未取到该品种）→ #4 不可判", BLOCKING, th_snap)

    pct = getattr(iv, "atmv_percentile", None)
    direction = getattr(iv, "direction", "unknown")
    detail = {**th_snap, "percentile": pct, "direction": direction,
              "dir_basis": getattr(iv, "dir_basis", "none"),
              "atmv_current": getattr(iv, "atmv_current", None),
              "atmv_1dchg": getattr(iv, "atmv_1dchg", None), "exp": getattr(iv, "exp", "")}

    if pct is None:
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           "IV 百分位字段缺失（openvlab 有该品种但无分位）→ #4 不可判", BLOCKING, detail)

    if pct < thr:                       # 低位（含分位=0 真值）→ 硬否决
        return CheckResult(RULE_ID, RULE_NAME, Verdict.FAIL,
                           f"IV 分位 {pct:.1f} < {thr:.0f}（{struct_label}门槛，低位硬否决，太便宜不卖）",
                           BLOCKING, detail)

    chg = getattr(iv, "atmv_1dchg", None)
    dir_label = _DIR_LABEL.get(direction, direction)
    if direction == "falling":
        basis = "IV日变"
        return CheckResult(RULE_ID, RULE_NAME, Verdict.PASS,
                           f"IV 分位 {pct:.1f} ≥ {thr:.0f}（{struct_label}高位）且回落（{basis}）→ 卖点质量佳",
                           BLOCKING, detail)
    if direction == "rising":
        return CheckResult(RULE_ID, RULE_NAME, Verdict.WARN,
                           f"IV 分位 {pct:.1f} ≥ {thr:.0f}（{struct_label}高位）但仍{dir_label} → 等回落再入",
                           BLOCKING, detail)
    if direction == "unknown" or chg is None:
        # 方向不明源于 IV 日变数据缺失（atmv_1dchg 无值）→ 属数据缺口，诚实报 MISSING，不冒充软提示
        return CheckResult(RULE_ID, RULE_NAME, Verdict.MISSING,
                           f"IV 分位 {pct:.1f} ≥ {thr:.0f}（{struct_label}高位）但 IV 日变数据缺失（atmv_1dchg 无值）→ 方向不可判，#4 无法判定",
                           BLOCKING, detail)
    # 余下 flat：有日变数据（chg==0）但无明确方向 → WARN 暂不入场
    return CheckResult(RULE_ID, RULE_NAME, Verdict.WARN,
                       f"IV 分位 {pct:.1f} ≥ {thr:.0f}（{struct_label}高位），方向{dir_label} → 暂不入场",
                       BLOCKING, detail)


# ---------------------------------------------------------------------------
# 实测：连 openvlab 取全品种 IV，用 ThresholdSet 门槛判 #4
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import datetime as dt
    import sys
    from collections import Counter

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.d13_iv_snapshot import OpenvlabSource, IvSnapshotRepo, fetch_d13
    from sell_option.thresholds import load_thresholds

    today = dt.date.today()
    varieties = [(v.code, v.exchange) for v in fetch_d01()]
    th = load_thresholds()

    print("=" * 72)
    print(f"#4 IV 分位 · today={today} · 品种 {len(varieties)} 个 · 源 openvlab（纯 HTTP）")
    print(f"门槛：单卖 ≥{th.iv_pct_single:.0f} 分位 · 双卖 ≥{th.iv_pct_double:.0f} 分位 · BLOCKING={BLOCKING}")

    src = OpenvlabSource()
    repo = IvSnapshotRepo()

    res_single = fetch_d13(varieties, today, structure="single", source=src, repo=repo, verbose=True)
    res_dual = fetch_d13(varieties, today, structure="dual", source=src, repo=repo, persist=False, verbose=False)

    # D13 只取观测（不判定）；#4 判定在此用统一阈值源 ThresholdSet 重判
    single = {c: evaluate(res_single.get(c), "single", th) for c, _ in varieties}
    dual = {c: evaluate(res_dual.get(c), "dual", th) for c, _ in varieties}

    covered = sum(1 for v in single.values() if v.verdict != Verdict.MISSING)
    print(f"\n[覆盖] openvlab 主力月 IV 命中：{covered}/{len(varieties)} 品种")
    print(f"[单卖判定分布] {dict(Counter(v.verdict.value for v in single.values()))}")
    print(f"[双卖判定分布] {dict(Counter(v.verdict.value for v in dual.values()))}")

    print("\n[全量明细]（分位 | IV现值% | 日变 | 方向 | 单卖 | 双卖）")
    for code, _exch in varieties:
        vs = res_single.get(code)
        vd = res_dual.get(code)
        if vs is None:
            print(f"  {code:<3} —不在 D01 品种表—")
            continue
        pct = f"{vs.atmv_percentile:.1f}" if vs.atmv_percentile is not None else "NA"
        atm = f"{vs.atmv_current:.2f}" if vs.atmv_current is not None else "NA"
        chg = f"{vs.atmv_1dchg:+.2f}" if vs.atmv_1dchg is not None else "NA"
        cs, cd = single.get(code), dual.get(code)
        print(f"  {code:<3} 分位{pct:>6} | IV{atm:>7} | 日变{chg:>7} | {vs.direction:<7}"
              f" | 单{cs.verdict.value:<6} | 双{cd.verdict.value if cd else '?'}")
        if cs.verdict != Verdict.PASS:
            print(f"       理由(单)：{cs.reason}")

    man = [c for c, v in single.items() if v.verdict == Verdict.MISSING]
    print(f"\n[MISSING 品种]（openvlab 无 IV）：{man or '无'}")
    print(f"[单卖 FAIL 品种数]：{sum(1 for v in single.values() if v.verdict == Verdict.FAIL)}")
    print(f"[单卖 PASS 品种]：{[c for c, v in single.items() if v.verdict == Verdict.PASS] or '无'}")
    print("=" * 72)
