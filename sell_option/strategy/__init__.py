"""策略聚合层（§七 → §八）：组合逻辑单元 + 漏斗汇总 + 输出分析 / 操作结果：
```
sell_option/
├── fetchers/       数据层（§六 D01–D18）：fetch_dXX() 产出可复用数据产物
├── thresholds.py   阈值配置层（§6.4）：ThresholdSet + load_thresholds()
├── checks/         逻辑单元层（§七）：cX_* 检查单元（只依赖数据层，独立可复用）
└── strategy/       策略聚合层：本包——【一策略一模块】+ 通用底座 base
```

本层【只组合、不判定】：具体判定在逻辑单元层（checks/）。本包按【一策略一模块】划分：
- `base.py`    通用机制（策略无关）：Strategy 契约 + combine_results 漏斗合并（labels 注入）；
- `opening.py` 开仓策略（§7.1 检查编排 + §7.2 结构选择）——已落地；
- `assemble.py` §7.2 选腿装配（取数→定结构→选腿→算经济性→填 OpeningData，runner 注入接缝的实体）——已落地；
- `runner.py`  单合约/全池开仓运行编排（时段状态先设好、贯穿 取数→check→策略）——已落地（装配回调注入）；
- `position.py` 仓位风控策略（§7.4 仓位限 P1）——已落地；
- `closing.py` 平仓策略（§7.3 出场规则编排 X1 止盈 / X2 止损 / X3 破位 / X4 到期）——已落地；
- `report.py`  综合日报合成（§八 A 开仓 / B 持仓处置 / C 组合风控 / D 提醒待人工，纯渲染层）——已落地；
- `cli.py`（§11.2 命令面 scan/check/exit/collect-oi/refresh/scope/report，接线 runner+closing+position+report+assemble）——已落地。

公共接口（惰性导出，见模块级 __getattr__）：
- 通用：`Strategy` / `combine_results`（strategy.base）；`CheckResult` / `Verdict` / `merge_verdicts`（复用 checks）；
- 开仓：`OpeningData` / `OPENING_CHECKS` / `run_opening_checks` / `evaluate_opening` / `classify_opening` / `funnel_label` / `FUNNEL_LABEL`（strategy.opening）；
- 装配：`assemble_candidate` / `make_assemble` / `default_bundle` / `select_legs` / `compute_economics` / `structure_word` / `FetchBundle` / `SpreadLegs` / `LegSelection` / `Economics`（strategy.assemble）；
- 编排：`run_candidate` / `run_pool` / `CandidateRun`（strategy.runner）；
- 仓位风控：`PositionData` / `POSITION_CHECKS` / `run_position_checks` / `evaluate_position` / `risk_label` / `RISK_LABEL`（strategy.position）；
- 平仓：`ClosingData` / `CLOSING_CHECKS` / `run_closing_checks` / `evaluate_closing` / `closing_label` / `closing_disposition` / `CLOSING_LABEL`（strategy.closing）；
- 报告：`ReportData` / `PositionDisposition` / `make_disposition` / `render_report` / `save_report` / `write_report` / `REPORTS_DIR`（strategy.report）；
- 命令面：`CliRuntime` / `default_runtime` / `build_parser` / `main` / `cmd_scan` / `cmd_check` / `cmd_exit` / `cmd_collect_oi` / `cmd_refresh` / `cmd_scope` / `cmd_report`（strategy.cli）。

**为何惰性导出**：本包含带 `__main__` 自检的子模块（opening…），若在包加载时 eager import，
`python -m sell_option.strategy.opening` 会触发 RuntimeWarning（子模块已在 sys.modules）。
故用 PEP 562 `__getattr__` 延迟到首次属性访问才 import（与 checks/ / fetchers/ 同一意图）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# 公共名 → 所属子模块（首次访问才 import，避免 -m 运行自检时的 RuntimeWarning）
_LAZY = {
    # strategy.base（聚合层通用机制）
    "Strategy": ".base", "combine_results": ".base",
    "CheckResult": ".base", "Verdict": ".base", "merge_verdicts": ".base",
    "base": ".base",
    # strategy.opening（开仓策略）
    "OpeningData": ".opening", "OPENING_CHECKS": ".opening", "OpeningCheck": ".opening",
    "run_opening_checks": ".opening", "evaluate_opening": ".opening",
    "classify_opening": ".opening", "LABEL_CANDIDATE": ".opening",
    "funnel_label": ".opening", "FUNNEL_LABEL": ".opening",
    "opening": ".opening",
    # strategy.assemble（§7.2 选腿装配：取数→定结构→选腿→算经济性→填 OpeningData，runner 注入实体）
    "assemble_candidate": ".assemble", "make_assemble": ".assemble", "default_bundle": ".assemble",
    "select_legs": ".assemble", "compute_economics": ".assemble", "structure_word": ".assemble",
    "FetchBundle": ".assemble", "SpreadLegs": ".assemble", "LegSelection": ".assemble",
    "Economics": ".assemble", "assemble": ".assemble",
    # strategy.runner（单合约/全池开仓运行编排：时段状态贯穿 取数→check→策略）
    "CandidateRun": ".runner", "run_candidate": ".runner", "run_pool": ".runner",
    "AssembleFn": ".runner", "runner": ".runner",
    # strategy.position（仓位风控策略：§7.4 仓位限 P1）
    "PositionData": ".position", "POSITION_CHECKS": ".position", "PositionCheck": ".position",
    "run_position_checks": ".position", "evaluate_position": ".position",
    "risk_label": ".position", "RISK_LABEL": ".position", "position": ".position",
    # strategy.closing（平仓策略：§7.3 出场规则 X1 止盈 / X2 止损 / X3 破位 / X4 到期）
    "ClosingData": ".closing", "CLOSING_CHECKS": ".closing", "ClosingCheck": ".closing",
    "run_closing_checks": ".closing", "evaluate_closing": ".closing",
    "closing_label": ".closing", "closing_disposition": ".closing", "CLOSING_LABEL": ".closing",
    "closing": ".closing",
    # strategy.report（综合日报合成 §八：A 开仓 / B 持仓处置 / C 组合风控 / D 提醒待人工，纯渲染层）
    "ReportData": ".report", "PositionDisposition": ".report", "make_disposition": ".report",
    "render_report": ".report", "save_report": ".report", "write_report": ".report",
    "REPORTS_DIR": ".report", "report": ".report",
    # strategy.cli（§11.2 命令面：scan/check/exit/collect-oi/refresh/scope/report，接线全策略层）
    "CliRuntime": ".cli", "default_runtime": ".cli", "build_parser": ".cli", "main": ".cli",
    "cmd_scan": ".cli", "cmd_check": ".cli", "cmd_exit": ".cli", "cmd_collect_oi": ".cli",
    "cmd_refresh": ".cli", "cmd_scope": ".cli", "cmd_report": ".cli", "cli": ".cli",
}

__all__ = list(_LAZY)


def __getattr__(name: str):
    """PEP 562 惰性属性解析：首次访问公共名时才 import 对应子模块。"""
    if name in _LAZY:
        import importlib

        mod = importlib.import_module(_LAZY[name], __name__)
        # 导出名即子模块本身（base / opening）→ 返回模块；否则返回模块内同名符号（combine_results…）
        return mod if _LAZY[name].rpartition(".")[2] == name else getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # 仅供静态分析/IDE 补全，运行时不执行
    from .base import Strategy, combine_results, CheckResult, Verdict, merge_verdicts
    from .opening import (
        OpeningData, OPENING_CHECKS, OpeningCheck, run_opening_checks,
        evaluate_opening, classify_opening, LABEL_CANDIDATE, funnel_label, FUNNEL_LABEL,
    )
    from .assemble import (
        assemble_candidate, make_assemble, default_bundle, select_legs,
        compute_economics, structure_word, FetchBundle, SpreadLegs, LegSelection, Economics,
    )
    from .runner import CandidateRun, run_candidate, run_pool, AssembleFn
    from .position import (
        PositionData, POSITION_CHECKS, PositionCheck, run_position_checks,
        evaluate_position, risk_label, RISK_LABEL,
    )
    from .closing import (
        ClosingData, CLOSING_CHECKS, ClosingCheck, run_closing_checks,
        evaluate_closing, closing_label, closing_disposition, CLOSING_LABEL,
    )
    from .report import (
        ReportData, PositionDisposition, make_disposition,
        render_report, save_report, write_report, REPORTS_DIR,
    )
    from .cli import (
        CliRuntime, default_runtime, build_parser, main,
        cmd_scan, cmd_check, cmd_exit, cmd_collect_oi, cmd_refresh, cmd_scope, cmd_report,
    )
