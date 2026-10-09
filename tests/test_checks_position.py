"""test_checks_position.py — P1 仓位风控单元测试。"""
from sell_option.fetchers.models import Verdict
from sell_option.fetchers.d16_positions import PositionBook
from sell_option.thresholds import load_thresholds

from tests.conftest import make_position


# ===========================================================================
# p_limits · 仓位限
# ===========================================================================
class TestPLimits:
    """P1 仓位限：总笔数 / 每品种 / 同持品种数（distinct codes）。"""

    def test_pass_under_limits(self):
        from sell_option.checks.p_limits import evaluate
        positions = [
            make_position(code="m"),
            make_position(code="AP"),
        ]
        book = PositionBook(positions=positions, loaded=True)
        th = load_thresholds()
        r = evaluate(book, th)
        assert r.verdict == Verdict.PASS

    def test_fail_total_exceeded(self):
        from sell_option.checks.p_limits import evaluate
        # 创建 7 笔持仓（> max_positions=6）
        positions = [make_position(code=f"v{i}") for i in range(7)]
        book = PositionBook(positions=positions, loaded=True)
        th = load_thresholds()
        r = evaluate(book, th)
        assert r.verdict == Verdict.FAIL

    def test_fail_per_code_exceeded(self):
        from sell_option.checks.p_limits import evaluate
        # 同品种 3 笔（> max_per_code=2）
        positions = [make_position(code="m") for _ in range(3)]
        book = PositionBook(positions=positions, loaded=True)
        th = load_thresholds()
        r = evaluate(book, th)
        assert r.verdict == Verdict.FAIL

    def test_missing_not_loaded(self):
        from sell_option.checks.p_limits import evaluate
        book = PositionBook(positions=[], loaded=False, load_error="文件不存在")
        th = load_thresholds()
        r = evaluate(book, th)
        assert r.verdict == Verdict.MISSING

    def test_empty_positions_pass(self):
        from sell_option.checks.p_limits import evaluate
        book = PositionBook(positions=[], loaded=True)
        th = load_thresholds()
        r = evaluate(book, th)
        assert r.verdict == Verdict.PASS
