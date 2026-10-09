"""D18 · 预算档（config 已确认：3000 元/笔），供 §7.1 #8 封顶预算『单笔最大亏损 ≤ 预算档』。

需求源 §6.1 D18 / §7.1 #8 / §三 最大亏损公式 / §八 四态：
- **D18 预算档**：口径＝静态参数，权威源＝`config.py`（已确认：3000 元/笔），供给 #8『单笔最大亏损 ≤ 预算档』。
- **#8 封顶预算检查**（§7.1）：判"最大亏损封顶可控"，最大亏损＝**（价差宽度 W − 净权利金 R）× 乘数**（§三），
  用真实价（tbpy，D09/D10 权利金 + D11 乘数）算。本单元提供【纯函数 max_loss_cap】做该折算 +
  【check_budget】拿折算结果（或上游已算好的最大亏损额）对预算档做阈值判定；权利金/乘数取数是上游的事。
- **与 #7（D17）的分工**：#7 限【保证金占用】（单笔 ≤1 万、合计 ≤100%×资金池）；#8 限【单笔最大亏损】
  （亏损维度，价差封顶后最坏亏多少）。两者同为开仓检查、互不替代。
- **预算档数值已确认**：`config.BUDGET_TIER="3000"`；留空("")即未配置 → #8 判 **MISSING**
  （§八 数据不足一律 MISSING，绝不假设预算档冒充通过）。

四态（#8）：预算档未配置/最大亏损额缺失→MISSING；最大亏损 > 预算档→FAIL；≤→PASS。
阈值归属：预算档数值本身即 D18（config 权威源）；#8 无额外比例阈值（与 #7 的 100% 不同，直接比绝对额）。

运行自检：``python -m sell_option.fetchers.d18_budget``（读真实 config.BUDGET_TIER 演示当前配置态）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # 包方式（python -m / import）：相对导入
    from .models import Verdict
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.models import Verdict

# doc §6.1 D18 口径"1500 起 / 3000"——候选档位列表（信息参考；激活档＝config/env 注入的单值）
DEFAULT_TIER_OPTIONS: tuple[float, ...] = (1500.0, 3000.0)


# ---------------------------------------------------------------------------
# 共享工具（离线可测）
# ---------------------------------------------------------------------------
def _to_float(value) -> float | None:
    """安全转 float：None/空串/非数/bool→None（不猜值）。"""
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


def _parse_tiers(value) -> tuple[float, ...]:
    """解析档位列表：接受 list/tuple 或分隔串（"1500,3000" / "1500/3000"）；坏项跳过、去重升序。"""
    if value is None:
        return ()
    items = value if isinstance(value, (list, tuple)) else str(value).replace("/", ",").split(",")
    out: list[float] = []
    for it in items:
        f = _to_float(it)
        if f is not None and f > 0 and f not in out:
            out.append(f)
    return tuple(sorted(out))


def _raw_from_config(attr: str, env_key: str, default):
    """优先读 config.py（D17–D18 权威源，其值已含 env 覆盖）；config 不可用/无该属性→回退 env→default。"""
    try:
        import config  # 项目根 config.py
        val = getattr(config, attr, None)
        if val is not None:
            return val
    except Exception:
        pass
    return os.getenv(env_key, default)


def max_loss_cap(spread_width, net_credit, multiplier) -> float | None:
    """§三 单笔最大亏损封顶＝（价差宽度 W − 净权利金 R）× 乘数；任一输入缺失→None（不猜值）。

    W＝短腿与保护腿行权价之差（D07/选腿产物）；R＝卖腿权利金 − 保护腿权利金（D09/D10，真实价 tbpy）；
    乘数＝D11。结果为该结构最坏情形亏损额（元/组），供 #8 对预算档判定。W≤R（净贷差为负）时返回 0.0
    ——封差价差不可能出现负最大亏损，如实折算不报错。"""
    w = _to_float(spread_width)
    r = _to_float(net_credit)
    m = _to_float(multiplier)
    if w is None or r is None or m is None:
        return None
    return max(w - r, 0.0) * m


# ---------------------------------------------------------------------------
# 产物数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BudgetTier:
    """D18 预算档 ＋ §7.1 #8 封顶预算判定。

    tier=None 或 ≤0 → 未配置 → #8 判 MISSING。
    options＝doc 口径候选档列表（1500/3000，信息参考，不参与判定）。"""

    tier: float | None                                        # 激活预算档（元/笔最大亏损上限）；None=未配置
    options: tuple[float, ...] = field(default=DEFAULT_TIER_OPTIONS)

    @property
    def configured(self) -> bool:
        """预算档是否已配置（tier 为正数）。"""
        return self.tier is not None and self.tier > 0

    # ---- §7.1 #8 封顶预算检查 ----
    def check_budget(self, max_loss) -> tuple[Verdict, str]:
        """单笔最大亏损是否 ≤ 预算档。

        max_loss：上游按 §三（W−R）×乘数 算好的【单笔最大亏损额】（或直接传 max_loss_cap 结果）。
        未配置预算档 / 最大亏损缺失 → MISSING（不冒充）；超档 → FAIL；未超 → PASS。"""
        if not self.configured:
            return Verdict.MISSING, "预算档未配置（D18）→ #8 封顶预算不可判"
        ml = _to_float(max_loss)
        if ml is None:
            return Verdict.MISSING, "单笔最大亏损额缺失（W/R/乘数不全，见 §三 公式）→ #8 无法判定"
        if ml > self.tier:
            return Verdict.FAIL, (f"单笔最大亏损 {ml:,.0f} > 预算档 {self.tier:,.0f} → #8 否决"
                                  f"（亏损封顶不可控）")
        return Verdict.PASS, (f"单笔最大亏损 {ml:,.0f} ≤ 预算档 {self.tier:,.0f}"
                              f"（余量 {self.tier - ml:,.0f}）→ #8 通过")

    def check_structure(self, spread_width, net_credit, multiplier) -> tuple[Verdict, str]:
        """便捷入口：吃结构三要素（W/R/乘数）→ 内部折算最大亏损 → check_budget。

        三要素任一缺失且预算档已配置 → MISSING（折算不出最大亏损）；预算档未配置 → MISSING（优先报未配置）。"""
        ml = max_loss_cap(spread_width, net_credit, multiplier)
        if ml is None and self.configured:
            return Verdict.MISSING, ("结构要素缺失（价差宽度 W / 净权利金 R / 乘数 任一为空）"
                                    "→ 折算不出单笔最大亏损 → #8 无法判定")
        return self.check_budget(ml)


# ---------------------------------------------------------------------------
# 编排：config → BudgetTier
# ---------------------------------------------------------------------------
def fetch_d18(tier=None, options=None) -> BudgetTier:
    """加载 D18 预算档 → BudgetTier。

    参数为【显式注入】（供测试/下游覆盖）；留空(None)则从 config.py 权威源解析（其值已含 env 覆盖）。
    预算档未配置（config.BUDGET_TIER 留空）→ tier=None → configured=False → #8 判 MISSING。
    options 未注入时读 env SELLOPT_BUDGET_OPTIONS（"1500,3000"）→ 默认 doc 口径 (1500, 3000)。"""
    t = _to_float(tier) if tier is not None else _to_float(
        _raw_from_config("BUDGET_TIER", "SELLOPT_BUDGET_TIER", ""))
    if options is not None:
        opts = _parse_tiers(options)
    else:
        opts = _parse_tiers(os.getenv("SELLOPT_BUDGET_OPTIONS", "")) or DEFAULT_TIER_OPTIONS
    return BudgetTier(tier=t, options=opts)


# ---------------------------------------------------------------------------
# 实测：读真实 config.BUDGET_TIER 演示当前配置态
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 72)
    print("D18 · 预算档（config 已确认：3000 元/笔）· #8 单笔最大亏损 ≤ 预算档 · 未配置→MISSING")

    real = fetch_d18()
    if real.configured:
        print(f"  D18 预算档 = {real.tier:,.0f} 元/笔")
        print(f"  候选档 = {real.options}")
        print(f"  #8 判定示例：最大亏损 1200 → {real.check_budget(1200)[0].value}")
    else:
        print("  D18 预算档 = 未配置（config.BUDGET_TIER 留空）→ #8 封顶预算将判 MISSING。")
        print(f"  候选档（doc 口径）= {real.options}；启用：$env:SELLOPT_BUDGET_TIER=\"3000\"")
        print(f"  （或改 config.py 默认值）后重跑即生效。")
    print("=" * 72)
