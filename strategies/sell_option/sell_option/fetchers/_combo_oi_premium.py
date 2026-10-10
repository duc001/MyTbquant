"""盘后合并取数：对选中腿一次性 get_history_n(fields=['position','close','time']) 同时取 OI 与收盘权利金。

仅盘后/假日（非交易时段）走此合并路径：d09/d10 权利金走日线 close、d14 OI 走 position，
二者同源 get_history_n、同 symbol 集合（选腿后的卖腿/保护腿）、同 count=1，
合并后每批只消耗 1 次 60s 令牌（原 d09/d10 与 d14 各消耗 1 次 → 盘后扫描期权取数调用数减半）。

盘中 premium 走 tick_book、OI 走 get_history_n，API 不同不可合并 → 自动回落到各自独立取数
（fetch_premium / fetch_oi，行为不变）。

设计要点（对齐既有契约）：
- 收盘权利金 close 写入 premium.db 缓存（与 fetch_premium 同库同口径，幂等），盘后重跑秒回。
- OI（position）：盘后合并取数由 combo_oi_close_batch 一并写入 premium.db（oi 列），仅用于
  「close+oi 均已缓存则秒回」节省 60s 令牌（见 d09 L386-389）；但 OiSet 的 OI 值始终以本次实时取回的
  oi_map 为准（不读缓存 OI 值），与 d14「OI 盘中持续变化、不缓存读值」的设计一致。
"""
from __future__ import annotations

import datetime as dt

try:  # 包方式（python -m / import）：相对导入
    from .d09_d10_premium import (
        PremiumSet, PremiumQuote, PremiumCacheRepo, TbpyPremiumSource, fetch_premium)
    from .d14_contract_oi import OiSet, OiQuote, fetch_oi
    from .models import Verdict, to_date as _as_date
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    from sell_option.fetchers.d09_d10_premium import (
        PremiumSet, PremiumQuote, PremiumCacheRepo, TbpyPremiumSource, fetch_premium)
    from sell_option.fetchers.d14_contract_oi import OiSet, OiQuote, fetch_oi
    from sell_option.fetchers.models import Verdict, to_date as _as_date

try:  # 取数归属交易日共享工具（按 D15 日历回退到最近交易日）
    from ._trading_day import pull_date_for
except ImportError:
    from sell_option.fetchers._trading_day import pull_date_for

try:  # 统一日志
    from ..logutil import get_logger
except ImportError:
    from sell_option.logutil import get_logger

logger = get_logger(__name__)


# _as_date 已统一到 models.to_date（见 sell_option/fetchers/models.py），此处不再重复定义。


def fetch_premium_and_oi(contracts, today: dt.date, *, roles=None,
                         market_open=None, verbose: bool = False):
    """盘后合并取 OI + 收盘权利金；盘中回落独立取数。返回 (PremiumSet, OiSet)。

    contracts：ChainContract 列表（选腿后的卖腿/保护腿，与 d09/d10、d14 同源 symbol 集合）。
    roles：{symbol: "short"|"protection"}（仅透传标注）。
    market_open：时段标志；None=自探。
    """
    roles = roles or {}
    sym_meta: dict[str, dict] = {}
    for c in contracts:
        sym = getattr(c, "symbol", None) or str(c)
        sym_meta[sym] = {
            "code": getattr(c, "code", ""),
            "underlying_month": getattr(c, "underlying_month", ""),
            "cp": getattr(c, "cp", ""),
            "strike": getattr(c, "strike", 0.0),
        }

    result_p = PremiumSet()
    result_o = OiSet()

    # 先查 premium 缓存（close）——按归属交易日严格查（避免模糊回退）
    repo = PremiumCacheRepo()
    pull_date = pull_date_for(today)
    cached: dict[str, dict] = {}
    for sym in sym_meta:
        rec = repo.load(sym, pull_date)
        if rec is not None:
            cached[sym] = rec

    # 判时段：盘中 → 各自独立取数（不可合并）
    src = TbpyPremiumSource(market_open=market_open)
    try:
        if src.is_trading():
            if verbose:
                print("[combo] 盘中：premium=tick + OI=get_history_n 各自独立取数（不可合并）")
            logger.info("[combo] 盘中：premium=tick + OI=get_history_n 各自独立取数（不可合并）")
            return (fetch_premium(contracts, today, roles=roles, market_open=True, verbose=verbose),
                    fetch_oi(contracts, today, roles=roles, market_open=True, verbose=verbose))

        # 盘后：合并一次取 OI+close（覆盖全量 symbol；当日 close+OI 已缓存则直接走缓存不占令牌，
        # 缺失则 get_history_n(position+close+time) 取回并写入缓存）
        oi_map: dict[str, tuple[int | None, str | None]] = {}
        if sym_meta:
            if verbose:
                print(f"[combo] 盘后合并取数：{len(sym_meta)} 合约 → "
                      f"get_history_n(position+close+time)")
            logger.info("[combo] 盘后合并取数：%d 合约 → get_history_n(position+close+time) · "
                        "已命中 premium.db 缓存=%d（不占 60s 令牌）", len(sym_meta), len(cached))
            combo = src.combo_oi_close_batch(list(sym_meta.keys()), today=today)
            for sym, (close_val, pos_val, bar_date_val) in combo.items():
                bar_date_date = _as_date(bar_date_val)
                bar_date_str = bar_date_date.isoformat() if bar_date_date else None
                if sym not in cached:
                    # close 已由 combo_oi_close_batch 写入 premium.db（含 oi 列，见 d09 L419），
                    # 此处仅维护内存 cached 供下方 PremiumSet 组装；切勿再 repo.save，否则会把 oi 覆盖成 NULL、
                    # 进而让 d09 combo_oi_close_batch 的「close+oi 均已缓存则秒回」优化失效（白耗 60s 令牌）。
                    cached[sym] = {"close": close_val, "source": "daily_close", "bar_date": bar_date_str}
                oi_map[sym] = (pos_val, bar_date_str)

        # 组装 PremiumSet（close 路径，完全对齐 fetch_premium 盘后逻辑）
        for sym, meta in sym_meta.items():
            rec = cached.get(sym)
            role = roles.get(sym)
            if rec and rec.get("close") is not None:
                bar_date_val = _as_date(rec.get("bar_date"))   # 对旧缓存 '...T00:00:00' 容错
                result_p.quotes[sym] = PremiumQuote(
                    symbol=sym, code=meta["code"], underlying_month=meta["underlying_month"],
                    cp=meta["cp"], strike=meta["strike"], role=role,
                    bid=None, ask=None, last=None, mid=None, spread=None, spread_pct=None,
                    close=rec["close"], premium=rec["close"],
                    source="daily_close", bar_date=bar_date_val, has_book=False,
                    state=Verdict.PASS, reason=f"daily_close @ {bar_date_val}")
            else:
                result_p.quotes[sym] = PremiumQuote(
                    symbol=sym, code=meta["code"], underlying_month=meta["underlying_month"],
                    cp=meta["cp"], strike=meta["strike"], role=role,
                    bid=None, ask=None, last=None, mid=None, spread=None, spread_pct=None,
                    close=None, premium=None, source="none", bar_date=None, has_book=False,
                    state=Verdict.MISSING, reason=f"{sym} 两路皆无")

        # 组装 OiSet（position 路径；不缓存，对齐 d14 设计——OI 每次实时取）
        for sym, meta in sym_meta.items():
            role = roles.get(sym)
            info = oi_map.get(sym)
            if info and info[0] is not None:
                pos_val = info[0]
                result_o.quotes[sym] = OiQuote(
                    symbol=sym, code=meta["code"], underlying_month=meta["underlying_month"],
                    cp=meta["cp"], strike=meta["strike"], role=role,
                    oi=pos_val, source="tbpy_hist", bar_date=_as_date(info[1]) if (info and info[1]) else pull_date,
                    state=Verdict.PASS,
                    reason=f"tbpy_hist OI={pos_val}")
            else:
                result_o.quotes[sym] = OiQuote(
                    symbol=sym, code=meta["code"], underlying_month=meta["underlying_month"],
                    cp=meta["cp"], strike=meta["strike"], role=role,
                    oi=None, source="none", bar_date=None, state=Verdict.MISSING,
                    reason="tbpy 拿不到 OI")
    finally:
        src.close()

    logger.info("D09/D10+D14 合并取数完成 · 合约=%d · 权利金 报价=%d 分布=%s · "
                "OI 报价=%d 分布=%s · 命中缓存=%d",
                len(sym_meta), len(result_p), result_p.distribution(),
                len(result_o), result_o.distribution(), len(cached))
    return result_p, result_o
