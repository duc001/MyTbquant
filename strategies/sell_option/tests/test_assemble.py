"""test_assemble.py — 选腿装配纯函数单元测试（structure_word / select_legs / compute_economics）+ D07 主力 provider 接线契约。"""
import datetime as dt

from tests.conftest import make_contract


# ===========================================================================
# structure_word — c3 详细结构 → 评估口径
# ===========================================================================
class TestStructureWord:
    """structure_word：iron_condor→dual, single_*→single, None→single（安全默认）。"""

    def test_iron_condor(self):
        from sell_option.strategy.assemble import structure_word, WORD_DUAL
        assert structure_word("iron_condor") == WORD_DUAL

    def test_single_put(self):
        from sell_option.strategy.assemble import structure_word, WORD_SINGLE
        assert structure_word("single_put") == WORD_SINGLE

    def test_single_call(self):
        from sell_option.strategy.assemble import structure_word, WORD_SINGLE
        assert structure_word("single_call") == WORD_SINGLE

    def test_none_fallback(self):
        from sell_option.strategy.assemble import structure_word, WORD_SINGLE
        assert structure_word(None) == WORD_SINGLE

    def test_unknown_fallback(self):
        from sell_option.strategy.assemble import structure_word, WORD_SINGLE
        assert structure_word("unknown_structure") == WORD_SINGLE


# ===========================================================================
# select_legs — §7.2 选腿
# ===========================================================================
class TestSelectLegs:
    """select_legs：在链上选短腿+保护腿价差。"""

    def _make_chain(self):
        """构造 PUT 侧链：行权价 2800-3200，间隔 100。"""
        return [make_contract(strike=k, cp="P") for k in range(2800, 3201, 100)]

    def test_pass_single_put(self):
        from sell_option.strategy.assemble import select_legs
        chain = self._make_chain()
        # spot=3000，近端候选集平值优先 → 短腿取平值 3000；保护腿=更虚第 prot_strikes 档（默认4）
        # PUT 侧 pool（strike<=3000）=[2600,2700,2800,2900,3000]；平值 3000 优先试 → 保护腿取更虚第4档 2600
        chain = [make_contract(strike=k, cp="P") for k in range(2600, 3201, 100)]
        sel = select_legs(chain, spot=3000.0, structure="single_put",
                          code="m", month4="2611")
        assert sel is not None
        assert sel.structure == "single_put"
        assert len(sel.spreads) == 1
        sp = sel.spreads[0]
        assert sp.side == "put"
        assert sp.short_strike < sp.prot_strike or sp.short_strike > sp.prot_strike  # 有价差

    def test_missing_no_spot(self):
        from sell_option.strategy.assemble import select_legs
        chain = self._make_chain()
        sel = select_legs(chain, spot=None, structure="single_put")
        assert sel is None

    def test_missing_zero_spot(self):
        from sell_option.strategy.assemble import select_legs
        chain = self._make_chain()
        sel = select_legs(chain, spot=0.0, structure="single_put")
        assert sel is None

    def test_missing_unknown_structure(self):
        from sell_option.strategy.assemble import select_legs
        chain = self._make_chain()
        sel = select_legs(chain, spot=3000.0, structure="unknown")
        assert sel is None

    def test_iron_condor_two_spreads(self):
        from sell_option.strategy.assemble import select_legs
        # 铁鹰需要 PUT 侧 + CALL 侧各一价差
        put_chain = [make_contract(strike=k, cp="P") for k in range(2600, 3001, 100)]
        call_chain = [make_contract(strike=k, cp="C") for k in range(3000, 3401, 100)]
        chain = put_chain + call_chain
        sel = select_legs(chain, spot=3000.0, structure="iron_condor",
                          code="m", month4="2611")
        if sel is not None:
            assert sel.structure == "iron_condor"
            assert len(sel.spreads) == 2  # PUT 侧 + CALL 侧

    def test_no_protection_leg_returns_none(self):
        from sell_option.strategy.assemble import select_legs
        # 只有一档行权价 → 选不出保护腿 → None
        chain = [make_contract(strike=2900.0, cp="P")]
        sel = select_legs(chain, spot=3000.0, structure="single_put")
        assert sel is None


# ===========================================================================
# near-candidate-set selection — 近端候选集选腿（§7.2：短腿平值/虚一/虚二由近及远、保护腿更虚 prot_strikes 档）
# ===========================================================================
class TestNearCandidateSet:
    """§7.2 近端候选集选腿：短腿=平值/虚一/虚二由近及远（平值优先），保护腿=更虚方向第 prot_strikes 档。"""

    def _put(self, strikes):
        return [make_contract(strike=k, cp="P") for k in strikes]

    def test_atm_short_preferred(self):
        # PUT spot=3000，链含平值3000 → 短腿取平值（平值优先），保护腿=平值下方第 prot_strikes 档
        from sell_option.strategy.assemble import select_legs
        chain = self._put(range(2600, 3001, 50))   # 2600..3000 step50
        sel = select_legs(chain, spot=3000.0, structure="single_put",
                          code="m", month4="2611", prot_strikes=4)
        assert sel is not None
        sp = sel.spreads[0]
        assert sp.short_strike == 3000.0                      # 平值优先
        assert sp.prot_strike == 2800.0                       # 平值下方第4档(2950,2900,2850,2800)
        assert sp.width == 200.0

    def test_protection_leg_distance_configurable(self):
        from sell_option.strategy.assemble import select_legs
        chain = self._put(range(2400, 3001, 100))
        one = select_legs(chain, spot=3000.0, structure="single_put",
                          code="m", month4="2611", prot_strikes=1)
        two = select_legs(chain, spot=3000.0, structure="single_put",
                          code="m", month4="2611", prot_strikes=2)
        assert one is not None and two is not None
        assert two.spreads[0].prot_strike < one.spreads[0].prot_strike      # =2 更远离短腿（更虚）
        assert two.spreads[0].width == one.spreads[0].width * 2

    def test_prot_strikes_clamps_to_available(self):
        from sell_option.strategy.assemble import select_legs
        # PUT spot=3000，链 2700/2800/2900/3000；prot_strikes=3 → 平值下方恰3档(2900,2800,2700)
        chain = self._put((2700, 2800, 2900, 3000))
        sel = select_legs(chain, spot=3000.0, structure="single_put",
                          code="m", month4="2611", prot_strikes=3)
        assert sel is not None
        assert sel.spreads[0].prot_strike == 2700.0                        # 取最远可用(3档)
        assert sel.spreads[0].width == 300.0

    def test_prot_strikes_exceeds_available_narrows_to_available(self):
        from sell_option.strategy.assemble import select_legs
        # 链仅 2700/2800/2900/3000，prot_strikes=4 → 平值下方仅3档 <4 → 收窄到可用档数（取3档），绝不丢品种
        chain = self._put((2700, 2800, 2900, 3000))
        sel = select_legs(chain, spot=3000.0, structure="single_put",
                          code="m", month4="2611", prot_strikes=4)
        assert sel is not None
        assert sel.spreads[0].prot_strike == 2700.0                    # 收窄到最远可用(3档)
        assert sel.spreads[0].width == 300.0

    def test_short_candidates_window(self):
        from sell_option.strategy.assemble import select_legs
        chain = self._put(range(2600, 3001, 50))
        # 候选集3档：平值3000够宽→取平值；缩到1档仍取平值
        base = select_legs(chain, spot=3000.0, structure="single_put",
                           code="m", month4="2611", prot_strikes=4, short_candidates=3)
        one = select_legs(chain, spot=3000.0, structure="single_put",
                          code="m", month4="2611", prot_strikes=4, short_candidates=1)
        assert base is not None and one is not None
        assert base.spreads[0].short_strike == 3000.0
        assert one.spreads[0].short_strike == 3000.0


# ===========================================================================
# compute_economics — 经济性（R / W / max_loss）
# ===========================================================================
class TestComputeEconomics:
    """compute_economics / max_loss_cap 纯函数。"""

    def test_max_loss_cap_basic(self):
        from sell_option.fetchers.d18_budget import max_loss_cap
        # W=200, R=60, 乘数=10 → max_loss=(200-60)*10=1400
        assert max_loss_cap(200, 60, 10) == 1400.0

    def test_max_loss_cap_zero_when_credit_wider(self):
        from sell_option.fetchers.d18_budget import max_loss_cap
        # W=50, R=100 → W-R=-50 → max(0, -50)*10=0（不可能负亏损）
        assert max_loss_cap(50, 100, 10) == 0.0

    def test_max_loss_cap_missing(self):
        from sell_option.fetchers.d18_budget import max_loss_cap
        assert max_loss_cap(None, 60, 10) is None
        assert max_loss_cap(200, None, 10) is None
        assert max_loss_cap(200, 60, None) is None

    def test_max_loss_cap_boundary(self):
        from sell_option.fetchers.d18_budget import max_loss_cap
        # W=R → max_loss=0
        assert max_loss_cap(100, 100, 10) == 0.0


# ===========================================================================
# fetch_d07 主力 provider 接线契约（生产链 assemble._RealFetch.candidates 注入行为守卫；离线 fake）
# ===========================================================================
class TestD07MainProviderWiring:
    """fetch_d07 的 main_provider 注入：接了才标主力、不接/失败全 False（不阻断候选集）。"""

    _TODAY = dt.date(2026, 10, 2)

    def _chain(self):
        from sell_option.fetchers.models import ChainContract
        out = []
        for month, expiry in (("2611", dt.date(2026, 11, 20)),   # DTE 49
                              ("2612", dt.date(2026, 12, 18))):  # DTE 77
            out.append(ChainContract(code="m", exchange="DCE", underlying_month=month,
                                     expiry=expiry, cp="P", strike=2800.0,
                                     symbol=f"m{month}-P-2800"))
        return out

    def _params(self):
        from sell_option.fetchers.d07_candidate_months import D07Params
        return D07Params(dte_min=20, dte_max=180, top_n=3)

    def test_provider_marks_main_month(self):
        from sell_option.fetchers.d07_candidate_months import fetch_d07
        cand = fetch_d07(self._chain(), self._TODAY, params=self._params(),
                         main_provider=lambda code, exch, today: "2612")
        ms = {m.month4: m for m in cand.months_for("m")}
        assert ms["2612"].is_main is True      # provider 指哪月标哪月
        assert ms["2611"].is_main is False

    def test_no_provider_all_false(self):
        from sell_option.fetchers.d07_candidate_months import fetch_d07
        cand = fetch_d07(self._chain(), self._TODAY, params=self._params())  # 缺省＝不接
        assert all(m.is_main is False for m in cand.months_for("m"))

    def test_provider_returns_none_degrades(self):
        from sell_option.fetchers.d07_candidate_months import fetch_d07
        cand = fetch_d07(self._chain(), self._TODAY, params=self._params(),
                         main_provider=lambda code, exch, today: None)  # tbpy 不可用同款降级
        assert all(m.is_main is False for m in cand.months_for("m"))
        assert len(cand.months_for("m")) == 2  # 候选集不因缺主力月而缩水


# ===========================================================================
# fetcher 纯函数/编排契约（D08 日期归一・D12 use_cache/全表；2026-10-02 实跑排障补）
# ===========================================================================
class TestD08ToDate:
    """_to_date：日线 time 元素可能是 datetime/带 T 的字符串——统一归一为 date，不炸链路。"""

    def test_datetime_to_date(self):
        from sell_option.fetchers.d08_spot_price import _to_date
        assert _to_date(dt.datetime(2026, 9, 30, 0, 0)) == dt.date(2026, 9, 30)

    def test_iso_datetime_string(self):
        from sell_option.fetchers.d08_spot_price import _to_date
        assert _to_date("2026-09-30T00:00:00") == dt.date(2026, 9, 30)  # 缓存里存的就是这形式

    def test_plain_date_and_none(self):
        from sell_option.fetchers.d08_spot_price import _to_date
        assert _to_date(dt.date(2026, 9, 30)) == dt.date(2026, 9, 30)
        assert _to_date("2026-09-30") == dt.date(2026, 9, 30)
        assert _to_date(None) is None
        assert _to_date("not-a-date") is None


class _FakeIndexSource:
    """D12 fake 源：记录是否被调，返回固定 bar 数据。"""

    def __init__(self, close_val=99.0):
        self.close_val = close_val
        self.called_with = None

    def history_batch(self, symbols, count, fields):
        self.called_with = list(symbols)
        n = 70  # ≥ MIN_BARS
        return {s: {"time": ["2026-09-30"] * n, "open": [self.close_val] * n,
                    "high": [self.close_val] * n, "low": [self.close_val] * n,
                    "close": [self.close_val] * n, "volume": [1.0] * n} for s in symbols}


class TestD12FetchContract:
    """fetch_d12：use_cache=False 强制重拉・codes=None→D03 全表（refresh 命令语义）。"""

    _TODAY = dt.date(2026, 10, 2)

    def _table(self):
        from sell_option.fetchers.d03_index_symbol import IndexSymbolTable
        return IndexSymbolTable({"m": "m9888.DCE", "rb": "rb888.SHFE"})

    def test_use_cache_false_forces_refetch(self, tmp_path):
        from sell_option.fetchers.d12_index_daily import DailyCacheRepo, fetch_d12
        from sell_option.fetchers._trading_day import pull_date_for
        repo = DailyCacheRepo(db_path=tmp_path / "daily.db")
        # 缓存 key 与 fetch_d12 落库约定一致：用「数据真实交易日」pull_date（长假回退最近交易日），
        # 而非字面 today——否则今日恰为闭市日（如国庆）时 key 对不上会误判未命中
        cache_day = pull_date_for(self._TODAY)
        repo.save("m9888.DCE", cache_day,
                  {"time": ["2026-09-30"], "open": [1.0], "high": [1.0],
                   "low": [1.0], "close": [1.0], "volume": [1.0]}, "stale")
        src_hit = _FakeIndexSource()
        res = fetch_d12(["m"], self._TODAY, index_table=self._table(),
                        source=src_hit, cache=repo)  # 默认 use_cache=True → 当日缓存命中
        assert src_hit.called_with is None
        assert res.get("m").source == "stale"
        src_force = _FakeIndexSource(close_val=88.0)
        res2 = fetch_d12(["m"], self._TODAY, index_table=self._table(),
                         source=src_force, cache=repo, use_cache=False)
        assert src_force.called_with == ["m9888.DCE"]  # 跳过缓存直接重拉
        assert res2.get("m").close[-1] == 88.0

    def test_codes_none_expands_full_table(self, tmp_path):
        from sell_option.fetchers.d12_index_daily import DailyCacheRepo, fetch_d12
        src = _FakeIndexSource()
        res = fetch_d12(None, self._TODAY, index_table=self._table(),
                        source=src, cache=DailyCacheRepo(db_path=tmp_path / "daily.db"))
        assert sorted(res.codes()) == ["m", "rb"]  # None → D03 全表展开，不炸 None 迭代


# ===========================================================================
# 早筛闸（assemble._early_block + opening.run_opening_checks 早筛跳过）
# 取期权权利金+持仓（get_history_n，62s/次，全池累计≈48min）极贵；
# 取数前先用便宜数据跑阻断检查(#4 IV/#5 安全垫/#7 保证金/#10 否决)，
# 命中即断定淘汰 → 跳过昂贵期权取数，#1/#2/#8/#9 标记「早筛跳过」。
# ===========================================================================
class _FakeCapitalPool:
    """D17 假资金池：#7 合计判定只读 .total / .configured。"""

    def __init__(self, total, configured=True):
        self.total = total
        self.configured = configured


class TestEarlyExclusion:
    """早筛闸：便宜阻断检查命中即跳过期权取数。"""

    def _base_data(self, th, *, iv_pct=75, iv_cur=12, closes=None,
                   margin_single=1000.0, pool_total=1_000_000.0, pool_configured=True):
        from sell_option.strategy.opening import OpeningData
        from tests.conftest import make_iv, make_spot, make_index_series
        return OpeningData(
            code="m", structure="single",
            iv=make_iv(atmv_percentile=iv_pct, atmv_current=iv_cur),
            spot=make_spot(3000.0), dte=45, prot_strike=2700.0,
            margin_single=margin_single, margin_existing=0.0,
            capital_pool=_FakeCapitalPool(pool_total, pool_configured),
            index_daily=make_index_series(closes if closes is not None else [100.0] * 50),
        )

    def test_pass_when_cheap_checks_ok(self, th):
        from sell_option.strategy.assemble import _early_block
        assert _early_block(self._base_data(th), th) is None

    def test_low_iv_triggers_four(self, th):
        from sell_option.strategy.assemble import _early_block
        assert _early_block(self._base_data(th, iv_pct=10), th) == "#4"

    def test_veto_gap_triggers_ten(self, th):
        from sell_option.strategy.assemble import _early_block
        closes = [100.0] * 49 + [103.0]          # 末根较昨收 +3% 跳空 ≥ 否决阈值 2%
        assert _early_block(self._base_data(th, closes=closes), th) == "#10"

    def test_run_opening_checks_skips_option_checks(self, th):
        from sell_option.strategy.opening import OpeningData, evaluate_opening
        from sell_option.checks.models import Verdict
        from tests.conftest import make_iv, make_spot
        # 已断定淘汰（early_excluded="#4"）→ 依赖期权取数的 #1/#2/#8/#9 应标记早筛跳过
        data = OpeningData(code="m", structure="single",
                           iv=make_iv(atmv_percentile=10), spot=make_spot(3000.0),
                           early_excluded="#4")
        summary, results = evaluate_opening(data, th, market_open=None)  # None：不触发 needs_live 跳过，专测早筛跳过
        skipped = {r.rule_id: r.detail.get("skipped") for r in results}
        for rid in ("#1", "#2", "#8", "#9"):
            assert skipped.get(rid) == "early_excluded", f"{rid} 应被早筛跳过而非取数"
        # 早筛已排除 → 汇总绝不可能是「可开」
        assert summary.verdict != Verdict.PASS
