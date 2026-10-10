"""tests 共享 fixtures：构造 PremiumQuote / PremiumSet / SpotQuote / IvVerdict / IndexSeries / ChainContract 等。

单元测试用 mock 数据构造器——与业务代码 __main__ 段（只连真实数据源）严格分离。"""
from __future__ import annotations
import sys
from pathlib import Path
_HERE = Path(__file__).resolve().parent
if (_HERE / 'sell_option' / '__init__.py').exists():
    _UNIT = _HERE
else:
    _UNIT = _HERE.parent
_STRATEGIES = _UNIT.parent
_ROOT = _STRATEGIES.parent
for _p in (str(_STRATEGIES), str(_UNIT), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
import sys

import pytest

# 确保项目根在 sys.path（pytest 从 tests/ 跑时）

from sell_option.fetchers.models import Verdict, ChainContract
from sell_option.fetchers.d09_d10_premium import PremiumQuote, PremiumSet
from sell_option.fetchers.d08_spot_price import SpotQuote
from sell_option.fetchers.d13_iv_snapshot import IvVerdict
from sell_option.fetchers.d12_index_daily import IndexSeries
from sell_option.fetchers.d16_positions import Leg, Position
from sell_option.thresholds import ThresholdSet, load_thresholds


# ---------------------------------------------------------------------------
# PremiumQuote / PremiumSet 构造器
# ---------------------------------------------------------------------------
def make_pq(symbol: str = "m2611-C-3400.DCE", role: str | None = "short",
            bid: float | None = 100.0, ask: float | None = 102.0,
            last: float | None = 101.0, code: str = "m", month: str = "2611",
            cp: str = "C", strike: float = 3400.0) -> PremiumQuote:
    """构造 PremiumQuote（has_book / mid / spread / premium 自洽）。"""
    has_book = bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid
    mid = (bid + ask) / 2.0 if has_book else None
    spread_val = (ask - bid) if has_book else None
    premium_val = last if last is not None else mid
    return PremiumQuote(
        symbol=symbol, code=code, underlying_month=month, cp=cp, strike=strike, role=role,
        bid=bid, ask=ask, last=last, mid=mid, spread=spread_val,
        spread_pct=(spread_val / mid if (spread_val is not None and mid) else None),
        close=None if has_book else last, premium=premium_val,
        source="tick" if has_book else "daily_close", bar_date=None, has_book=has_book,
        state=Verdict.PASS, reason="")


def make_ps(*quotes: PremiumQuote) -> PremiumSet:
    return PremiumSet(quotes={q.symbol: q for q in quotes})


# ---------------------------------------------------------------------------
# SpotQuote 构造器
# ---------------------------------------------------------------------------
def make_spot(price: float | None = 3000.0, code: str = "m", month4: str = "2611",
              state: Verdict = Verdict.PASS) -> SpotQuote:
    return SpotQuote(code=code, month4=month4, futures_symbol=f"{code}{month4}.DCE",
                     price=price, source="tick" if price is not None else "none",
                     bar_date=None, state=state, reason="")


# ---------------------------------------------------------------------------
# IvVerdict 构造器
# ---------------------------------------------------------------------------
def make_iv(atmv_current: float | None = 18.0, atmv_percentile: float | None = 75.0,
            atmv_1dchg: float | None = -0.5, code: str = "m",
            structure: str = "single") -> IvVerdict:
    direction = "falling" if (atmv_1dchg is not None and atmv_1dchg < 0) else "rising"
    return IvVerdict(code=code, structure=structure, exp="2611", atmv_current=atmv_current,
                     atmv_percentile=atmv_percentile, atmv_1dchg=atmv_1dchg,
                     direction=direction, dir_basis="1_day")


# ---------------------------------------------------------------------------
# IndexSeries 构造器
# ---------------------------------------------------------------------------
def make_index_series(closes: list[float], code: str = "m") -> IndexSeries:
    """由 close 序列构造 IndexSeries（high/low 各外扩 0.4%；state=PASS）。"""
    high = tuple(c * 1.004 for c in closes)
    low = tuple(c * 0.996 for c in closes)
    op = tuple(closes)
    cl = tuple(closes)
    vol = tuple(1000.0 for _ in closes)
    dates = tuple(dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(len(closes)))
    return IndexSeries(code=code, index_symbol=f"{code}9888.DCE", dates=dates,
                       open=op, high=high, low=low, close=cl, volume=vol,
                       source="tbpy_rbw", bar_date=dates[-1] if dates else None,
                       state=Verdict.PASS, reason="test")


# ---------------------------------------------------------------------------
# ChainContract 构造器（选腿测试用）
# ---------------------------------------------------------------------------
def make_contract(strike: float, cp: str = "P", code: str = "m",
                  exchange: str = "DCE", month: str = "2611") -> ChainContract:
    sym = f"{code}{month}-{cp}-{int(strike)}"
    return ChainContract(code=code, exchange=exchange, underlying_month=month,
                         expiry=dt.date(2026, 11, 20), cp=cp, strike=strike, symbol=sym)


# ---------------------------------------------------------------------------
# Position / Leg 构造器（平仓/仓位测试用）
# ---------------------------------------------------------------------------
def make_leg(symbol: str = "m2611-P-3000", strike: float = 3000.0,
             premium: float | None = 60.0) -> Leg:
    return Leg(symbol=symbol, strike=strike, premium=premium)


def make_position(code: str = "m", exchange: str = "DCE",
                  structure: str = "single_put",
                  short_leg: Leg | None = None,
                  protection_leg: Leg | None = None,
                  max_loss: float = 100.0, expiry_date: dt.date | None = None,
                  entry_date: dt.date | None = None) -> Position:
    if short_leg is None:
        short_leg = make_leg()
    if entry_date is None:
        entry_date = dt.date(2026, 9, 1)
    if expiry_date is None:
        expiry_date = dt.date(2026, 10, 11)      # 默认＝entry+40 天（DTE 由它现算、不入库）
    return Position(entry_date=entry_date, code=code, exchange=exchange, structure=structure,
                    short_leg=short_leg, protection_leg=protection_leg,
                    max_loss=max_loss, expiry_date=expiry_date)


# ---------------------------------------------------------------------------
# ThresholdSet 便捷 fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def th():
    """默认阈值集。"""
    return load_thresholds()
