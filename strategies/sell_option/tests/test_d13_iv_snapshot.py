"""D13 IV 快照：openvlab 抖动时的回退健壮性。

核心回归：当 openvlab 不可达且【当日】快照缺失时，fetch_d13 必须回退到【最近一日】快照，
使 atmv_current 仍有值（选腿/#5 不静默失效），而非返回 None → 退化为固定 10% 兜底。
"""
import datetime as dt

from sell_option.fetchers.d13_iv_snapshot import (
    IvSnapshotRepo, OpenvlabIV, fetch_d13,
)


class _DeadSource:
    """模拟 openvlab 完全不可达：iv() 一律返回 None。"""
    last_error = "RemoteDisconnected: Remote end closed connection without response"

    def iv(self, code):
        return None


def _save_past(repo: IvSnapshotRepo, code: str, day: dt.date, cur: float, pct: float):
    iv = OpenvlabIV(code=code, exchange="SHFE", exp="202611",
                   atmv_current=cur, atmv_percentile=pct, atmv_1dchg=1.0)
    repo.save(day, {code: iv})


def test_recent_fallback_when_today_missing(tmp_path):
    """当日快照缺失 + openvlab 抖动 → 回退最近一日，atmv_current 仍有值。"""
    repo = IvSnapshotRepo(db_path=tmp_path / "iv.db")
    past = dt.date(2026, 9, 27)
    today = dt.date(2026, 10, 6)
    _save_past(repo, "PF", past, 43.47, 95.65)          # 仅过去日快照，无今日

    res = fetch_d13([("PF", "SHFE")], today, structure="single",
                    source=_DeadSource(), repo=repo, persist=True, verbose=False)
    v = res.get("PF")
    assert v is not None
    assert v.atmv_current == 43.47                       # 回退到最近一日 → 自适应有 IV
    assert v.atmv_percentile == 95.65

    # 回退后应以【今日】日期写回（backfill），后续 get_today 命中
    assert repo.get_today("PF", today) is not None


def test_today_preferred_over_recent(tmp_path):
    """当日快照存在时优先用当日，不回退到陈旧的最近一日。"""
    repo = IvSnapshotRepo(db_path=tmp_path / "iv.db")
    past = dt.date(2026, 9, 27)
    today = dt.date(2026, 10, 6)
    _save_past(repo, "PF", past, 43.47, 95.65)
    _save_past(repo, "PF", today, 41.10, 88.20)         # 当日快照（不同值）

    res = fetch_d13([("PF", "SHFE")], today, structure="single",
                    source=_DeadSource(), repo=repo, persist=False, verbose=False)
    v = res.get("PF")
    assert v is not None
    assert v.atmv_current == 41.10                       # 用当日，而非陈旧的 43.47
