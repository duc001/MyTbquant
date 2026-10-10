"""D13 · #4 IV 分位·方向模块 · openvlab 纯 HTTP 取数（只产观测；#4 判定在 checks/c4_iv）。

需求源 §6.1 D13 / §7.1 #4 / §6.3 C·E（用户拍板 2026-09-27）：
- **D13 ATM IV 现值·百分位·日变**：权威源 openvlab（ctamap-all 一次请求带回），tbpy 期权链反解仅对账、不判定；
- **D13 N 日历史序列**：本地自建——每日落地 IV 快照攒序列（供未来 N 日判据）；方向主判据＝atmv_1dchg（1 日），N 日方向判据待积累后启用；
- **（#4 判定规则在 checks/c4_iv；本模块只取 IV 观测 + 算方向）** 方向判据＝atmv_1dchg < 0 即判回落（拐点零延迟）。

**取数方式（实测 2026-09-27，纠正需求文档 §6.2/§6.3 旧口径）**：openvlab `/api/ctamap-all` 是**纯公开 HTTP JSON**，
urllib 直接 GET 即可，**无需 Playwright/浏览器、无需登录/邀请码/token**；一次请求返回全部品种，覆盖我方 **47/47**
（含 PG=LPG，故文档担心的“PG 须走 chart/light 补抓”对**主力月 IV** 是多余的）。#4 为**品种级判定、用主力月口径**
（§6.3 E：一品种一门槛、不随月份翻倍），ctamap-all 一行一品种主力月，粒度正好契合。

**非主力月 IV（#5 安全垫用）不在本模块**：ctamap-all 只给主力月；#5 逐月 IV 的取数与“回退主力月标注近似”另属 D13 扩展（§6.3 E：openvlab chart/light，待落地）。

运行自检：``python -m sell_option.fetchers.d13_iv_snapshot``（纯 HTTP，不依赖 tbpy 连接）
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:  # 包方式（python -m / import）：相对导入
    from .d06_external_sources import OpenvlabIV, OpenvlabSource
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.d06_external_sources import OpenvlabIV, OpenvlabSource

# 每日 IV 快照数据库（D13 攒序列）；data/ 已 git 忽略
DEFAULT_IV_DB = Path(__file__).resolve().parents[1] / "data" / "iv_snapshot.db"

# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IvVerdict:
    """D13 产出的 #4 IV 观测（判定在 checks/c4_iv 做，本模块只取数 + 算方向）。"""

    code: str
    structure: str                 # single / dual
    exp: str = ""                  # 主力月 YYYYMM（口径标注）
    atmv_current: float | None = None
    atmv_percentile: float | None = None
    atmv_1dchg: float | None = None
    direction: str = "unknown"     # falling / rising / flat / unknown（_direction 算）
    dir_basis: str = "none"        # 1_day（IV日变）/ none


@dataclass
class IvCheckResult:
    """fetch_d13 产物：品种 → 判定；附带覆盖统计，直接供报告与 #5 复用。"""

    verdicts: dict[str, IvVerdict] = field(default_factory=dict)
    source: str = "openvlab"

    def get(self, code: str) -> IvVerdict | None:
        return self.verdicts.get(code)

    def __len__(self) -> int:
        return len(self.verdicts)


# ---------------------------------------------------------------------------
# D13 快照仓库：每日落地 IV 快照（攒序列，供未来 N 日判据）
# ---------------------------------------------------------------------------
class IvSnapshotRepo:
    """data/iv_snapshot.db：sqlite3 缓存每日 IV 快照（与 spot.db / premium.db / daily.db 统一模式）。

    表 ``iv_snapshot(variety, snapshot_date, atmv_current, atmv_percentile, atmv_1dchg, exp)``；
    主键 (variety, snapshot_date) 保证 INSERT OR REPLACE 幂等。
    每日快照持久化（攒序列）；当前方向判据只用 1 日 IV 日变，N 日序列读写待积累后启用。"""

    def __init__(self, db_path: Path = DEFAULT_IV_DB):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS iv_snapshot (
                    variety       TEXT NOT NULL,
                    snapshot_date TEXT NOT NULL,
                    atmv_current  REAL,
                    atmv_percentile REAL,
                    atmv_1dchg    REAL,
                    exp           TEXT,
                    PRIMARY KEY (variety, snapshot_date)
                )
            """)

    def save(self, today: dt.date, ivs: dict[str, OpenvlabIV | None]) -> int:
        """落地今日快照（INSERT OR REPLACE，幂等）；返回写入品种数。"""
        day_str = today.isoformat()
        rows = []
        for code, iv in ivs.items():
            if iv is None or iv.atmv_percentile is None:
                continue
            rows.append((code, day_str, iv.atmv_current, iv.atmv_percentile,
                         iv.atmv_1dchg, iv.exp))
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO iv_snapshot "
                    "(variety, snapshot_date, atmv_current, atmv_percentile, atmv_1dchg, exp) "
                    "VALUES (?, ?, ?, ?, ?, ?)", rows)
        except sqlite3.Error:
            pass
        return len(rows)

    def get_today(self, code: str, today: dt.date) -> "tuple[float | None, float | None, float | None, str] | None":
        """读回该品种今日快照（atmv_current, atmv_percentile, atmv_1dchg, exp）；无则返回 None。

        供 fetch_d13 的 live→快照回退：当 openvlab ctamap-all 偶发返回缺 atmv_percentile 的行时，
        用本日已落地的完整快照兜底，避免 #4 IV 硬否决被静默失效。"""
        day_str = today.isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT atmv_current, atmv_percentile, atmv_1dchg, exp FROM iv_snapshot "
                    "WHERE variety = ? AND snapshot_date = ?", (code, day_str))
                row = cur.fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return (row["atmv_current"], row["atmv_percentile"], row["atmv_1dchg"], row["exp"] or "")

    def get_recent(self, code: str, today: dt.date) -> "tuple[float | None, float | None, float | None, str] | None":
        """回退：读该品种截至 today 的【最近一日】完整快照（atmv_current, atmv_percentile, atmv_1dchg, exp）。

        当日快照缺失（openvlab 抖动 + 今日未落地）时，用最近一日兜底，保证选腿/#5 仍有 IV、
        不静默失效。落地时 fetch_d13 会把此值以今日日期写回（backfill），后续读取即命中 get_today。"""
        day_str = today.isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT atmv_current, atmv_percentile, atmv_1dchg, exp FROM iv_snapshot "
                    "WHERE variety = ? AND snapshot_date <= ? AND atmv_current IS NOT NULL "
                    "ORDER BY snapshot_date DESC LIMIT 1", (code, day_str))
                row = cur.fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return (row["atmv_current"], row["atmv_percentile"], row["atmv_1dchg"], row["exp"] or "")


# ---------------------------------------------------------------------------
# 方向判据（IV 日变 1 日）
# ---------------------------------------------------------------------------
def _direction(iv: OpenvlabIV) -> tuple[str, str]:
    """IV 方向：atmv_1dchg < 0 即判回落（拐点零延迟）。

    设计原则（2026-10-02 改进）：
    - 卖方入场时机 = IV 高位 + 刚开始下降的那一刻；
    - atmv_1dchg 由 openvlab 统一源计算（今日 vs 上一交易日），无跨源/跨假期不一致；
    - OLS 斜率（旧版）在拐点处延迟 2-3 天，不适合抓拐点。

    返回 (direction, basis)：direction ∈ falling/rising/flat/unknown；basis ∈ 1_day/none。
    """
    # ---- 核心判定：atmv_1dchg < 0 → 拐头（openvlab 统一源，无跨源不一致）----
    chg = iv.atmv_1dchg
    if chg is None:
        return "unknown", "none"
    if chg < 0:
        return "falling", "1_day"
    if chg > 0:
        return "rising", "1_day"
    return "flat", "1_day"

# ---------------------------------------------------------------------------
# 观测打包（只取数 + 算方向；#4 判定在 checks/c4_iv）
# ---------------------------------------------------------------------------
def _observe(code: str, structure: str, iv: OpenvlabIV | None) -> IvVerdict:
    """把 openvlab IV 观测打包成 IvVerdict（不含判定，#4 判定交给 c4_iv）。"""
    if iv is None:
        return IvVerdict(code, structure,
                         atmv_current=None, atmv_percentile=None, atmv_1dchg=None,
                         direction="unknown", dir_basis="none")
    direction, basis = _direction(iv)
    return IvVerdict(code, structure, exp=iv.exp,
                     atmv_current=iv.atmv_current, atmv_percentile=iv.atmv_percentile,
                     atmv_1dchg=iv.atmv_1dchg, direction=direction, dir_basis=basis)


# ---------------------------------------------------------------------------
# 取数 + 判定编排
# ---------------------------------------------------------------------------
def fetch_d13(varieties: list[tuple[str, str]], today: dt.date, *,
              structure: str = "single", source: OpenvlabSource | None = None,
              repo: IvSnapshotRepo | None = None,
              persist: bool = True, verbose: bool = True) -> IvCheckResult:
    """为每品种取 openvlab 主力月 IV 快照（只产观测；#4 判定在 c4_iv）；默认落地今日快照供 D13 攒序列。

    varieties：[(code, exchange), ...]（一般来自 D01）；structure：single/dual（决定 60/70 门槛）。
    openvlab 一次 HTTP 拉全品种（懒加载缓存），逐品种评估；单品种缺失只降级判 MISSING、不崩整轮。
    """
    source = source or OpenvlabSource()
    repo = repo or IvSnapshotRepo()

    ivs: dict[str, OpenvlabIV | None] = {}
    for code, _ex in varieties:
        try:
            iv = source.iv(code)
            # openvlab ctamap-all 偶发返回缺 atmv_percentile 的行（且被 _ctamap 缓存 → 整轮全池 MISSING）；
            # 回退到本日已落地的 iv_snapshot.db（若含完整百分位）兜底，避免 #4 IV 硬否决被静默失效。
            if iv is None or iv.atmv_percentile is None:
                snap = repo.get_today(code, today)
                stale = False
                if snap is None:
                    snap = repo.get_recent(code, today)   # 当日快照缺失 → 最近一日兜底（openvlab 抖动不失效）
                    stale = snap is not None
                if snap is not None:
                    cur, pct, chg, exp = snap
                    iv = OpenvlabIV(code=code, exchange=getattr(iv, "exchange", "") if iv else "",
                                    exp=exp or "", atmv_current=cur, atmv_percentile=pct, atmv_1dchg=chg)
                    if verbose:
                        if stale:
                            print(f"[#4] {code} 当日快照缺失 → 回退最近一日快照（atmv_current={cur}）")
                        else:
                            print(f"[#4] {code} live 缺百分位 → 回退 iv_snapshot.db（百分位 {pct}）")
            ivs[code] = iv
        except Exception as e:  # noqa: BLE001 - 单品种异常只降级
            if verbose:
                print(f"[#4] {code} 取 IV 异常降级：{type(e).__name__}: {e}")
            ivs[code] = None

    if persist:
        try:
            n = repo.save(today, ivs)
            if verbose:
                print(f"[#4] 今日 IV 快照已落地：{repo.db_path.name}（{n} 品种）")
        except OSError as e:
            if verbose:
                print(f"[#4] 快照落地失败（不影响判定）：{type(e).__name__}: {e}")

    result = IvCheckResult()
    for code, _ex in varieties:
        iv = ivs.get(code)
        result.verdicts[code] = _observe(code, structure, iv)
    if source.last_error and verbose:
        print(f"[#4] openvlab last_error={source.last_error}")
    return result


# ---------------------------------------------------------------------------
# 自检：纯 HTTP 取 47 品种 IV + #4 判定（单卖/双卖）+ D13 快照落地
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    from sell_option.fetchers.d01_variety_list import fetch_d01

    today = dt.date.today()
    varieties = [(v.code, v.exchange) for v in fetch_d01()]
    print("=" * 72)
    print(f"D13 IV 观测自检 · today={today} · 品种 {len(varieties)} 个 · 源 openvlab /api/ctamap-all（纯 HTTP）")
    print(f"方向判据：atmv_1dchg < 0 即判回落（拐点零延迟）")

    src = OpenvlabSource()
    repo = IvSnapshotRepo()

    # 取数 + 落地今日快照（只产出观测；#4 判定在 checks/c4_iv）
    res = fetch_d13(varieties, today, structure="single", source=src, repo=repo, verbose=True)
    covered = sum(1 for v in res.verdicts.values() if v.atmv_percentile is not None)
    print(f"\n[覆盖] openvlab 主力月 IV 命中：{covered}/{len(varieties)} 品种")

    # 全量明细：展示分位/IV现值/日变/方向（判定请跑 `python -m sell_option.checks.c4_iv`）
    print("\n[全量明细]（分位 | IV现值% | 日变 | 方向）")
    for code, _exch in varieties:
        v = res.get(code)
        if v is None:
            print(f"  {code:<3} —不在 D01 品种表—")
            continue
        pct = f"{v.atmv_percentile:.1f}" if v.atmv_percentile is not None else "NA"
        atm = f"{v.atmv_current:.2f}" if v.atmv_current is not None else "NA"
        chg = f"{v.atmv_1dchg:+.2f}" if v.atmv_1dchg is not None else "NA"
        print(f"  {code:<3} 分位{pct:>6} | IV{atm:>7} | 日变{chg:>7} | {v.direction:<7}")

    # 无 IV / 分位缺失品种（openvlab 无数据）
    missing = [c for c, v in res.verdicts.items() if v.atmv_percentile is None]
    print(f"\n[无 IV 品种]（openvlab 无该品种 IV）：{missing or '无'}")


