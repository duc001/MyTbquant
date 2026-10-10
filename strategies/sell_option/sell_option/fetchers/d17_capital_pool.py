"""D17 · 资金池（config 人工确认），供 §7.1 #7 保证金『合计占用 ≤ 100% × 资金池』。

需求源 §6.1 D17 / §7.1 #7 / §八 四态：
- **D17 资金池**：口径＝静态参数，权威源＝`config.py`（已确认：4 万 / 100% / 单笔 1 万），供给 #7『合计 ≤100%×资金池』。
- **#7 保证金检查**（§7.1）：单笔 ≤1 万；**合计 ≤ 100% × 资金池**。占用额为估算制（现价×乘数×保证金率×手数×组合优惠系数，
  见 D04/D05，标注『估算』）；本单元只吃【已算好的占用额】做阈值判定，不重算保证金（那是 #7 上游/D04-D05 的事）。
- **资金池只在【交易/开仓决策】时控制**（用户口径）：资金池是 #7 开仓检查的【闸门】——拟开仓后合计占用
  （存量持仓占用 ＋ 本次拟开新仓占用）≤ 100%×资金池；超限只否决【该新仓】、**绝不因占用超限强平存量持仓**，
  也**不用于 §7.3 平仓扫描、不作盘后/日常持续监控**（本工具只读决策支持、不接账户不自动交易）。入口＝`check_open_margin`。
- **资金池数值已确认**：`config.CAPITAL_POOL="40000"`、`margin_pool_ratio=1.00`、`margin_per_trade=10000`；留空("")即未配置 → #7 合计检查判 **MISSING**
  （§八 数据不足一律 MISSING，绝不假设资金池冒充通过）。

四态（#7）：资金池未配置/占用额缺失→MISSING；占用超上限→FAIL；未超→PASS。
阈值归属：占用比例 100%（`pool_ratio`）、单笔上限 1 万（`per_trade_cap`）本质属 **阈值配置层**，
旧 SELLOPT_MARGIN_* env 常量已删，缺省一律读 `load_thresholds().margin_pool_ratio / margin_per_trade`（§6.4 单一事实源）。

运行自检：``python -m sell_option.fetchers.d17_capital_pool``（读真实 config.CAPITAL_POOL 演示当前配置态）
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # 包方式（python -m / import）：相对导入
    from .models import Verdict
    from ..thresholds import load_thresholds
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.models import Verdict
    from sell_option.thresholds import load_thresholds


# ---------------------------------------------------------------------------
# 共享工具（离线可测）
# ---------------------------------------------------------------------------
def _to_float(value) -> float | None:
    """安全转 float：None/空串/非数→None（不猜值）。"""
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


# ---------------------------------------------------------------------------
# 产物数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CapitalPool:
    """D17 资金池 ＋ §7.1 #7 保证金阈值判定。

    total=None 或 ≤0 → 未配置 → 合计检查判 MISSING。"""

    total: float | None                              # 资金池总额（元）；None=未配置
    pool_ratio: float                                # #7 合计占用上限比例（fetch_d17 从 th.margin_pool_ratio 解析注入）
    per_trade_cap: float                             # #7 单笔保证金上限（同上，th.margin_per_trade）

    @property
    def configured(self) -> bool:
        """资金池是否已人工确认配置（total 为正数）。"""
        return self.total is not None and self.total > 0

    def max_total_margin(self) -> float | None:
        """#7 合计保证金占用上限＝pool_ratio × 资金池；未配置→None。"""
        return self.total * self.pool_ratio if self.configured else None

    def usage_ratio(self, occupied_total) -> float | None:
        """合计占用 / 资金池（报告展示用）；未配置或占用缺失→None。"""
        occ = _to_float(occupied_total)
        if not self.configured or occ is None:
            return None
        return occ / self.total

    # ---- §7.1 #7 合计保证金检查 ----
    def check_total_margin(self, occupied_total) -> tuple[Verdict, str]:
        """合计保证金占用是否 ≤ pool_ratio×资金池（底层原语；开仓语境请用 check_open_margin）。

        occupied_total：#7 上游按估算制（D04/D05）算出的【合计占用额】＝存量持仓 ＋ 拟开新仓。
        未配置资金池 / 占用额缺失 → MISSING（不冒充）；超上限 → FAIL；未超 → PASS。"""
        if not self.configured:
            return Verdict.MISSING, "资金池未配置（D17 口径待人工确认）→ #7 合计检查不可判"
        occ = _to_float(occupied_total)
        if occ is None:
            return Verdict.MISSING, "合计保证金占用额缺失 → #7 无法判定"
        limit = self.max_total_margin()
        if occ > limit:
            return Verdict.FAIL, (f"合计占用 {occ:,.0f} > 上限 {limit:,.0f}"
                                  f"（{self.pool_ratio:.0%}×资金池 {self.total:,.0f}）→ #7 否决")
        return Verdict.PASS, (f"合计占用 {occ:,.0f} ≤ 上限 {limit:,.0f}"
                              f"（{self.pool_ratio:.0%}×资金池，余量 {limit - occ:,.0f}）")

    # ---- §7.1 #7 开仓闸门（资金池控制唯一入口：仅交易/开仓决策时调用）----
    def check_open_margin(self, existing_occupied, candidate_margin) -> tuple[Verdict, str]:
        """拟开仓后【合计】占用 ＝ 存量持仓占用(D17 各持仓保证金之和) ＋ 本次拟开新仓占用 ≤ 100%×资金池？

        **资金池只在交易（开仓）时控制**（用户口径）：本判定是 #7 开仓检查的闸门——
          · 超限只否决【拟开的这笔新仓】（FAIL＝不可开）；
          · **绝不因资金池占用超限去强平存量持仓**；
          · **不用于 §7.3 平仓扫描、不作盘后/日常持续监控**。
        资金池未配置 / 存量占用缺失 / 拟开新仓占用缺失 → MISSING（数据不足不冒充）。"""
        if not self.configured:
            return Verdict.MISSING, "资金池未配置（D17 数值待人工确认）→ #7 开仓闸门不可判"
        ex = _to_float(existing_occupied)
        if ex is None:
            return Verdict.MISSING, "存量持仓保证金占用缺失（D17）→ #7 开仓闸门无法判定"
        cand = _to_float(candidate_margin)
        if cand is None:
            return Verdict.MISSING, "拟开新仓保证金占用缺失 → #7 开仓闸门无法判定"
        total = ex + cand
        limit = self.max_total_margin()
        head = f"拟开仓后合计占用 {total:,.0f}（存量 {ex:,.0f}＋新仓 {cand:,.0f}）"
        if total > limit:
            return Verdict.FAIL, f"{head} > 上限 {limit:,.0f}（{self.pool_ratio:.0%}×资金池）→ #7 否决，该新仓不可开"
        return Verdict.PASS, f"{head} ≤ 上限 {limit:,.0f}（余量 {limit - total:,.0f}）→ #7 通过"

    # ---- §7.1 #7 单笔保证金检查 ----
    def check_single_margin(self, margin) -> tuple[Verdict, str]:
        """单笔保证金占用是否 ≤ per_trade_cap（默认 1 万）；缺失→MISSING、超限→FAIL、未超→PASS。"""
        m = _to_float(margin)
        if m is None:
            return Verdict.MISSING, "单笔保证金占用额缺失 → #7 无法判定"
        if self.per_trade_cap is not None and m > self.per_trade_cap:
            return Verdict.FAIL, f"单笔保证金 {m:,.0f} > 上限 {self.per_trade_cap:,.0f} → #7 否决"
        return Verdict.PASS, f"单笔保证金 {m:,.0f} ≤ 上限 {self.per_trade_cap:,.0f}"


# ---------------------------------------------------------------------------
# 编排：config → CapitalPool
# ---------------------------------------------------------------------------
def fetch_d17(total=None, pool_ratio=None, per_trade_cap=None) -> CapitalPool:
    """加载 D17 资金池 → CapitalPool。

    参数为【显式注入】（供测试/下游覆盖）；留空(None)则解析：资金池总额←config.CAPITAL_POOL（D17 权威源、含 env 覆盖），
    两阈值←`load_thresholds()`（§6.4 阈值配置层单一事实源，旧 SELLOPT_MARGIN_* env 分支已删）。
    资金池未配置（config.CAPITAL_POOL 留空）→ total=None → configured=False → #7 合计判 MISSING。"""
    th = load_thresholds()
    t = _to_float(total) if total is not None else _to_float(
        _raw_from_config("CAPITAL_POOL", "SELLOPT_CAPITAL_POOL", ""))
    r = _to_float(pool_ratio) if pool_ratio is not None else th.margin_pool_ratio
    c = _to_float(per_trade_cap) if per_trade_cap is not None else th.margin_per_trade
    return CapitalPool(total=t, pool_ratio=r, per_trade_cap=c)


# ---------------------------------------------------------------------------
# 实测：读真实 config.CAPITAL_POOL 演示当前配置态
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 72)
    print("D17 · 资金池（config 人工确认）· #7 合计占用 ≤ 100%×资金池 · 未配置→MISSING")

    real = fetch_d17()
    if real.configured:
        print(f"  D17 资金池 = {real.total:,.0f} 元")
        print(f"  #7 合计上限 = {real.max_total_margin():,.0f}（{real.pool_ratio:.0%}×资金池）")
        print(f"  #7 单笔上限 = {real.per_trade_cap:,.0f}")
    else:
        print("  D17 资金池 = 未配置（config.CAPITAL_POOL 留空）→ #7 合计检查将判 MISSING。")
        print("  启用：$env:SELLOPT_CAPITAL_POOL=\"200000\"（或改 config.py 默认值）后重跑即生效。")
    print("=" * 72)
