"""test_checks_opening.py — #1–#10 开仓检查纯函数单元测试。

测试覆盖：c1_spread(#1)、c3_regime(#3)、c4_iv(#4)、c5_safety(#5)、c8_budget(#8)、c9_fatthin(#9)。
各 evaluate() 为纯函数，输入 mock 数据层产物 + ThresholdSet → 验证 CheckResult 四态/detail。"""
import math

from sell_option.fetchers.models import Verdict
from sell_option.checks.models import CheckResult
from sell_option.thresholds import ThresholdSet, load_thresholds

from tests.conftest import make_pq, make_ps, make_spot, make_iv, make_index_series


# ===========================================================================
# #1 盘口价差（c1_spread）
# ===========================================================================
class TestC1Spread:
    """#1 盘口价差：买卖价差比 ≤ 权利金×10%。"""

    def test_pass_narrow_spread(self, th):
        from sell_option.checks.c1_spread import evaluate
        ps = make_ps(
            make_pq("S.DCE", "short", 100.0, 102.0, 101.0),
            make_pq("P.DCE", "protection", 20.0, 21.0, 20.5),
        )
        r = evaluate(ps, th)
        assert r.verdict == Verdict.PASS
        assert r.rule_id == "#1"
        assert r.blocking is True

    def test_fail_wide_spread(self, th):
        from sell_option.checks.c1_spread import evaluate
        # 卖腿 bid=100 ask=120 → spread=20, ref=110, ratio≈18.2% > 10%
        ps = make_ps(
            make_pq("S.DCE", "short", 100.0, 120.0, 110.0),
            make_pq("P.DCE", "protection", 20.0, 21.0, 20.5),
        )
        r = evaluate(ps, th)
        assert r.verdict == Verdict.FAIL

    def test_boundary_exact_10pct(self, th):
        from sell_option.checks.c1_spread import evaluate
        # bid=100 ask=110 last=100 → spread=10, ratio=10/100=10% → PASS（≤含等号）
        ps = make_ps(make_pq("e.DCE", "short", 100.0, 110.0, 100.0))
        r = evaluate(ps, th)
        assert r.verdict == Verdict.PASS

    def test_missing_no_book(self, th):
        from sell_option.checks.c1_spread import evaluate
        # 盘后无 bid/ask → MISSING
        ps = make_ps(make_pq("S.DCE", "short", None, None, 101.0))
        r = evaluate(ps, th)
        assert r.verdict == Verdict.MISSING

    def test_empty_premium_set(self, th):
        from sell_option.checks.c1_spread import evaluate
        from sell_option.fetchers.d09_d10_premium import PremiumSet
        assert evaluate(PremiumSet(quotes={}), th).verdict == Verdict.MISSING
        assert evaluate(None, th).verdict == Verdict.MISSING

    def test_merge_priority_fail_over_missing(self, th):
        from sell_option.checks.c1_spread import evaluate
        # 卖腿 FAIL + 保护腿 MISSING → 合并 FAIL
        ps = make_ps(
            make_pq("S.DCE", "short", 100.0, 120.0, 110.0),
            make_pq("P.DCE", "protection", None, None, 20.5),
        )
        r = evaluate(ps, th)
        assert r.verdict == Verdict.FAIL

    def test_threshold_injection(self):
        from sell_option.checks.c1_spread import evaluate
        th_strict = load_thresholds(spread_ratio_max=0.05)
        # ratio=10% > 5% → FAIL
        ps = make_ps(make_pq("e.DCE", "short", 100.0, 110.0, 100.0))
        r = evaluate(ps, th_strict)
        assert r.verdict == Verdict.FAIL

    def test_fallback_to_mid(self, th):
        from sell_option.checks.c1_spread import evaluate
        # last=None → 基准回退 mid=(100+110)/2=105, spread=10, ratio=10/105≈9.5% → PASS
        ps = make_ps(make_pq("e.DCE", "short", 100.0, 110.0, None))
        r = evaluate(ps, th)
        assert r.verdict == Verdict.PASS
        assert abs(r.detail["legs"][0]["spread"]["ratio"] - 10 / 105) < 1e-9


# ===========================================================================
# #3 市况 regime（c3_regime）
# ===========================================================================
class TestC3Regime:
    """#3 市况：ADX+20日斜率 → 震荡/趋势。永不 FAIL。"""

    def test_missing_no_series(self, th):
        from sell_option.checks.c3_regime import evaluate
        r = evaluate(None, th)
        assert r.verdict == Verdict.MISSING

    def test_missing_insufficient_bars(self, th):
        from sell_option.checks.c3_regime import evaluate
        # 只给 10 根 bar（需 ≥28=2×14）
        series = make_index_series([100.0 + i * 0.1 for i in range(10)])
        r = evaluate(series, th)
        assert r.verdict == Verdict.MISSING

    def test_ranging(self, th):
        from sell_option.checks.c3_regime import evaluate, STRUCTURE_MAP
        # 震荡＝ADX<25 且 |斜率|<0.15%/日 → 构造小幅震荡序列（有波动但无趋势）
        closes = [100.0 + 2.0 * math.sin(i * 0.5) for i in range(60)]  # 正弦震荡，无趋势
        series = make_index_series(closes)
        r = evaluate(series, th)
        # 正弦震荡 → ADX 低 + 斜率≈0 → ranging
        assert r.verdict == Verdict.PASS
        assert r.detail["regime"] == "ranging"
        assert r.detail["structure"] == STRUCTURE_MAP["ranging"]  # iron_condor

    def test_uptrend(self, th):
        from sell_option.checks.c3_regime import evaluate, STRUCTURE_MAP
        # 强上涨序列 → 高 ADX + 正斜率
        closes = [100.0 + i * 2.0 for i in range(60)]
        series = make_index_series(closes)
        r = evaluate(series, th)
        assert r.verdict == Verdict.PASS
        assert r.detail["regime"] == "uptrend"
        assert r.detail["structure"] == "single_put"

    def test_downtrend(self, th):
        from sell_option.checks.c3_regime import evaluate
        # 强下跌序列 → 高 ADX + 负斜率
        closes = [200.0 - i * 2.0 for i in range(60)]
        series = make_index_series(closes)
        r = evaluate(series, th)
        assert r.verdict == Verdict.PASS
        assert r.detail["regime"] == "downtrend"
        assert r.detail["structure"] == "single_call"

    def test_never_fail(self, th):
        """#3 永不 FAIL（BLOCKING=False）。"""
        from sell_option.checks.c3_regime import evaluate, BLOCKING
        assert BLOCKING is False


# ===========================================================================
# #4 IV 分位·方向（c4_iv）
# ===========================================================================
class TestC4Iv:
    """#4 IV 分位·方向：高位+回落→PASS；低位→FAIL。"""

    def test_pass_high_falling(self, th):
        from sell_option.checks.c4_iv import evaluate
        iv = make_iv(atmv_percentile=75.0, atmv_1dchg=-0.5)
        r = evaluate(iv, "single", th)
        assert r.verdict == Verdict.PASS

    def test_fail_low_percentile(self, th):
        from sell_option.checks.c4_iv import evaluate
        iv = make_iv(atmv_percentile=30.0, atmv_1dchg=-0.5)
        r = evaluate(iv, "single", th)
        assert r.verdict == Verdict.FAIL

    def test_warn_high_rising(self, th):
        from sell_option.checks.c4_iv import evaluate
        iv = make_iv(atmv_percentile=75.0, atmv_1dchg=0.5)
        r = evaluate(iv, "single", th)
        assert r.verdict == Verdict.WARN

    def test_missing_no_iv(self, th):
        from sell_option.checks.c4_iv import evaluate
        r = evaluate(None, "single", th)
        assert r.verdict == Verdict.MISSING

    def test_missing_no_percentile(self, th):
        from sell_option.checks.c4_iv import evaluate
        from sell_option.fetchers.d13_iv_snapshot import IvVerdict
        iv = IvVerdict(code="m", structure="single", exp="2611",
                       atmv_current=18.0, atmv_percentile=None)
        r = evaluate(iv, "single", th)
        assert r.verdict == Verdict.MISSING

    def test_dual_higher_threshold(self, th):
        from sell_option.checks.c4_iv import evaluate
        # 双卖门槛 70，分位 65 → FAIL（低于双卖门槛）
        iv = make_iv(atmv_percentile=65.0, atmv_1dchg=-0.5)
        r = evaluate(iv, "dual", th)
        assert r.verdict == Verdict.FAIL

    def test_missing_direction_unknown_when_chg_missing(self, th):
        from sell_option.checks.c4_iv import evaluate
        from sell_option.fetchers.d13_iv_snapshot import IvVerdict
        # 分位高位但 IV 日变数据缺失（atmv_1dchg=None → direction="unknown"）
        # → 属数据缺口，诚实报 MISSING，而非伪装成 ⚠ 软提示
        iv = IvVerdict(code="m", structure="single", exp="2611",
                       atmv_current=18.0, atmv_percentile=80.0,
                       atmv_1dchg=None, direction="unknown")
        r = evaluate(iv, "single", th)
        assert r.verdict == Verdict.MISSING
        assert r.reason

    def test_warn_flat_still_warn(self, th):
        from sell_option.checks.c4_iv import evaluate
        from sell_option.fetchers.d13_iv_snapshot import IvVerdict
        # flat：有日变数据（chg==0）但无明确方向 → 仍 WARN（有数据的犹豫，非数据缺口）
        iv = IvVerdict(code="m", structure="single", exp="2611",
                       atmv_current=18.0, atmv_percentile=80.0,
                       atmv_1dchg=0.0, direction="flat")
        r = evaluate(iv, "single", th)
        assert r.verdict == Verdict.WARN


# ===========================================================================
# #5 安全垫（c5_safety）
# ===========================================================================
class TestC5Safety:
    """#5 安全垫（新口径）：现价→保护腿 缓冲(|保护腿−现价|) ≥ Nσ。"""

    def test_pass_boundary(self, th):
        from sell_option.checks.c5_safety import evaluate
        # price=100, IV=20%, DTE=365 → 预期移动=100×0.20×√(365/365)=20
        # 保护腿行权价120 → 缓冲|120−100|=20 → σ=1.0 → PASS（≥含等号）
        r = evaluate(make_spot(100.0), make_iv(20.0), 365, 120.0, th)
        assert r.verdict == Verdict.PASS
        assert abs(r.detail["sigma_multiple"] - 1.0) < 1e-9

    def test_pass_wide_margin(self, th):
        from sell_option.checks.c5_safety import evaluate
        # 保护腿130 → 缓冲|130−100|=30 / 预期移动20 = 1.5σ → PASS
        r = evaluate(make_spot(100.0), make_iv(20.0), 365, 130.0, th)
        assert r.verdict == Verdict.PASS
        assert abs(r.detail["sigma_multiple"] - 1.5) < 1e-9

    def test_fail_insufficient(self, th):
        from sell_option.checks.c5_safety import evaluate
        # 保护腿110 → 缓冲10 / 20 = 0.5σ < 1.0 → FAIL
        r = evaluate(make_spot(100.0), make_iv(20.0), 365, 110.0, th)
        assert r.verdict == Verdict.FAIL
        assert r.blocking is True

    def test_fail_boundary_just_below(self, th):
        from sell_option.checks.c5_safety import evaluate
        # 缓冲19.9 / 20 → σ=0.995 < 1.0 → FAIL
        r = evaluate(make_spot(100.0), make_iv(20.0), 365, 119.9, th)
        assert r.verdict == Verdict.FAIL

    def test_dte_effect(self, th):
        from sell_option.checks.c5_safety import evaluate
        # DTE 越短 → 预期移动越小 → 越容易达标
        r60 = evaluate(make_spot(100.0), make_iv(20.0), 60, 110.0, th)
        r365 = evaluate(make_spot(100.0), make_iv(20.0), 365, 110.0, th)
        assert r60.verdict == Verdict.PASS
        assert r365.verdict == Verdict.FAIL

    def test_iv_effect(self, th):
        from sell_option.checks.c5_safety import evaluate
        # IV 越高 → 预期移动越大 → 越难达标
        r_lo = evaluate(make_spot(100.0), make_iv(10.0), 365, 115.0, th)
        r_hi = evaluate(make_spot(100.0), make_iv(40.0), 365, 115.0, th)
        assert r_lo.verdict == Verdict.PASS
        assert r_hi.verdict == Verdict.FAIL

    def test_put_below(self, th):
        from sell_option.checks.c5_safety import evaluate
        # PUT 保护腿在下方：|80-100|=20 → σ=1.0 PASS
        r = evaluate(make_spot(100.0), make_iv(20.0), 365, 80.0, th)
        assert r.verdict == Verdict.PASS

    def test_missing_no_spot(self, th):
        from sell_option.checks.c5_safety import evaluate
        assert evaluate(None, make_iv(20.0), 365, 120.0, th).verdict == Verdict.MISSING

    def test_missing_no_iv(self, th):
        from sell_option.checks.c5_safety import evaluate
        assert evaluate(make_spot(100.0), None, 365, 120.0, th).verdict == Verdict.MISSING

    def test_missing_no_dte(self, th):
        from sell_option.checks.c5_safety import evaluate
        assert evaluate(make_spot(100.0), make_iv(20.0), None, 120.0, th).verdict == Verdict.MISSING

    def test_missing_no_strike(self, th):
        from sell_option.checks.c5_safety import evaluate
        assert evaluate(make_spot(100.0), make_iv(20.0), 365, None, th).verdict == Verdict.MISSING


# ===========================================================================
# #8 封顶预算（c8_budget）
# ===========================================================================
class TestC8Budget:
    """#8 封顶预算：max_loss ≤ 预算档。"""

    def test_pass_within_budget(self):
        from sell_option.checks.c8_budget import evaluate
        from sell_option.fetchers.d18_budget import BudgetTier
        bt = BudgetTier(tier=3000.0)
        r = evaluate(max_loss=1000.0, budget_tier=bt)
        assert r.verdict == Verdict.PASS

    def test_fail_exceeds_budget(self):
        from sell_option.checks.c8_budget import evaluate
        from sell_option.fetchers.d18_budget import BudgetTier
        bt = BudgetTier(tier=3000.0)
        r = evaluate(max_loss=5000.0, budget_tier=bt)
        assert r.verdict == Verdict.FAIL

    def test_boundary_exact(self):
        from sell_option.checks.c8_budget import evaluate
        from sell_option.fetchers.d18_budget import BudgetTier
        bt = BudgetTier(tier=3000.0)
        r = evaluate(max_loss=3000.0, budget_tier=bt)
        assert r.verdict == Verdict.PASS  # ≤ 含等号

    def test_missing_no_max_loss(self):
        from sell_option.checks.c8_budget import evaluate
        from sell_option.fetchers.d18_budget import BudgetTier
        bt = BudgetTier(tier=3000.0)
        r = evaluate(max_loss=None, budget_tier=bt)
        assert r.verdict == Verdict.MISSING

    def test_missing_unconfigured(self):
        from sell_option.checks.c8_budget import evaluate
        from sell_option.fetchers.d18_budget import BudgetTier
        bt = BudgetTier(tier=None)  # 未配置
        r = evaluate(max_loss=1000.0, budget_tier=bt)
        assert r.verdict == Verdict.MISSING

    def test_missing_no_budget_tier(self):
        from sell_option.checks.c8_budget import evaluate
        r = evaluate(max_loss=1000.0, budget_tier=None)
        assert r.verdict == Verdict.MISSING


# ===========================================================================
# #9 肥瘦（c9_fatthin）
# ===========================================================================
class TestC9Fatthin:
    """#9 肥瘦：R/W ≥ 20%（rw_min，〔待校准〕）。"""

    def test_pass_fat(self, th):
        from sell_option.checks.c9_fatthin import evaluate
        # R=50, W=100 → R/W=0.50 ≥ 0.20 → PASS
        r = evaluate(net_credit=50.0, spread_width=100.0, th=th)
        assert r.verdict == Verdict.PASS
        assert r.rule_id == "#9"

    def test_fail_thin(self, th):
        from sell_option.checks.c9_fatthin import evaluate
        # R=10, W=100 → R/W=0.10 < 0.20 → FAIL
        r = evaluate(net_credit=10.0, spread_width=100.0, th=th)
        assert r.verdict == Verdict.FAIL
        assert r.blocking is True

    def test_boundary_exact_20pct(self, th):
        from sell_option.checks.c9_fatthin import evaluate
        # R=20, W=100 → R/W=0.20 → PASS（≥含等号）
        r = evaluate(net_credit=25.0, spread_width=100.0, th=th)
        assert r.verdict == Verdict.PASS

    def test_missing_no_credit(self, th):
        from sell_option.checks.c9_fatthin import evaluate
        r = evaluate(net_credit=None, spread_width=100.0, th=th)
        assert r.verdict == Verdict.MISSING

    def test_missing_no_width(self, th):
        from sell_option.checks.c9_fatthin import evaluate
        r = evaluate(net_credit=50.0, spread_width=None, th=th)
        assert r.verdict == Verdict.MISSING

    def test_threshold_injection(self):
        from sell_option.checks.c9_fatthin import evaluate
        th_strict = load_thresholds(rw_min=0.50)
        # R/W=0.40 < 0.50 → FAIL
        r = evaluate(net_credit=40.0, spread_width=100.0, th=th_strict)
        assert r.verdict == Verdict.FAIL
