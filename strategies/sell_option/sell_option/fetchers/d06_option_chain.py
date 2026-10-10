"""D06 · 期权链合约清单（行权价×到期日×C/P）· 多源降级主单元。

需求源 §6.1 D06 / §6.3 F（用户拍板 2026-09-27）：
源优先级 tbpy option_index（主源，隔离子进程 get_symbols 预建）→ 新浪行权价阶梯＋openvlab
到期日（month_resolver）拼单月真链（弱备源；openvlab 实测无行权价阶梯、不独立建链）；外部源 symbol 按 D02 模板回拼 + get_instrument 单合约复核；
全源确拿不到 → 该品种断源、依赖它的检查判 MISSING，不猜合约。链清单＝元数据、多源不碰串价铁律。

**三源实测能力（2026-09-27 探针，决定了本单元的真实降级形态）**：
- tbpy 主源：get_symbols 返回 ~11 万字符串，匹配 47 品种商品期权 **15894 个**、distinct
  (品种,月) **313**；到期日不在 symbol 里，由干净进程 get_instrument 逐月补（期权 delisted_date
  ≠ 标的期货 delisted_date，必须用期权自己的）。**唯一能给出完整多月链的源**。
- 新浪备源①：仅主力月单月 T 型盘口、代码无月份（mC2300）、无到期日 → 结构上无法独立重建
  完整链，只能作"主力月行权价阶梯"弱补充（月份/到期日仍须 tbpy 侧注入）。详见 d06_external_sources。
- openvlab 备源②：4 个接口是**公开 HTTP JSON**（无需浏览器/登录/token）。/api/product-exps 覆盖
  47/47 品种全部到期月份+精确到期日（实测与 tbpy 期权 delisted_date 完全吻合）、/api/ctamap-all
  覆盖 47/47 的 IV；**但无行权价阶梯** → 不能独立建链。其价值＝到期日的 tbpy-无关备份/交叉验证
  + 作新浪的 month_resolver（新浪阶梯 + openvlab 到期日 → 纯 HTTP 单月真链）。详见 d06_external_sources。

**降级设计据实调整**：D06 真正可用的降级是"今日索引 → 近日缓存索引（合约清单日间变化极小）
→ 到期日 tbpy 挂时用 openvlab product-exps 兜底 → 新浪阶梯+openvlab到期日拼单月链（经 get_instrument
复核）→ MISSING"。缓存复用与 openvlab 到期日兜底是链清单最实用的两道备份，均本单元内置。

运行自检：``python -m sell_option.fetchers.d06_option_chain``（会 spawn 构建 + 313 次 get_instrument 补到期日）
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

try:  # 包方式（python -m / import）：相对导入
    from .models import ChainContract, as_date, expand_month
    from ._option_index_builder import DEFAULT_INDEX_PATH, _atomic_write_json
    from .d06_external_sources import OpenvlabSource, SinaChainSource, revalidate_via_tbpy
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.models import ChainContract, as_date, expand_month
    from sell_option.fetchers._option_index_builder import DEFAULT_INDEX_PATH, _atomic_write_json
    from sell_option.fetchers.d06_external_sources import (
        OpenvlabSource, SinaChainSource, revalidate_via_tbpy)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 索引仓库：加载 / spawn 隔离子进程构建 / 干净进程补到期日
# ---------------------------------------------------------------------------
class OptionIndexRepo:
    """option_index.json 的读写与新鲜度管理。

    - 构建（get_symbols）只在**隔离子进程**做（堆损坏随子进程销毁，§11.3.6）；
    - 补到期日（get_instrument）在**本干净进程**做（全程不调 get_symbols，故原生调用可靠）；
    - 不以子进程退出码判成败，以"索引已落盘且为今日"为准（子进程可能写盘后在断连阶段崩）。
    """

    def __init__(self, index_path: Path = DEFAULT_INDEX_PATH):
        self.path = index_path
        self._data: dict | None = None

    def data(self) -> dict | None:
        if self._data is None:
            self._data = self._read()
        return self._data

    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def build_date(self) -> str | None:
        d = self.data()
        return d.get("build_date") if d else None

    def ensure_fresh(self, today: dt.date, *, allow_stale_days: int = 5,
                     enrich: bool = True, verbose: bool = True) -> dict:
        """确保索引可用：今日索引优先；否则尝试重建；重建失败则容忍近日缓存（降级）。

        返回状态 dict：{status, build_date, stale_days, expiry_complete, filled, failed, reason}
        status ∈ {fresh, rebuilt, stale_cache, unavailable}
        """
        data = self._read()
        today_iso = today.isoformat()
        have_today = data is not None and data.get("build_date") == today_iso

        if not have_today:
            is_weekend = today.weekday() >= 5
            if is_weekend and data is not None:
                # 周末休市，期权链不会新增合约，直接复用最近一份缓存，避免触发 tbpy get_symbols 重建
                if verbose:
                    print(f"[D06] 周末({today_iso})复用已有索引缓存 build_date={data.get('build_date')}，跳过 tbpy 重建")
            else:
                if verbose:
                    print(f"[D06] 今日索引缺失/过期（现有 build_date={self.build_date()}），spawn 隔离子进程重建…")
                self._spawn_builder(verbose)
                data = self._read()
                have_today = data is not None and data.get("build_date") == today_iso

        if data is None:
            return {"status": "unavailable", "reason": "构建未产出索引且无缓存", "build_date": None}

        status = "fresh" if have_today else "rebuilt"
        stale_days = 0
        if not have_today:
            # 重建失败：退回近日缓存（合约清单日间变化极小，可降级复用），过期太久则不可用
            try:
                bd = dt.date.fromisoformat(data["build_date"])
                stale_days = (today - bd).days
            except (KeyError, ValueError):
                stale_days = 999
            if stale_days > allow_stale_days:
                return {"status": "unavailable", "reason": f"重建失败且缓存过期 {stale_days} 天",
                        "build_date": data.get("build_date")}
            status = "stale_cache"

        filled = failed = 0
        if enrich and not data.get("expiry_complete"):
            filled, failed = self._enrich_expiry(verbose)
            data = self._read() or data
        self._data = data
        return {"status": status, "build_date": data.get("build_date"), "stale_days": stale_days,
                "expiry_complete": bool(data.get("expiry_complete")), "filled": filled, "failed": failed}

    def _spawn_builder(self, verbose: bool) -> None:
        cmd = [sys.executable, "-m", "sell_option.fetchers._option_index_builder"]
        try:
            proc = subprocess.run(cmd, cwd=str(_PROJECT_ROOT), capture_output=True,
                                  text=True, timeout=300, encoding="utf-8", errors="replace")
            if verbose and proc.stdout:
                for line in proc.stdout.strip().splitlines():
                    print(f"    {line}")
            if proc.returncode != 0 and verbose and proc.stderr:
                print(f"    [builder stderr] {proc.stderr.strip()[:300]}")
        except (subprocess.TimeoutExpired, OSError) as e:
            if verbose:
                print(f"    [builder 未能运行] {type(e).__name__}: {e}")

    def _enrich_expiry(self, verbose: bool) -> tuple[int, int]:
        """补到期日：① tbpy get_instrument（干净进程，权威）；② 仍有缺口则用 openvlab
        /api/product-exps 的 expDate 兜底（实测与 tbpy delisted_date 完全吻合、且不依赖 tbpy 连接）。
        返回 (累计补齐, 剩余缺口)；剩余缺口=0 → expiry_complete=True。"""
        data = self._read()
        if data is None:
            return 0, 0
        tbpy_filled, _ = self._enrich_via_tbpy(data, verbose)
        filled = tbpy_filled
        remaining = self._count_null_expiry(data)
        if remaining:
            olv_filled, _ = self._fill_from_openvlab(data, verbose)
            filled += olv_filled
            remaining = self._count_null_expiry(data)
        data["expiry_complete"] = (remaining == 0)
        _atomic_write_json(self.path, data)
        if verbose:
            print(f"[D06] 到期日补齐：成功 {filled}（tbpy {tbpy_filled}）· 剩余缺口 {remaining} · "
                  f"expiry_complete={data['expiry_complete']}")
        return filled, remaining

    @staticmethod
    def _count_null_expiry(data: dict) -> int:
        return sum(1 for v in data.get("varieties", {}).values()
                   for e in v.get("month_expiry", {}).values() if not e)

    def _enrich_via_tbpy(self, data: dict, verbose: bool) -> tuple[int, int]:
        """干净进程 get_instrument 补到期日（权威源，全程不调 get_symbols）；tbpy 不可用则 (0,0)。不写盘。"""
        try:
            import tbpy
            import config
            from runtime import connect, disconnect
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"[D06] 无法导入 tbpy 补到期日：{e}（转 openvlab 兜底）")
            return 0, 0
        if not connect(config.PRODUCT_KEY):
            if verbose:
                print("[D06] tbpy 未连接（转 openvlab product-exps 兜底到期日）")
            return 0, 0

        filled = failed = 0
        varieties = data.get("varieties", {})
        total_months = sum(len(v.get("month_expiry", {})) for v in varieties.values())
        done = 0
        try:
            for code, entry in varieties.items():
                mexp = entry.get("month_expiry", {})
                for month in list(mexp):
                    if mexp[month]:
                        continue
                    sym = next((c["symbol"] for c in entry["contracts"] if c["month"] == month), None)
                    done += 1
                    if sym is None:
                        failed += 1
                        continue
                    exp = _option_delisted_date(tbpy, sym)
                    if exp is not None:
                        mexp[month] = exp.isoformat()
                        filled += 1
                    else:
                        failed += 1
                    if verbose and done % 60 == 0:
                        print(f"    补到期日 {done}/{total_months}（成功 {filled} 失败 {failed}）")
        finally:
            disconnect()
        return filled, failed

    def _fill_from_openvlab(self, data: dict, verbose: bool) -> tuple[int, int]:
        """openvlab /api/product-exps 兜底：为 tbpy 未补上的 (品种,月) 填 expDate（纯 HTTP、不依赖 tbpy）。

        openvlab exp 为 YYYYMM；索引月份键可能是 4 位（DCE/SHFE/GFEX '2611'）或 3 位（CZCE '611'），
        故同时按 4 位与去年代十位的 3 位建查表，直接匹配索引键、无需 expand_month。"""
        olv = OpenvlabSource()
        filled = failed = 0
        for code, entry in data.get("varieties", {}).items():
            mexp = entry.get("month_expiry", {})
            nulls = [m for m in mexp if not mexp[m]]
            if not nulls:
                continue
            emap = olv.expiry_map(code)          # {'2611': date, ...}
            if not emap:
                failed += len(nulls)
                continue
            lookup: dict[str, dt.date] = {}
            for m4, d in emap.items():
                lookup[m4] = d                   # 4 位（DCE/SHFE/GFEX）
                lookup[m4[1:]] = d               # 3 位（CZCE 索引键）
            for m in nulls:
                d = lookup.get(m)
                if d is not None:
                    mexp[m] = d.isoformat()
                    filled += 1
                else:
                    failed += 1
        if verbose and (filled or failed):
            tail = f" · last_error={olv.last_error}" if olv.last_error else ""
            print(f"    [openvlab 兜底] 补 {filled} · 未覆盖 {failed}{tail}")
        return filled, failed


def _option_delisted_date(tbpy_mod, symbol: str) -> dt.date | None:
    """get_instrument 单合约读 delisted_date（每属性只读一次；sec_name 非空判有效）。"""
    try:
        inst = tbpy_mod.get_instrument(symbol=symbol)
    except Exception:  # noqa: BLE001
        return None
    if inst is None:
        return None
    name = inst.sec_name           # 只读一次
    if not name:
        return None
    d = inst.delisted_date         # 只读一次
    if d is None:
        return None
    try:
        return as_date(d)
    except ValueError:
        return None


def make_tbpy_validator() -> Callable[[str], bool]:
    """§6.3 F 外部源 symbol 复核器：get_instrument + sec_name 非空判有效（需 tbpy 已连接）。"""
    import tbpy

    def is_valid(symbol: str) -> bool:
        try:
            inst = tbpy.get_instrument(symbol=symbol)
        except Exception:  # noqa: BLE001
            return False
        if inst is None:
            return False
        return bool(inst.sec_name)
    return is_valid


# ---------------------------------------------------------------------------
# tbpy 主源：读索引组装 ChainContract
# ---------------------------------------------------------------------------
class TbpyIndexSource:
    """主源：从 option_index.json（含到期日）组装完整多月链。authoritative=True。"""

    name = "tbpy_index"
    authoritative = True

    def __init__(self, repo: OptionIndexRepo, today: dt.date):
        self.repo = repo
        self.today = today

    def fetch_chain(self, code: str, exchange: str) -> list[ChainContract]:
        data = self.repo.data()
        if not data:
            return []
        v = data.get("varieties", {}).get(code)
        if not v:
            return []
        mexp = v.get("month_expiry", {})
        out: list[ChainContract] = []
        for c in v.get("contracts", []):
            exp_iso = mexp.get(c["month"])
            if not exp_iso:            # 无到期日→无法算 DTE，剔除（不猜）
                continue
            try:
                expiry = dt.date.fromisoformat(exp_iso)
            except ValueError:
                continue
            out.append(ChainContract(
                code=code, exchange=v.get("exchange", exchange),
                underlying_month=expand_month(c["month"], self.today),
                expiry=expiry, cp=c["cp"], strike=float(c["strike"]), symbol=c["symbol"]))
        return out


# ---------------------------------------------------------------------------
# 编排器：多源降级 + 溯源
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ChainProvenance:
    code: str
    exchange: str
    source: str            # tbpy_index / sina / openvlab / manual
    complete: bool         # 是否完整多月链（外部备源=False）
    n_contracts: int
    n_months: int
    note: str = ""


@dataclass
class D07ChainSet:
    """D06 产物：品种 → 期权链 + 溯源。可直接喂 D07（all_contracts）。"""

    chains: dict[str, list[ChainContract]] = field(default_factory=dict)
    provenance: dict[str, ChainProvenance] = field(default_factory=dict)

    def for_code(self, code: str) -> list[ChainContract]:
        return self.chains.get(code, [])

    def all_contracts(self) -> list[ChainContract]:
        return [c for chain in self.chains.values() for c in chain]

    def codes(self) -> list[str]:
        return list(self.chains)

    def by_source(self) -> dict[str, int]:
        agg: dict[str, int] = {}
        for p in self.provenance.values():
            agg[p.source] = agg.get(p.source, 0) + 1
        return agg

    def manual_codes(self) -> list[str]:
        return [c for c, p in self.provenance.items() if p.source == "manual"]

    def __len__(self) -> int:
        return len(self.chains)


def _months_of(chain: Iterable[ChainContract]) -> int:
    return len({c.underlying_month for c in chain})


def fetch_d06(varieties: list[tuple[str, str]], today: dt.date, *,
              sources: list | None = None, repo: OptionIndexRepo | None = None,
              verbose: bool = True) -> D07ChainSet:
    """按源优先级为每品种取链，首个非空源胜出并溯源；全空→manual（不猜合约）。

    varieties：[(code, exchange), ...]（一般来自 D01）；sources：有序 ChainSource 列表，
    默认 [tbpy 主源, 新浪(+openvlab 到期日解析)]；openvlab 无行权价阶梯、不独立建链，另作
    到期日兜底见 OptionIndexRepo._enrich_expiry。外部备源产出的 symbol 会经 get_instrument 复核（§6.3 F）。
    """
    repo = repo or OptionIndexRepo()
    if sources is None:
        # openvlab 改作新浪的 month_resolver：新浪出主力月行权价阶梯 + openvlab 出该月到期日 → 单月真链
        olv = OpenvlabSource()
        sources = [TbpyIndexSource(repo, today), SinaChainSource(month_resolver=olv.resolve_front)]

    validator: Callable[[str], bool] | None = None
    result = D07ChainSet()
    for code, exchange in varieties:
        chosen: list[ChainContract] = []
        src_name, complete, note = "manual", False, "全源拿不到→断源判 MISSING"
        for src in sources:
            try:
                chain = src.fetch_chain(code, exchange)
            except Exception as e:  # noqa: BLE001 - 单源故障只降级不崩整轮（§6.2 健壮性）
                if verbose:
                    print(f"[D06] {code} 源 {getattr(src, 'name', '?')} 异常降级：{type(e).__name__}: {e}")
                continue
            if not chain:
                continue
            if not getattr(src, "authoritative", False):
                # 外部备源：symbol 须经 get_instrument 复核有效后才可用（§6.3 F）
                if validator is None:
                    try:
                        validator = make_tbpy_validator()
                    except Exception:  # noqa: BLE001 - tbpy 不可用则无法复核
                        validator = lambda _s: False  # noqa: E731
                ok_syms = set(revalidate_via_tbpy([c.symbol for c in chain], validator))
                chain = [c for c in chain if c.symbol in ok_syms]
                complete = False
                note = f"外部备源 {src.name}（仅主力月/需复核，非完整链）"
                if not chain:
                    continue
            else:
                complete = True
                note = "tbpy option_index 完整多月链"
            chosen, src_name = chain, src.name
            break
        result.chains[code] = chosen
        result.provenance[code] = ChainProvenance(
            code=code, exchange=exchange, source=src_name, complete=complete,
            n_contracts=len(chosen), n_months=_months_of(chosen), note=note)
    return result


# ---------------------------------------------------------------------------
# 自检：三源各跑一遍 + 降级编排 + 真链喂 D06
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.d02_symbol_template import fetch_d02
    from sell_option.fetchers.d07_candidate_months import fetch_d07

    today = dt.date.today()
    varieties = [(v.code, v.exchange) for v in fetch_d01()]
    exch_of = dict(varieties)
    print("=" * 72)
    print(f"D06 自检 · today={today} · 品种 {len(varieties)} 个")

    # ---- 主源：确保索引新鲜 + 补到期日（313 次 get_instrument，干净进程）----
    repo = OptionIndexRepo()
    st = repo.ensure_fresh(today, verbose=True)
    print(f"\n[主源索引] status={st['status']} build_date={st.get('build_date')} "
          f"expiry_complete={st.get('expiry_complete')} filled={st.get('filled', 0)} failed={st.get('failed', 0)}")

    # ---- 主源组装：全 47 品种完整多月链 ----
    tsrc = TbpyIndexSource(repo, today)
    print("\n[主源 tbpy_index] 全品种完整链：")
    for code, exchange in varieties:
        chain = tsrc.fetch_chain(code, exchange)
        months = sorted({c.underlying_month for c in chain})
        print(f"  {code:<3} 合约 {len(chain):>4} 个 · 标的月 {months}")

    # ---- 备源①新浪：抓主力月 T 型盘口（如实展示其“无月份”局限）----
    print("\n[备源① sina] 主力月行权价阶梯（无月份/无到期日，仅补充）：")
    sina = SinaChainSource()
    for code in ("m", "AP", "rb"):
        ladder = sina.fetch_ladder(code, exch_of.get(code, ""))
        if ladder is None:
            print(f"  {code:<3} 拿不到（网络/结构）")
            continue
        cs, ps = ladder.strikes("C"), ladder.strikes("P")
        crange = f"{cs[0]:g}~{cs[-1]:g}" if cs else "NA"
        prange = f"{ps[0]:g}~{ps[-1]:g}" if ps else "NA"
        print(f"  {code:<3} 看涨 {len(cs)} 档 行权价 {crange} · 看跌 {len(ps)} 档 {prange}")

    # ---- 备源②openvlab：纯 HTTP 公开接口（到期日备份 + IV），并与 tbpy 索引交叉验证 ----
    print("\n[备源② openvlab] 纯 HTTP 公开接口（无需浏览器/登录）：")
    olv = OpenvlabSource()
    idx_varieties = (repo.data() or {}).get("varieties", {})
    hit = sum(1 for code, _ex in varieties if olv.expiry_map(code))
    print(f"  product-exps 到期月覆盖：{hit}/{len(varieties)} 品种")
    print(f"  fetch_chain('m') -> {len(olv.fetch_chain('m', 'DCE'))} 个（无行权价阶梯，不独立建链）")
    # 交叉验证：openvlab expDate vs tbpy 索引 delisted_date（抽 4 品种各前 2 月）
    print("  到期日交叉验证（openvlab expDate vs tbpy 索引）：")
    for code in ("m", "AP", "rb", "lc"):
        emap = olv.expiry_map(code)
        mexp = idx_varieties.get(code, {}).get("month_expiry", {})
        cmp = []
        for mk, iso in list(mexp.items())[:2]:
            if not iso:
                continue
            olv_d = emap.get(expand_month(mk, today))   # 索引键（CZCE 3 位）补年代后查 openvlab 4 位
            ok = "✓" if (olv_d and olv_d.isoformat() == iso) else "✗"
            cmp.append(f"{mk} tbpy={iso} olv={olv_d.isoformat() if olv_d else 'NA'} {ok}")
        print(f"    {code:<3} " + " | ".join(cmp))
    iv = olv.iv("m")
    if iv:
        print(f"  ctamap IV 快照 m：主力月{iv.exp} 平值隐波{iv.atmv_current}% 百分位{iv.atmv_percentile} "
              f"skew{iv.skew_current} rv22{iv.rv22}")
    # 新浪阶梯 + openvlab 到期日 → 纯 HTTP 单月真链（演示 sina+openvlab 组合备源）
    combo = SinaChainSource(month_resolver=olv.resolve_front).fetch_chain("m", "DCE")
    if combo:
        s = combo[0]
        print(f"  新浪阶梯+openvlab到期日 拼单月链：m -> {len(combo)} 个合约；"
              f"样例 {s.symbol} 月={s.underlying_month} 到期={s.expiry}")
    else:
        print(f"  新浪阶梯+openvlab到期日：m 未拼出（olv.last_error={olv.last_error}）")

    # ---- 降级编排：全 47 品种，看溯源分布 ----
    print("\n[降级编排] 全 47 品种按源优先级取链：")
    cs_all = fetch_d06(varieties, today, repo=repo, verbose=False)
    print(f"  源分布：{cs_all.by_source()}")
    print(f"  总合约：{len(cs_all.all_contracts())} · MISSING 品种：{cs_all.manual_codes() or '无'}")
    tot = sum(p.n_contracts for p in cs_all.provenance.values())
    print(f"  逐品种合约数合计：{tot}")

    # ---- 真链喂 D07：出真实候选月（前 N=3 近月，DTE 20-180）----
    print("\n[接 D07] 真链灌入漏斗（DTE 20-180 · 前 3 近月）：")
    cand = fetch_d07(cs_all.all_contracts(), today, symbol_templates=fetch_d02())
    for code, exchange in varieties:
        ms = cand.months_for(code)
        desc = " ".join(f"{x.month4}(DTE{x.dte})" for x in ms) or "—无候选—"
        print(f"  {code:<3} {desc}")
    print(f"  （共 {len(cand)} 品种出候选月）")
    print("=" * 72)
