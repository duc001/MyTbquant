"""平仓估值共享纯函数（§7.3 止盈/止损的公共底座）：给定持仓 + 当前权利金 → 平回成本 / 浮动盈亏。

三层定位：本模块是【逻辑单元层 checks/】的**共享纯函数底座**（同 models.py / base.py 性质，非检查单元、
无 RULE_ID/evaluate），供 x1_takeprofit（止盈）与 x2_stoploss（止损）复用——两者判定同源于「当前平回成本
vs 开仓净权利金 R」，只是阈值方向不同。放 checks/ 而非 strategy/ 是因逻辑单元层不可反向依赖聚合层。

**估值口径（信用价差卖方标准惯例；〔待〕用户校准）**：
- 开仓净权利金 R＝`Position.net_credit`＝卖腿净收入 − 保护腿净支出（D16 已存快照，单腿则＝卖腿净收入）。
- 当前平回成本 close_cost＝买回卖腿 − 卖出保护腿＝`premium_of(short.symbol) − premium_of(protection.symbol)`
  （单腿无保护腿则＝卖腿现值）。权利金值取 D09/D10 `PremiumQuote.premium`（盘中 last→mid、盘后日线 close）。
- 浮动盈亏 profit＝R − close_cost（正＝浮盈：价差价值已缩水）；**收益率 profit_ratio＝profit / R**（占最大
  权利金比例，R>0 才有意义——最大盈利＝价差归零时的 R）。止损浮亏 loss＝close_cost − R（正＝浮亏）。
- **不可估值即 valued=False**（→ 上游 x1/x2 判 MISSING，绝不用部分数据冒充）：R 缺失 / R≤0 / 卖腿现值缺失 /
  价差结构缺保护腿现值。

串价铁律：权利金只取【真实期权合约】现价（D09/D10，symbol 精确匹配持仓腿），绝不碰指数价。
运行自检：``python -m sell_option.checks.x_pnl``（接真实数据链从 positions.db 取 D16 持仓 + D09/D10 权利金，展示平仓估值）
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

try:  # 包方式（python -m / import）：相对导入
    from .models import Verdict  # noqa: F401 - 供下游一致引用；本模块只产数据不判四态
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if TYPE_CHECKING:  # 数据层产物类型仅标注用（运行时鸭子类型读字段）
    from ..fetchers.d16_positions import Position
    from ..fetchers.d09_d10_premium import PremiumSet


@dataclass(frozen=True)
class ClosePnl:
    """一笔持仓的当前平仓估值快照（x1 止盈 / x2 止损 共用）。

    valued=False → 无法可靠估值（上游判 MISSING、不冒充）；此时 net_credit/close_cost/profit/profit_ratio 可能为 None。"""

    net_credit: float | None       # R＝开仓净权利金（最大盈利）
    close_cost: float | None       # 当前平回成本（买回卖腿−卖出保护腿）
    profit: float | None           # 浮动盈亏＝R − close_cost（正＝浮盈）
    profit_ratio: float | None     # 收益率＝profit / R（占最大权利金比例；R>0 才有）
    valued: bool                   # 是否可估值（False → 上游 MISSING）
    reason: str = ""               # 不可估值原因（valued=False 时填）

    @property
    def loss(self) -> float | None:
        """浮亏额＝close_cost − R（正＝浮亏；负/None→无浮亏）。止损判定用。"""
        if self.close_cost is None or self.net_credit is None:
            return None
        return self.close_cost - self.net_credit


def close_pnl(position: "Position | None", premium: "PremiumSet | None") -> ClosePnl:
    """从持仓 + 当前权利金算平回成本 / 浮动盈亏（不可估值→valued=False，交上游判 MISSING）。

    position：D16 持仓（读 net_credit=R、short_leg/protection_leg.symbol、has_protection）；
    premium：D09/D10 权利金集（按腿 symbol 取现值 premium_of）。任一缺失/退化→valued=False。"""
    if position is None:
        return ClosePnl(None, None, None, None, False, "持仓缺失（无 D16 记录）→ 无法估值")

    r = position.net_credit                      # 开仓净权利金（卖腿缺 premium → None）
    if r is None:
        return ClosePnl(None, None, None, None, False, "开仓净权利金 R 缺失（卖腿无 premium）→ 无法估值")
    if r <= 0:
        return ClosePnl(r, None, None, None, False, f"净权利金 R={r} ≤ 0（非 credit 价差）→ 收益率无意义")

    short_sym = position.short_leg.symbol if position.short_leg else None
    if premium is None or short_sym is None:
        return ClosePnl(r, None, None, None, False, "卖腿现值不可得（无 D09/D10 权利金或缺 symbol）→ 无法估值")
    short_now = premium.premium_of(short_sym)
    if short_now is None:
        return ClosePnl(r, None, None, None, False, f"卖腿 {short_sym} 现值缺失 → 无法估值（不冒充）")

    prot_now = 0.0
    if position.has_protection:
        prot_sym = position.protection_leg.symbol
        pn = premium.premium_of(prot_sym) if prot_sym else None
        if pn is None:
            return ClosePnl(r, None, None, None, False,
                            f"保护腿 {prot_sym} 现值缺失 → 价差平回成本算不出（不用部分数据冒充）")
        prot_now = pn

    close_cost = short_now - prot_now            # 买回卖腿 − 卖出保护腿
    profit = r - close_cost
    return ClosePnl(net_credit=r, close_cost=close_cost, profit=profit,
                    profit_ratio=profit / r, valued=True)


# ---------------------------------------------------------------------------
# 真实数据自检：从 positions.db 加载 D16 持仓 + D09/D10 权利金，展示平仓估值
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d16_positions import fetch_d16
    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.d06_option_chain import fetch_d06, OptionIndexRepo
    from sell_option.fetchers.d09_d10_premium import fetch_premium

    today = __import__("datetime").date.today()
    book = fetch_d16()

    print("=" * 72)
    print(f"x_pnl 平仓估值 · today={today}")
    if not book.loaded or not book.positions:
        print(f"  持仓未加载或为空 → 无法估值")
    else:
        varieties = [(v.code, v.exchange) for v in fetch_d01()]
        repo = OptionIndexRepo()
        repo.ensure_fresh(today, verbose=False)
        cs_all = fetch_d06(varieties, today, repo=repo, verbose=False)
        # 收集持仓中所有腿的 symbol
        legs = []
        for pos in book.positions:
            for leg in [pos.short_leg, pos.protection_leg]:
                if leg and leg.symbol:
                    cc = next((c for c in cs_all.all_contracts() if c.symbol == leg.symbol), None)
                    if cc:
                        legs.append(cc)
        prem = fetch_premium(legs, today, verbose=False) if legs else None
        for pos in book.positions:
            r = close_pnl(pos, prem)
            if r.valued:
                print(f"  {pos.code} {pos.structure} R={r.net_credit:g} "
                      f"close_cost={r.close_cost:g} profit={r.profit:g} ratio={r.profit_ratio:.1%}")
            else:
                print(f"  {pos.code} {pos.structure} → 不可估值：{r.reason}")
    print("=" * 72)
