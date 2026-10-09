"""prot_strikes 1/2/3 对照（纯缓存、零 tbpy）：走真实 select_legs + compute_economics（含保护腿护栏）。

锚定 09-30 价格 pull_date 日；手续费假设 1.5 元/手/边（仅估占比，compute_economics 本身不含手续费）；
到期月经 openvlab /api/product-exps 兜底补齐（tbpy 不可达）。

归档自根目录 _ab_prot.py（2026-10-06）：原分析脚本，依赖真实 db，非确定性单测，故以 analyze_ 前缀
置于 tests/ 下（pytest 默认只收集 test_*.py，不会误跑），保留为人工诊断工具。
运行：从项目根 `python tests/analyze_prot_strikes.py`。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3, datetime as _dt
from collections import defaultdict
from sell_option.thresholds import load_thresholds
from sell_option.fetchers.d06_option_chain import OptionIndexRepo, TbpyIndexSource
from sell_option.strategy.assemble import select_legs, compute_economics, SpreadLegs, LegSelection, _detect_prot_anomaly
from sell_option.fetchers.d11_contract_multiplier import fetch_d11
from sell_option.fetchers.d04_margin_rate import fetch_d04
from sell_option.fetchers.d05_combo_discount import fetch_d05

TODAY = _dt.date(2026, 9, 30)   # 价格数据 pull_date 日（premium/spot 实为 09-30），DTE 据此算
FEE = 1.5                       # 元/手/边（手续费假设，仅估占比）
TS = 1.2
DTE_MIN, DTE_MAX, TOPN = 20, 180, 3
PROT = [1, 2, 3]

# 缓存模式：不连 tbpy；option_index 的到期月经 openvlab 兜底补齐
import runtime as _rt
_rt.connect = lambda key: False

repo = OptionIndexRepo()
repo._enrich_expiry(verbose=False)
src = TbpyIndexSource(repo, TODAY)
prem = sqlite3.connect("sell_option/data/premium.db")
spotdb = sqlite3.connect("sell_option/data/spot.db")
ivdb = sqlite3.connect("sell_option/data/iv_snapshot.db")
d04, d05, d11 = fetch_d04(), fetch_d05(), fetch_d11()
th = load_thresholds()


class _Prem:
    def premium_of(self, sym):
        r = prem.execute("SELECT close FROM premium WHERE symbol=? AND close IS NOT NULL "
                         "ORDER BY pull_date DESC LIMIT 1", (sym,)).fetchone()
        return r[0] if r else None


def spot_price(sym):
    r = spotdb.execute("SELECT price FROM spot_price WHERE symbol=? AND price IS NOT NULL "
                      "ORDER BY pull_date DESC LIMIT 1", (sym,)).fetchone()
    return r[0] if r else None


def iv_dec(code):
    r = ivdb.execute("SELECT atmv_current FROM iv_snapshot WHERE variety=? "
                     "ORDER BY snapshot_date DESC LIMIT 1", (code,)).fetchone()
    return (r[0] / 100.0) if r and r[0] is not None else None


prem_set = _Prem()

varieties = sorted(repo.data()["varieties"].keys())
rows: dict[str, dict] = {}
for code in varieties:
    chain = src.fetch_chain(code, "")
    if not chain:
        continue
    exch = chain[0].symbol.rsplit(".", 1)[-1]
    bym = defaultdict(list)
    for c in chain:
        bym[c.underlying_month].append(c)
    months = []
    for m, cs in bym.items():
        dte = (min(c.expiry for c in cs) - TODAY).days
        if DTE_MIN <= dte <= DTE_MAX:
            months.append((m, dte))
    months.sort(key=lambda x: x[0])
    months = months[:TOPN]
    if not months:
        continue
    rec: dict = {}
    for (m, dte) in months:
        mc = [c for c in chain if c.underlying_month == m]
        num = m[1:] if exch == "CZCE" else m
        sp = spot_price(f"{code}{num}.{exch}")
        iv = iv_dec(code)
        if sp is None or iv is None:
            continue
        try:
            mult = float(d11.contract_unit(code))
        except Exception:
            mult = None
        if not mult or mult <= 0:
            continue
        ok_all = True
        for p in PROT:
            sel = select_legs(mc, sp, "single_put", code=code, month4=m,
                              prot_strikes=p)
            if sel is None:
                ok_all = False
                break
            econ = compute_economics(sel, prem_set, multiplier=mult, d04=d04, d05=d05,
                                     d11=d11, code=code, exchange=exch, th=th)
            rec[p] = (m, mult, econ)
        if ok_all:
            rows[code] = rec
            break

print(f"=== prot_strikes {PROT} 对照（锚定 {TODAY}·single_put·手续费假设 {FEE}元/手/边·"
      f"#9 R/W≥{th.rw_min:.0%}·护栏 ratio={th.prot_premium_max_ratio}）===")
print(f"可比品种数={len(rows)}  （权利金缺数→该 prot 净信用金=None→不计入；护栏仅观测标记、不据其置空）\n")

for p in PROT:
    econs = [(c, rows[c][p][1], rows[c][p][2]) for c in rows
             if rows[c].get(p) and rows[c][p][2].net_credit is not None]
    W = [e.spread_width for _, _, e in econs if e.spread_width]
    Rp = [e.net_credit for _, _, e in econs if e.net_credit is not None]
    ML = [e.max_loss for _, _, e in econs if e.max_loss is not None]
    fee = [2 * FEE / (e.net_credit * mult) * 100 for _, mult, e in econs
           if e.net_credit and e.net_credit > 0]
    p9 = sum(1 for _, _, e in econs
             if e.spread_width and e.net_credit / e.spread_width >= th.rw_min)
    aw = sum(W) / len(W) if W else 0
    ar = sum(Rp) / len(Rp) if Rp else 0
    aml = sum(ML) / len(ML) if ML else 0
    af = sum(fee) / len(fee) if fee else 0
    print(f"[prot={p}] 有效={len(econs)} 均值 W={aw:.1f} R(点)={ar:.2f} "
          f"ML(元)={aml:.0f} 手续费占R≈{af:.1f}%  #9通过={p9}/{len(econs)}")

print("\n--- 逐品种（code|月| prot1[W,R点,rw%,fee%] prot2[...] prot3[...]）---")
for code in rows:
    parts = [f"{code:4s}"]
    for p in PROT:
        e = rows[code].get(p)
        if not e or e[2].net_credit is None:
            parts.append(f"p{p}=[净信用金缺]")
            continue
        m, mult, econ = e
        rw = (econ.net_credit / econ.spread_width) if econ.spread_width else 0
        f = 2 * FEE / (econ.net_credit * mult) * 100 if econ.net_credit > 0 else 0
        if p == 1:
            parts.append(f"{m:>4}|p1[{econ.spread_width:.0f},{econ.net_credit:.1f},{rw*100:.1f}%,{f:.1f}%]")
        else:
            parts.append(f"p{p}[{econ.spread_width:.0f},{econ.net_credit:.1f},{rw*100:.1f}%,{f:.1f}%]")
    print(" ".join(parts))

# ---- 护栏观测探针：保护腿权利金倒挂/脏数据 → 仅标记（检测·不淘汰），不置空、不拒开；下方断言验证诚实算 R ----
class _C:
    def __init__(self, sym):
        self.symbol = sym


class _FakePS:
    def __init__(self, m):
        self.m = m

    def premium_of(self, sym):
        return self.m.get(sym)


print("\n--- 护栏观测探针单测（不阻挡：只标记、诚实算 R）---")
healthy = _FakePS({"s.X": 50.0, "p.X": 30.0})   # prot < short（正常）
anomaly = _FakePS({"s.X": 50.0, "p.X": 48.0})   # prot ≥ short×0.90（疑似脏/倒挂，但照常算 R）
dirty = _FakePS({"s.X": 50.0, "p.X": 60.0})     # prot ≥ short（倒挂）
sl_obj = SpreadLegs(side="put", short=_C("s.X"), protection=_C("p.X"),
                    short_strike=100.0, prot_strike=90.0, width=10.0)
ls = lambda ps: LegSelection(code="X", month4="2701", structure="single_put",
                             spreads=(sl_obj,), spot=100.0)
e_h = compute_economics(ls(healthy), healthy, multiplier=10, d04=d04, d05=d05, d11=d11,
                        code="X", exchange="CZCE", th=th)
e_a = compute_economics(ls(anomaly), anomaly, multiplier=10, d04=d04, d05=d05, d11=d11,
                        code="X", exchange="CZCE", th=th)
note_a = _detect_prot_anomaly(ls(anomaly), anomaly, th=th)
note_h = _detect_prot_anomaly(ls(healthy), healthy, th=th)
print(f"  正常腿(prot=30<short=50): net_credit={e_h.net_credit}（应=20）；观测={note_h}")
print(f"  疑似脏腿(prot=48≥50×{th.prot_premium_max_ratio}): net_credit={e_a.net_credit}（应=2，诚实不拦截）；观测={note_a}")
assert e_h.net_credit == 20 and e_a.net_credit == 2 and note_a and note_h is None
print("DONE")
