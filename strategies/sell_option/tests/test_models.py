"""test_models.py — Verdict / merge_verdicts / CheckResult 单元测试。"""
import pytest
from sell_option.fetchers.models import Verdict
from sell_option.checks.models import CheckResult, merge_verdicts, VERDICT_RANK


class TestVerdict:
    """Verdict 枚举基础。"""

    def test_four_states(self):
        assert Verdict.PASS.value == "PASS"
        assert Verdict.FAIL.value == "FAIL"
        assert Verdict.WARN.value == "WARN"
        assert Verdict.MISSING.value == "MISSING"

    def test_is_str(self):
        """Verdict 继承 str，可直接入报告/JSON。"""
        assert isinstance(Verdict.PASS, str)
        assert Verdict.FAIL == "FAIL"


class TestMergeVerdicts:
    """merge_verdicts：FAIL > MISSING > WARN > PASS。"""

    def test_empty_returns_pass(self):
        assert merge_verdicts([]) == Verdict.PASS

    def test_single_pass(self):
        assert merge_verdicts([Verdict.PASS]) == Verdict.PASS

    def test_fail_wins_all(self):
        assert merge_verdicts([Verdict.PASS, Verdict.WARN, Verdict.MISSING, Verdict.FAIL]) == Verdict.FAIL

    def test_missing_beats_warn_and_pass(self):
        assert merge_verdicts([Verdict.PASS, Verdict.WARN, Verdict.MISSING]) == Verdict.MISSING

    def test_warn_beats_pass(self):
        assert merge_verdicts([Verdict.PASS, Verdict.WARN]) == Verdict.WARN

    def test_fail_beats_missing(self):
        assert merge_verdicts([Verdict.MISSING, Verdict.FAIL]) == Verdict.FAIL

    def test_all_same(self):
        assert merge_verdicts([Verdict.WARN, Verdict.WARN, Verdict.WARN]) == Verdict.WARN

    def test_rank_order(self):
        """VERDICT_RANK 严格递增。"""
        assert VERDICT_RANK[Verdict.PASS] < VERDICT_RANK[Verdict.WARN]
        assert VERDICT_RANK[Verdict.WARN] < VERDICT_RANK[Verdict.MISSING]
        assert VERDICT_RANK[Verdict.MISSING] < VERDICT_RANK[Verdict.FAIL]


class TestCheckResult:
    """CheckResult 数据结构。"""

    def test_frozen(self):
        r = CheckResult("#1", "盘口价差", Verdict.PASS, "ok")
        with pytest.raises(AttributeError):
            r.verdict = Verdict.FAIL

    def test_defaults(self):
        r = CheckResult("#1", "盘口价差", Verdict.PASS, "ok")
        assert r.blocking is True
        assert r.detail == {}

    def test_detail_dict(self):
        r = CheckResult("#1", "盘口价差", Verdict.PASS, "ok", detail={"ratio": 0.05})
        assert r.detail["ratio"] == 0.05
