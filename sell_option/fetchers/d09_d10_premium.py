"""D09/D10 · 期权权利金（卖腿 / 保护腿·买一卖一），双路取数。

需求源 §6.1 D09/D10 / §7.1 #1 价差 · #8 净权利金 · #9 肥瘦 / §7.3 平仓估值（用户拍板 2026-09-27）：
- **绝对价**，权威源 **tbpy**，串价铁律「绝不用指数价替代」——本单元只取【真实期权合约】
  （ChainContract.symbol，如 m2611-C-3400.DCE）的权利金，永不碰 888/9888 指数。
- D09＝卖腿（short）、D10＝保护腿（protection）；两者**同一取数机制**，仅 role 标注不同，
  独立缺失、独立判（选腿在 §7.2 上游，本单元只负责"给定期权合约→权利金"）。

**双路取数（2026-09-27 周日非盘中实测确立，与 D08 同款）**：
- 盘中：``get_current_tick(option_symbol)`` → ``bidasks[0].bid_p/ask_p``（买一卖一）＋ ``last``；
  可算 mid / spread / spread_pct（供 #1 价差），tick 无 60s 限流。
- 盘后/非交易时段：``get_current_tick`` 返回 **None**（实测纠正需求文档 §11.3.5"返回最近快照"），
  回退【该期权合约自身】``get_history_n('1d', count=1).close``——实测 m2611-C-3250…3500、rb2611C
  系列 **10/10** 期权可得收盘权利金（如 m2611-C-3400=67.0），仍是 tbpy 真实合约绝对价、不违串价铁律。
- **权利金【值】**两路皆可回退（供 #8 净权利金 / #9 肥瘦 / 7.3 平仓估值）；但日线**无 bid/ask**，
  故【买卖价差】（#1 盘口价差）盘后无源 → ``has_book=False``，该子项由下游单独判 MISSING。
- 两路皆无 → 该合约 **MISSING**（绝不用代理数据冒充，守 §八 四态原则）。

**限流实测（决定盘后批量形态，同 D08/D12）**：``get_history_n`` 是「1 次成功 / 60s」，但 symbol
传【列表】可一次携带 ≤20 合约、算 1 次查询（返回嵌套 ``{symbol:{field:arr}}``）。故盘后按
**≤20/批 + 批间 62s 冷却** 拉；配 ``data/premium.db``（sqlite3）磁盘缓存，当日重跑秒回（tick 盘口不缓存）。

运行自检：``python -m sell_option.fetchers.d09_d10_premium``（离线用假源验证双路/盘口/缓存/分批/
解析/判定；tbpy 在线则再拉几个真期权演示盘后 close）
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

try:  # 包方式（python -m / import）：相对导入
    from .models import Verdict, to_date as _to_date
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.models import Verdict, to_date as _to_date

try:  # 全局串行令牌：跨取数器遵守 get_history_n 60s 限流，避免互相挤掉额度
    from ._history_gate import gated_get_history_n
except ImportError:
    from sell_option.fetchers._history_gate import gated_get_history_n

try:  # 取数归属交易日共享工具（按 D15 日历回退到最近交易日）
    from ._trading_day import pull_date_for
except ImportError:
    from sell_option.fetchers._trading_day import pull_date_for

# 盘后日线收盘权利金缓存（sqlite3 单文件，合并幂等）
DEFAULT_PREMIUM_DB = Path(__file__).resolve().parents[1] / "data" / "premium.db"
BATCH_MAX = int(os.getenv("SELLOPT_D10_BATCH", "20"))       # get_history_n 单次调用合约上限（实测 20）
COOLDOWN = float(os.getenv("SELLOPT_D10_COOLDOWN", "62"))   # 两批 history 间隔（限流 60s + 余量）

ROLE_SHORT = "short"              # 卖腿 → D09
ROLE_PROTECTION = "protection"    # 保护腿 → D10
_ROLE_LABEL = {ROLE_SHORT: "卖腿(D09)", ROLE_PROTECTION: "保护腿(D10)", None: "未指定腿"}
_SRC_LABEL = {"tick": "盘中tick盘口", "daily_close": "日线收盘", "none": "无"}


# ---------------------------------------------------------------------------
# 小工具：批量合并取数（combo_oi_close_batch）共用
# ---------------------------------------------------------------------------
# _to_date 已统一到 models.to_date（见 sell_option/fetchers/models.py），此处不再重复定义。


def _first_float(arr):
    """取数组末值转 float；空/异常 → None。"""
    if arr is None or not hasattr(arr, "__len__") or len(arr) == 0:
        return None
    try:
        return float(arr[-1])
    except (TypeError, ValueError):
        return None


def _first_int(arr):
    """取数组末值转非负 int（OI 不会为负）；空/异常/负 → None。"""
    if arr is None or not hasattr(arr, "__len__") or len(arr) == 0:
        return None
    try:
        v = int(arr[-1])
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PremiumQuote:
    """单个期权合约的权利金快照（D09 卖腿 / D10 保护腿共用；role 区分）。

    盘中（tick）：bid/ask/last/mid/spread/spread_pct 齐、has_book=True、premium=last→mid；
    盘后（日线 close）：仅 close/premium 有值、bid/ask/mid/spread=None、has_book=False。
    """

    symbol: str                   # 完整期权 symbol（真实合约，如 m2611-C-3400.DCE）
    code: str                     # 标的编码
    underlying_month: str         # 标的月份（4 位）
    cp: str                       # "C" / "P"
    strike: float                 # 行权价
    role: str | None              # short=卖腿(D09) / protection=保护腿(D10) / None
    # 盘中盘口（tick）——盘后全 None：
    bid: float | None             # 买一价
    ask: float | None             # 卖一价
    last: float | None            # 最新价
    mid: float | None             # (bid+ask)/2
    spread: float | None          # ask-bid（#1 价差分子）
    spread_pct: float | None      # spread/mid（#1 相对价差）
    # 盘后回退（日线收盘）：
    close: float | None           # 该期权合约最近交易日收盘权利金
    # 代表权利金值（盘中 last→mid；盘后 close）——供 #8/#9/7.3：
    premium: float | None
    source: str                   # tick / daily_close / none
    bar_date: dt.date | None      # 价格所属交易日（bar_date＝该 bar 真实交易日；tick=当日；日线收盘=该 bar 日期）
    has_book: bool                # bid/ask 是否齐（False → #1 价差无源、下游判 MISSING）
    state: Verdict                # PASS（拿到权利金值）/ MISSING（两路皆无，不冒充）
    reason: str

    @property
    def role_label(self) -> str:
        return _ROLE_LABEL.get(self.role, str(self.role))


@dataclass
class PremiumSet:
    """fetch_premium 产物：期权 symbol → 权利金。供 #1 价差、#8 净权利金、#9 肥瘦、7.3 平仓估值取用。"""

    quotes: dict[str, PremiumQuote] = field(default_factory=dict)

    def get(self, symbol: str) -> PremiumQuote | None:
        return self.quotes.get(symbol)

    def premium_of(self, symbol: str) -> float | None:
        q = self.quotes.get(symbol)
        return q.premium if q else None

    def by_role(self, role: str) -> list[PremiumQuote]:
        return [q for q in self.quotes.values() if q.role == role]

    def all_quotes(self) -> list[PremiumQuote]:
        return list(self.quotes.values())

    def codes(self) -> list[str]:
        seen: list[str] = []
        for q in self.quotes.values():
            if q.code not in seen:
                seen.append(q.code)
        return seen

    def distribution(self) -> dict[str, int]:
        agg: dict[str, int] = {}
        for q in self.quotes.values():
            agg[q.state.value] = agg.get(q.state.value, 0) + 1
        return agg

    def manual(self) -> list[PremiumQuote]:
        return [q for q in self.quotes.values() if q.state == Verdict.MISSING]

    def book_missing(self) -> list[PremiumQuote]:
        """拿到权利金值但无盘口（has_book=False）→ #1 价差须判 MISSING 的合约。"""
        return [q for q in self.quotes.values() if q.state == Verdict.PASS and not q.has_book]

    def __len__(self) -> int:
        return len(self.quotes)


# ---------------------------------------------------------------------------
# 数据源契约（tbpy 为唯一实现：串价铁律「绝对价一律 tbpy」）
# ---------------------------------------------------------------------------
@runtime_checkable
class PremiumSource(Protocol):
    """D09/D10 权利金源契约：盘中 tick 盘口 + 盘后日线 close 双路。"""

    def is_trading(self) -> bool: ...
    def tick_book(self, symbol: str) -> dict | None: ...
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


def _pos_float(v) -> float | None:
    """安全转非负浮点数：非数/负数/None → None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def _safe_attr(obj, name: str):
    """只读一次 boost 属性（§11.3.6 原生属性重复访问不稳）；异常→None。"""
    try:
        return getattr(obj, name)
    except Exception:  # noqa: BLE001
        return None


def _book_metrics(bid: float | None, ask: float | None,
                  last: float | None) -> dict:
    """从 bid/ask/last 计算 mid/spread/spread_pct/has_book/premium。"""
    has_book = (bid is not None and ask is not None
                and bid > 0 and ask > 0 and ask >= bid)
    mid = (bid + ask) / 2.0 if has_book else None
    spread = (ask - bid) if has_book else None
    spread_pct = (spread / mid) if (spread is not None and mid) else None
    premium = last if last is not None else mid
    return {"mid": mid, "spread": spread, "spread_pct": spread_pct,
            "has_book": has_book, "premium": premium}


def _parse_close_history(h: dict, chunk: list[str]) -> dict[str, tuple[float | None, dt.date | None]]:
    """解析 get_history_n 日线 close 返回 → {symbol: (close, bar_date)}。"""
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
        bar_date_val = (time_arr[-1] if time_arr is not None
                     and hasattr(time_arr, "__len__") and len(time_arr) > 0 else None)
        if close_val is not None:
            try:
                out[sym] = (float(close_val), bar_date_val)
            except (TypeError, ValueError):
                pass
    return out


# ---------------------------------------------------------------------------
# 源：tbpy（唯一实现——串价铁律「绝对价一律 tbpy」）
# ---------------------------------------------------------------------------
class TbpyPremiumSource:
    """tbpy 权利金源：盘中 tick 盘口 bid/ask/last / 盘后 get_history_n close 双路。

    懒连接；限流 ≤20/批 + 62s 冷却。"""

    def __init__(self, probe_symbol: str = "m2611-C-3400.DCE",
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

    def is_trading(self) -> bool:
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

    def tick_book(self, symbol: str) -> dict | None:
        """盘中取 tick 盘口 → {bid, ask, last, bar_date}；拿不到 → None。"""
        if not self._connect():
            return None
        try:
            t = self._tbpy.get_current_tick(symbol=symbol)
            if t is None:
                return None
            # 期权 tick 的 bidasks[0] 为买一卖一
            bidasks = _safe_attr(t, "bidasks")
            bid = ask = None
            if bidasks and len(bidasks) > 0:
                ba = bidasks[0]
                bid = _pos_float(getattr(ba, "bid_p", None) if hasattr(ba, "bid_p") else (ba[0] if len(ba) > 0 else None))
                ask = _pos_float(getattr(ba, "ask_p", None) if hasattr(ba, "ask_p") else (ba[1] if len(ba) > 1 else None))
            last = _pos_float(_safe_attr(t, "last"))
            if last is None and bid is None and ask is None:
                return None
            return {"bid": bid, "ask": ask, "last": last, "bar_date": dt.date.today()}
        except Exception:
            return None

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

    def combo_oi_close_batch(self, symbols: list[str], today: dt.date | None = None) -> dict[str, tuple[float | None, int | None, dt.date | None]]:
        """盘后合并取数：一次 get_history_n 同取 position(OI) + close(收盘权利金) + time。

        返回 {symbol: (close, position, bar_date)}；任一字段取不到则为 None（不影响同批其它 symbol）。
        bar_date＝该 bar 真实交易日；pull_date＝取数归属交易日（按 pull_date 查缓存，盘后重跑秒回、不占 60s 令牌）。
        OI 在盘后/假日不变，一并写入缓存（设计见 d08/d09 文档：当日缓存幂等、重跑秒回）。
        仅盘后/假日使用；盘中 premium 走 tick_book、OI 走 get_history_n，由编排层回落独立取数。"""
        out: dict[str, tuple[float | None, int | None, dt.date | None]] = {}
        syms = list(dict.fromkeys(s for s in symbols if s))
        if not syms or not self._connect():
            return out
        tbpy = self._tbpy
        today = today or dt.date.today()
        pull_date = pull_date_for(today)
        repo = PremiumCacheRepo()
        # 分离：pull_date 当日 close+OI 均已缓存 → 直接走缓存（不占 60s 令牌）
        miss: list[str] = []
        for sym in syms:
            rec = repo.load(sym, pull_date)
            if rec and rec.get("close") is not None and rec.get("oi") is not None:
                out[sym] = (rec["close"], int(rec["oi"]), _to_date(rec.get("bar_date")))
            else:
                miss.append(sym)
        if not miss:
            return out
        for i, chunk in enumerate(_chunk(miss, BATCH_MAX)):
            if i > 0:
                time.sleep(COOLDOWN)
            try:
                r = gated_get_history_n(
                    tbpy,
                    chunk if len(chunk) > 1 else chunk[0],
                    "1d", 1, fields=["position", "close", "time"], timeout="30s")
            except Exception as e:  # noqa: BLE001
                self.last_error = f"get_history_n 异常：{type(e).__name__}: {e}"
                continue
            if not r:
                self.last_error = tbpy.get_last_err() or None
                continue
            for sym in chunk:
                data = r.get(sym) if len(chunk) > 1 else r
                if data is None:
                    continue
                close_val = _first_float(data.get("close"))
                pos_val = _first_int(data.get("position"))
                time_arr = data.get("time")
                bar_date_val = _to_date(time_arr[-1]) if time_arr is not None and hasattr(time_arr, "__len__") and len(time_arr) > 0 else None
                out[sym] = (close_val, pos_val, bar_date_val)
                repo.save(sym, pull_date, close_val, "daily_close", bar_date_val, oi=pos_val)
        return out


# ---------------------------------------------------------------------------
# 缓存仓库（sqlite3 单文件，当日幂等写入）
# ---------------------------------------------------------------------------
class PremiumCacheRepo:
    """data/premium.db 缓存：主键 (symbol, pull_date)。

    pull_date＝取数归属交易日（缓存分桶键）；bar_date＝该 bar 真实交易日（仅元数据）。
    """

    def __init__(self, db_path: Path = DEFAULT_PREMIUM_DB):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as cn:
            cn.execute("""
                CREATE TABLE IF NOT EXISTS premium (
                    symbol TEXT NOT NULL,
                    pull_date TEXT NOT NULL,
                    close REAL,
                    source TEXT,
                    bar_date TEXT,
                    oi REAL,
                    PRIMARY KEY (symbol, pull_date)
                )
            """)
            # 老库迁移：旧列名 cache_date/as_of → pull_date/bar_date（ALTER 安全）
            try:
                cols = {r[1] for r in cn.execute("PRAGMA table_info(premium)")}
                if "cache_date" in cols and "pull_date" not in cols:
                    cn.execute("ALTER TABLE premium RENAME COLUMN cache_date TO pull_date")
                if "as_of" in cols and "bar_date" not in cols:
                    cn.execute("ALTER TABLE premium RENAME COLUMN as_of TO bar_date")
            except Exception:
                pass
            # 老库兼容：缺 oi 列则补齐（缓存可重建，ALTER 安全）
            try:
                cols = {r[1] for r in cn.execute("PRAGMA table_info(premium)")}
                if "oi" not in cols:
                    cn.execute("ALTER TABLE premium ADD COLUMN oi REAL")
            except Exception:
                pass

    def load(self, symbol: str, date: dt.date) -> dict | None:
        with sqlite3.connect(self.db_path) as cn:
            row = cn.execute(
                "SELECT close, source, bar_date, oi FROM premium WHERE symbol=? AND pull_date=?",
                (symbol, date.isoformat())).fetchone()
        if row is None:
            return None
        return {"close": row[0], "source": row[1], "bar_date": row[2], "oi": row[3]}

    def save(self, symbol: str, date: dt.date, close: float | None,
             source: str, bar_date: dt.date | None, oi: int | None = None):
        with sqlite3.connect(self.db_path) as cn:
            cn.execute(
                "INSERT OR REPLACE INTO premium (symbol, pull_date, close, source, bar_date, oi) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (symbol, date.isoformat(), close, source,
                 bar_date.isoformat() if bar_date else None, oi))

# ---------------------------------------------------------------------------
# 编排器：fetch_premium
# ---------------------------------------------------------------------------
def fetch_premium(contracts: list, today: dt.date, *,
                  roles: dict[str, str] | None = None,
                  source: PremiumSource | None = None,
                  cache: PremiumCacheRepo | None = None,
                  market_open: bool | None = None,
                  verbose: bool = False) -> PremiumSet:
    """D09/D10 权利金：每合约双路取数（盘中 tick 盘口 / 盘后日线 close）。

    contracts: ChainContract 列表；roles: {symbol: role}。
    """
    repo = cache or PremiumCacheRepo()
    role_map = roles or {}
    result = PremiumSet()

    # 收集所有需要取价的 symbol
    sym_meta: dict[str, dict] = {}
    for c in contracts:
        sym = c.symbol if hasattr(c, "symbol") else str(c)
        sym_meta[sym] = {
            "code": getattr(c, "code", ""),
            "underlying_month": getattr(c, "underlying_month", ""),
            "cp": getattr(c, "cp", ""),
            "strike": getattr(c, "strike", 0.0),
        }

    # 先查缓存（按归属交易日严格查；盘中实时盘口不缓存、强制刷新 tick）
    pull_date = pull_date_for(today)
    cached: dict[str, dict] = {}
    if market_open is not True:
        for sym in sym_meta:
            rec = repo.load(sym, pull_date)
            if rec is not None:
                cached[sym] = rec

    # 缺的走 tbpy
    missing_syms = [s for s in sym_meta if s not in cached]
    fetched: dict[str, dict] = {}
    if missing_syms:
        src = source or TbpyPremiumSource(market_open=market_open)
        try:
            is_live = src.is_trading()
            if is_live:
                # 盘中：逐合约 tick 盘口
                for sym in missing_syms:
                    t = src.tick_book(sym)
                    if t is not None:
                        fetched[sym] = {**t, "source": "tick"}
            else:
                # 盘后：批量日线 close
                close_data = src.close_batch(missing_syms)
                for sym, (price, bar_date) in close_data.items():
                    fetched[sym] = {"close": price, "bar_date": bar_date, "source": "daily_close"}
        finally:
            if hasattr(src, "close"):
                src.close()

        # 回写缓存：key 用 pull_date（取数归属交易日），bar_date 存该 bar 真实交易日
        for sym, data in fetched.items():
            close_val = data.get("close")
            src_label = data.get("source", "cache")
            bar_date_pull = data.get("bar_date")
            if src_label == "tick":
                # 盘中实时盘口不缓存（见文件头 L21「tick 盘口不缓存」）；落库会写 close=None 行 →
                # 盘后重跑该 pull_date 命中空行致假 MISSING（claim B）
                continue
            pull_date_use = _to_date(bar_date_pull) or pull_date
            repo.save(sym, pull_date_use, close_val, src_label, bar_date_pull)
            cached[sym] = {"close": close_val, "source": src_label,
                           "bar_date": pull_date_use.isoformat() if pull_date_use else None}

    # 组装 PremiumSet
    for sym, meta in sym_meta.items():
        role = role_map.get(sym)
        rec = cached.get(sym)
        tick_data = fetched.get(sym)

        if tick_data and tick_data.get("source") == "tick":
            # 盘中 tick 盘口
            bm = _book_metrics(tick_data.get("bid"), tick_data.get("ask"),
                               tick_data.get("last"))
            result.quotes[sym] = PremiumQuote(
                symbol=sym, code=meta["code"],
                underlying_month=meta["underlying_month"],
                cp=meta["cp"], strike=meta["strike"], role=role,
                bid=tick_data.get("bid"), ask=tick_data.get("ask"),
                last=tick_data.get("last"), mid=bm["mid"],
                spread=bm["spread"], spread_pct=bm["spread_pct"],
                close=None, premium=bm["premium"],
                source="tick", bar_date=tick_data.get("bar_date"),
                has_book=bm["has_book"],
                state=Verdict.PASS,
                reason=f"tick @ {tick_data.get('bar_date')}")
        elif rec and rec.get("close") is not None:
            # 盘后日线 close
            bar_date_val = _to_date(rec.get("bar_date"))
            result.quotes[sym] = PremiumQuote(
                symbol=sym, code=meta["code"],
                underlying_month=meta["underlying_month"],
                cp=meta["cp"], strike=meta["strike"], role=role,
                bid=None, ask=None, last=None, mid=None,
                spread=None, spread_pct=None,
                close=rec["close"], premium=rec["close"],
                source="daily_close", bar_date=bar_date_val,
                has_book=False,
                state=Verdict.PASS,
                reason=f"daily_close @ {bar_date_val}")
        else:
            result.quotes[sym] = PremiumQuote(
                symbol=sym, code=meta["code"],
                underlying_month=meta["underlying_month"],
                cp=meta["cp"], strike=meta["strike"], role=role,
                bid=None, ask=None, last=None, mid=None,
                spread=None, spread_pct=None,
                close=None, premium=None,
                source="none", bar_date=None,
                has_book=False,
                state=Verdict.MISSING,
                reason=f"{sym} 两路皆无")

    return result


# ---------------------------------------------------------------------------
# CLI 自检（真实数据）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 72)
    print("[真实数据] D09/D10 权利金（每品种 ATM 期权示例）")
    print("=" * 72)

    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.d02_symbol_template import fetch_d02
    from sell_option.fetchers.d06_option_chain import fetch_d06, OptionIndexRepo
    from sell_option.fetchers.d07_candidate_months import fetch_d07, main_month_via_tbpy
    from sell_option.fetchers.d08_spot_price import fetch_d08

    today_real = dt.date.today()
    varieties = [(v.code, v.exchange) for v in fetch_d01()]
    repo = OptionIndexRepo()
    repo.ensure_fresh(today_real, verbose=False)
    cs_all = fetch_d06(varieties, today_real, repo=repo, verbose=False)
    templates = fetch_d02()
    cand = fetch_d07(cs_all.all_contracts(), today_real, symbol_templates=templates,
                    main_provider=main_month_via_tbpy)
    spots = fetch_d08(cand, today_real, verbose=False)

    # 每品种第一候选月 ATM Call + ATM Put
    atm_options: list = []
    all_chain = cs_all.all_contracts()
    for code, exchange in varieties:
        ms = cand.months_for(code)
        if not ms:
            continue
        month = ms[0]
        spot = spots.price_of(code, month.month4)
        if spot is None:
            continue
        month_contracts = [c for c in all_chain
                           if c.code == code and c.underlying_month == month.month4]
        for cp in ("C", "P"):
            strikes = sorted(set(c.strike for c in month_contracts if c.cp == cp))
            if not strikes:
                continue
            atm_strike = min(strikes, key=lambda s: abs(s - spot))
            atm_options.extend(c for c in month_contracts
                               if c.cp == cp and c.strike == atm_strike)

    prem = fetch_premium(atm_options, today_real, verbose=True)

    by_code: dict[str, list] = {}
    for q in prem.all_quotes():
        by_code.setdefault(q.code, []).append(q)
    print(f"\n品种 {len(varieties)} 个 · ATM 期权 {len(prem)} 个 · 分布 {prem.distribution()}")
    print("-" * 100)
    for code, exchange in varieties:
        spot = spots.price_of(code, cand.months_for(code)[0].month4) if cand.months_for(code) else None
        spot_str = f"{spot:g}" if spot else "—"
        qs = by_code.get(code, [])
        if not qs:
            print(f"{code:<4} {exchange:<5}  标的={spot_str}  —无ATM期权—")
            continue
        parts = []
        for q in sorted(qs, key=lambda x: (x.cp, x.strike)):
            if q.state == Verdict.PASS:
                parts.append(f"{q.cp}{q.strike:g}={q.premium:g}")
            else:
                parts.append(f"{q.cp}{q.strike:g}=MISS")
        print(f"{code:<4} {exchange:<5}  标的={spot_str}  {'  '.join(parts)}")
    print("-" * 100)
    print(f"共 {len(prem)} 个 ATM 期权权利金")
    print("=" * 72)
