"""D07 · 标的月份集合映射（品种 → 候选标的月，标主力）。

需求源 §6.1 D07 / §7.2 月份选择（用户拍板 2026-09-27）：
候选集 = 有挂牌期权 ∧ DTE ∈ [20, 180] ∧ 区间内按到期由近及远前 N=3 近月；
**主源由 D06 期权链反推**（零额外原生调用），get_main_instrument 仅供主力标记
（展示用：D07/D08 自检 ★・主力现价；D13 不消费——openvlab 自身即主力月口径），不作候选筛选；
生产链已接线——但原生调用**绝不走主进程**（未连接裸调 get_main_instrument 必 AV
0xC0000005，2026-10-02 实测），而是隔离子进程 _main_month_builder 构建
 data/main_month.json，本模块 provider 只读该缓存（当日缺失自动 spawn 重建・旧缓存短用）。

本单元是**纯漏斗逻辑**：输入 D06 产出的链清单（Iterable[ChainContract]），
输出每品种 ≤3 个候选月；tbpy 依赖全部走注入（main_provider），因此可完全离线
开发与自检——D06 落地后把真链灌进来即接通，逻辑零改动。

参数口径：D07Params 默认值即 §7.2 拍板值；阈值配置层（config SELLOPT_THRESHOLDS）落地后由调用方
注入替换，本单元不自行读 config。

运行自检：``python -m sell_option.fetchers.d07_candidate_months``（接 D06 全链，展示全品种候选月）
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

try:  # 包方式（python -m / import）：相对导入
    from .models import ChainContract, expand_month
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.models import ChainContract, expand_month


@dataclass(frozen=True)
class D07Params:
    """§7.2 拍板值；将来由阈值配置层 config 注入覆盖。边界含义：DTE 闭区间 [dte_min, dte_max]。"""

    dte_min: int = 20
    dte_max: int = 180
    top_n: int = 3


@dataclass(frozen=True)
class CandidateMonth:
    code: str
    exchange: str
    month4: str              # 标的月份，统一 4 位 "2701"
    expiry: dt.date          # 该月期权的最早到期（DTE 基准）
    dte: int
    is_main: bool            # 主力标记（仅路由/展示用，不参与候选判定）
    futures_symbol: str | None = None  # 注入 D02 模板时自动拼好，供 D08 直接取数


class CandidateSet:
    """D07 产物：品种 → 有序候选月（至多 N 个，按到期由近及远）。"""

    def __init__(self, by_code: dict[str, tuple[CandidateMonth, ...]]):
        self._by_code = by_code

    def months_for(self, code: str) -> tuple[CandidateMonth, ...]:
        return self._by_code.get(code, ())

    def codes(self) -> list[str]:
        return list(self._by_code)

    def __len__(self) -> int:
        return len(self._by_code)

    def __contains__(self, code: str) -> bool:
        return code in self._by_code


# 月份年代补全已上提至 models.expand_month（D07/D06 共用）；保留别名供本模块调用点
_expand_month = expand_month


def _default_no_main(code: str, exchange: str, today: dt.date) -> str | None:
    return None


def fetch_d07(
    chain: Iterable[ChainContract],
    today: dt.date,
    params: D07Params | None = None,
    main_provider: Callable[[str, str, dt.date], str | None] = _default_no_main,
    symbol_templates=None,  # D02 SymbolTemplates：传入则候选月自动带 futures_symbol
) -> CandidateSet:
    """跑三级漏斗。链中空集 → 空 CandidateSet（调用方按 §八 判 MISSING，不猜月份）。"""
    p = params or D07Params()
    # ① 按品种→标的月聚合，月级到期取该月期权最早到期（同月本应一致，保守取 min）
    month_expiry: dict[tuple[str, str], dt.date] = {}
    variety_exchange: dict[str, str] = {}
    for c in chain:
        month = _expand_month(c.underlying_month, today)
        key = (c.code, month)
        variety_exchange.setdefault(c.code, c.exchange)
        cur = month_expiry.get(key)
        month_expiry[key] = min(cur, c.expiry) if cur else c.expiry

    # ② DTE 闭区间过滤 + ③ 由近及远取前 N；主力标记仅附加、不过滤
    grouped: dict[str, list[CandidateMonth]] = defaultdict(list)
    for (code, month), expiry in month_expiry.items():
        dte = (expiry - today).days
        if not (p.dte_min <= dte <= p.dte_max):
            continue
        grouped[code].append(
            CandidateMonth(code=code, exchange=variety_exchange[code], month4=month,
                           expiry=expiry, dte=dte, is_main=False)
        )

    by_code: dict[str, tuple[CandidateMonth, ...]] = {}
    main_provider_used = main_provider is not _default_no_main
    for code, months in grouped.items():
        months.sort(key=lambda m: (m.expiry, m.month4))
        picked = months[: p.top_n]
        main_month = None
        if main_provider_used:
            raw = main_provider(code, picked[0].exchange, today)
            main_month = _expand_month(raw, today) if raw else None
        # 主力标记与 futures_symbol 拼接各自独立生效（互不依赖）
        by_code[code] = tuple(
            CandidateMonth(**{**m.__dict__,
                              "is_main": main_month is not None and m.month4 == main_month,
                              "futures_symbol": _fut(code, m, symbol_templates)})
            for m in picked
        )
    return CandidateSet(dict(sorted(by_code.items())))


def _fut(code: str, m: CandidateMonth, templates) -> str | None:
    return templates.futures_symbol(code, m.exchange, m.month4) if templates is not None else None


# ---------------------------------------------------------------------------
# 主力月 provider：读隔离子进程构建的 data/main_month.json（主进程零原生调用）
# ---------------------------------------------------------------------------
_MAIN_MONTH_PATH = Path(__file__).resolve().parents[1] / "data" / "main_month.json"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STALE_DAYS_OK = 7          # 主力月漂移慢（月内换 1–2 次），重建失败时近日旧缓存仍可用

_months_cache: dict[str, str | None] | None = None   # 进程级一次装载 {大写品种码: 主力月4位}
_months_build_attempted = False                       # 同进程至多 spawn 一次，失败不反复重试


def _read_main_month() -> dict | None:
    try:
        return json.loads(_MAIN_MONTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _spawn_main_month_builder() -> None:
    """原生调用扔子进程（堆/AV 随子进程销毁，同 _option_index_builder 隔离范式 §11.3.6）。"""
    try:
        subprocess.run([sys.executable, "-m", "sell_option.fetchers._main_month_builder"],
                       cwd=str(_PROJECT_ROOT), capture_output=True, text=True,
                       timeout=300, encoding="utf-8", errors="replace")
    except (subprocess.TimeoutExpired, OSError):
        pass   # 起不来（无 tbpy 环境/超时）→ 后续走旧缓存/全 None 降级


def _load_main_months(today: dt.date) -> dict[str, str | None]:
    """{大写品种码: 主力月4位}。今日缺失→spawn 子进程建一次；仍缺→复用 ≤7 天旧缓存；都没有→空（全不标）。"""
    global _months_cache, _months_build_attempted
    if _months_cache is not None:
        return _months_cache
    data = _read_main_month()
    if data is None or data.get("build_date") != today.isoformat():
        if not _months_build_attempted:
            _months_build_attempted = True
            # 周末休市，主力月不会切换，直接复用旧缓存、不 spawn tbpy 子进程
            if today.weekday() >= 5 and data is not None:
                pass
            else:
                _spawn_main_month_builder()
                data = _read_main_month()
    if data is not None:
        try:
            stale = (today - dt.date.fromisoformat(data.get("build_date") or "")).days
        except ValueError:
            stale = 999
        if 0 <= stale <= _STALE_DAYS_OK:
            _months_cache = {str(k).upper(): (e or {}).get("month")
                             for k, e in (data.get("months") or {}).items()}
            return _months_cache
    _months_cache = {}
    return _months_cache


def main_month_via_tbpy(code: str, exchange: str, today: dt.date) -> str | None:
    """主力月查询（D07 is_main 展示标记）：读 _main_month_builder 子进程落盘的缓存。
    主进程绝不调 get_main_instrument（未连接裸调实测必 AV）；拿不到返回 None（不标、不阻断）。"""
    return _load_main_months(today).get((code or "").upper())


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    # ---- 真实数据：接 D06 全链，展示全 47 品种候选月 ----
    print("=" * 72)
    print("[真实数据] 接 D06 全链 → D07 漏斗（DTE 20-180 · 前 3 近月）")
    sys.stdout.flush()

    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.d02_symbol_template import fetch_d02
    from sell_option.fetchers.d06_option_chain import fetch_d06, OptionIndexRepo

    today_real = dt.date.today()
    varieties = [(v.code, v.exchange) for v in fetch_d01()]
    repo = OptionIndexRepo()
    repo.ensure_fresh(today_real, verbose=False)
    cs_all = fetch_d06(varieties, today_real, repo=repo, verbose=False)
    templates = fetch_d02()
    cand = fetch_d07(cs_all.all_contracts(), today_real, symbol_templates=templates,
                     main_provider=main_month_via_tbpy)  # 实接 tbpy 主力月（与生产链 assemble 同源，★才真会亮）

    sys.stdout.flush()
    print(f"品种 {len(varieties)} 个 · 候选品种 {len(cand)} 个")
    print(f"格式：品种 交易所  候选月(DTE)→期货合约/到期日")
    print("-" * 100)
    for code, exchange in varieties:
        ms = cand.months_for(code)
        if not ms:
            print(f"{code:<4} {exchange:<5}  —无候选—")
            continue
        parts = []
        for x in ms:
            main_tag = "★" if x.is_main else " "
            parts.append(f"{main_tag}{x.month4}(DTE{x.dte})→{x.futures_symbol or '?'}/{x.expiry}")
        print(f"{code:<4} {exchange:<5}  {'  '.join(parts)}")
    print("-" * 100)
    print(f"共 {len(cand)} 品种出候选月（★=主力）")
    print("=" * 72)
