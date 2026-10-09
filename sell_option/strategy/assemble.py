"""§7.2 选腿装配（runner 注入接缝 `AssembleFn` 的实体实现）：取数 → 定结构 → 选腿 → 算经济性 → 填 OpeningData。

三层定位：本模块是【策略聚合层】的取数装配器——**只组合、不判定**（判定仍只在 checks/opening）。它把
用户约定的选腿规则固化成代码，串起数据层 D01–D18 与逻辑单元 c3_regime（定结构）/ c7_margin（估占用），
产出 `OpeningData` 交给 `runner.run_candidate` → `opening.evaluate_opening` 跑 #1–#10。依赖单向
strategy(assemble)→checks(c3/c7)→fetchers(dXX)，装配内**绝不碰 tbpy**（全部经可注入的 `FetchBundle` 回调）。

需求源 §7.2 结构选择 + §6.3 D 选腿规则 + §三 最大亏损：
- **月份**：候选集＝D06（有挂牌期权 ∧ DTE∈[dte_min,dte_max] ∧ 前 N=candidate_months 近月）；逐候选月由近及远，
  取第一个能选出差价的月（N 档之外不取数、不评估）。
- **结构**（＝#3 映射，`c3_regime.STRUCTURE_MAP`）：震荡→`iron_condor`（双卖铁鹰，两侧价差）；上涨→`single_put`
  （单卖下方 PUT 价差）；下跌→`single_call`（单卖上方 CALL 价差）；判不出→无腿可选（下游判 MISSING）。
  另存 `OpeningData.structure`＝**评估口径 single/dual**（iron_condor→dual、single_*→single；#4 IV 门槛据此）。
- **选腿**（§7.2）：短腿在 {平值, 虚一, 虚二, …} 候选集里**由近及远逐档尝试**（默认3档＝平值/虚一/虚二）；保护腿＝该短腿更虚方向**第 prot_strikes 档**（默认4，取值限1..4；PUT 下一更低、CALL 下一更高）。
  候选档要求其后至少还有 prot_strikes 档更虚可作保护腿，满足即成交（最靠近平值的优先）；全部候选都凑不出→该侧不成立→诚实降级 MISSING。
  允许平值或弱虚值、看时间价值；链浅则自动退到更虚候选，仍不足才放弃（绝不裸卖、不猜档、绝不因加宽丢品种）。
- **经济性**（§三 / #8 / #9 同源）：R＝净权利金＝Σ(卖腿−保护腿权利金)（D09/D10）；W＝价差宽度＝单价差 |短−保护|、
  铁鹰取两侧较宽者（最坏一侧决定封顶亏损）；max_loss＝`d18.max_loss_cap(W,R,乘数)`＝(W−R)×乘数；
  margin_single＝`c7_margin.estimate_candidate_margin`（现价×乘数×率×组合优惠，估算制）。
- **诚实降级铁律**：任一环节缺数（断源 / 无现价 / 判不出市况 / 无保护档 / 权利金缺）→ 相应字段留 None，
  **绝不猜值冒充**，交下游检查判 MISSING。

运行自检：``python -m sell_option.strategy.assemble``（展示选腿装配配置 + 数据层依赖状态）
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

try:  # 包方式（python -m / import）：相对导入
    from ..checks import c3_regime, c4_iv, c5_safety, c7_margin, c10_veto
    from ..checks.models import Verdict
    from ..fetchers.d01_variety_list import fetch_d01
    from ..fetchers.d02_symbol_template import fetch_d02
    from ..fetchers.d04_margin_rate import fetch_d04
    from ..fetchers.d05_combo_discount import fetch_d05
    from ..fetchers.d07_candidate_months import D07Params, fetch_d07, main_month_via_tbpy
    from ..fetchers.d06_option_chain import OptionIndexRepo, fetch_d06
    from ..fetchers.d08_spot_price import fetch_d08
    from ..fetchers.d09_d10_premium import ROLE_PROTECTION, ROLE_SHORT, fetch_premium
    from ..fetchers.d11_contract_multiplier import fetch_d11
    from ..fetchers.d12_index_daily import fetch_d12
    from ..fetchers.d13_iv_snapshot import fetch_d13
    from ..fetchers.d14_contract_oi import fetch_oi
    from ..fetchers._combo_oi_premium import fetch_premium_and_oi
    from ..fetchers.d15_calendar import fetch_d15_events
    from ..fetchers.d16_positions import fetch_d16
    from ..fetchers.d17_capital_pool import fetch_d17
    from ..fetchers.d18_budget import fetch_d18, max_loss_cap
    from ..thresholds import ThresholdSet
    import config
    from ..logutil import get_logger, log_call, flow
    from .opening import OpeningData
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import config
    from sell_option.checks import c3_regime, c4_iv, c5_safety, c7_margin, c10_veto
    from sell_option.checks.models import Verdict
    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.d02_symbol_template import fetch_d02
    from sell_option.fetchers.d04_margin_rate import fetch_d04
    from sell_option.fetchers.d05_combo_discount import fetch_d05
    from sell_option.fetchers.d07_candidate_months import D07Params, fetch_d07, main_month_via_tbpy
    from sell_option.fetchers.d06_option_chain import OptionIndexRepo, fetch_d06
    from sell_option.fetchers.d08_spot_price import fetch_d08
    from sell_option.fetchers.d09_d10_premium import ROLE_PROTECTION, ROLE_SHORT, fetch_premium
    from sell_option.fetchers.d11_contract_multiplier import fetch_d11
    from sell_option.fetchers.d12_index_daily import fetch_d12
    from sell_option.fetchers.d13_iv_snapshot import fetch_d13
    from sell_option.fetchers.d14_contract_oi import fetch_oi
    from sell_option.fetchers._combo_oi_premium import fetch_premium_and_oi
    from sell_option.fetchers.d15_calendar import fetch_d15_events
    from sell_option.fetchers.d16_positions import fetch_d16
    from sell_option.fetchers.d17_capital_pool import fetch_d17
    from sell_option.fetchers.d18_budget import fetch_d18, max_loss_cap
    from sell_option.thresholds import ThresholdSet
    from sell_option.logutil import get_logger, log_call, flow
    from sell_option.strategy.opening import OpeningData

logger = get_logger(__name__)

if TYPE_CHECKING:  # 数据层产物类型仅标注用（运行时鸭子类型；不在包加载期 import 重模块）
    from ..fetchers.d07_candidate_months import CandidateSet
    from ..fetchers.d06_option_chain import D07ChainSet
    from ..fetchers.d08_spot_price import SpotSet
    from ..fetchers.d09_d10_premium import PremiumSet
    from ..fetchers.d12_index_daily import IndexDailySet
    from ..fetchers.d13_iv_snapshot import IvCheckResult
    from ..fetchers.d14_contract_oi import OiSet
    from ..fetchers.d15_calendar import EventCalendar
    from ..fetchers.d16_positions import PositionBook
    from ..fetchers.d17_capital_pool import CapitalPool
    from ..fetchers.d18_budget import BudgetTier
    from ..fetchers.market_session import MarketSession
    from ..fetchers.models import ChainContract
    from .runner import AssembleFn

# 结构词汇（c3_regime 产 detailed 3 值 → 选腿方向用；OpeningData.structure 是 single/dual 2 值 → #4 门槛用）
STRUCTURE_IRON = "iron_condor"
STRUCTURE_PUT = "single_put"
STRUCTURE_CALL = "single_call"
WORD_SINGLE = "single"
WORD_DUAL = "dual"
_WORD_OF = {STRUCTURE_IRON: WORD_DUAL, STRUCTURE_PUT: WORD_SINGLE, STRUCTURE_CALL: WORD_SINGLE}

# 价差侧（选腿方向）
SIDE_PUT = "put"
SIDE_CALL = "call"
_SIDES_OF = {STRUCTURE_IRON: (SIDE_PUT, SIDE_CALL), STRUCTURE_PUT: (SIDE_PUT,), STRUCTURE_CALL: (SIDE_CALL,)}

__all__ = [
    "SpreadLegs", "LegSelection", "Economics", "FetchBundle",
    "select_legs", "compute_economics", "structure_word",
    "assemble_candidate", "make_assemble", "default_bundle",
]


# ---------------------------------------------------------------------------
# 选腿 / 经济性 产物结构（纯数据）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SpreadLegs:
    """一侧垂直价差的两腿：短腿（卖出，近端候选集平值优先选出）+ 保护腿（买入，更虚第 prot_strikes 档）。"""
    side: str                        # "put" / "call"
    short: "ChainContract"           # 短腿（卖出）
    protection: "ChainContract"      # 保护腿（买入，更虚第 prot_strikes 档，默认4）
    short_strike: float
    prot_strike: float
    width: float                     # |short_strike − prot_strike|（价差宽度，恒正）


@dataclass(frozen=True)
class LegSelection:
    """§7.2 选腿结果：结构 + 标的月 + 一到两侧价差（单价差 1 侧、铁鹰 2 侧）+ 选腿依据（现价）。"""
    code: str
    month4: str
    structure: str                              # iron_condor / single_put / single_call（c3 词汇）
    spreads: tuple[SpreadLegs, ...]
    spot: float


    @property
    def spread_width(self) -> float:
        """价差宽度 W：单价差＝该侧宽度；铁鹰＝两侧较宽者（最坏一侧决定封顶亏损）。"""
        return max(sp.width for sp in self.spreads)

    def contracts(self) -> list["ChainContract"]:
        out: list["ChainContract"] = []
        for sp in self.spreads:
            out.extend((sp.short, sp.protection))
        return out

    def roles(self) -> dict[str, str]:
        r: dict[str, str] = {}
        for sp in self.spreads:
            r[sp.short.symbol] = ROLE_SHORT
            r[sp.protection.symbol] = ROLE_PROTECTION
        return r

    def binding_prot_strike(self) -> float:
        """#5 安全垫输入（新口径·spot→保护腿 缓冲）：取距现价最近短腿那一侧的保护腿行权价——铁鹰两侧里缓冲最小（最保守）的一侧。"""
        b = min(self.spreads, key=lambda s: abs(s.short_strike - self.spot))
        return b.prot_strike


@dataclass(frozen=True)
class Economics:
    """选腿经济性（§三 / #8 封顶预算 / #9 肥瘦 同源）：R / W / max_loss / margin / 乘数。任一缺→None（不猜值）。"""
    net_credit: float | None       # R＝Σ(卖腿−保护腿权利金)
    spread_width: float | None     # W＝价差宽度（铁鹰取较宽侧）
    max_loss: float | None         # (W−R)×乘数（d18.max_loss_cap）
    margin: float | None           # 拟开新仓保证金占用（c7 估算制）
    multiplier: float | None       # D11 合约乘数


# ---------------------------------------------------------------------------
# 纯函数：选腿（§7.2 核心，离线可测；只吃合约清单 + 现价 + 结构，不碰数据层对象）
# ---------------------------------------------------------------------------
def structure_word(detailed: str | None) -> str:
    """c3 详细结构（iron_condor/single_put/single_call）→ 评估口径 single/dual（#4 IV 门槛用）。

    未知 / None（判不出市况）→ 回落 single（安全默认；此时不选腿、下游判 MISSING，structure 值不影响结论）。"""
    return _WORD_OF.get(detailed or "", WORD_SINGLE)


def _contract_at(pool: list["ChainContract"], strike: float) -> "ChainContract | None":
    for c in pool:
        if c.strike == strike:
            return c
    return None


def _select_side(chain: list["ChainContract"], spot: float, side: str, *,
                 short_candidates: int = 3, prot_strikes: int = 4) -> SpreadLegs | None:
    """选一侧价差（§7.2）：短腿在 {平值, 虚一, 虚二, …} 候选集里由近及远逐档尝试；
    保护腿＝该短腿更虚方向第 prot_strikes 档（PUT 更低 / CALL 更高）。

    候选集从平值起往虚值方向取 short_candidates 档（默认3：平值/虚一/虚二；优先级：平值>虚一>虚二）；
    每档优先取其后第 prot_strikes 档更虚作保护腿（最靠近平值的优先）；
    链浅（更虚档不足）时收窄到可用档数（取最大 ≥1 档），绝不因宽度不足丢品种；
    仅当该侧完全无更虚档可作保护腿 → None（下游判 MISSING）。
    绝不裸卖、不猜档。"""
    cp = "P" if side == SIDE_PUT else "C"
    if side == SIDE_PUT:
        pool = [c for c in chain if c.cp == cp and c.strike is not None and c.strike <= spot]  # 允许平值
    else:
        pool = [c for c in chain if c.cp == cp and c.strike is not None and c.strike >= spot]  # 允许平值
    if not pool:
        return None
    strikes = sorted({c.strike for c in pool})
    # ---- 候选短腿集：从平值起往虚值方向取 short_candidates 档（优先级：平值 > 虚一 > 虚二 …）----
    n = max(1, min(short_candidates, len(strikes)))
    if side == SIDE_PUT:
        cand = strikes[-n:]                         # 平值(最大)起向下取 n 档
    else:
        cand = strikes[:n]                          # 平值(最小)起向上取 n 档
    # 由近及远（平值优先）：PUT 从平值向虚值递减试、CALL 从平值向虚值递增试
    order = reversed(cand) if side == SIDE_PUT else cand
    for short_strike in order:
        # 更虚方向的保护腿候选（CALL: >short；PUT: <short）
        if side == SIDE_PUT:
            further = sorted([k for k in strikes if k < short_strike], reverse=True)   # 下方由近及远
        else:
            further = sorted([k for k in strikes if k > short_strike])                 # 上方由近及远
        if not further:                               # 该候选完全无更虚档可作保护腿 → 退下一档更虚候选
            continue
        # 期望宽度＝prot_strikes；链浅更虚档不足时收窄到可用档数（取最大 ≥1 档），绝不因宽度不足丢品种
        width = min(prot_strikes, len(further))
        prot_strike = further[width - 1]
        short_c, prot_c = _contract_at(pool, short_strike), _contract_at(pool, prot_strike)
        if short_c is None or prot_c is None:
            continue
        return SpreadLegs(side=side, short=short_c, protection=prot_c,
                          short_strike=float(short_strike), prot_strike=float(prot_strike),
                          width=abs(float(short_strike) - float(prot_strike)))
    return None


def select_legs(chain: list["ChainContract"], spot: float | None, structure: str | None, *,
                code: str = "", month4: str = "",
                short_candidates: int = 3, prot_strikes: int = 4) -> LegSelection | None:
    """§7.2 选腿主入口（纯函数）：按结构在该月链上选出短腿 + 保护腿（铁鹰＝PUT 侧 + CALL 侧各一价差）。

    chain：该标的月的期权链清单；spot：标的月真实合约现价（D08 绝对价）；
    structure：c3 详细结构（iron_condor/single_put/single_call）。
    short_candidates：短腿候选集档数（默认3＝平值/虚一/虚二，由近及远优先级）；
    prot_strikes：保护腿距短腿期望档数（默认4，取值限1..4；链浅更虚档不足时收窄到可用档数、取最大≥1，绝不因宽度不足丢品种）。
    现价缺失/非正、结构未知、任一侧完全无更虚档可作保护腿 → None（诚实降级：无腿可选，下游判 MISSING）。"""
    sides = _SIDES_OF.get(structure or "")
    if not sides or spot is None or spot <= 0:
        return None
    spreads: list[SpreadLegs] = []
    for side in sides:
        sp = _select_side(chain, float(spot), side,
                          short_candidates=short_candidates, prot_strikes=prot_strikes)
        if sp is None:                     # 铁鹰缺一侧即整体不成立（不做残缺结构）
            return None
        spreads.append(sp)
    return LegSelection(code=code, month4=month4, structure=structure or "",
                        spreads=tuple(spreads), spot=float(spot))


# ---------------------------------------------------------------------------
# 纯函数：经济性（§三 / #8 / #9 同源；只吃选腿结果 + 权利金包 + 估算表）
# ---------------------------------------------------------------------------
def compute_economics(selection: LegSelection, premium_set: "PremiumSet", *, multiplier: float | None,
                      d04, d05, d11, code: str, exchange: str,
                      th: "ThresholdSet | None" = None) -> Economics:
    """算 R / W / max_loss / margin（§三 封顶亏损 + #9 肥瘦同源；#7 占用估算）。

    R＝Σ各侧净权利金（任一侧缺 → R=None）；W＝价差宽度（铁鹰取较宽侧）；max_loss＝(W−R)×乘数；
    margin＝c7.estimate_candidate_margin（现价×乘数×率×组合优惠，估算制）。任一原料缺 → 相应值 None（不冒充）。

    保护腿护栏现为【检测·不淘汰】：异常偏高腿由 _detect_prot_anomaly 标记（写 OpeningData.guard
    + 日志 + 漏斗），供人工观察真实数据决策；run_opening_checks 据此把 #8/#9 降级待人工（放行到人工）；
    此处只诚实算 R/W/max_loss/margin，不拦不降。
    与 #9 肥瘦(R/W≥rw_min) 正交：#9 挡"整体过瘦"（此处只诚实算 R/W，不据护栏自行拦截）。"""
    credits: list[float] = []
    for sp in selection.spreads:
        short_p = premium_set.premium_of(sp.short.symbol)
        prot_p = premium_set.premium_of(sp.protection.symbol)
        if short_p is None or prot_p is None:                # 权利金缺数 → 诚实判缺，不冒充
            return Economics(net_credit=None, spread_width=selection.spread_width,
                             max_loss=None, margin=None, multiplier=multiplier)
        credits.append(short_p - prot_p)
    net_credit = sum(credits)
    spread_width = selection.spread_width if selection.spreads else None
    loss = max_loss_cap(spread_width, net_credit, multiplier)
    margin = c7_margin.estimate_candidate_margin(selection.spot, code, exchange,
                                                 d04=d04, d05=d05, d11=d11)
    return Economics(net_credit=net_credit, spread_width=spread_width, max_loss=loss,
                     margin=margin, multiplier=multiplier)


def _detect_prot_anomaly(selection: LegSelection, premium_set: "PremiumSet", *,
                         th: "ThresholdSet | None" = None) -> str | None:
    """保护腿护栏【检测·不淘汰】：检测各侧保护腿权利金是否异常偏高
    （≥ 短腿 × prot_premium_max_ratio，默认 0.90）。本函数仅观测标记（不修改选择、不据其下判决）；
    下游 run_opening_checks 命中后把 #8/#9 降级为 MISSING（待人工），放行到人工。
    命中仅返回可读提示（含腿/权利金/比例），供人工观察真实数据决策。
    权利金缺数不算异常（由 #9 等如实判 MISSING）。"""
    if th is None:
        return None
    ratio = getattr(th, "prot_premium_max_ratio", None)
    if not ratio:
        return None
    hits = []
    for sp in selection.spreads:
        short_p = premium_set.premium_of(sp.short.symbol)
        prot_p = premium_set.premium_of(sp.protection.symbol)
        if short_p and prot_p and prot_p >= short_p * ratio:
            hits.append(f"{sp.side}:保护腿{prot_p:.1f}≥短腿{short_p:.1f}×{ratio}")
    return ("护栏观测(不阻挡): " + "; ".join(hits)) if hits else None


# ---------------------------------------------------------------------------
# 取数依赖注入（真实 fetcher 回调集）：一个 bundle＝一组 fetch 回调，装配只经它取数、绝不直接碰 tbpy
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FetchBundle:
    """assemble 取数依赖的可注入回调集。默认经 `default_bundle()` 绑真实 fetcher（共享昂贵资源）。

    各回调签名（编排器按此调用）：
    - varieties() -> [Variety]；templates() -> SymbolTemplates；margin_tables() -> (d04, d05, d11)
    - chain(varieties:[(code,exch)], today) -> D07ChainSet；candidates(contracts, today, params, templates) -> CandidateSet
    - spot(candidates, today, market_open) -> SpotSet；index_daily(codes, today) -> IndexDailySet
    - premium(contracts, today, roles, market_open) -> PremiumSet；oi(contracts, today, roles, market_open) -> OiSet
    - premium_oi(contracts, today, roles, market_open) -> (PremiumSet, OiSet)：盘后合并一次取数（替代分别调 premium+oi），装配统一经 bundle 走、不再直连 tbpy
    - iv(varieties:[(code,exch)], today, structure) -> IvCheckResult
    - positions() -> PositionBook；capital_pool() -> CapitalPool；budget() -> BudgetTier；events() -> EventCalendar
    """
    varieties: Callable[[], list]
    templates: Callable[[], object]
    chain: Callable[[list, dt.date], "D07ChainSet"]
    candidates: Callable[[list, dt.date, D07Params, object], "CandidateSet"]
    spot: Callable[[object, dt.date, "bool | None"], "SpotSet"]
    index_daily: Callable[[list, dt.date], "IndexDailySet"]
    premium: Callable[[list, dt.date, dict, "bool | None"], "PremiumSet"]
    oi: Callable[[list, dt.date, dict, "bool | None"], "OiSet"]
    premium_oi: Callable[[list, dt.date, dict, "bool | None"], "tuple[PremiumSet, OiSet]"]
    iv: Callable[[list, dt.date, str], "IvCheckResult"]
    positions: Callable[[], "PositionBook"]
    capital_pool: Callable[[], "CapitalPool"]
    budget: Callable[[], "BudgetTier"]
    events: Callable[[], "EventCalendar"]
    margin_tables: Callable[[], tuple]


class _RealFetch:
    """`default_bundle()` 的实现：真实 fetcher + 跨品种共享昂贵资源（期权索引 repo / openvlab HTTP / 边界表 / 估算表）。

    一个 bundle 实例＝一套共享资源，全池逐品种复用它即只 spawn 一次索引子进程、只拉一次 openvlab（懒缓存）。
    IV 用 persist=True：逐品种装配只补写该品种当日 IV 行（iv_snapshot 主键 (variety, snapshot_date)，
    不覆盖其它品种），每日攒快照供 D13 序列（§九）。"""

    def __init__(self, verbose: bool = False):
        self._verbose = verbose
        self._varieties = None
        self._templates = None
        self._index_table = None
        self._margin = None
        self._repo = None
        self._repo_ready = False
        self._openvlab = None

    def varieties(self) -> list:
        if self._varieties is None:
            self._varieties = fetch_d01()
        return self._varieties

    def templates(self):
        if self._templates is None:
            self._templates = fetch_d02()
        return self._templates

    def margin_tables(self) -> tuple:
        if self._margin is None:
            self._margin = (fetch_d04(), fetch_d05(), fetch_d11())
        return self._margin

    def _ready_repo(self, today: dt.date) -> OptionIndexRepo:
        if self._repo is None:
            self._repo = OptionIndexRepo()
        if not self._repo_ready:                       # 全池只 ensure_fresh 一次（今日索引优先、否则重建/近日缓存）
            self._repo.ensure_fresh(today, verbose=self._verbose)
            self._repo_ready = True
        return self._repo

    def chain(self, varieties: list, today: dt.date):
        return fetch_d06(varieties, today, repo=self._ready_repo(today), verbose=self._verbose)

    def candidates(self, contracts: list, today: dt.date, params: D07Params, templates):
        # 主力标记接线（§6.1 D07）：provider 只读子进程落盘的 main_month.json，主进程零原生调用
        # （get_main_instrument 未连接裸调必 AV 2026-10-02 实测；隔离构建+进程级缓存均在 d07 内部）
        return fetch_d07(contracts, today, params=params, symbol_templates=templates,
                         main_provider=main_month_via_tbpy)

    def spot(self, candidates, today: dt.date, market_open):
        return fetch_d08(candidates, today, market_open=market_open, verbose=self._verbose)

    def index_daily(self, codes: list, today: dt.date):
        if self._index_table is None:
            try:
                from ..fetchers.d03_index_symbol import fetch_d03
            except ImportError:  # 脚本直跑
                from sell_option.fetchers.d03_index_symbol import fetch_d03
            self._index_table = fetch_d03()
        return fetch_d12(codes, today, index_table=self._index_table, verbose=self._verbose)

    def premium(self, contracts: list, today: dt.date, roles: dict, market_open):
        return fetch_premium(contracts, today, roles=roles, market_open=market_open, verbose=self._verbose)

    def oi(self, contracts: list, today: dt.date, roles: dict, market_open):
        return fetch_oi(contracts, today, roles=roles, market_open=market_open, verbose=self._verbose)

    def premium_oi(self, contracts: list, today: dt.date, roles: dict, market_open):
        # 盘后合并一次 get_history_n(position+close) 同时取 OI 与收盘权利金（见 _combo_oi_premium）；
        # 经 bundle 注入，补 FetchBundle 契约缺口——装配内不再直连 tbpy（守模块头注释「绝不碰 tbpy」）
        return fetch_premium_and_oi(contracts, today, roles=roles, market_open=market_open,
                                    verbose=self._verbose)

    def iv(self, varieties: list, today: dt.date, structure: str):
        if self._openvlab is None:
            try:
                from ..fetchers.d06_external_sources import OpenvlabSource
            except ImportError:  # 脚本直跑
                from sell_option.fetchers.d06_external_sources import OpenvlabSource
            self._openvlab = OpenvlabSource()            # 共享实例：ctamap-all 一次 HTTP 拉全品种、懒缓存复用
        return fetch_d13(varieties, today, structure=structure, source=self._openvlab,
                         persist=True, verbose=self._verbose)  # persist=True：每日攒 IV 快照（供未来 N 日判据）


def default_bundle(*, verbose: bool = False) -> FetchBundle:
    """构造真实取数 bundle（生产用）。共享资源随 bundle 生命周期复用于全池。"""
    rf = _RealFetch(verbose=verbose)
    return FetchBundle(
        varieties=rf.varieties, templates=rf.templates, chain=rf.chain, candidates=rf.candidates,
        spot=rf.spot, index_daily=rf.index_daily, premium=rf.premium, oi=rf.oi, premium_oi=rf.premium_oi, iv=rf.iv,
        positions=fetch_d16, capital_pool=fetch_d17, budget=fetch_d18, events=fetch_d15_events,
        margin_tables=rf.margin_tables,
    )


# ---------------------------------------------------------------------------
# 编排：一候选（品种）→ 取数装配 → OpeningData（交 runner/opening 跑 #1–#10）
# ---------------------------------------------------------------------------
def _exchange_of(code: str, varieties: list) -> str | None:
    """大小写不敏感比对（D01 代码混用大小写：DCE/SHFE 小写 m/rb、CZCE 大写 AP/SA）。"""
    for v in varieties:
        if (getattr(v, "code", None) or "").upper() == (code or "").upper():
            return getattr(v, "exchange", None)
    return None


def _multiplier_of(d11, code: str) -> float | None:
    """D11 合约乘数（缺品种 / 非正 / 异常 → None，诚实降级）。"""
    try:
        m = float(d11.contract_unit(code))
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    return m if m > 0 else None


def _early_block(data: "OpeningData", th: ThresholdSet) -> str | None:
    """早筛闸：期权权利金+持仓取数（get_history_n，62s/次）极贵；先用【便宜数据】——
    index_daily(D12)/spot(D08)/iv(D13,HTTP 不限流)/margin(由 spot 估算)/capital_pool/events——
    跑「只吃便宜数据且 BLOCKING=淘汰」的检查（#4 IV / #5 安全垫 / #7 保证金 / #10 否决）。
    命中即返回该 rule_id（上游据此免取期权数据）；None＝放行、需取期权数据。
    各检查纯函数、只读数、不改 data；与下游 evaluate_opening 用的是同一份便宜数据，故 FAIL 即真淘汰。"""
    cheap_blocking = [
        (c4_iv.RULE_ID, c4_iv.evaluate(data.iv, data.structure, th)),
        (c5_safety.RULE_ID, c5_safety.evaluate(data.spot, data.iv, data.dte, data.prot_strike, th)),
        (c7_margin.RULE_ID, c7_margin.evaluate(data.margin_single, data.margin_existing, data.capital_pool, th)),
        (c10_veto.RULE_ID, c10_veto.evaluate(data.index_daily, th)),
    ]
    for rid, r in cheap_blocking:
        if r.verdict == Verdict.FAIL and r.blocking:
            return rid
    return None


@log_call(level=logging.DEBUG)
def assemble_candidate(code: str, session: "MarketSession", th: ThresholdSet, bundle: FetchBundle, *,
                       today: dt.date | None = None, structure_override: str | None = None) -> OpeningData:
    """装配【单个候选品种】的 OpeningData（§7.2 选腿 + 全量取数）。runner 的 `assemble(code, session)` 实体。

    时序（时段状态 session.open 贯穿取数全程，作 market_open 下送 D08/D09-D10/D15）：
     D01 交易所 → D06 期权链 → D07 候选月 → D12 指数日线 → c3 定结构 →
     D13 IV（openvlab HTTP·无 60s 限流，提前到选腿前取，供早筛廉价阻断 #4/#5/#7/#10）→
     D08 现价（逐候选月）→ §7.2 选腿（走近端候选集，不依赖 IV）→
     早筛闸（D13 IV + spot 估算 margin 先跑阻断，命中即早退省 62s 期权取数）→
     D09/D10 权利金 + D14 OI（盘后合并为 premium_oi 一次 get_history_n）→ D11/D04/D05 占用估算 + max_loss →
     保护腿护栏检测（只标记·不淘汰）→ D15 事件 → D17 资金池 + D16 存量占用 → D18 预算档 → 填 OpeningData。
    `structure_override="dual"`（命令 --dual）强制评估铁鹰、忽略 #3 结构建议；None 则用 #3 建议结构。
    任一环节缺数即**就地止步**、已取字段保留、其余留 None（诚实降级，下游检查据此判 MISSING）。"""
    today = today or dt.date.today()
    market_open = session.open
    data = OpeningData(code=code, today=today)

    # ---- 与结构无关的静态/配置取数（先填，缺 leg 也不影响这几项判定）----
    data.events = bundle.events()
    data.capital_pool = bundle.capital_pool()
    data.budget_tier = bundle.budget()
    data.margin_existing = bundle.positions().total_margin_occupied()

    exchange = _exchange_of(code, bundle.varieties())
    if exchange is None:                              # 品种不在名单 → 无从取链，止步（下游全 MISSING）
        logger.debug("assemble %s：品种不在名单，止步", code)
        return data

    # ---- D06 期权链（断源→无腿可选，止步）----
    chain = bundle.chain([(code, exchange)], today).for_code(code)
    if not chain:
        logger.debug("assemble %s：无期权链，止步", code)
        return data

    # ---- D07 候选月（DTE 区间 + 前 N 近月）----
    templates = bundle.templates()
    params = D07Params(dte_min=th.dte_min, dte_max=th.dte_max, top_n=th.candidate_months)
    months = bundle.candidates(chain, today, params, templates).months_for(code)
    if not months:
        logger.debug("assemble %s：无候选月，止步", code)
        return data

    # ---- D12 指数日线 → c3 定结构（#3 会在 evaluate 里对同一 index_daily 复判；此处只借它选腿）----
    index_series = bundle.index_daily([code], today).get(code)
    data.index_daily = index_series
    detailed = (STRUCTURE_IRON if structure_override == WORD_DUAL
                else c3_regime.evaluate(index_series, th).detail.get("structure"))
    data.structure = structure_word(detailed)
    logger.debug("assemble %s：结构=%s（detailed=%s）· 候选月=%d · 期权链=%d 腿",
                 code, data.structure, detailed, len(months), len(chain))

    # ---- D13 IV（openvlab HTTP，无 60s 限流）提前到选腿前取：供早筛阻断 _early_block 的廉价判定
    #     （#4 IV 分位/方向、#5 安全垫、#7 保证金、#10 否决信号）使用，避免为被阻断品种浪费 62s 期权取数。
    #     选腿本身不依赖 IV（走近端候选集 short_candidates/prot_strikes），此处取 IV 仅为早筛。----
    iv_verdict = bundle.iv([(code, exchange)], today, data.structure).get(code)
    iv_dec = (iv_verdict.atmv_current / 100.0) if (iv_verdict and iv_verdict.atmv_current is not None) else None

    # ---- D08 现价（逐候选月由近及远）→ §7.2 选腿：取第一个能选出差价的月 ----
    spot_set = bundle.spot(bundle.candidates(chain, today, params, templates), today, market_open)
    selection = None
    for m in months:
        quote = spot_set.get(code, m.month4)
        price = quote.price if quote is not None else None
        month_chain = [c for c in chain if c.underlying_month == m.month4]
        sel = select_legs(month_chain, price, detailed, code=code, month4=m.month4,
                          short_candidates=th.short_candidates, prot_strikes=th.prot_strikes)
        if sel is not None:
            selection, data.spot, data.dte = sel, quote, m.dte
            data.iv = iv_verdict                     # 仅选腿成功才落 OpeningData（与旧行为一致：早筛前才取 IV）
            break
    if selection is None:                             # 无现价 / 判不出结构 / 无保护档 → 无腿（下游 MISSING）
        logger.debug("assemble %s：选腿失败（无现价/无保护档），止步", code)
        return data

    data.prot_strike = selection.binding_prot_strike()     # #5 安全垫新口径：spot→保护腿 缓冲
    logger.debug("assemble %s：选腿成功 · 标的月=%s · 现价=%s · 保护腿行权价=%s · 价差侧=%d",
                 code, selection.month4, selection.spot, data.prot_strike, len(selection.spreads))

    # ---- 早筛闸：期权权利金+持仓取数（get_history_n，62s/次，全池 47 品种累计≈48 分钟）极贵 ----
    # 先用便宜数据跑阻断检查：D13 IV 改在期权取数前取（openvlab HTTP，无 60s 限流），margin 由 spot 估算（不依赖期权）。
    # 命中便宜阻断检查即断定淘汰 → 直接返回，省掉本次 62s 期权取数（下游 run_opening_checks 标记 #1/#2/#8/#9 跳过）。
    d04, d05, d11 = bundle.margin_tables()
    data.margin_single = c7_margin.estimate_candidate_margin(
        selection.spot, code, exchange, d04=d04, d05=d05, d11=d11)
    blocked_by = _early_block(data, th)
    if config.EARLY_EXCLUDE_ENABLED and blocked_by is not None:
        data.early_excluded = blocked_by            # 已断定淘汰 → 早退、不取期权数据
        logger.info("assemble %s：早筛阻断=%s → 省期权取数（D08/D09-D10/D14）", code, blocked_by)
        return data
    # EARLY_EXCLUDE_ENABLED=False（默认）：不早退，所有品种均取全量期权数据，便于横向比较淘汰程度

    # ---- D09/D10 权利金（算 R/W/max_loss/margin）+ D14 OI（腿级）----
    # 盘后/假日：合并一次 get_history_n(position+close) 同取 OI 与收盘权利金，省一次 60s 令牌；
    # 盘中：各自独立取数（premium=tick + OI=get_history_n，API 不同不可合并，函数内自动回落）。
    legs, roles = selection.contracts(), selection.roles()
    data.premium, data.oi = bundle.premium_oi(legs, today, roles, market_open)
    multiplier = _multiplier_of(d11, code)
    econ = compute_economics(selection, data.premium, multiplier=multiplier,
                             d04=d04, d05=d05, d11=d11, code=code, exchange=exchange, th=th)
    data.net_credit, data.spread_width = econ.net_credit, econ.spread_width
    data.max_loss, data.margin_single = econ.max_loss, econ.margin

    # ---- 保护腿护栏【检测·不淘汰】：标记异常偏高保护腿（下游 run_opening_checks 据其把 #8/#9 降级待人工·放行到人工）----
    note = _detect_prot_anomaly(selection, data.premium, th=th)
    if note:
        data.guard = note
        logger.warning("assemble %s：%s", code, note)

    logger.info("assemble %s：取数装配完成 · R净权利金=%s · 价差宽=%s · 最大亏损=%s · 保证金=%s · 乘数=%s%s",
                code, econ.net_credit, econ.spread_width, econ.max_loss, econ.margin, multiplier,
                f" · {data.guard}" if data.guard else "")
    return data


def make_assemble(th: ThresholdSet, bundle: FetchBundle | None = None, *,
                  structure: str | None = None, today: dt.date | None = None) -> "AssembleFn":
    """工厂：把阈值 / 取数 bundle / 结构覆盖 / 基准日固化成 runner 契约的 `AssembleFn`＝(code, session)->OpeningData。

    bundle=None → `default_bundle()`（真实取数）。
    structure="dual"（命令 --dual）→ 全池强制评估铁鹰；None → 用 #3 结构建议。"""
    bundle = bundle or default_bundle()

    def _assemble(code: str, session: "MarketSession") -> OpeningData:
        return assemble_candidate(code, session, th, bundle, today=today, structure_override=structure)

    return _assemble


# ---------------------------------------------------------------------------
# 实测：展示选腿装配配置 + 数据层依赖状态
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.thresholds import load_thresholds

    th = load_thresholds()
    print("=" * 72)
    print("strategy.assemble · 选腿装配配置")
    print(f"  OTM 展示比例：{th.otm_pct:.0%}（仅展示字段·选腿已改近端候选集）")
    print(f"  DTE 范围：{th.dte_min}–{th.dte_max} 天")
    print(f"  候选月数：{th.candidate_months}")
    print(f"  结构：single_put / single_call / iron_condor")
    print(f"\n  数据层依赖：")
    print(f"    D01 品种列表 · D02 合约模板 · D03 指数符号 · D04 保证金率 · D05 组合优惠")
    print(f"    D06 期权链 · D07 候选月 · D08 标的现价 · D09/D10 权利金 · D11 合约乘数")
    print(f"    D12 指数日线 · D13 IV 快照 · D14 合约 OI · D15 事件日历 · D16 持仓")
    print(f"    D17 资金池 · D18 预算档")
    print(f"\n  装配流程：探时段 → 取数 → 选腿 → 算经济性 → 跑 checks → 产出 OpeningData")
    print(f"  运行时由 runner.py 编排，本模块提供 make_assemble() 装配函数。")
    print("=" * 72)
