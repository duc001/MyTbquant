"""D08 · 标的现价（每个候选标的月真实合约各一份，不限主力月）。

需求源 §6.1 D08 / §7.1 #5 安全垫 / §7.3 破位对照价（用户拍板 2026-09-27）：
- **绝对价**，权威源 **tbpy**，铁律「绝不用指数价替代」——串价铁律只禁"主连指数价冒充真实
  合约价"；本单元只取【D06 候选标的月的真实合约】价（futures_symbol），永不碰 888/9888 指数。
- 每个候选月各一份现价；#5 安全垫的预期移动、§7.3 破位对照价都以"持仓所属标的月真实合约现价"为准。

**双路取数（2026-09-27 周日非盘中实测确立）**：
- 盘中：``get_current_tick(futures_symbol).last``（实时；tick 无 60s 限流）。
- 盘后/非交易时段：``get_current_tick`` 返回 **None**（实测，**纠正需求文档 §11.3.5"返回最近快照"的错误说法**），回退【同一真实合约】
  ``get_history_n('1d', count=1).close``——它仍是 tbpy 真实合约绝对价、**不违串价铁律**
  （实测 m2611.DCE 盘后 close=3409.0 可得，时点=上一交易日 2026-09-24）。
- 两路皆无 → 该合约 **MISSING**（绝不用指数价 / 代理数据冒充，守 §八 四态原则）。

**限流实测（决定盘后批量形态）**：``get_history_n`` 单 symbol 调用是「1 次成功 / 60s」（24 连发
只第 1 个成功、其余空、err='间隔=60000ms,合约数量=20'）；但 symbol 传【列表】可一次携带 ≤20
合约、算 1 次查询（实测 5 合约一次全返回、结构为嵌套 ``{symbol:{field:arr}}``、get_last_err 空）。
故盘后按 **≤20/批 + 批间 62s 冷却** 拉：47 品种×3 候选月≈141 合约 ≈8 批≈8 分钟；配
``data/spot.db``（sqlite3）磁盘缓存，当日重跑秒回（tick 实时价不缓存）。

运行自检：``python -m sell_option.fetchers.d08_spot_price``（真实数据：D01→D02→D06→D07→D08 全链，
双路取现价展示全 47 品种；盘后限流 ≤20/批＋62s，spot.db 当日重跑秒回）

TODO · 备源（2026-10-01 用户提出）：
  当前 tbpy 不可用且无缓存时，全部现价 MISSING → 策略无法运行。
  待实现：新浪期货行情备源（真实合约盘口 last），需验证口径与 tbpy 一致后接入。
  约束：串价铁律仍适用——备源必须提供【真实合约】绝对价，不可用连续合约/指数替代。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

try:  # 包方式（python -m / import）：相对导入
    from .models import Verdict, to_date as _to_date
    from .d07_candidate_months import CandidateSet
    from ..logutil import get_logger
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.models import Verdict, to_date as _to_date
    from sell_option.fetchers.d07_candidate_months import CandidateSet
    from sell_option.logutil import get_logger

logger = get_logger(__name__)

try:  # 全局串行令牌：跨取数器遵守 get_history_n 60s 限流，避免互相挤掉额度
    from ._history_gate import gated_get_history_n
except ImportError:
    from sell_option.fetchers._history_gate import gated_get_history_n

try:  # 取数归属交易日共享工具（按 D15 日历回退到最近交易日）
    from ._trading_day import pull_date_for
except ImportError:
    from sell_option.fetchers._trading_day import pull_date_for

# 盘后日线收盘缓存（sqlite3 单文件，按日 keyed）
DEFAULT_SPOT_DB = Path(__file__).resolve().parents[1] / "data" / "spot.db"
BATCH_MAX = int(os.getenv("SELLOPT_D08_BATCH", "20"))        # get_history_n 单次调用合约上限（实测 20）
COOLDOWN = float(os.getenv("SELLOPT_D08_COOLDOWN", "62"))    # 两批 history 间隔（限流 60s + 余量）

_SRC_LABEL = {"tick": "盘中tick", "daily_close": "日线收盘", "none": "无"}


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SpotQuote:
    """单个候选标的月真实合约的现价快照。"""

    code: str
    month4: str                     # 标的月份（4 位）
    futures_symbol: str | None      # 真实合约 symbol（如 m2611.DCE）；None=无法取数
    price: float | None             # 现价（绝对价）
    source: str                     # tick / daily_close / none
    bar_date: dt.date | None        # 价格所属交易日（bar_date＝该 bar 真实交易日；tick=当日；日线收盘=该 bar 日期）
    state: Verdict                  # PASS（拿到价）/ MISSING（拿不到，不冒充）
    reason: str
    is_main: bool = False           # 主力标记（仅展示，随 D06 透传）


@dataclass
class SpotSet:
    """fetch_d08 产物：品种 → 标的月 → 现价。供 #5 安全垫、#7/#8/#9、§7.3 破位取用。"""

    quotes: dict[str, dict[str, SpotQuote]] = field(default_factory=dict)

    def get(self, code: str, month4: str) -> SpotQuote | None:
        return self.quotes.get(code, {}).get(month4)

    def price_of(self, code: str, month4: str) -> float | None:
        q = self.get(code, month4)
        return q.price if q else None

    def quotes_for(self, code: str) -> list[SpotQuote]:
        d = self.quotes.get(code, {})
        return [d[k] for k in sorted(d)]

    def codes(self) -> list[str]:
        return list(self.quotes)

    def all_quotes(self) -> list[SpotQuote]:
        return [q for d in self.quotes.values() for q in d.values()]

    def distribution(self) -> dict[str, int]:
        agg: dict[str, int] = {}
        for q in self.all_quotes():
            agg[q.state.value] = agg.get(q.state.value, 0) + 1
        return agg

    def manual(self) -> list[SpotQuote]:
        return [q for q in self.all_quotes() if q.state == Verdict.MISSING]

    def __len__(self) -> int:
        return sum(len(d) for d in self.quotes.values())


# ---------------------------------------------------------------------------
# 数据源契约（tbpy 为唯一实现：串价铁律「绝对价一律 tbpy」）
# ---------------------------------------------------------------------------
@runtime_checkable
class SpotSource(Protocol):
    """D08 现价源契约：盘中 tick + 盘后日线 close 双路取数。"""

    def is_trading(self) -> bool: ...
    def tick(self, symbol: str) -> SpotQuote | None: ...
    def tick_batch(self, symbols: list[str]) -> dict[str, dict]: ...
    def close_batch(self, symbols: list[str]) -> dict[str, tuple[float | None, dt.date | None]]: ...


# ---------------------------------------------------------------------------
# 纯函数工具（离线可测）
# ---------------------------------------------------------------------------
def _chunk(seq, n: int) -> list[list]:
    """按 ≤n 切批（保序去重由调用方保证）；n<=0 时整体一批。"""
    items = list(seq)
    if n <= 0:
        return [items]
    return [items[i:i + n] for i in range(0, len(items), n)]


# _to_date 已统一到 models.to_date（见 sell_option/fetchers/models.py），此处不再重复定义。


def _parse_close_history(h: dict, chunk: list[str]) -> dict[str, tuple[float | None, dt.date | None]]:
    """解析 get_history_n 日线 close 返回 → {symbol: (close, bar_date)}。

    单 symbol → 扁平 {field:ndarray}；多 symbol → 嵌套 {symbol:{field:ndarray}}。
    """
    out: dict[str, tuple[float | None, dt.date | None]] = {}
    for sym in chunk:
        data = h.get(sym) if len(chunk) > 1 else h
        if data is None:
            continue
        close_arr = data.get("close")
        time_arr = data.get("time")
        if close_arr is None or not hasattr(close_arr, "__len__") or len(close_arr) == 0:
            continue
        close_val = close_arr[-1]
        bar_date_val = time_arr[-1] if time_arr is not None and hasattr(time_arr, "__len__") and len(time_arr) > 0 else None
        if close_val is not None:
            try:
                out[sym] = (float(close_val), _to_date(bar_date_val))
            except (TypeError, ValueError):
                pass
    return out


# ---------------------------------------------------------------------------
# 源：tbpy（唯一实现——串价铁律「绝对价一律 tbpy」）
# ---------------------------------------------------------------------------
class TbpySpotSource:
    """tbpy 现价源：盘中 tick.last / 盘后 get_history_n close 双路。

    懒连接（首次用到 tbpy 才 connect，离线测试不触达）；用毕 close() 断开。
    限流：get_history_n 1 次/60s，symbol 传列表 ≤20/批、批间 62s 冷却。"""

    def __init__(self, probe_symbol: str = "m2611.DCE",
                 market_open: bool | None = None):
        self.probe_symbol = probe_symbol
        self._market_open = market_open
        self._conn = False
        self._tbpy = None
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._conn

    def _connect(self) -> bool:
        if self._conn:
            return True
        try:
            import tbpy
            import config
            from runtime import connect
        except Exception as e:
            self.last_error = f"导入 tbpy 失败：{type(e).__name__}: {e}"
            return False
        self._tbpy = tbpy
        if connect(config.PRODUCT_KEY):
            self._conn = True
            return True
        # connect 返回 False 可能是「已初始化」（本进程已 init 过 tbpy，如 market_session 先连）
        # → tbpy 其实可用（connect 返回 False 但含「已初始化」字样），按已连接处理
        try:
            if "已初始化" in (tbpy.get_last_err() or ""):
                self._conn = True
                return True
        except Exception:
            pass
        self.last_error = "tbpy 未连接（请先启动并登录 TBQuant3 客户端）"
        return False

    def close(self) -> None:
        if self._conn:
            try:
                from runtime import disconnect
                disconnect()
            except Exception:
                pass
            self._conn = False

    def is_trading(self) -> bool:
        """盘中判定：编排层已探明（_market_open 非 None）则直接采信；否则自探。"""
        if self._market_open is not None:
            return self._market_open
        # 本地时段判定不依赖 tbpy 连接：连不上不应误判休市，故先本地判、再按需连接
        try:
            from sell_option.fetchers.calendar_cache import is_closed_date
            import datetime as _dt
            if is_closed_date(_dt.date.today()):
                return False
            from sell_option.fetchers.session_hours import is_trading_time
            if not is_trading_time(self.probe_symbol):
                return False
        except Exception:
            return False
        if not self._connect():
            return False
        return True

    def tick(self, symbol: str) -> dict | None:
        """盘中取单合约 tick → {last, bar_date, snapshot_time}；拿不到 → None。

        实测（2026-10-14 沪金夜盘）：``get_current_tick`` 必须传 **list**——
        传单个字符串会**静默返回 None**（不报错）。故单合约也走批量。
        """
        return self.tick_batch([symbol]).get(symbol)

    def tick_batch(self, symbols: list[str]) -> dict[str, dict]:
        """盘中批量取 tick → {symbol: {last, bar_date, snapshot_time}}。

        实测要点：
        - 必须传 list；传单个字符串返回 None 且不报错。
        - 返回条数 **≤ 传入数**：平台只回【有实时行情】的合约，缺席者不报错，
          按 MISSING 处理（绝不拿其它价冒充）。
        - 实时查询同样限流：1 次 / 60s、单次 ≤20 合约（超限报
          ``查询实时失败,查询限制,间隔=60000ms,合约数量=20``）→ ≤20/批 + 批间 62s。
        """
        out: dict[str, dict] = {}
        syms = list(dict.fromkeys(s for s in symbols if s))
        if not syms or not self._connect():
            return out
        tbpy = self._tbpy
        for i, chunk in enumerate(_chunk(syms, BATCH_MAX)):
            if i > 0:
                time.sleep(COOLDOWN)
            try:
                r = tbpy.get_current_tick(symbol=chunk)
            except Exception as e:
                self.last_error = f"get_current_tick 异常：{type(e).__name__}: {e}"
                continue
            if not r:
                self.last_error = tbpy.get_last_err() or None
                continue
            for t in r:
                if t is None:
                    continue
                sym = getattr(t, "symbol", None)
                last = getattr(t, "last", None)
                if sym is None or last is None:
                    continue
                tick_time = getattr(t, "time", None)
                out[sym] = {
                    "last": float(last),
                    "bar_date": _to_date(tick_time) if tick_time is not None else dt.date.today(),
                    "snapshot_time": tick_time,
                }
        return out

    def close_batch(self, symbols: list[str]) -> dict[str, tuple[float | None, dt.date | None]]:
        """盘后批量取日线 close（≤20/批 + 62s 冷却）。"""
        out: dict[str, tuple[float | None, dt.date | None]] = {}
        syms = list(dict.fromkeys(s for s in symbols if s))
        if not syms or not self._connect():
            return out
        tbpy = self._tbpy
        for i, chunk in enumerate(_chunk(syms, BATCH_MAX)):
            if i > 0:
                time.sleep(COOLDOWN)
            try:
                r = gated_get_history_n(
                    tbpy,
                    chunk if len(chunk) > 1 else chunk[0],
                    "1d", 1, fields=["close", "time"], timeout="30s")
            except Exception as e:
                self.last_error = f"get_history_n 异常：{type(e).__name__}: {e}"
                continue
            if not r:
                self.last_error = tbpy.get_last_err() or None
                continue
            out.update(_parse_close_history(r, chunk))
        return out


# ---------------------------------------------------------------------------
# 缓存仓库（sqlite3 单文件，当日幂等写入）
# ---------------------------------------------------------------------------
class SpotCacheRepo:
    """data/spot.db 缓存：主键 (symbol, pull_date)，严格按 (symbol, pull_date) 查询。

    pull_date＝取数归属交易日（pull_date_for，不变量，做查询键）；
    bar_date＝该 bar 真实交易日（仅元数据，不参与查询）。脏数据不迁移，整表丢弃重建即可。
    """

    def __init__(self, db_path: Path = DEFAULT_SPOT_DB):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as cn:
            cn.execute("""
                CREATE TABLE IF NOT EXISTS spot_price (
                    symbol TEXT NOT NULL,
                    pull_date TEXT NOT NULL,
                    price REAL,
                    source TEXT,
                    bar_date TEXT,
                    PRIMARY KEY (symbol, pull_date)
                )
            """)
            # 老库迁移：PK bar_date -> pull_date、meta pull_date -> bar_date（旧列名互换；值相等，ALTER 安全）
            try:
                cols = {r[1] for r in cn.execute("PRAGMA table_info(spot_price)")}
                if "bar_date" in cols and "pull_date" not in cols:
                    cn.execute("ALTER TABLE spot_price RENAME COLUMN bar_date TO pull_date_tmp")
                    cn.execute("ALTER TABLE spot_price RENAME COLUMN pull_date TO bar_date")
                    cn.execute("ALTER TABLE spot_price RENAME COLUMN pull_date_tmp TO pull_date")
            except Exception:
                pass

    def load(self, symbol: str, pull_date: dt.date) -> dict | None:
        """加载指定 (symbol, pull_date) 缓存；无 → None。"""
        with sqlite3.connect(self.db_path) as cn:
            row = cn.execute(
                "SELECT price, source, bar_date FROM spot_price WHERE symbol=? AND pull_date=?",
                (symbol, pull_date.isoformat())).fetchone()
        if row is None:
            return None
        return {"price": row[0], "source": row[1], "bar_date": row[2]}

    def save(self, symbol: str, pull_date: dt.date, price: float | None,
             source: str, bar_date: dt.date | None):
        """幂等写入（REPLACE）。key=pull_date（交易日·不变量）；bar_date=该 bar 真实交易日（仅元数据）。"""
        with sqlite3.connect(self.db_path) as cn:
            cn.execute(
                "INSERT OR REPLACE INTO spot_price (symbol, pull_date, price, source, bar_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (symbol, pull_date.isoformat(), price, source,
                 bar_date.isoformat() if bar_date else None))


# ---------------------------------------------------------------------------
# 编排器：fetch_d08
# ---------------------------------------------------------------------------
def fetch_d08(candidates: "CandidateSet", today: dt.date, *,
              source: SpotSource | None = None,
              cache: SpotCacheRepo | None = None,
              market_open: bool | None = None,
              verbose: bool = False) -> SpotSet:
    """D08 标的现价：每个候选标的月真实合约各一份（双路：盘中 tick / 盘后日线 close）。

    流程：先查缓存 → 缺的走 tbpy（盘中 tick / 盘后 close）→ 回写缓存 → 组装 SpotSet。
    缓存命中 + tbpy 成功 → PASS；两路皆无 → MISSING（绝不冒充）。
    """
    repo = cache or SpotCacheRepo()
    result = SpotSet()

    # 收集所有需要取价的 (code, month, futures_symbol, is_main)
    items: list[tuple[str, str, str | None, bool]] = []
    for code in candidates.codes():
        for m in candidates.months_for(code):
            items.append((code, m.month4, m.futures_symbol, m.is_main))

    # 解析取数归属交易日（pull_date＝价格所属交易日，不变量）：问 D15 日历"最近交易日"
    pull_date = pull_date_for(today)

    # 先查缓存（仅非盘中：按归属交易日严格查；盘中实时价不缓存、强制刷新 tick）
    cached: dict[str, dict] = {}
    if market_open is not True:
        for code, month4, fsym, is_main in items:
            if not fsym:
                continue
            rec = repo.load(fsym, pull_date)
            if rec is not None:
                cached[fsym] = rec

    # 缺的走 tbpy
    missing_syms = list(dict.fromkeys(
        fsym for _, _, fsym, _ in items if fsym and fsym not in cached))

    fetched: dict[str, tuple[float | None, dt.date | None, str]] = {}
    if missing_syms:
        src = source or TbpySpotSource(market_open=market_open)
        try:
            is_live = src.is_trading()
            if is_live:
                # 盘中：批量 tick（必须传 list；≤20/批 + 批间 62s）
                tick_data = src.tick_batch(missing_syms)
                for sym, t in tick_data.items():
                    fetched[sym] = (t["last"], t["bar_date"], "tick")
            else:
                # 盘后：批量日线 close
                close_data = src.close_batch(missing_syms)
                for sym, (price, src_bar_date) in close_data.items():
                    fetched[sym] = (price, src_bar_date, "daily_close")
        finally:
            if hasattr(src, "close"):
                src.close()

        # 回写缓存：key 固定用 pull_date（交易日·不变量）；bar_date 仅记该 bar 真实交易日（元数据，不参与查询）
        for sym, (price, bar_date_pull, src_label) in fetched.items():
            if src_label == "tick":
                # 盘中实时盘口不落库（与「盘中不缓存」口径一致），但写入内存 cached 供下方 SpotSet 组装取价
                cached[sym] = {"price": price, "source": src_label,
                               "bar_date": _to_date(bar_date_pull).isoformat() if _to_date(bar_date_pull) else None}
                continue
            repo.save(sym, pull_date, price, src_label, _to_date(bar_date_pull))
            cached[sym] = {"price": price, "source": src_label,
                           "bar_date": _to_date(bar_date_pull).isoformat() if _to_date(bar_date_pull) else None}

    # 组装 SpotSet
    for code, month4, fsym, is_main in items:
        if not fsym:
            result.quotes.setdefault(code, {})[month4] = SpotQuote(
                code=code, month4=month4, futures_symbol=None,
                price=None, source="none", bar_date=None,
                state=Verdict.MISSING, reason="无 futures_symbol（D02 模板缺失）",
                is_main=is_main)
            continue
        rec = cached.get(fsym)
        if rec is not None and rec.get("price") is not None:
            result.quotes.setdefault(code, {})[month4] = SpotQuote(
                code=code, month4=month4, futures_symbol=fsym,
                price=rec["price"], source=rec.get("source", "cache"),
                bar_date=pull_date,
                state=Verdict.PASS,
                reason=f"{rec.get('source', 'cache')} @ {pull_date.isoformat()}",
                is_main=is_main)
        else:
            result.quotes.setdefault(code, {})[month4] = SpotQuote(
                code=code, month4=month4, futures_symbol=fsym,
                price=None, source="none", bar_date=None,
                state=Verdict.MISSING, reason=f"{fsym} 两路皆无",
                is_main=is_main)

    # 源分布（缓存/盘中tick/日线收盘）统计，便于排查看数（如限流导致拿不到）
    src_break: dict[str, int] = {}
    for q in result.all_quotes():
        src_break[q.source] = src_break.get(q.source, 0) + 1
    logger.info("D08 现价取数完成 · 品种=%d · 报价=%d · 源分布=%s · 状态=%s",
                len(candidates.codes()), len(result), src_break, result.distribution())
    return result


# ---------------------------------------------------------------------------
# CLI 自检（真实数据）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 72)
    print("[真实数据] D08 标的现价（双路：盘中 tick / 盘后日线 close）")
    print("=" * 72)

    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.d02_symbol_template import fetch_d02
    from sell_option.fetchers.d06_option_chain import fetch_d06, OptionIndexRepo
    from sell_option.fetchers.d07_candidate_months import fetch_d07, main_month_via_tbpy

    today_real = dt.date.today()
    varieties = [(v.code, v.exchange) for v in fetch_d01()]
    repo = OptionIndexRepo()
    repo.ensure_fresh(today_real, verbose=False)
    cs_all = fetch_d06(varieties, today_real, repo=repo, verbose=False)
    templates = fetch_d02()
    cand = fetch_d07(cs_all.all_contracts(), today_real, symbol_templates=templates,
                     main_provider=main_month_via_tbpy)  # 实接主力月：下方 main_q 展示项才有真值

    spots = fetch_d08(cand, today_real, verbose=True)

    dist = spots.distribution()
    print(f"\n品种 {len(varieties)} 个 · 现价 {len(spots)} 个 · 分布 {dist}")
    print(f"格式：品种  现价(源)@日期  候选月现价…")
    print("-" * 100)
    for code, exchange in varieties:
        qs = spots.quotes_for(code)
        if not qs:
            print(f"{code:<4} {exchange:<5}  —无现价—")
            continue
        parts = []
        for q in qs:
            if q.state == Verdict.PASS:
                src_tag = "T" if q.source == "tick" else "C"
                parts.append(f"{q.month4}={q.price:g}({src_tag}@{q.bar_date})")
            else:
                parts.append(f"{q.month4}=MISS")
        main_q = next((q for q in qs if q.is_main), None)
        main_price = f"{main_q.price:g}" if main_q and main_q.price else "—"
        print(f"{code:<4} {exchange:<5}  主力={main_price}  {'  '.join(parts)}")
    print("-" * 100)
    print(f"共 {len(spots)} 个现价（T=tick盘中 / C=close日线收盘）")
    print("=" * 72)
