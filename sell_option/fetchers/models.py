"""取数单元共用的数据模型与工具（D06/D07 等共享，避免互相 import 成环）。"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class Verdict(str, Enum):
    """判定四态（需求 §八）：所有 #1–#10 检查项的统一结论口径。

    数据不足一律 MISSING（绝不用代理数据冒充）；FAIL 为硬否决；WARN 为"达标但时机未到"；
    PASS 为通过。继承 str 以便直接入报告 / JSON 序列化。
    """

    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    MISSING = "MISSING"  # 数据缺失（原 MISSING，语义更清晰）


def as_date(value: dt.datetime | dt.date | str) -> dt.date:
    """tbpy 日期字段类型归一化（§11.3.4 实测坑：delisted_date 会随连接状态返回
    datetime / date / str，直接用会 .date() 抛错或比较出错）。"""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return dt.datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    raise ValueError(f"无法归一化的日期类型/格式：{value!r}（{type(value).__name__}）")


def to_date(value: dt.datetime | dt.date | str | None) -> dt.date | None:
    """归一化 tbpy / 缓存时间值 → date（datetime/date/str 均可；解不出 → None）。

    与 as_date 区别：对无法解析的输入返回 None 而非抛错，用于缓存回写 / 读取的容错
    （旧缓存可能混入带 'T' 的 datetime 字符串）。d08/d09/combo 的 _to_date/_as_date 已统一指向此处。"""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def expand_month(month: str, today: dt.date) -> str:
    """CZCE 3 位月份按年代补全为 4 位（如 "701"→"2701"）；4 位原样返回。

    年代个位与首位数字匹配、取不早于今天的最近年份（交易所/软件侧同款
    “最近未来月份”惯例）。D07 组装 ChainContract 与 D06 漏斗共用本函数，
    避免年代推算逻辑多处发散。
    """
    if len(month) == 4:
        return month
    if len(month) != 3 or not month.isdigit():
        raise ValueError(f"月份须为 3 或 4 位数字，收到 {month!r}")
    decade_digit = int(month[0])
    yy = today.year % 100
    for candidate in (yy, yy + 10):  # 本年度个位匹配则取本年，否则下一个十年
        if candidate % 10 == decade_digit:
            return f"{candidate:02d}{month[1:]}"
    return f"{(yy + (10 - (yy % 10 - decade_digit)) % 10):02d}{month[1:]}"


@dataclass(frozen=True)
class ChainContract:
    """期权链上的一个合约（D07 产物元素；D06 只用到标的月/到期日两列，其余列供选腿）。"""

    code: str                 # 标的编码（边界表主键，如 "m" / "AP"）
    exchange: str             # 交易所后缀
    underlying_month: str     # 标的月份，统一 4 位 "2701"（CZCE 3 位需按年代补全，见 D07）
    expiry: dt.date           # 该期权的最后交易日（=DTE 基准；标的月级取同月最小值）
    cp: str                   # "C" / "P"
    strike: float             # 行权价
    symbol: str               # 完整期权 symbol（带交易所后缀）


@runtime_checkable
class ChainSource(Protocol):
    """D06 期权链数据源契约（多源降级用）。

    每个源实现 fetch_chain(code, exchange)，拿得到返回合约列表、拿不到返回 []；
    编排器按 authoritative 与返回是否为空决定采用哪个源、如何溯源。
    """

    name: str                 # 源标识：tbpy_index / sina / openvlab
    authoritative: bool       # True=完整多月链（tbpy 主源）；False=部分/降级（外部备源）

    def fetch_chain(self, code: str, exchange: str) -> list[ChainContract]:
        """返回该品种期权链；拿不到返回空列表（不抛异常，由编排器降级）。"""
        ...
