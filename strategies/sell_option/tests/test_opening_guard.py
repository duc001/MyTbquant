"""护栏「放行到人工」单测（零 tbpy，纯逻辑）。

覆盖本轮两大新逻辑（Qoder 报告观察项点名缺正式测试）：
1) _detect_prot_anomaly：只标记异常、不阻挡；
2) compute_economics：对脏腿诚实算 R（不返回 None、不拦截）；
3) 护栏命中 → run_opening_checks 把 #8/#9 腿经济量检查降级为 MISSING（待人工），
   品种**不据脏数据被机器淘汰**；
4) 漏斗 guard_anomaly：仅登记告警、不计入淘汰。

源脚本：根目录 _test_guard.py（已归档进 tests/）。
"""
import pytest

from sell_option.strategy.assemble import (
    LegSelection, SpreadLegs, _detect_prot_anomaly, compute_economics,
)
from sell_option.strategy.opening import OpeningData, run_opening_checks
from sell_option.strategy.funnel import build_funnel_stats
from sell_option.fetchers.d04_margin_rate import fetch_d04
from sell_option.fetchers.d05_combo_discount import fetch_d05
from sell_option.fetchers.d11_contract_multiplier import fetch_d11
from sell_option.thresholds import load_thresholds
from sell_option.checks.models import Verdict


class _C:
    def __init__(self, sym):
        self.symbol = sym


class _PS:
    def __init__(self, m):
        self.m = m

    def premium_of(self, s):
        return self.m.get(s)


def _mk(p_short_sym, p_prot_sym, w):
    return LegSelection(code="X", month4="2701", structure="single_put",
                        spreads=(SpreadLegs(side="put", short=_C(p_short_sym), protection=_C(p_prot_sym),
                                            short_strike=100.0, prot_strike=100.0 - w, width=w),),
                        spot=100.0)


@pytest.fixture(scope="module")
def env():
    th = load_thresholds()
    d04, d05, d11 = fetch_d04(), fetch_d05(), fetch_d11()
    mult = 10.0
    return th, d04, d05, d11, mult


class TestProtAnomalyDetect:
    """_detect_prot_anomaly：只标记异常、不阻挡。"""

    def test_anomaly_flagged(self, env):
        th, *_ = env
        sel = _mk("s", "p", 100)
        note = _detect_prot_anomaly(sel, _PS({"s": 50.0, "p": 48.0}), th=th)
        assert note is not None and note.startswith("护栏观测(不阻挡)"), "A 应标记观测告警"

    def test_normal_no_flag(self, env):
        th, *_ = env
        sel = _mk("s", "p", 100)
        assert _detect_prot_anomaly(sel, _PS({"s": 50.0, "p": 30.0}), th=th) is None, "B 应无告警"


class TestComputeEconomicsHonest:
    """compute_economics：对脏腿诚实算 R（不返回 None、不拦截）。"""

    def test_honest_with_dirty_leg(self, env):
        th, d04, d05, d11, mult = env
        sel = _mk("s", "p", 100)
        econ = compute_economics(sel, _PS({"s": 50.0, "p": 48.0}), multiplier=mult,
                                 d04=d04, d05=d05, d11=d11, code="X", exchange="CZCE", th=th)
        assert econ.net_credit is not None and econ.net_credit == 2.0, "C 应算得 R=2（不拦截）"

    def test_missing_protection_is_none(self, env):
        th, d04, d05, d11, mult = env
        sel = _mk("s", "p", 100)
        econ = compute_economics(sel, _PS({"s": 50.0}), multiplier=mult,
                                 d04=d04, d05=d05, d11=d11, code="X", exchange="CZCE", th=th)
        assert econ.net_credit is None, "D 缺数应诚实判 None（非护栏拦截）"


class TestFunnelGuardAnomaly:
    """漏斗 guard_anomaly：仅登记告警、不计入淘汰。"""

    def test_registers_anomaly(self):
        class _Run:
            def __init__(self, code, data):
                self.code, self.data = code, data

        class _Data:
            def __init__(self, guard=None):
                self.guard = guard

        st = build_funnel_stats([
            _Run("PX", _Data("护栏观测(不阻挡): put:保护腿48.0≥短腿50.0×0.9")),
            _Run("AP", _Data(None)),
        ])
        assert st.guard_anomaly == ["PX"], "E 应登记观测告警"


class TestGuardPassThroughToHuman:
    """护栏命中 → run_opening_checks 把 #8/#9 降级为 MISSING（待人工），品种不据脏数据淘汰。"""

    def test_guard_downgrades_eight_nine(self, env):
        th, *_ = env
        data = OpeningData()
        data.net_credit = 5.0          # 脏腿压出的「瘦」R/W（5%），若据其判会 FAIL 淘汰
        data.spread_width = 100.0
        data.guard = "护栏观测(不阻挡): put:保护腿48.0≥短腿50.0×0.9"
        results = run_opening_checks(data, th, market_open=True)
        c8 = next((r for r in results if r.rule_id == "#8"), None)
        c9 = next((r for r in results if r.rule_id == "#9"), None)
        assert c9 is not None and c9.verdict == Verdict.MISSING, "F #9 应降级为 MISSING（待人工）"
        assert "护栏放行" in c9.reason, "F #9 reason 应含放行标记"
        assert c8 is None or c8.verdict == Verdict.MISSING, "F #8 应降级为 MISSING（待人工）"

    def test_no_guard_still_fails(self, env):
        th, *_ = env
        data = OpeningData()
        data.net_credit = 5.0
        data.spread_width = 100.0
        data.guard = None
        results = run_opening_checks(data, th, market_open=True)
        c9 = next((r for r in results if r.rule_id == "#9"), None)
        assert c9 is not None and c9.verdict == Verdict.FAIL, "G 无护栏时 #9 应正常 FAIL（真瘦才淘汰）"
