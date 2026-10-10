"""D12 · 主连指数日线（后复权 RolloverBackWard，≥250 条）。

需求源 §6.1 D12 / §7.1 #3 市况 / #10 否决信号 / #5 ATR 对账（用户拍板 2026-09-27）：
- **相对指标**（两层价格体系的"连续指数"层），权威源 tbpy ``get_history_n + QuoteFlag.RolloverBackWard``（后复权）。
- 取数对象 = D03 品种→888/9888 主连指数 symbol（每品种一份，默认 47 品种全表）。
- 供 #3（ADX＋20 日斜率判震荡/趋势）· #10（连 5 同向 / 真实跳空 / 区间贴边 / ATR 扩张否决，
  **跳空必须在后复权序列上判**：后复权消除换月台阶、只留真实跳空）· #5（ATR 仅对账）。

**串价铁律的反面（§一.9 头号缺陷，务必牢记）**：D12 是连续指数后复权值，**只用于相对指标**
（收益率/波动率/趋势/ADX/ATR/区间/跳空），**绝不能当绝对价**——实测 m9888.DCE 后复权末值 ≈14798，
而真实合约 m2611.DCE ≈3409（≈4.3 倍）；混用即 bug。绝对价一律走 D08（真实合约现价）。

**API（tbpy教程 §4.1.1 权威 + 2026-09-27 探针实测）**：
- ``get_history_n(symbol, frequency, count, flag=QuoteFlag.Null, fields, timeout)``；symbol 可传【列表】批量。
- ``flag=QuoteFlag.RolloverBackWard`` 后复权——实测确被应用（m9888 RBW 首/中/末=12966/13750/14798，
  而 flag=Null 为 3054/3029/3426，两者迥异）。
- 返回：symbol=str→扁平 ``{field:ndarray}``；symbol=list→嵌套 ``{symbol:{field:ndarray}}``。
  fields 取值 open/close/high/low/volume/position/rollover/time。
- 实测批量：5 品种一次返回各 260 bars、volume 有值、get_last_err 空、耗时 0.3s。

**限流（复用 D08 实测结论）**：get_history_n 是「1 次成功 / 60s」，但 symbol 传列表一次 ≤20 合约算
1 次查询。故按 **≤20/批 + 批间 62s 冷却**：47 品种 ≈3 批 ≈2–3 分钟；配 ``data/daily.db``（sqlite3）
当日缓存，重跑秒回。盘中运行时最后一根是当日未收盘的形成中 bar（bar_date==today），#3/#10 日线指标
应基于已收盘 bar，下游可据 bar_date 与交易时段自行裁剪——D12 只忠实取数不裁剪。

运行自检：``python -m sell_option.fetchers.d12_index_daily``（离线假源验证批量/缓存/四态/纯函数/DB 往返；
tbpy 在线则拉 5 品种演示后复权日线）
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
    from .models import Verdict
    from .d03_index_symbol import IndexSymbolTable, fetch_d03
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.models import Verdict
    from sell_option.fetchers.d03_index_symbol import IndexSymbolTable, fetch_d03

try:  # 全局串行令牌：跨取数器遵守 get_history_n 60s 限流，避免互相挤掉额度
    from ._history_gate import gated_get_history_n
except ImportError:
    from sell_option.fetchers._history_gate import gated_get_history_n

try:  # 取数归属交易日共享工具（按 D15 日历回退到最近交易日）
    from ._trading_day import pull_date_for
except ImportError:
    from sell_option.fetchers._trading_day import pull_date_for

# 后复权主连日线缓存（sqlite3 单文件，与 spot.db / premium.db 统一模式）
DEFAULT_DAILY_DB = Path(__file__).resolve().parents[1] / "data" / "daily.db"
BATCH_MAX = int(os.getenv("SELLOPT_D12_BATCH", "20"))     # get_history_n 单次调用合约上限（实测 20）
COOLDOWN = float(os.getenv("SELLOPT_D12_COOLDOWN", "62"))  # 两批 history 间隔（限流 60s + 余量）
BARS = int(os.getenv("SELLOPT_D12_BARS", "300"))          # 拉取 bar 数（目标 ≥250，留余量）
MIN_BARS = int(os.getenv("SELLOPT_D12_MIN", "60"))        # PASS 最少 bar（够 ADX14+ATR14+斜率20+区间）
FIELDS = ["time", "open", "high", "low", "close", "volume"]
_OHLCV = ["open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IndexSeries:
    """单个品种主连指数的后复权日线序列（相对指标专用，绝不当绝对价）。"""

    code: str
    index_symbol: str | None      # D03 指数 symbol（如 m9888.DCE）；None=品种不在 D03
    dates: tuple                  # tuple[dt.date|None, ...] 升序
    open: tuple
    high: tuple
    low: tuple
    close: tuple
    volume: tuple                 # 指数 volume 可能缺失 → 统一 0.0 兜底
    source: str                   # tbpy_rbw（新拉）/ cache（当日缓存）/ none
    bar_date: dt.date | None      # 最新 bar 日期（bar_date＝该 bar 真实交易日）
    state: Verdict                # PASS（≥MIN_BARS）/ WARN（不足）/ MISSING（拿不到）
    reason: str
    adjusted: bool = True         # 后复权已应用（RolloverBackWard）

    @property
    def count(self) -> int:
        return sum(1 for c in self.close if c is not None)

    def tail(self, n: int = 5) -> list[tuple]:
        """末尾 n 根 (date, open, high, low, close, volume)，便于核对 / 下游取近段。"""
        idx = list(range(len(self.close)))[-n:]
        return [(self.dates[i], self.open[i], self.high[i], self.low[i],
                 self.close[i], self.volume[i]) for i in idx]


@dataclass
class IndexDailySet:
    """fetch_d12 产物：品种 → 后复权主连日线序列。供 #3 市况 / #10 否决 / #5 ATR 对账取用。"""

    series: dict[str, IndexSeries] = field(default_factory=dict)

    def get(self, code: str) -> IndexSeries | None:
        return self.series.get(code)

    def codes(self) -> list[str]:
        return list(self.series)

    def all_series(self) -> list[IndexSeries]:
        return list(self.series.values())

    def distribution(self) -> dict[str, int]:
        agg: dict[str, int] = {}
        for s in self.series.values():
            agg[s.state.value] = agg.get(s.state.value, 0) + 1
        return agg

    def manual(self) -> list[IndexSeries]:
        return [s for s in self.series.values() if s.state == Verdict.MISSING]

    def __len__(self) -> int:
        return len(self.series)


# ---------------------------------------------------------------------------
# 数据源契约（tbpy 为唯一实现：相对指标建在后复权连续指数上）
# ---------------------------------------------------------------------------
@runtime_checkable
class IndexDailySource(Protocol):
    """D12 日线源契约：批量取后复权主连日线。"""

    def history_batch(self, symbols: list[str], count: int,
                      fields: list[str]) -> dict[str, dict[str, list]]: ...


# ---------------------------------------------------------------------------
# 纯函数工具（离线可测）
# ---------------------------------------------------------------------------
def _chunk(seq, n: int) -> list[list]:
    """按 ≤n 切批（保序去重由调用方保证）；n<=0 时整体一批。"""
    items = list(seq)
    if n <= 0:
        return [items]
    return [items[i:i + n] for i in range(0, len(items), n)]


def _parse_history(h: dict, chunk: list[str],
                   fields: list[str]) -> dict[str, dict[str, list]]:
    """解析 get_history_n 日线返回 → {symbol: {field: list}}。

    单 symbol → 扁平 {field:ndarray}；多 symbol → 嵌套 {symbol:{field:ndarray}}。
    ndarray 转 list 便于后续序列化 / 缓存。
    """
    out: dict[str, dict[str, list]] = {}
    for sym in chunk:
        data = h.get(sym) if len(chunk) > 1 else h
        if data is None:
            continue
        rec: dict[str, list] = {}
        for f in fields:
            arr = data.get(f)
            if arr is not None and hasattr(arr, "__len__"):
                # time 列是 numpy datetime64/Timestamp——不成 str 则 json 落盘必炸（不可序列化，实测）
                rec[f] = [str(x) if x is not None else None for x in arr] if f == "time" else list(arr)
            else:
                rec[f] = []
        if rec:
            out[sym] = rec
    return out


def _series_from_rec(code: str, sym: str, rec: dict[str, list],
                     src_label: str) -> IndexSeries:
    """从解析后的 {field: list} 构建 IndexSeries。"""
    dates = tuple(rec.get("time", []))
    op = tuple(rec.get("open", []))
    hi = tuple(rec.get("high", []))
    lo = tuple(rec.get("low", []))
    cl = tuple(rec.get("close", []))
    vol = tuple(0.0 if v is None else v for v in rec.get("volume", []))
    n = sum(1 for c in cl if c is not None)
    bar_date = next((d for d in reversed(dates) if d), None)

    if n >= MIN_BARS:
        state, reason = Verdict.PASS, f"{src_label} 日线 {n} 条（目标≥{BARS}）· bar_date {bar_date}"
    elif n > 0:
        state, reason = Verdict.WARN, f"{src_label} 日线仅 {n} 条（<{MIN_BARS}）· bar_date {bar_date}"
    else:
        state, reason = Verdict.MISSING, f"无法获取 {src_label} 日线"

    return IndexSeries(
        code=code, index_symbol=sym, dates=dates, open=op, high=hi, low=lo,
        close=cl, volume=vol, source=src_label, bar_date=bar_date,
        state=state, reason=reason, adjusted=True)


# ---------------------------------------------------------------------------
# 源：tbpy（唯一实现——后复权连续指数）
# ---------------------------------------------------------------------------
class TbpyIndexDailySource:
    """tbpy 日线源：get_history_n + QuoteFlag.RolloverBackWard 后复权。

    懒连接；限流 ≤20/批 + 62s 冷却。"""

    def __init__(self, market_open: bool | None = None):
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
        self.last_error = "tbpy 未连接"
        return False

    def close(self) -> None:
        if self._conn:
            try:
                from runtime import disconnect
                disconnect()
            except Exception:
                pass
            self._conn = False

    def history_batch(self, symbols: list[str], count: int,
                      fields: list[str]) -> dict[str, dict[str, list]]:
        """批量取后复权日线（≤20/批 + 62s 冷却）。"""
        out: dict[str, dict[str, list]] = {}
        syms = list(dict.fromkeys(s for s in symbols if s))
        if not syms or not self._connect():
            return out
        tbpy = self._tbpy
        # 尝试获取 RolloverBackWard flag
        try:
            flag = tbpy.QuoteFlag.RolloverBackWard
        except Exception:
            flag = None

        for i, chunk in enumerate(_chunk(syms, BATCH_MAX)):
            if i > 0:
                time.sleep(COOLDOWN)
            try:
                kwargs = dict(timeout="30s")
                if flag is not None:
                    kwargs["flag"] = flag
                r = gated_get_history_n(
                    tbpy,
                    chunk if len(chunk) > 1 else chunk[0],
                    "1d", count, fields=fields, **kwargs)
            except Exception as e:
                self.last_error = f"get_history_n 异常：{type(e).__name__}: {e}"
                continue
            if not r:
                self.last_error = tbpy.get_last_err() or None
                continue
            out.update(_parse_history(r, chunk, fields))
        return out


# ---------------------------------------------------------------------------
# 缓存仓库（sqlite3 单文件，当日幂等写入）
# ---------------------------------------------------------------------------
class DailyCacheRepo:
    """data/daily.db 缓存：主键 (symbol, pull_date)，严格按 (symbol, pull_date) 查询。"""

    def __init__(self, db_path: Path = DEFAULT_DAILY_DB):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as cn:
            cn.execute("""
                CREATE TABLE IF NOT EXISTS index_daily (
                    symbol TEXT NOT NULL,
                    pull_date TEXT NOT NULL,
                    bar_date TEXT,
                    data_json TEXT,
                    close_json TEXT,
                    source TEXT,
                    PRIMARY KEY (symbol, pull_date)
                )
            """)
            # 老库迁移：cache_date -> pull_date（列名重构，ALTER 安全、幂等）
            try:
                cols = {r[1] for r in cn.execute("PRAGMA table_info(index_daily)")}
                if "cache_date" in cols and "pull_date" not in cols:
                    cn.execute("ALTER TABLE index_daily RENAME COLUMN cache_date TO pull_date")
            except Exception:
                pass

    def load(self, symbol: str, date: dt.date) -> dict | None:
        """按 (symbol, pull_date) 严格加载缓存；无 → None。"""
        import json as _json
        with sqlite3.connect(self.db_path) as cn:
            row = cn.execute(
                "SELECT data_json, close_json, source, pull_date FROM index_daily "
                "WHERE symbol=? AND pull_date=?",
                (symbol, date.isoformat())).fetchone()
        if row is None:
            return None
        data = _json.loads(row[0]) if row[0] else {}
        close = _json.loads(row[1]) if row[1] else None
        return {**data, "close": close, "source": row[2], "pull_date": row[3]}

    def save(self, symbol: str, date: dt.date, rec: dict[str, list],
             source: str):
        """幂等写入。rec 为 {field: list} 格式。"""
        import json as _json
        close_list = rec.get("close", [])
        data_without_close = {k: v for k, v in rec.items() if k != "close"}
        with sqlite3.connect(self.db_path) as cn:
            cn.execute(
                "INSERT OR REPLACE INTO index_daily "
                "(symbol, pull_date, bar_date, data_json, close_json, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (symbol, date.isoformat(),
                 rec.get("time", [None])[-1] if rec.get("time") else None,
                 _json.dumps(data_without_close, default=str),
                 _json.dumps(close_list, default=str),
                 source))


# ---------------------------------------------------------------------------
# 编排器：fetch_d12
# ---------------------------------------------------------------------------
def fetch_d12(codes: list[str] | None, today: dt.date, *,
              index_table: "IndexSymbolTable | None" = None,
              source: IndexDailySource | None = None,
              cache: DailyCacheRepo | None = None,
              verbose: bool = False,
              use_cache: bool = True) -> IndexDailySet:
    """D12 后复权主连日线（≥250 条）。

    流程：先查缓存 → 缺的走 tbpy 后复权 → 回写缓存 → 组装 IndexDailySet。
    use_cache=False（refresh --all）→ 跳过缓存查询、全量强制重拉。
    """
    repo = cache or DailyCacheRepo()
    table = index_table or fetch_d03()
    result = IndexDailySet()
    codes = list(codes) if codes is not None else [c for c, _ in table.items()]  # None→D03 全表（refresh 语义）
    pull_date = pull_date_for(today)   # 取数归属交易日：严格按最近交易日查缓存

    # 构建 code → symbol 映射
    code_sym: list[tuple[str, str]] = []
    for code in codes:
        sym = table.get(code)
        if sym:
            code_sym.append((code, sym))

    # 先查缓存（use_cache=False 强制重拉时跳过）
    cached: dict[str, dict] = {}
    if use_cache:
        for code, sym in code_sym:
            rec = repo.load(sym, pull_date)
            if rec is not None and rec.get("close"):
                cached[sym] = rec

    # 缺的走 tbpy
    missing_syms = [sym for _, sym in code_sym if sym not in cached]
    if missing_syms:
        src = source or TbpyIndexDailySource()
        try:
            raw = src.history_batch(missing_syms, BARS, FIELDS)
            for sym, rec in raw.items():
                repo.save(sym, pull_date, rec, "tbpy_rbw")
                cached[sym] = {**rec, "source": "tbpy_rbw"}
        finally:
            if hasattr(src, "close"):
                src.close()

    # 组装 IndexDailySet
    for code, sym in code_sym:
        rec = cached.get(sym)
        if rec is not None and rec.get("close"):
            series = _series_from_rec(code, sym, rec, rec.get("source", "cache"))
        else:
            series = IndexSeries(
                code=code, index_symbol=sym,
                dates=tuple(), open=tuple(), high=tuple(), low=tuple(),
                close=tuple(), volume=tuple(),
                source="none", bar_date=None,
                state=Verdict.MISSING,
                reason=f"{sym} 无法获取日线", adjusted=False)
        result.series[code] = series

    # 无 symbol 的品种也填 MISSING
    sym_set = {sym for _, sym in code_sym}
    for code in codes:
        if code not in result.series:
            result.series[code] = IndexSeries(
                code=code, index_symbol=None,
                dates=tuple(), open=tuple(), high=tuple(), low=tuple(),
                close=tuple(), volume=tuple(),
                source="none", bar_date=None,
                state=Verdict.MISSING,
                reason=f"品种 {code} 不在 D03 指数表", adjusted=False)

    return result


# ---------------------------------------------------------------------------
# CLI 自检（真实数据）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 72)
    print("[真实数据] D12 后复权主连日线（data/daily.db 缓存）")
    print("=" * 72)

    today_real = dt.date.today()
    cache_real = DailyCacheRepo()
    table_real = fetch_d03()
    varieties_real = [(c, sym) for c, sym in table_real.items()]

    print(f"品种 {len(varieties_real)} 个 · today={today_real} · 严格 pull_date 模式")
    print(f"格式：品种  交易所  指数symbol  bars  首close  末close  pull_date")
    print("-" * 110)
    pull_date_real = pull_date_for(today_real)
    hit, miss = 0, 0
    for code, sym in varieties_real:
        exch = sym.split(".")[-1] if "." in sym else "?"
        rec = cache_real.load(sym, pull_date_real)
        if rec and rec.get("close"):
            hit += 1
            cl = rec["close"]
            dates = rec.get("time", [])
            first_idx = next((i for i, c in enumerate(cl) if c is not None), None)
            last_idx = next((i for i in reversed(range(len(cl))) if cl[i] is not None), None)
            first_close = f"{cl[first_idx]:.2f}" if first_idx is not None else "—"
            last_close = f"{cl[last_idx]:.2f}" if last_idx is not None else "—"
            bar_date = dates[last_idx] if last_idx is not None and last_idx < len(dates) else "?"
            print(f"{code:<4} {exch:<5} {sym:<14} {len(cl):>4}  {first_close:>10}  {last_close:>10}  {bar_date}")
        else:
            miss += 1
            print(f"{code:<4} {exch:<5} {sym:<14}    —无缓存—")
    print("-" * 110)
    print(f"共 {len(varieties_real)} 品种 · 缓存命中 {hit} · 缺失 {miss}")
    print("=" * 72)
