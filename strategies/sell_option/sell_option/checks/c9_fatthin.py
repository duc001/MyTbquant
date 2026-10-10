"""#9 肥瘦（§7.1）逻辑单元：收益/风险比 R/W ＝ 净权利金 / 价差宽度 ≥ rw_min（默认 20%，硬否决）。

三层定位：本模块是【逻辑单元层】的一个可复用纯函数单元。判定入口 `evaluate` **只吃【已算好的 R/W 标量】**
＋ 阈值集 → 判四态 CheckResult（与 c1–c8 一致：判定权归逻辑单元、读 `th.rw_min` 作权威阈值——沿用
#6/#7/#8【方案A】先例；R/W 无专门数据层 check，W/R 由选腿装配从 D06 行权价 + D09/D10 权利金算出，
c9 是唯一定判方）。

需求源 §7.1 #9 + §三 术语 + §四 绝对价：
- **判什么**：封差价差的"肥瘦"＝卖腿收到的【净权利金 R】占【价差宽度 W】的比例。比例越高越"肥"
  （同样风险下收得多、最大亏损 (W−R)×乘数 越小）；过低（"瘦"）则收益不抵风险、不值得开 → 机械否决。
- **术语严格区分**（见 checks 设计范式）：#9 的 **W＝strike_width（行权价间距＝短腿−保护腿）**、
  **R＝net_credit（卖腿权利金−保护腿权利金）**；与 #1 盘口价差的"价差"(ask−bid 盘口宽度) 不是一回事。
  W/R 与 **#8 封顶预算同源**——#8 用 (W−R)×乘数 判最大亏损、#9 用 R/W 判肥瘦，二者【正交、互不替代】。
- **绝对价口径**（§四）：R/W 用真实合约价（权利金 D09/D10、行权价 D06），绝不用后复权指数价。
- **四态**（§八）：R 或 W 缺失 / W≤0（行权价倒挂、比值无意义）→ **MISSING**（不猜、不冒充）；
  R/W < rw_min → **FAIL**（瘦、硬否决）；≥ rw_min → **PASS**（含边界相等）。无 WARN（肥瘦是硬门槛）。
- **BLOCKING=True**：doc §7.1 #9 未标"非阻断"（对比 #2/#3/#6 均显式标注），与 #1/#4/#5 同列硬否决。
- **needs_live=False**（装配层标记）：R/W 依赖真实价权利金，盘后可回退日线 close（同 #5/#7/#8），粗筛也能判。

运行自检：``python -m sell_option.checks.c9_fatthin``（展示阈值配置 + 与 #8 同源关系）
"""
from __future__ import annotations

from pathlib import Path

try:  # 包方式（python -m / import）：相对导入
    from .models import CheckResult, Verdict
    from ..thresholds import ThresholdSet
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.checks.models import CheckResult, Verdict
    from sell_option.thresholds import ThresholdSet

# 规则元数据（§7.1 #9；聚合层汇总用）
RULE_ID = "#9"
RULE_NAME = "肥瘦"
BLOCKING = True                   # 硬否决：R/W < rw_min（瘦）即淘汰该候选


# ---------------------------------------------------------------------------
# 纯函数工具（离线可测）
# ---------------------------------------------------------------------------
def _to_float(value) -> float | None:
    """安全转 float：None/空串/非数/bool→None（不猜值）。同 c7/c8 惯例。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 检查入口（逻辑单元统一产物 CheckResult；数据入参＝已算好的 R/W 标量 + 阈值）
# ---------------------------------------------------------------------------
def evaluate(net_credit, spread_width, th: ThresholdSet) -> CheckResult:
    """#9 肥瘦：收益/风险比 R/W ＝ 净权利金 / 价差宽度 是否 ≥ rw_min？

    参数（均为已备好的标量，纯函数、不取数）：
      net_credit：净权利金 R（元）＝卖腿权利金 − 保护腿权利金（D09/D10，选腿装配算好）；None/非数→MISSING。
                  R≤0（净贷差/倒挂）不另作缺失处理——自然使 R/W<rw_min→FAIL（瘦、不值得开）。
      spread_width：价差宽度 W（元）＝短腿行权价 − 保护腿行权价（D06，选腿装配算好）；None/非数→MISSING；
                    W≤0（行权价倒挂、比值无意义）→MISSING（数据异常待核，不猜）。
      th：注入阈值集，读 th.rw_min（权威阈值，默认 0.20）。

    四态：R 或 W 缺失 / W≤0 → MISSING；R/W < rw_min → FAIL（硬否决，太瘦）；≥ rw_min → PASS（含边界）。"""
    rw_min = th.rw_min
    th_snap = {"rw_min": rw_min}
    r = _to_float(net_credit)
    w = _to_float(spread_width)

    if r is None or w is None:
        verdict = Verdict.MISSING
        reason = "净权利金 R / 价差宽度 W 缺失（选腿要素不全）→ #9 肥瘦不可判"
        ratio = None
    elif w <= 0:
        verdict = Verdict.MISSING
        reason = f"价差宽度 W={w:,.0f} ≤ 0（行权价倒挂、比值无意义）→ #9 数据异常待核"
        ratio = None
    else:
        ratio = r / w
        if ratio >= rw_min:
            verdict = Verdict.PASS
            reason = f"R/W ＝ {r:,.0f}/{w:,.0f} ＝ {ratio:.1%} ≥ {rw_min:.0%}（肥）→ #9 通过"
        else:
            verdict = Verdict.FAIL
            reason = f"R/W ＝ {r:,.0f}/{w:,.0f} ＝ {ratio:.1%} < {rw_min:.0%}（瘦）→ #9 否决（收益不抵风险）"

    detail = {**th_snap, "net_credit": r, "spread_width": w, "rw_ratio": ratio}
    return CheckResult(RULE_ID, RULE_NAME, verdict, reason, BLOCKING, detail)


# ---------------------------------------------------------------------------
# 实测：展示阈值配置 + 与 #8 同源关系
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.thresholds import load_thresholds
    from sell_option.fetchers.d18_budget import max_loss_cap

    th = load_thresholds()

    print("=" * 72)
    print(f"#9 肥瘦 · R/W ≥ {th.rw_min:.0%} · 瘦→FAIL · BLOCKING={BLOCKING}")
    print(f"  阈值 rw_min = {th.rw_min}")

    W, R, mult = 200, 60, 10
    ratio = R / W
    ml = max_loss_cap(W, R, mult)
    r = evaluate(R, W, th)
    print(f"\n[示例] W={W}, R={R}, 乘数={mult}")
    print(f"  R/W = {ratio:.1%} · #9 {r.verdict.value}")
    print(f"  #8 同源：最大亏损 = (W−R)×乘数 = ({W}−{R})×{mult} = {ml:,.0f} 元")
    print("\n实际 R/W 取决于选腿装配结果，策略运行时自动计算。")
    print("=" * 72)
