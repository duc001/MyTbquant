"""阈值配置层 · 全部判定阈值参数集（config 单一事实源），供 §七 全部规则阈值。

需求源 §6.4 阈值配置层 / §7.1–§7.4：
- **定位**：独立于数据项（D01–D18），是规则的"配置面"——阈值不会缺失、不判 MISSING，改动须人工拍板。
  覆盖 **#1–#7 · #9 · #10（#8 预算档＝D18 金额、不在阈值层）· DTE 区间 · 候选月数 N · 破位档 · §7.4 仓位限** 的全部判定阈值。
- **权威源**：`config.py` 的 `SELLOPT_THRESHOLDS` 字典（单一事实源），每项支持 env `SELLOPT_T_*` 覆盖。
- **与 D17/D18 的分工**：D17＝资金池【金额】（config.CAPITAL_POOL）、D18＝预算档【金额】（config.BUDGET_TIER）；
  阈值层＝所有【比例/门槛/天数/倍数】——#7 的 100%、1 万属阈值层（虽然 D17 也 env 兜底）。
  三者互补、各为权威源、不重复定义。
- **已收敛（迁移完成）**：数据层旧 env 常量（d14 OI_THRESHOLD、d15 EVENT_WARN_DAYS/LONG_HOLIDAY_MIN/HOLIDAY_WARN_DAYS、
  d16 DTE_*/MAX_*、d17 DEFAULT_POOL_RATIO/DEFAULT_PER_TRADE_CAP、d13 IvThresholds.SELLOPT_IV_PCT_SINGLE/DUAL）已全部删除，
  其缺省参改读 `load_thresholds()`；
  旧阈值 env 名（SELLOPT_D14_* 阈值类如 OI_THRESHOLD、D15_EVENTS_*/D15_HOLIDAYS_*/D16_*/MARGIN_*/IV_PCT_*）不再起作用，唯一入口＝SELLOPT_T_*；
  取数层限流基础设施参数（SELLOPT_D08/D10/D12/D14 的 *_BATCH/*_COOLDOWN/*_BARS）仍各自生效，不在阈值层。

本模块不直接做 PASS/FAIL 判定——它是【阈值供给方】，下游检查单元拿阈值后各自判。
提供 `validate()` 方法做合理性自检（如 dte_min < dte_max、tp_floor < tp_extend），
异常值→报告 warning list（不阻塞运行、不冒充）。

运行自检：``python -m sell_option.thresholds``（展示当前阈值配置集摘要）
"""
from __future__ import annotations

from dataclasses import dataclass, fields, field
from typing import Any


# ---------------------------------------------------------------------------
# 阈值结构体（frozen；按 §七 规则分组）
# ---------------------------------------------------------------------------
# 默认值来源（关键）：业务默认值**唯一源 = config.py SELLOPT_THRESHOLDS**
# （含 env SELLOPT_T_* 覆盖）。下方 _FIELD_TYPES + _default_for 在每次实例化时
# 从 config 派生字段默认，**本模块不再独立硬编码任何业务数值**——
# 消除此前「dataclass 默认 vs config 字面量」双真值漂移（原冗余兜底层 ③）。
# config 完全不可用时的极终兜底＝类型零值（仅保不崩，非业务真值）。
_FIELD_TYPES: dict[str, type] = {
    "oi_min": int, "spread_ratio_max": float,
    "adx_ranging": float, "slope_ranging": float,
    "iv_pct_single": float, "iv_pct_double": float,
    "safety_sigma": float, "event_warn_days": int,
    "margin_pool_ratio": float, "margin_per_trade": float,
    "rw_min": float,
    "veto_gap_pct": float, "veto_range_edge_pct": float, "veto_atr_ratio": float,
    "dte_min": int, "dte_max": int, "candidate_months": int, "otm_pct": float,
    "prot_strikes": int, "short_candidates": int, "prot_premium_max_ratio": float,
    "tp_floor": float, "tp_extend": float, "sl_multiplier": float,
    "breach_default": float, "breach_m": float, "breach_sr": float,
    "expiry_warn_dte": int, "expiry_must_dte": int,
    "holiday_warn_days": int, "long_holiday_min": int,
    "max_positions": int, "max_per_code": int, "max_series": int,
}

_cfg_cache: dict | None = None
def _default_for(name: str):
    """字段默认：从 config.SELLOPT_THRESHOLDS 取（权威源）；缺失→类型零值兜底。"""
    global _cfg_cache
    if _cfg_cache is None:
        _cfg_cache = _load_from_config()
    typ = _FIELD_TYPES[name]
    v = _cfg_cache.get(name)
    return typ(v) if v is not None else (0 if typ is int else 0.0)

@dataclass(frozen=True)
class ThresholdSet:
    """全部判定阈值。每个字段对应 §七 一条规则阈值，字段名即语义。

    状态标注见 config.py 注释（✔＝已确认；〔待〕＝工作默认、数值待人工最终拍板）。
    修改方式：改 config.py 默认值或设对应 env（SELLOPT_T_*），重启生效。"""

    # ---- §7.1 #1 盘口价差 + #2 持仓量OI（原"#1 流动性"拆分）----
    oi_min: int = field(default_factory=lambda: _default_for("oi_min"))                   # #2 单合约 OI ≥ 此值（门槛保持·已确认 ✔）
    spread_ratio_max: float = field(default_factory=lambda: _default_for("spread_ratio_max"))      # #1 价差 ≤ 权利金 × 此比例（✔）

    # ---- §7.1 #3 市况 regime ----
    adx_ranging: float = field(default_factory=lambda: _default_for("adx_ranging"))           # ADX < 此值 = 震荡（✔）
    slope_ranging: float = field(default_factory=lambda: _default_for("slope_ranging"))       # |20日斜率| < 此值 = 震荡（0.15%/日，✔）

    # ---- §7.1 #4 IV 分位·方向 ----
    iv_pct_single: float = field(default_factory=lambda: _default_for("iv_pct_single"))         # 单卖分位门槛 ≥60（✔）
    iv_pct_double: float = field(default_factory=lambda: _default_for("iv_pct_double"))         # 双卖分位门槛 ≥70（✔）

    # ---- §7.1 #5 安全垫 ----
    safety_sigma: float = field(default_factory=lambda: _default_for("safety_sigma"))           # 现价→保护腿 缓冲 ≥ Nσ（✔ #5 及格底线）

    # ---- §7.1 #6 事件窗口 ----
    event_warn_days: int = field(default_factory=lambda: _default_for("event_warn_days"))            # 事件临近提醒天数（只提醒不否决，✔）

    # ---- §7.1 #7 保证金（金额=D17；此处=比例/单笔上限）----
    margin_pool_ratio: float = field(default_factory=lambda: _default_for("margin_pool_ratio"))     # 合计占用 ≤ 此比例×资金池（✔ 100%）
    margin_per_trade: float = field(default_factory=lambda: _default_for("margin_per_trade"))   # 单笔保证金上限·元（✔ 1 万）

    # ---- §7.1 #9 肥瘦 ----
    rw_min: float = field(default_factory=lambda: _default_for("rw_min"))                # R/W ≥ 此值（〔待〕）

    # ---- §7.1 #10 否决信号（3 形态任一→否决；原"连N同向"已上移 #3 市况，余 3 为〔待〕工作默认）----
    veto_gap_pct: float = field(default_factory=lambda: _default_for("veto_gap_pct"))          # 真实跳空：|今开−昨收|/昨收 ≥ 此比例→否决（〔待〕，默认 2%）
    veto_range_edge_pct: float = field(default_factory=lambda: _default_for("veto_range_edge_pct"))   # 区间贴边：close 处近 20 日区间顶/底 ≤ 此比例（占跨度）→否决（〔待〕，默认 2%）
    veto_atr_ratio: float = field(default_factory=lambda: _default_for("veto_atr_ratio"))         # 波动扩张：ATR(14) ≥ 此倍数×滚动基线（当前窗之前近 60 日）→否决（〔待〕，默认 1.5×）

    # ---- §7.2 结构选择 ----
    dte_min: int = field(default_factory=lambda: _default_for("dte_min"))                   # DTE 区间下限·天（✔）
    dte_max: int = field(default_factory=lambda: _default_for("dte_max"))                  # DTE 区间上限·天（✔）
    candidate_months: int = field(default_factory=lambda: _default_for("candidate_months"))           # 区间内前 N 月（✔）
    otm_pct: float = field(default_factory=lambda: _default_for("otm_pct"))              # （显示字段；选腿已改近端候选集，不再用于选腿）
    prot_strikes: int = field(default_factory=lambda: _default_for("prot_strikes"))       # 保护腿距短腿期望档数（链浅更虚档不足时收窄到可用档数；取值限1..4，默认4）
    short_candidates: int = field(default_factory=lambda: _default_for("short_candidates"))  # 短腿候选集档数（默认3＝平值/虚一/虚二，由近及远优先级）
    prot_premium_max_ratio: float = field(default_factory=lambda: _default_for("prot_premium_max_ratio"))  # 保护腿护栏【检测·不淘汰】：保护腿权利金 ≥ 短腿×此比例即标记异常；下游 run_opening_checks 据其把 #8/#9 降级待人工（放行到人工）

    # ---- §7.3 平仓规则 ----
    tp_floor: float = field(default_factory=lambda: _default_for("tp_floor"))              # 50% 保底止盈（〔待校准〕）
    tp_extend: float = field(default_factory=lambda: _default_for("tp_extend"))             # 结构完好续持到 75%（〔待校准〕）
    sl_multiplier: float = field(default_factory=lambda: _default_for("sl_multiplier"))          # 2× 止损（〔待校准〕）
    breach_default: float = field(default_factory=lambda: _default_for("breach_default"))        # 破位档默认 7%（C 等，✔）＝距短腿 K 最小安全垫（X3 保垫侵蚀口径，未越 K 先平）
    breach_m: float = field(default_factory=lambda: _default_for("breach_m"))              # M 破位档 5%（✔）
    breach_sr: float = field(default_factory=lambda: _default_for("breach_sr"))             # SR 破位档 5%（✔）
    expiry_warn_dte: int = field(default_factory=lambda: _default_for("expiry_warn_dte"))            # 到期 ≤5 提醒（✔）
    expiry_must_dte: int = field(default_factory=lambda: _default_for("expiry_must_dte"))            # 到期 ≤3 必平（✔）
    holiday_warn_days: int = field(default_factory=lambda: _default_for("holiday_warn_days"))          # 长假节前提醒窗·天（✔）
    long_holiday_min: int = field(default_factory=lambda: _default_for("long_holiday_min"))           # 连续 ≥N 闭市日 = 长假（✔）

    # ---- §7.4 仓位与风控 ----
    max_positions: int = field(default_factory=lambda: _default_for("max_positions"))              # 最多持仓笔数（〔待校准〕）
    max_per_code: int = field(default_factory=lambda: _default_for("max_per_code"))               # 每品种 ≤N 笔（〔待校准〕）
    max_series: int = field(default_factory=lambda: _default_for("max_series"))                 # 同持 ≤N 个品种（distinct 品种代码计数、无板块归并；字段名沿用系列旧称，〔待校准〕）

    # ---- 便捷查询方法 ----
    def breach_pct(self, code: str) -> float:
        """查品种破位阈值（bp＝距短腿 K 的安全垫线距离，X3 保垫侵蚀口径）：M/SR→各自专用值，其余→breach_default。code 大小写不敏感。"""
        c = code.strip().upper() if code else ""
        if c == "M":
            return self.breach_m
        if c == "SR":
            return self.breach_sr
        return self.breach_default

    def validate(self) -> list[str]:
        """合理性自检：返回 warning 列表（空=全合理）。不阻塞运行——仅报告异常供人工审视。"""
        w: list[str] = []
        if self.dte_min >= self.dte_max:
            w.append(f"dte_min({self.dte_min}) ≥ dte_max({self.dte_max})，区间无效")
        if self.tp_floor >= self.tp_extend:
            w.append(f"tp_floor({self.tp_floor}) ≥ tp_extend({self.tp_extend})，止盈梯度反转")
        if self.expiry_must_dte > self.expiry_warn_dte:
            w.append(f"expiry_must_dte({self.expiry_must_dte}) > expiry_warn_dte({self.expiry_warn_dte})，必平比提醒还早")
        if self.iv_pct_single > self.iv_pct_double:
            w.append(f"iv_pct_single({self.iv_pct_single}) > iv_pct_double({self.iv_pct_double})，单卖门槛高于双卖不合理")
        if self.oi_min <= 0:
            w.append(f"oi_min({self.oi_min}) ≤ 0，OI 门槛无效")
        if self.candidate_months <= 0:
            w.append(f"candidate_months({self.candidate_months}) ≤ 0，候选月数无效")
        if self.sl_multiplier <= 0:
            w.append(f"sl_multiplier({self.sl_multiplier}) ≤ 0，止损倍数无效")
        if self.margin_pool_ratio <= 0 or self.margin_pool_ratio > 1:
            w.append(f"margin_pool_ratio({self.margin_pool_ratio}) 不在 (0,1] 范围")
        if not (0 < self.prot_premium_max_ratio <= 1):
            w.append(f"prot_premium_max_ratio({self.prot_premium_max_ratio}) 不在 (0,1] 范围")
        if not (1 <= self.prot_strikes <= 4):
            w.append(f"prot_strikes({self.prot_strikes}) 不在 1..4 范围")
        if self.short_candidates <= 0:
            w.append(f"short_candidates({self.short_candidates}) ≤ 0，短腿候选集无效")
        if self.max_per_code > self.max_positions:
            w.append(f"max_per_code({self.max_per_code}) > max_positions({self.max_positions})，单品限超总限")
        if not (0 < self.veto_gap_pct <= 1):
            w.append(f"veto_gap_pct({self.veto_gap_pct}) 不在 (0,1] 范围")
        if not (0 < self.veto_range_edge_pct <= 1):
            w.append(f"veto_range_edge_pct({self.veto_range_edge_pct}) 不在 (0,1] 范围")
        if self.veto_atr_ratio <= 0:
            w.append(f"veto_atr_ratio({self.veto_atr_ratio}) ≤ 0，波动扩张倍数无效")
        return w

    def as_dict(self) -> dict[str, Any]:
        """转为 dict（报告输出/对账用）。"""
        return {f.name: getattr(self, f.name) for f in fields(self)}


# ---------------------------------------------------------------------------
# 编排：config → ThresholdSet
# ---------------------------------------------------------------------------
def _load_from_config() -> dict[str, Any]:
    """读 config.SELLOPT_THRESHOLDS（权威源）；config 不可用→返回空 dict（回退全默认）。"""
    try:
        import config
        raw = getattr(config, "SELLOPT_THRESHOLDS", None)
        if isinstance(raw, dict):
            return dict(raw)
    except Exception:
        pass
    return {}


def _coerce(default_val: Any, value: Any) -> Any:
    """按字段默认值类型强转 config 值（兼容 from __future__ annotations）；转不动→None 回退默认。"""
    if value is None:
        return None
    try:
        if isinstance(default_val, int) and not isinstance(default_val, bool):
            return int(value)
        if isinstance(default_val, float):
            return float(value)
    except (TypeError, ValueError):
        pass
    return None  # 坏值→回退默认


def load_thresholds(**overrides) -> ThresholdSet:
    """加载阈值配置层 → ThresholdSet。

    优先级：overrides（显式注入，测试/下游覆盖）> config.SELLOPT_THRESHOLDS（权威源，含 env）> 字段默认。
    config dict 中不存在或值类型不对的字段→安全回退到 dataclass 默认值。"""
    cfg = _load_from_config()
    cfg.update({k: v for k, v in overrides.items() if v is not None})

    # 按 ThresholdSet 字段名、默认值类型做安全取值
    defaults = ThresholdSet()  # 全默认实例，用于取各字段默认值类型
    kwargs: dict[str, Any] = {}
    for f in fields(ThresholdSet):
        raw = cfg.get(f.name)
        if raw is not None:
            coerced = _coerce(getattr(defaults, f.name), raw)
            if coerced is not None:
                kwargs[f.name] = coerced
    return ThresholdSet(**kwargs)



# ---------------------------------------------------------------------------
# 实测：展示当前阈值配置集摘要
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 72)
    print("阈值配置层 · 全部判定阈值参数集（config 单一事实源 · §6.4）")

    real = load_thresholds()
    print(f"\n  §7.1 开仓检查：")
    print(f"    #1 价差≤{real.spread_ratio_max:.0%}×权利金 · #2 OI≥{real.oi_min}")
    print(f"    #3 ADX<{real.adx_ranging:.0f} · |slope|<{real.slope_ranging*100:.2f}%/日")
    print(f"    #4 IV分位 ≥{real.iv_pct_single:.0f}(单)/{real.iv_pct_double:.0f}(双)")
    print(f"    #5 安全垫 ≥{real.safety_sigma}σ · #6 事件≤{real.event_warn_days}日提醒")
    print(f"    #7 合计≤{real.margin_pool_ratio:.0%}×资金池 · 单笔≤{real.margin_per_trade:,.0f}")
    print(f"    #9 R/W≥{real.rw_min:.0%}")
    print(f"    #10 跳空≥{real.veto_gap_pct:.0%}"
          f"/贴边≤{real.veto_range_edge_pct:.0%}/ATR≥{real.veto_atr_ratio}× 任一否决")
    print(f"\n  §7.2 结构选择：")
    print(f"    DTE {real.dte_min}–{real.dte_max} · N={real.candidate_months} · OTM {real.otm_pct:.0%}")
    print(f"    选腿：短腿候选={real.short_candidates}档(平值/虚一/虚二) · 保护腿宽度={real.prot_strikes}档")
    print(f"\n  §7.3 平仓规则：")
    print(f"    止盈 {real.tp_floor:.0%}/{real.tp_extend:.0%} · 止损 {real.sl_multiplier}×")
    print(f"    破位 default={real.breach_default:.0%} M={real.breach_m:.0%} SR={real.breach_sr:.0%}")
    print(f"    到期≤{real.expiry_warn_dte}提醒/≤{real.expiry_must_dte}必平"
          f" · 长假≥{real.long_holiday_min}日 · 节前≤{real.holiday_warn_days}日提醒")
    print(f"\n  §7.4 仓位与风控：")
    print(f"    最多{real.max_positions}笔 · 每品种≤{real.max_per_code}"
          f" · 同持≤{real.max_series}品种")
    rw = real.validate()
    if rw:
        print(f"\n  ⚠ validate warnings: {rw}")
    else:
        print(f"\n  validate: 全合理 ✓")
    print("=" * 72)
