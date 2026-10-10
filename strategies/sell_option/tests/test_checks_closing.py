"""test_checks_closing.py — X1–X4 平仓检查 + x_pnl 估值底座单元测试。"""
import datetime as dt
import pytest

from sell_option.fetchers.models import Verdict
from sell_option.fetchers.d09_d10_premium import PremiumQuote, PremiumSet
from sell_option.fetchers.d16_positions import Position

from tests.conftest import make_leg, make_position, make_spot


# ===========================================================================
# x_pnl · close_pnl 平仓估值底座
# ===========================================================================
def _pq(symbol: str, premium_val: float) -> PremiumQuote:
    return PremiumQuote(symbol=symbol, code="m", underlying_month="2611", cp="P", strike=3000.0,
                        role=None, bid=None, ask=None, last=premium_val, mid=None, spread=None,
                        spread_pct=None, close=premium_val, premium=premium_val,                         source="daily_close",
                        bar_date=None, has_book=False, state=Verdict.PASS, reason="")


class TestClosePnl:
    """close_pnl：平回成本 / 浮动盈亏 / 不可估值降级。"""

    def test_spread_profit(self):
        from sell_option.checks.x_pnl import close_pnl
        pos = make_position(
            short_leg=make_leg("m2611-P-3000", 3000.0, 60.0),
            protection_leg=make_leg("m2611-P-2900", 2900.0, 20.0),
        )
        prem = PremiumSet(quotes={
            "m2611-P-3000": _pq("m2611-P-3000", 25.0),
            "m2611-P-2900": _pq("m2611-P-2900", 5.0),
        })
        pnl = close_pnl(pos, prem)
        # R=40, close_cost=25-5=20, profit=20, ratio=0.5
        assert pnl.valued
        assert pnl.net_credit == 40.0
        assert pnl.close_cost == 20.0
        assert pnl.profit == 20.0
        assert abs(pnl.profit_ratio - 0.5) < 1e-9
        assert pnl.loss == -20.0  # 浮盈 → loss 为负

    def test_spread_loss(self):
        from sell_option.checks.x_pnl import close_pnl
        pos = make_position(
            short_leg=make_leg("m2611-P-3000", 3000.0, 60.0),
            protection_leg=make_leg("m2611-P-2900", 2900.0, 20.0),
        )
        prem = PremiumSet(quotes={
            "m2611-P-3000": _pq("m2611-P-3000", 110.0),
            "m2611-P-2900": _pq("m2611-P-2900", 10.0),
        })
        pnl = close_pnl(pos, prem)
        # close_cost=100, profit=40-100=-60, loss=60
        assert pnl.valued
        assert pnl.close_cost == 100.0
        assert pnl.profit == -60.0
        assert pnl.loss == 60.0
        assert pnl.profit_ratio == -1.5

    def test_single_leg(self):
        from sell_option.checks.x_pnl import close_pnl
        pos = Position(entry_date=dt.date(2026, 9, 1), code="rb", exchange="SHFE",
                       structure="single_call",
                       short_leg=make_leg("rb2610-C-3100", 3100.0, 40.0),
                       protection_leg=None, max_loss=4000.0, expiry_date=dt.date(2026, 10, 1))
        prem = PremiumSet(quotes={"rb2610-C-3100": _pq("rb2610-C-3100", 20.0)})
        pnl = close_pnl(pos, prem)
        # R=40, close_cost=20, profit=20, ratio=0.5
        assert pnl.valued
        assert pnl.net_credit == 40.0
        assert pnl.close_cost == 20.0
        assert pnl.profit_ratio == 0.5

    def test_missing_no_position(self):
        from sell_option.checks.x_pnl import close_pnl
        prem = PremiumSet(quotes={"x": _pq("x", 20.0)})
        pnl = close_pnl(None, prem)
        assert not pnl.valued
        assert "持仓缺失" in pnl.reason

    def test_missing_no_premium(self):
        from sell_option.checks.x_pnl import close_pnl
        pos = make_position()
        pnl = close_pnl(pos, None)
        assert not pnl.valued

    def test_missing_no_short_premium_value(self):
        from sell_option.checks.x_pnl import close_pnl
        pos = make_position(short_leg=make_leg("m2611-P-3000", 3000.0, None))
        prem = PremiumSet(quotes={"m2611-P-3000": _pq("m2611-P-3000", 25.0)})
        pnl = close_pnl(pos, prem)
        assert not pnl.valued
        assert "R" in pnl.reason

    def test_missing_protection_leg_value(self):
        from sell_option.checks.x_pnl import close_pnl
        pos = make_position(
            short_leg=make_leg("m2611-P-3000", 3000.0, 60.0),
            protection_leg=make_leg("m2611-P-2900", 2900.0, 20.0),
        )
        # 只有卖腿现值，缺保护腿现值
        prem = PremiumSet(quotes={"m2611-P-3000": _pq("m2611-P-3000", 25.0)})
        pnl = close_pnl(pos, prem)
        assert not pnl.valued
        assert "保护腿" in pnl.reason

    def test_r_zero_or_negative(self):
        from sell_option.checks.x_pnl import close_pnl
        # R=0（卖腿 premium=保护腿 premium）→ 不可估值
        pos = make_position(
            short_leg=make_leg("m2611-P-3000", 3000.0, 20.0),
            protection_leg=make_leg("m2611-P-2900", 2900.0, 20.0),
        )
        prem = PremiumSet(quotes={
            "m2611-P-3000": _pq("m2611-P-3000", 25.0),
            "m2611-P-2900": _pq("m2611-P-2900", 5.0),
        })
        pnl = close_pnl(pos, prem)
        assert not pnl.valued
        assert "≤ 0" in pnl.reason


# ===========================================================================
# X3 · x3_breach 破位（保垫侵蚀口径：未越 K 先平，档线在虚值侧）
# ===========================================================================
class TestX3Breach:
    """X3 破位：卖 PUT 线=K×(1+bp)、卖 CALL 线=K×(1−bp)；越线即 FAIL 平仓（K 未被穿）。"""

    def test_put_breached_before_strike(self, th):
        # 卖 PUT K=3000，M 档 5% → 安全垫线 3150；现价 3100 仍在 K 上方但已跌破线 → 平仓
        from sell_option.checks import x3_breach
        pos = make_position(code="m", structure="single_put",
                            short_leg=make_leg("m2611-P-3000", 3000.0, 60.0))
        r = x3_breach.evaluate(pos, make_spot(3100.0, code="m"), th)
        assert r.verdict == Verdict.FAIL
        assert r.detail["disposition"] == "破位"
        assert r.detail["threshold"] == pytest.approx(3150.0)

    def test_put_holding_above_line(self, th):
        from sell_option.checks import x3_breach
        pos = make_position(code="m", structure="single_put",
                            short_leg=make_leg("m2611-P-3000", 3000.0, 60.0))
        r = x3_breach.evaluate(pos, make_spot(3200.0, code="m"), th)
        assert r.verdict == Verdict.PASS
        assert r.detail["disposition"] == "续持"

    def test_call_breached_before_strike(self, th):
        # 卖 CALL K=3400，default 档 7% → 线=3162；现价 3170 已涨破线（≥3162）→ FAIL，但尚未触及 K
        from sell_option.checks import x3_breach
        pos = make_position(code="AP", exchange="CZCE", structure="single_call",
                            short_leg=make_leg("AP2611-C-3400", 3400.0, 60.0))
        r = x3_breach.evaluate(pos, make_spot(3170.0, code="AP"), th)
        assert r.verdict == Verdict.FAIL
        assert r.detail["threshold"] == pytest.approx(3162.0)

    def test_call_holding_below_line(self, th):
        from sell_option.checks import x3_breach
        pos = make_position(code="AP", exchange="CZCE", structure="single_call",
                            short_leg=make_leg("AP2611-C-3400", 3400.0, 60.0))
        r = x3_breach.evaluate(pos, make_spot(3000.0, code="AP"), th)
        assert r.verdict == Verdict.PASS

    def test_iron_condor_row_is_missing(self, th):
        # 铁鹰应拆两笔单腿录入；直接出 iron_condor 行 → MISSING 不猜
        from sell_option.checks import x3_breach
        pos = make_position(code="m", structure="iron_condor",
                            short_leg=make_leg("m2611-P-3000", 3000.0, 60.0))
        r = x3_breach.evaluate(pos, make_spot(3100.0, code="m"), th)
        assert r.verdict == Verdict.MISSING

    def test_no_spot_missing(self, th):
        from sell_option.checks import x3_breach
        pos = make_position(code="m", structure="single_put",
                            short_leg=make_leg("m2611-P-3000", 3000.0, 60.0))
        r = x3_breach.evaluate(pos, None, th)
        assert r.verdict == Verdict.MISSING
        assert "主力月价冒充" in r.reason
