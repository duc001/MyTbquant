"""逻辑单元层（§七）：每个检查项一个可复用纯函数单元，直接吃数据层产物 + 阈值 → 判四态：
```
sell_option/
├── fetchers/       数据层（§六 D01–D18）：fetch_dXX() 产出可复用数据产物
├── thresholds.py   阈值配置层（§6.4）：ThresholdSet + load_thresholds()
├── checks/         逻辑单元层（§七）：本包——cX_* 检查单元（只依赖数据层，独立可复用）
└── strategy/       策略聚合层：组合逻辑单元 + 漏斗汇总 + 报告 / 操作结果
```

公共接口（惰性导出，见模块级 __getattr__）：
- `CheckResult` / `Verdict` / `merge_verdicts` / `VERDICT_RANK`（checks.models）；
- `CheckUnit`（checks.base，元数据契约）；
- `c1_spread`（#1 盘口价差，硬否决）/ `c2_oi`（#2 持仓量OI，非阻断）/ `c3_regime`（#3 市况）/
  `c4_iv`（#4 IV 分位）/ `c5_safety`（#5 安全垫）/ `c6_event`（#6 事件窗口，非阻断·只提醒）/
  `c7_margin`（#7 保证金，硬否决）/ `c8_budget`（#8 封顶预算，硬否决）/ `c9_fatthin`（#9 肥瘦，硬否决）/
  `c10_veto`（#10 否决信号，硬否决）——§7.1 开仓检查 #1–#10 全部落地；
- `p_limits`（P1 仓位限）——§7.4 仓位风控单元（硬否决·触线暂停开新仓、不强平存量）；
- `x1_takeprofit`（X1 止盈）/ `x2_stoploss`（X2 止损）/ `x3_breach`（X3 破位）/ `x4_expiry`（X4 到期）——§7.3
  平仓出场单元（FAIL→平仓）；`x_pnl`（共享估值底座 close_pnl / ClosePnl，非检查单元）。

四态合并优先级：**FAIL > MISSING > WARN > PASS**（数据缺失一律 MISSING，绝不冒充）。

**为何惰性导出**：本包含带 `__main__` 自检的子模块（cX_*），若在包加载时 eager import，
`python -m sell_option.checks.cX_*` 会触发 RuntimeWarning（子模块已在 sys.modules）。故用 PEP 562
`__getattr__` 延迟到首次属性访问才 import（与 fetchers 极简 __init__ 同一意图）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# 公共名 → 所属子模块（首次访问才 import，避免 -m 运行自检时的 RuntimeWarning）
_LAZY = {
    # checks.models
    "CheckResult": ".models", "Verdict": ".models",
    "merge_verdicts": ".models", "VERDICT_RANK": ".models",
    # checks.base
    "CheckUnit": ".base",
    # 检查单元子模块（#1–#10）
    "c1_spread": ".c1_spread",
    "c2_oi": ".c2_oi",
    "c3_regime": ".c3_regime",
    "c4_iv": ".c4_iv",
    "c5_safety": ".c5_safety",
    "c6_event": ".c6_event",
    "c7_margin": ".c7_margin",
    "c8_budget": ".c8_budget",
    "c9_fatthin": ".c9_fatthin",
    "c10_veto": ".c10_veto",
    # 仓位风控单元子模块（§7.4 P1）
    "p_limits": ".p_limits",
    # 平仓出场单元子模块（§7.3 X1–X4 + x_pnl 共享估值底座）
    "x_pnl": ".x_pnl", "ClosePnl": ".x_pnl", "close_pnl": ".x_pnl",
    "x1_takeprofit": ".x1_takeprofit",
    "x2_stoploss": ".x2_stoploss",
    "x3_breach": ".x3_breach",
    "x4_expiry": ".x4_expiry",
}

__all__ = list(_LAZY)


def __getattr__(name: str):
    """PEP 562 惰性属性解析：首次访问公共名时才 import 对应子模块。"""
    if name in _LAZY:
        import importlib

        mod = importlib.import_module(_LAZY[name], __name__)
        # 导出名即子模块本身（c1_spread）→ 返回模块；否则返回模块内同名符号（CheckResult…）
        return mod if _LAZY[name].rpartition(".")[2] == name else getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # 仅供静态分析/IDE 补全，运行时不执行
    from .models import CheckResult, Verdict, merge_verdicts, VERDICT_RANK
    from .base import CheckUnit
    from . import c1_spread, c2_oi, c3_regime, c4_iv, c5_safety, c6_event, c7_margin, c8_budget, c9_fatthin, c10_veto
    from . import p_limits
    from . import x1_takeprofit, x2_stoploss, x3_breach, x4_expiry
    from .x_pnl import ClosePnl, close_pnl
