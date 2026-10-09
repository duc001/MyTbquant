"""D16 · 持仓记录（开仓+平仓共用 positions.db），供 §7.3 平仓判据 / §7.4 仓位限三值。

需求源 §6.1 D16 / §7.3 平仓规则 / §7.4 仓位与风控 / §八 四态：
- **人工录入**（开仓 INSERT status='open'，平仓 UPDATE status='closed'+exit 字段）。
- **status='open'**＝当前持仓（供平仓扫描）；**status='closed'**＝平仓历史（供复盘）。
- **DB 为空即无持仓**→ loaded=True、positions=[]（不是 MISSING，空表=没开仓）。
- **只存不变量**：入库的是【到期日 expiry_date】（合约最后交易日，交易所规则钉死、永不变），
  DTE 为派生量一律现算 `(expiry_date − today).days`、**不入库**——旧方案存「开仓日剩几天」快照
  逼人工口算、录错一次永久错（曾现 86 vs 77），已废。
- **表结构**：positions(id, entry_date, code, exchange, structure, short_symbol, short_strike,
  short_premium, short_bid, short_ask, prot_symbol, prot_strike, prot_premium, max_loss, expiry_date, margin,
  status, exit_date, exit_short_premium, exit_prot_premium, realized_pnl, exit_reason, note)

运行自检：``python -m sell_option.fetchers.d16_positions``（连真实 positions.db 展示持仓+历史）
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

try:  # 包方式（python -m / import）：相对导入
    from .models import Verdict, as_date
    from ..thresholds import load_thresholds
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.models import Verdict, as_date
    from sell_option.thresholds import load_thresholds

# 持仓 DB（data/ 已 git 忽略；空表=没开仓，不是 MISSING）
POSITIONS_DB = Path(__file__).resolve().parents[1] / "data" / "positions.db"

# §7.3 到期口径、§7.4 仓位限：旧 D16_* env 常量已删，阈值唯一权威源＝ThresholdSet（§6.4），
# 缺省参数运行时读 load_thresholds()；策略链路（x4_expiry / p_limits）均显式注入 th.*。

# 从期权 symbol 提取标的月份：字母后紧跟的 3-4 位数字（DCE/SHFE 4 位如 2611、CZCE 3 位如 610）
_MONTH_RE = re.compile(r"[A-Za-z]+(\d{3,4})")


# ---------------------------------------------------------------------------
# 共享工具：类型归一化
# ---------------------------------------------------------------------------
def _safe_date(value) -> dt.date | None:
    """安全归一化日期（复用 models.as_date）；坏值→None。"""
    try:
        return as_date(value)
    except (ValueError, TypeError):
        return None


def _safe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_str(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# ---------------------------------------------------------------------------
# DB 仓库：positions.db
# ---------------------------------------------------------------------------
class PositionRepo:
    """data/positions.db 持仓仓库：开仓+平仓共用一表，status 区分 open/closed。"""

    def __init__(self, db_path: Path = POSITIONS_DB):
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        cn = sqlite3.connect(self.db_path)
        cn.execute(
            "CREATE TABLE IF NOT EXISTS positions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "entry_date TEXT, code TEXT, exchange TEXT, structure TEXT, "
            "short_symbol TEXT, short_strike REAL, short_premium REAL, short_bid REAL, short_ask REAL, "
            "prot_symbol TEXT, prot_strike REAL, prot_premium REAL, "
            "max_loss REAL, expiry_date TEXT, margin REAL, "  # expiry_date＝最后交易日 YYYY-MM-DD（不变量；DTE 现算不入库）
            "status TEXT DEFAULT 'open', "  # open / closed
            "exit_date TEXT, exit_short_premium REAL, exit_prot_premium REAL, "
            "realized_pnl REAL, exit_reason TEXT, "
            "note TEXT)"
        )
        return cn

    def load_all(self, status: str | None = None) -> list[Position]:
        """读取持仓 → Position 列表；status=None 返回全部，'open' 只返回持仓，'closed' 只返回已平仓。"""
        try:
            with self._conn() as cn:
                if status:
                    rows = cn.execute(
                        "SELECT id, entry_date, code, exchange, structure, "
                        "short_symbol, short_strike, short_premium, short_bid, short_ask, "
                        "prot_symbol, prot_strike, prot_premium, "
                        "max_loss, expiry_date, margin, status, "
                        "exit_date, exit_short_premium, exit_prot_premium, realized_pnl, exit_reason, "
                        "note FROM positions WHERE status=? ORDER BY entry_date",
                        (status,)
                    ).fetchall()
                else:
                    rows = cn.execute(
                        "SELECT id, entry_date, code, exchange, structure, "
                        "short_symbol, short_strike, short_premium, short_bid, short_ask, "
                        "prot_symbol, prot_strike, prot_premium, "
                        "max_loss, expiry_date, margin, status, "
                        "exit_date, exit_short_premium, exit_prot_premium, realized_pnl, exit_reason, "
                        "note FROM positions ORDER BY entry_date"
                    ).fetchall()
        except Exception:
            return []
        positions: list[Position] = []
        for r in rows:
            entry_date = _safe_date(r[1])
            code = _safe_str(r[2])
            exchange = _safe_str(r[3])
            structure = _safe_str(r[4])
            short_leg = Leg(symbol=_safe_str(r[5]), strike=_safe_float(r[6]),
                            premium=_safe_float(r[7]), bid_premium=_safe_float(r[8]),
                            ask_premium=_safe_float(r[9]))
            prot_sym = _safe_str(r[10])
            protection_leg = Leg(symbol=prot_sym, strike=_safe_float(r[11]),
                                 premium=_safe_float(r[12])) if prot_sym else None
            positions.append(Position(
                id=r[0], entry_date=entry_date, code=code, exchange=exchange, structure=structure,
                short_leg=short_leg, protection_leg=protection_leg,
                max_loss=_safe_float(r[13]), expiry_date=_safe_date(r[14]),
                margin=_safe_float(r[15]), status=_safe_str(r[16]) or "open",
                exit_date=_safe_date(r[17]), exit_short_premium=_safe_float(r[18]),
                exit_prot_premium=_safe_float(r[19]), realized_pnl=_safe_float(r[20]),
                exit_reason=_safe_str(r[21]), note=str(r[22] or ""),
            ))
        return positions


# ---------------------------------------------------------------------------
# 产物数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Leg:
    """期权腿（short_leg / protection_leg）。缺字段一律 None，不猜值。

    premium：卖腿＝净收入、保护腿＝净支出；bid_premium 买一（§7.3 止盈判断）、ask_premium 卖一（平仓估值）。"""

    symbol: str | None = None
    strike: float | None = None
    premium: float | None = None
    bid_premium: float | None = None
    ask_premium: float | None = None


@dataclass(frozen=True)
class Position:
    """一笔持仓记录（开仓+平仓共用，status 区分 open/closed）。"""

    entry_date: dt.date | None          # 开仓日
    code: str | None                    # 品种代码（M/rb/AP…）
    exchange: str | None                # 交易所（DCE/SHFE/CZCE/GFEX）
    structure: str | None               # single_call / single_put（单腿侧价差，带保护腿）——与 assemble.c3 词汇一致；双卖铁鹰落库时拆成两笔单腿录入，本表不单独存 iron_condor（§7.3）
    short_leg: Leg                      # 卖腿
    protection_leg: Leg | None          # 保护腿（vertical spread 的保护侧）。本策略落库两结构均带保护腿：single_call/single_put 为单腿侧价差、prot_* 非 null；仅完全裸卖（不在 structure 枚举内）才为 None
    max_loss: float | None              # 最大亏损 =（价差宽度 − 净权利金）× 乘数
    expiry_date: dt.date | None         # 到期日（期权最后交易日，不变量；DTE 由它现算、不入库）
    margin: float | None = None         # 保证金占用额（元，人工录入；供 §7.1 #7 合计占用＝存量+新仓）
    # 状态 + 平仓字段（开平仓共用）
    id: int | None = None               # DB 主键
    status: str = "open"                # open / closed
    exit_date: dt.date | None = None    # 平仓日
    exit_short_premium: float | None = None  # 平仓时卖腿权利金
    exit_prot_premium: float | None = None   # 平仓时保护腿权利金
    realized_pnl: float | None = None   # 已实现盈亏
    exit_reason: str | None = None      # 止盈/止损/破位/到期/手动
    note: str = ""
    raw: dict = field(default_factory=dict)   # 原始 dict（供下游取未建模字段）

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def is_closed(self) -> bool:
        return self.status == "closed"

    @property
    def exit_net_premium(self) -> float | None:
        """平仓净权利金＝exit_short_premium − exit_prot_premium。"""
        if self.exit_short_premium is None:
            return None
        return self.exit_short_premium - (self.exit_prot_premium or 0.0)

    # ---- 派生标识 ----
    @property
    def position_id(self) -> str:
        """无显式 id：优先 raw['id']，否则 short_leg.symbol，再退回 code-structure-entry_date 合成。"""
        explicit = self.raw.get("id") if isinstance(self.raw, dict) else None
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        if self.short_leg.symbol:
            return self.short_leg.symbol
        parts = [self.code, self.structure, self.entry_date.isoformat() if self.entry_date else None]
        joined = "-".join(p for p in parts if p)
        return joined or "unknown"

    @property
    def has_protection(self) -> bool:
        return self.protection_leg is not None and self.protection_leg.symbol is not None

    @property
    def net_credit(self) -> float | None:
        """开仓净权利金＝卖腿净收入 − 保护腿净支出（无保护腿则＝卖腿净收入）；卖腿 premium 缺→None。"""
        sp = self.short_leg.premium
        if sp is None:
            return None
        pp = self.protection_leg.premium if self.protection_leg else None
        return sp - (pp or 0.0)

    @property
    def underlying_month(self) -> str | None:
        """从卖腿 symbol best-effort 提取标的月份（如 m2611-C-3200→2611、AP610-...→610）；供 §7.3 破位对照价 D08。"""
        if not self.short_leg.symbol:
            return None
        m = _MONTH_RE.search(self.short_leg.symbol)
        return m.group(1) if m else None

    def current_dte(self, today: dt.date) -> int | None:
        """当前 DTE＝(expiry_date − today).days 现算；缺 expiry_date→None（下游 X4 判 MISSING，不冒充）。

        到期日可直接照抄挂牌信息、零口算；已故的「开仓日剩几天」快照方案逼人工口算、录错永久错。"""
        if self.expiry_date is None:
            return None
        return (self.expiry_date - today).days


@dataclass(frozen=True)
class ExpiryAlert:
    """§7.3 到期提醒（纯日期口径）：level ∈ {"必平"(≤3), "提醒"(≤5)}。"""

    position: Position
    dte: int
    level: str

    @property
    def text(self) -> str:
        return f"⚠ {self.position.position_id} 距到期约 {self.dte} 天 → {self.level}"


@dataclass
class PositionBook:
    """fetch_d16 产物：持仓簿 ＋ 分组/到期提醒/仓位计数查询。

    loaded=False（文件缺失/损坏）→ runnable_state 判 MISSING（平仓扫描不可跑）。"""

    positions: list[Position] = field(default_factory=list)
    loaded: bool = False
    load_error: str | None = None

    # ---- 基础查询 ----
    def get(self, position_id: str) -> Position | None:
        return next((p for p in self.positions if p.position_id == position_id), None)

    def codes(self) -> list[str]:
        """去重保序的品种代码列表。"""
        seen: list[str] = []
        for p in self.positions:
            if p.code and p.code not in seen:
                seen.append(p.code)
        return seen

    def by_code(self) -> dict[str, list[Position]]:
        out: dict[str, list[Position]] = {}
        for p in self.positions:
            out.setdefault(p.code or "?", []).append(p)
        return out

    def __len__(self) -> int:
        return len(self.positions)

    # ---- 可跑性（§6.1 D16：单文件整体缺失即平仓扫描不可跑）----
    def runnable_state(self) -> tuple[Verdict, str]:
        """文件可读→PASS（数据可用）；缺失/损坏→MISSING（平仓扫描不可跑，不冒充）。"""
        if not self.loaded:
            return Verdict.MISSING, f"持仓记录不可用（{self.load_error}）→ 平仓扫描不可跑"
        return Verdict.PASS, f"持仓 {len(self.positions)} 笔 · 品种 {self.codes()}"

    # ---- §7.1 #7 存量保证金占用合计（供 c7_margin 合计占用＝存量+新仓）----
    def total_margin_occupied(self) -> float | None:
        """存量持仓保证金占用合计（元）＝各持仓人工录入 margin 之和。

        文件未加载 / 任一持仓缺 margin → **None**（合计不可靠 → #7 合计判 MISSING，绝不用部分和冒充、
        以免低估占用误放行）；无持仓 → 0.0（存量为零，合计即拟开新仓占用）。"""
        if not self.loaded:
            return None
        total = 0.0
        for p in self.positions:
            if p.margin is None:
                return None
            total += p.margin
        return total

    # ---- §7.3 到期提醒（已确认：≤3 必平、≤5 提醒；纯日期口径，当前 DTE 由 expiry_date 现算）----
    def expiry_alerts(self, today: dt.date, warn: int | None = None,
                      must: int | None = None) -> list[ExpiryAlert]:
        """当前 DTE ≤warn 的持仓提醒；≤must 升级为"必平"。无 expiry_date 的持仓跳过（下游另判）。

        warn/must=None → 读 th.expiry_warn_dte / expiry_must_dte（单一事实源）。"""
        if warn is None:
            warn = load_thresholds().expiry_warn_dte
        if must is None:
            must = load_thresholds().expiry_must_dte
        alerts: list[ExpiryAlert] = []
        for p in self.positions:
            dte = p.current_dte(today)
            if dte is None or dte > warn:
                continue
            level = "必平" if dte <= must else "提醒"
            alerts.append(ExpiryAlert(position=p, dte=dte, level=level))
        alerts.sort(key=lambda a: a.dte)
        return alerts

    # ---- §7.4 仓位计数（『_规则』已确认阈值）----
    def limit_status(self, max_positions: int | None = None, max_per_code: int | None = None,
                     max_codes: int | None = None) -> dict:
        """汇总当前仓位计数 + 超阈违规清单（最多6笔/每品种≤2/≤3品种）。

        缺省 None → 读 th.max_positions / max_per_code / max_series（单一事实源）。"""
        th = load_thresholds()
        if max_positions is None:
            max_positions = th.max_positions
        if max_per_code is None:
            max_per_code = th.max_per_code
        if max_codes is None:
            max_codes = th.max_series
        by_code = self.by_code()
        violations: list[str] = []
        if len(self.positions) > max_positions:
            violations.append(f"总持仓 {len(self.positions)} 笔 > 上限 {max_positions}")
        for c, ps in by_code.items():
            if len(ps) > max_per_code:
                violations.append(f"品种 {c} 持 {len(ps)} 笔 > 每品种上限 {max_per_code}")
        if len(by_code) > max_codes:
            violations.append(f"同持 {len(by_code)} 个品种 > 上限 {max_codes}")
        return {
            "total": len(self.positions),
            "max_positions": max_positions,
            "codes": {c: len(ps) for c, ps in by_code.items()},
            "max_per_code": max_per_code,
            "distinct_codes": len(by_code),
            "max_codes": max_codes,
            "violations": violations,
        }


# ---------------------------------------------------------------------------
# 加载：positions.db → PositionBook
# ---------------------------------------------------------------------------
def fetch_d16(db_path: Path = POSITIONS_DB) -> PositionBook:
    """加载当前持仓（status='open'）→ PositionBook。

    空表 → loaded=True、positions=[]（空表=没开仓，不是 MISSING）；
    DB 文件不存在 / 表结构不可读 → loaded=False + load_error（下游判 MISSING，绝不冒充「无持仓」）。
    ⚠ 必须先探文件存在：sqlite3.connect 对不存在路径会静默新建空库。"""
    if not Path(db_path).exists():
        return PositionBook(positions=[], loaded=False, load_error=f"{Path(db_path).name} 不存在")
    repo = PositionRepo(db_path)
    try:  # 全列探测（与 load_all 同字段集）：旧库缺 exit_* 列 / 非 sqlite 文件 → 报错而非静默空仓
        with repo._conn() as cn:
            cn.execute(
                "SELECT id, entry_date, code, exchange, structure, "
                "short_symbol, short_strike, short_premium, short_bid, short_ask, "
                "prot_symbol, prot_strike, prot_premium, "
                "max_loss, expiry_date, margin, status, "
                "exit_date, exit_short_premium, exit_prot_premium, realized_pnl, exit_reason, "
                "note FROM positions LIMIT 0"
            )
    except Exception as e:  # noqa: BLE001 - 任何读表异常都如实标不可用，不冒充
        return PositionBook(positions=[], loaded=False, load_error=f"{type(e).__name__}: {e}")
    positions = repo.load_all(status="open")
    return PositionBook(positions=positions, loaded=True)


def fetch_closed(db_path: Path = POSITIONS_DB) -> list[Position]:
    """加载平仓历史（status='closed'）→ Position 列表，按平仓日倒序。"""
    repo = PositionRepo(db_path)
    closed = repo.load_all(status="closed")
    closed.sort(key=lambda p: p.exit_date or dt.date.min, reverse=True)
    return closed


# ---------------------------------------------------------------------------
# 自检：加载真实 data/positions.db，展示持仓状态
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    today = dt.date.today()
    print("=" * 72)
    print(f"D16 持仓记录 · today={today} · DB={POSITIONS_DB}")

    book = fetch_d16()
    if not book.loaded:
        print(f"  ⚠ 持仓库不可用：{book.load_error} → 平仓扫描不可跑（MISSING，不冒充无持仓）")
    print(f"  持仓 {len(book)} 笔 · 品种 {book.codes() or '无'}")

    if book.positions:
        margin_total = book.total_margin_occupied()
        margin_s = f"{margin_total:,.0f}" if margin_total is not None else "缺失"
        print(f"  保证金占用合计：{margin_s} 元")

        alerts = book.expiry_alerts(today)
        if alerts:
            print(f"\n[§7.3 到期提醒]")
            for a in alerts:
                print(f"  {a.text}")

        ls = book.limit_status()
        print(f"\n[§7.4 仓位计数] 总={ls['total']} · 品种数={ls['distinct_codes']} · 违规={ls['violations'] or '无'}")

        print(f"\n[持仓明细]")
        for p in book.positions:
            dte = p.current_dte(today)
            nc = p.net_credit
            nc_s = f"{nc:.1f}" if nc is not None else "?"
            mg = f"{p.margin:,.0f}" if p.margin is not None else "?"
            ml = f"{p.max_loss:,.0f}" if p.max_loss is not None else "?"
            print(f"  {p.code} {p.structure} · 到期={p.expiry_date or '?'} DTE={dte or '?'} · margin={mg} · max_loss={ml}")
            sl = p.short_leg
            print(f"    卖腿: {sl.symbol}  K={sl.strike}  权利金={sl.premium}")
            if p.protection_leg:
                pl = p.protection_leg
                print(f"    保护: {pl.symbol}  K={pl.strike}  权利金={pl.premium}")
    else:
        print("  （无当前持仓）")

    # 平仓历史
    history = fetch_closed()
    if history:
        total_pnl = sum(p.realized_pnl or 0 for p in history)
        print(f"\n[平仓历史] {len(history)} 笔 · 累计盈亏 {total_pnl:+,.0f} 元")
        for p in history:
            pnl_s = f"{p.realized_pnl:+,.0f}" if p.realized_pnl is not None else "?"
            print(f"  {p.exit_date} {p.code} {p.exit_reason or '?'}: {pnl_s} 元")
            sl = p.short_leg
            exit_s = p.exit_short_premium or 0
            print(f"    卖腿: {sl.symbol}  开仓={sl.premium}  平仓={exit_s}")
    else:
        print(f"\n[平仓历史] 无")

    print("=" * 72)
