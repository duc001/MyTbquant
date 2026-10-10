"""D14 · 合约级 OI（卖腿＋保护腿各期权合约），tbpy 取数。

需求源 §6.1 D14 / §7.1 #2 持仓量门槛（单合约 OI ≥oi_min，默认 100、单一事实源 ThresholdSet.oi_min；两腿分别查）/ §6.3 B OI 口径 / §11.3.2：
- OI 是【数量】不是价格，不碰串价铁律（铁律只约束价格类）；查【期权合约自身】的持仓量——
  卖腿期权、保护腿期权各一份，两腿分别判 #2 门槛（选腿在 §7.2 上游，本单元只负责"给定期权合约→OI"）。
- **数据源**：tbpy get_history_n → position 字段（盘后/假日均可取上一交易日 OI）。
  Tick 无 oi 字段、`open_int` 字段 get_history_n 返回空（实测），正确字段名是 `position`。
  全源拿不到 → **MISSING**（守 §八 四态原则，绝不用代理数据冒充）。
- **门槛判定（OI ≥ oi_min=100）属 #2 检查项（下游）**；本单元只取 OI 值 + 来源 + 四态，另附
  meets_threshold 辅助（缺省读 `ThresholdSet.oi_min` 单一事实源；#2 实际以注入的 ThresholdSet 为准）。
- **不使用缓存**：OI 盘中持续变化（新开仓增、平仓减），缓存会过时，每次直接取实时源。

运行自检：``python -m sell_option.fetchers.d14_contract_oi``（真实连接 tbpy，从 option_index
取全品种合约，用 get_history_n 取 position 字段 OI）
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

try:  # 包方式（python -m / import）：相对导入
    from .models import Verdict, ChainContract
    from ..thresholds import load_thresholds
    from ..logutil import get_logger
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from sell_option.fetchers.models import Verdict, ChainContract
    from sell_option.thresholds import load_thresholds
    from sell_option.logutil import get_logger

logger = get_logger(__name__)

try:  # 全局串行令牌：跨取数器遵守 get_history_n 60s 限流，避免互相挤掉额度
    from ._history_gate import gated_get_history_n
except ImportError:
    from sell_option.fetchers._history_gate import gated_get_history_n

BATCH_MAX = int(os.getenv("SELLOPT_D14_BATCH", "20"))            # tbpy 单批合约上限
COOLDOWN = float(os.getenv("SELLOPT_D14_COOLDOWN", "62"))  # 两批 history 间隔（限流 60s + 余量）

ROLE_SHORT = "short"              # 卖腿
ROLE_PROTECTION = "protection"    # 保护腿
_ROLE_LABEL = {ROLE_SHORT: "卖腿", ROLE_PROTECTION: "保护腿", None: "未指定腿"}
_SRC_LABEL = {"tbpy_hist": "tbpy历史查询", "none": "无"}


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OiQuote:
    """单个期权合约的 OI 快照（卖腿 / 保护腿共用；role 区分）。

    拿到 OI → state=PASS、oi=持仓量、source=来源；全源拿不到 → state=MISSING、oi=None。
    meets_threshold 供下游 #2 门槛判定（OI≥threshold），本单元不做 PASS/FAIL 门槛裁决。
    """

    symbol: str                   # 完整期权 symbol（真实合约，如 m2611-C-3400.DCE）
    code: str                     # 标的编码
    underlying_month: str         # 标的月份（4 位）
    cp: str                       # "C" / "P"
    strike: float                 # 行权价
    role: str | None              # short=卖腿 / protection=保护腿 / None
    oi: int | None                # 持仓量（数量；非价格，不碰串价铁律）
    source: str                   # tbpy_hist / none
    bar_date: dt.date | None      # OI 所属交易日（bar_date＝该 bar 真实交易日）
    state: Verdict                # PASS（拿到 OI）/ MISSING（全源拿不到，不冒充）
    reason: str

    @property
    def role_label(self) -> str:
        return _ROLE_LABEL.get(self.role, str(self.role))

    def meets_threshold(self, threshold: int | None = None) -> bool | None:
        """#2 门槛辅助：OI≥threshold→True；<threshold→False；无 OI→None（下游判 MISSING）。

        threshold=None → 读 `load_thresholds().oi_min`（单一事实源）。"""
        if self.oi is None:
            return None
        if threshold is None:
            threshold = load_thresholds().oi_min
        return self.oi >= threshold


@dataclass
class OiSet:
    """fetch_oi 产物：期权 symbol → OI。供 #2 持仓量门槛（两腿分别查）取用。"""

    quotes: dict[str, OiQuote] = field(default_factory=dict)

    def get(self, symbol: str) -> OiQuote | None:
        return self.quotes.get(symbol)

    def oi_of(self, symbol: str) -> int | None:
        q = self.quotes.get(symbol)
        return q.oi if q else None

    def by_role(self, role: str) -> list[OiQuote]:
        return [q for q in self.quotes.values() if q.role == role]

    def all_quotes(self) -> list[OiQuote]:
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

    def manual(self) -> list[OiQuote]:
        return [q for q in self.quotes.values() if q.state == Verdict.MISSING]

    def below_threshold(self, threshold: int | None = None) -> list[OiQuote]:
        """拿到 OI 但 <门槛 的合约（#2 持仓量不足，供下游提示·非阻断）；threshold=None→读 th.oi_min。"""
        if threshold is None:
            threshold = load_thresholds().oi_min
        return [q for q in self.quotes.values()
                if q.state == Verdict.PASS and q.oi is not None and q.oi < threshold]

    def __len__(self) -> int:
        return len(self.quotes)


# ---------------------------------------------------------------------------
# 数据源契约
# ---------------------------------------------------------------------------
@runtime_checkable
class OiSource(Protocol):
    """D14 OI 源契约：批量取期权合约持仓量。

    oi_batch 返回 {symbol: (oi, bar_date)}；拿不到的 symbol 不出现在结果里（交编排器降级）。"""

    name: str
    authoritative: bool

    def is_trading(self) -> bool: ...
    def oi_batch(self, symbols: list[str]) -> dict[str, tuple[int, dt.date | None]]: ...


# ---------------------------------------------------------------------------
# 纯函数工具（离线可测）：分批
# ---------------------------------------------------------------------------
def _chunk(seq, n: int) -> list[list]:
    """按 ≤n 切批（保序去重由调用方保证）；n<=0 时整体一批。"""
    items = list(seq)
    if n <= 0:
        return [items]
    return [items[i:i + n] for i in range(0, len(items), n)]


def _pos_int(v) -> int | None:
    """安全转非负整数：非数/负数/None → None（OI 字段可能为空或脏值）。"""
    try:
        i = int(float(v))
    except (TypeError, ValueError):
        return None
    return i if i >= 0 else None


# ---------------------------------------------------------------------------
# 源①：tbpy get_history_n → position 字段（盘后/假日均可）
# ---------------------------------------------------------------------------
class TbpyOiSource:
    """tbpy OI 源：用 get_history_n 取 position 字段（实测假日/盘后也可拿到上一交易日 OI）。

    ⚠ 字段名是 `position` 不是 `open_int`（Bar 对象属性叫 open_int，但 get_history_n 字典 key 是 position）。
    限流：1 次查询/60s，可批量 ≤20 symbol/次。故按 ≤20/批调度，批间睡 62s 避限流。
    懒连接（首次用到 tbpy 才 connect，离线测试不触达）；close() 断开。"""

    name = "tbpy_hist"
    authoritative = True
    live_only = False         # get_history_n 盘后/假日均可取上一交易日数据

    def __init__(self, probe_symbol: str = "m2611.DCE",
                 market_open: bool | None = None):
        self.probe_symbol = probe_symbol      # 判交易时段用的流动性合约（仅 market_open=None 自探时用）
        self._market_open = market_open       # 编排层按品种探明的时段标志；非 None 时优先采信
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
        except Exception as e:  # noqa: BLE001 - 无 tbpy 环境（CI/离线）视为拿不到
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
            except Exception:  # noqa: BLE001
                pass
            self._conn = False

    def is_trading(self) -> bool:
        """盘中判定：编排层已按品种探明（_market_open 非 None）则直接采信；否则非假日 ∧ 在交易时段自探。"""
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
        except Exception:  # noqa: BLE001
            return False
        if not self._connect():
            return False
        return True

    def oi_batch(self, symbols: list[str]) -> dict[str, tuple[int, dt.date | None]]:
        """用 get_history_n 取 position 字段（最新 1 条日线）→ OI。

        限流：1 次查询/60s，可批量 ≤20 symbol/次。按 ≤20/批调度，批间睡 62s。
        返回格式：单 symbol→扁平 {field:ndarray}；list→嵌套 {symbol:{field:ndarray}}。
        拿不到 / 空数组 → 该 symbol 不出现在结果里（交编排器降级）。"""
        out: dict[str, tuple[int, dt.date | None]] = {}
        syms = list(dict.fromkeys(s for s in symbols if s))
        if not syms or not self._connect():
            return out
        tbpy = self._tbpy
        today = dt.date.today()

        for i, chunk in enumerate(_chunk(syms, BATCH_MAX)):
            if i > 0:
                time.sleep(COOLDOWN)  # 批间冷却避限流
            try:
                r = gated_get_history_n(
                    tbpy,
                    chunk if len(chunk) > 1 else chunk[0],
                    "1d", 1, fields=["position"], timeout="30s")
            except Exception as e:  # noqa: BLE001
                self.last_error = f"get_history_n 异常：{type(e).__name__}: {e}"
                continue
            if not r:
                self.last_error = tbpy.get_last_err() or None
                continue
            # 解析：单 symbol 扁平 / 多 symbol 嵌套
            if len(chunk) == 1:
                _parse_position_flat(chunk[0], r, out, today)
            else:
                for sym in chunk:
                    if sym in r:
                        _parse_position_flat(sym, r[sym], out, today)
        return out


def _parse_position_flat(sym: str, data: dict, out: dict, today: dt.date):
    """从 {position: ndarray} 解析最新 OI 写入 out。"""
    arr = data.get("position")
    if arr is None or not hasattr(arr, "__len__") or len(arr) == 0:
        return
    oi = _pos_int(arr[-1])
    if oi is not None:
        out[sym] = (oi, today)


# ---------------------------------------------------------------------------
# 编排：取 OI
# ---------------------------------------------------------------------------
def fetch_oi(contracts: Iterable[ChainContract], today: dt.date, *,
             roles: dict[str, str] | None = None,
             sources: list[OiSource] | None = None,
             market_open: bool | None = None,
             verbose: bool = True) -> OiSet:
    """为给定期权合约（选腿后的卖腿 / 保护腿）取合约级 OI（不使用缓存）。

    contracts：可迭代 ChainContract（须有 .symbol/.code/.underlying_month/.cp/.strike；按 symbol 去重）；
    roles：{symbol: "short"|"protection"}（可选，标注卖腿 / 保护腿，仅透传到结果）；
    sources：有序 OiSource 列表（默认 [tbpy]）；
    market_open：时段标志（编排层按品种探明后下送）——TbpyOiSource 采信此标志而非自探；
      None=旧行为（各源自探 is_trading）。
    拿不到 → 该合约 MISSING（不冒充）。
    不使用缓存：OI 盘中持续变化，每次直接取实时源。
    门槛判定（OI≥oi_min=100）留给下游 #2。
    """
    owns_sources = sources is None
    roles = roles or {}

    # ① 收集目标（按 symbol 去重，保序）
    targets: list[tuple[str, str, str, str, float, str | None]] = []
    seen: set[str] = set()
    for c in contracts:
        sym = getattr(c, "symbol", None)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        targets.append((sym, c.code, c.underlying_month, c.cp, c.strike, roles.get(sym)))

    result = OiSet()
    if not targets:
        return result

    # ② 默认源链（注入则用注入的）
    if sources is None:
        sources = [TbpyOiSource(market_open=market_open)]

    # sym -> (oi, bar_date, source_name)
    resolved: dict[str, tuple[int, dt.date | None, str]] = {}
    try:
        remaining = [t[0] for t in targets]

        # ③ 逐源降级：每源只对剩余 symbol 拉，拿到即定源
        for src in sources:
            if not remaining:
                break
            # 收盘（market_open=False）时跳过「只能盘中出数」的源（live_only=True），省无谓连接/订阅
            if market_open is False and getattr(src, "live_only", False):
                if verbose:
                    logger.info("[D14] 收盘跳过盘中源 %s（market_open=False）", getattr(src, "name", "?"))
                continue
            try:
                batch = src.oi_batch(remaining)
            except Exception as e:  # noqa: BLE001 - 单源异常只降级、不崩整轮
                batch = {}
                if verbose:
                    logger.warning("[D14] 源 %s 异常降级：%s: %s",
                                   getattr(src, "name", "?"), type(e).__name__, e)
            got = 0
            for s in list(remaining):
                if s in batch:
                    oi, bar_date = batch[s]
                    resolved[s] = (oi, bar_date, src.name)
                    remaining.remove(s)
                    got += 1
            if verbose:
                logger.info("[D14] 源 %s：命中 %d · 剩余 %d", getattr(src, "name", "?"), got, len(remaining))

        # ④ 组装 OiQuote（PASS=拿到 OI / MISSING=全源皆无）
        for sym, code, month, cp, strike, role in targets:
            r = resolved.get(sym)
            if r:
                oi, bar_date, src = r
                result.quotes[sym] = OiQuote(
                    symbol=sym, code=code, underlying_month=month, cp=cp, strike=strike,
                    role=role, oi=oi, source=src, bar_date=bar_date, state=Verdict.PASS,
                    reason=f"{_SRC_LABEL.get(src, src)} OI={oi}")
            else:
                result.quotes[sym] = OiQuote(
                    symbol=sym, code=code, underlying_month=month, cp=cp, strike=strike,
                    role=role, oi=None, source="none", bar_date=None, state=Verdict.MISSING,
                    reason="tbpy 拿不到 OI")
    finally:
        if owns_sources:
            for src in sources:
                try:
                    src.close()
                except Exception:  # noqa: BLE001
                    pass
    return result


# ---------------------------------------------------------------------------
# 自检：真实连接 tbpy，从 option_index 取全品种合约，用 get_history_n 取 position 字段 OI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    _today = dt.date.today()
    print("=" * 72)
    print(f"D14 合约 OI 实测 · today={_today} · tbpy get_history_n → position")

    # 从 option_index.json 加载全品种合约
    _idx_path = Path(__file__).resolve().parents[1] / "data" / "option_index.json"
    try:
        _idx_data = json.loads(_idx_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as _e:
        print(f"  失败：option_index.json 不可用（{_e}）")
        sys.exit(1)

    _varieties = _idx_data.get("varieties", {})
    print(f"  索引：{_idx_data.get('build_date', '?')} · {len(_varieties)} 品种 · "
          f"{_idx_data.get('total_options', '?')} 合约")

    # 每品种挑近月 ATM call
    _contracts = []
    _codes = []          # (code, symbol) 保序
    for _code in sorted(_varieties):
        _v = _varieties[_code]
        _exch = _v["exchange"]
        _months = sorted(_v.get("month_expiry", {}).keys())
        if not _months:
            continue
        _near = _months[0]          # 近月
        _near_cs = sorted(
            [c for c in _v["contracts"] if c["month"] == _near and c["cp"] == "C"],
            key=lambda c: c["strike"])
        if not _near_cs:
            continue
        _mid = len(_near_cs) // 2   # 中间档 ≈ ATM
        _pick = _near_cs[_mid]
        _contracts.append(ChainContract(
            code=_code, exchange=_exch, underlying_month=_near,
            expiry=None, cp="C",
            strike=float(_pick["strike"]), symbol=_pick["symbol"]))
        _codes.append((_code, _pick["symbol"]))

    if not _contracts:
        print("  索引中无可用合约")
        sys.exit(1)

    # 连接 tbpy，取 OI
    _src = TbpyOiSource()
    if not _src.connected and not _src._connect():
        print(f"  失败：{_src.last_error}")
        sys.exit(1)

    _syms = [c.symbol for c in _contracts]
    print(f"  tbpy 查询 {len(_syms)} 合约 position 字段（限流：≤20/批，批间 62s）...")
    _batch = _src.oi_batch(_syms)
    _src.close()

    _pass_n = len(_batch)
    _miss_n = len(_contracts) - _pass_n
    _oi_min = load_thresholds().oi_min
    _below_n = sum(1 for oi, _ in _batch.values() if oi < _oi_min)
    print(f"\n  命中：{_pass_n}/{len(_contracts)} · "
          f"MISSING={_miss_n} · 低于门槛(<{_oi_min})={_below_n}")
    if _src.last_error:
        print(f"  last_error：{_src.last_error}")

    # 逐品种展示
    print()
    print(f"  {'品种':<6}{'symbol':<28}{'行权价':>8}  {'OI':>8}  {'状态'}")
    print(f"  {'-'*6}{'-'*28}{'-'*8}  {'-'*8}  {'-'*10}")
    for _code, _sym in _codes:
        _c_obj = next(c for c in _contracts if c.symbol == _sym)
        if _sym in _batch:
            _oi_val, _oi_date = _batch[_sym]
            _flag = "PASS" if _oi_val >= _oi_min else "WARN"
            print(f"  {_code:<6}{_sym:<28}{_c_obj.strike:>8.0f}  {_oi_val:>8}  {_flag}")
        else:
            print(f"  {_code:<6}{_sym:<28}{_c_obj.strike:>8.0f}  {'—':>8}  MISSING")

    print(f"\n  [结果] 覆盖 {len(_contracts)} 品种 · PASS={_pass_n} MISSING={_miss_n}")
    print("=" * 72)
