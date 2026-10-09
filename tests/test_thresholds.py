"""test_thresholds.py — ThresholdSet / load_thresholds / validate 单元测试。"""
import pytest
from sell_option.thresholds import ThresholdSet, load_thresholds


class TestThresholdSet:
    """ThresholdSet 默认值与便捷方法。"""

    def test_defaults(self):
        th = ThresholdSet()
        assert th.oi_min == 100
        assert th.spread_ratio_max == 0.10
        assert th.adx_ranging == 25.0
        assert th.safety_sigma == 1.0
        assert th.dte_min == 20
        assert th.dte_max == 180
        assert th.otm_pct == 0.10
        assert th.tp_floor == 0.50
        assert th.tp_extend == 0.75
        assert th.sl_multiplier == 2.0

    def test_frozen(self):
        th = ThresholdSet()
        with pytest.raises(AttributeError):
            th.oi_min = 999

    def test_breach_pct_m(self):
        th = ThresholdSet()
        assert th.breach_pct("m") == 0.05
        assert th.breach_pct("M") == 0.05

    def test_breach_pct_sr(self):
        th = ThresholdSet()
        assert th.breach_pct("SR") == 0.05
        assert th.breach_pct("sr") == 0.05

    def test_breach_pct_default(self):
        th = ThresholdSet()
        assert th.breach_pct("AP") == 0.07
        assert th.breach_pct("rb") == 0.07
        assert th.breach_pct("") == 0.07

    def test_as_dict(self):
        th = ThresholdSet()
        d = th.as_dict()
        assert isinstance(d, dict)
        assert "oi_min" in d
        assert d["oi_min"] == 100


class TestValidate:
    """validate() 合理性自检。"""

    def test_default_no_warnings(self):
        th = ThresholdSet()
        assert th.validate() == []

    def test_dte_invalid(self):
        th = ThresholdSet(dte_min=200, dte_max=100)
        w = th.validate()
        assert any("dte_min" in x for x in w)

    def test_tp_gradient_reversed(self):
        th = ThresholdSet(tp_floor=0.80, tp_extend=0.50)
        w = th.validate()
        assert any("tp_floor" in x for x in w)

    def test_oi_min_zero(self):
        th = ThresholdSet(oi_min=0)
        w = th.validate()
        assert any("oi_min" in x for x in w)

    def test_iv_pct_inverted(self):
        th = ThresholdSet(iv_pct_single=80.0, iv_pct_double=60.0)
        w = th.validate()
        assert any("iv_pct_single" in x for x in w)

    def test_expiry_inverted(self):
        th = ThresholdSet(expiry_must_dte=10, expiry_warn_dte=3)
        w = th.validate()
        assert any("expiry_must_dte" in x for x in w)


class TestLoadThresholds:
    """load_thresholds() 加载与覆盖。"""

    def test_default_load(self):
        th = load_thresholds()
        assert isinstance(th, ThresholdSet)
        assert th.oi_min == 100

    def test_override(self):
        th = load_thresholds(oi_min=1000, spread_ratio_max=0.05)
        assert th.oi_min == 1000
        assert th.spread_ratio_max == 0.05

    def test_override_coercion(self):
        """config 值可能是字符串数字，load 应做类型强转。"""
        th = load_thresholds(oi_min="800")
        assert th.oi_min == 800
        assert isinstance(th.oi_min, int)

    def test_override_none_ignored(self):
        """overrides 中 None 值应被忽略，回退默认。"""
        th = load_thresholds(oi_min=None)
        assert th.oi_min == 100
