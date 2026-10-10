"""D01 · 47 品种名单（含品种中文名）。

需求源 §6.1 D01：静态参数，唯一权威源 `品种边界表.md`（人工维护），
供给 §五扫描范围 与 报告品种名；改动须人工。

降级原则：边界表是人工维护的静态文件——读不到 / 解析坏 / 名单异常
属**全局前置故障**，直接抛错终止整轮（不做逐品种 MISSING 静默降级）。

运行自检：``python -m sell_option.fetchers.d01_variety_list``
"""
from __future__ import annotations

from dataclasses import dataclass

try:  # 包方式（python -m / import）：相对导入
    from .boundary_table import load_boundary_table
except ImportError:  # 脚本直跑（PyCharm 右键 Run / python d01_variety_list.py）：补项目根后绝对导入
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.boundary_table import load_boundary_table

# 境内四所后缀（边界表 §二.2；CFFEX/INE 本表无品种）
VALID_EXCHANGES = frozenset({"CZCE", "DCE", "SHFE", "GFEX"})

# §五 确认的当前规模；名单以表为准，数目变化仅提示、不判死（增删品种须人工改表＝改权威源）
EXPECTED_COUNT = 47


@dataclass(frozen=True)
class Variety:
    """一个可交易品种：代码即主键（大小写按表内正确值，CZCE 大写、其余小写）。"""

    code: str       # 标的编码，如 "AP" / "m"
    name_cn: str    # 品种中文名，如 "苹果" / "豆粕"
    exchange: str   # 交易所后缀，如 "CZCE" / "DCE"

    @property
    def product_id(self) -> str:
        """品种级 tbpy 产品代码（查主力/期权链等品种级接口用），如 "m.DCE"。"""
        return f"{self.code}.{self.exchange}"


def fetch_d01() -> list[Variety]:
    """取 D01 品种名单，按边界表行序返回；表缺失/畸形/重复/非法交易所 → 抛 ValueError。"""
    rows = load_boundary_table()
    varieties: list[Variety] = []
    seen: set[str] = set()
    for n, row in enumerate(rows, start=1):
        code = row["标的编码"]
        name_cn = row["品种"]
        exchange = row["交易所编码"]
        if not code or not name_cn or not exchange:
            raise ValueError(f"边界表第 {n} 行（{code or '?'}）关键字段为空：标的编码/品种/交易所编码")
        if code in seen:
            raise ValueError(f"边界表品种代码重复：{code}（第 {n} 行）")
        if exchange not in VALID_EXCHANGES:
            raise ValueError(f"边界表第 {n} 行交易所编码非法：{exchange!r}（合法：{sorted(VALID_EXCHANGES)}）")
        seen.add(code)
        varieties.append(Variety(code=code, name_cn=name_cn, exchange=exchange))
    return varieties


if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    varieties = fetch_d01()
    by_exchange: dict[str, list[str]] = {}
    for v in varieties:
        by_exchange.setdefault(v.exchange, []).append(v.code)

    print(f"D01 品种名单：共 {len(varieties)} 个" + ("" if len(varieties) == EXPECTED_COUNT else f"（≠ 预期 {EXPECTED_COUNT}，请人工核对边界表）"))
    for exchange in sorted(by_exchange):
        codes = by_exchange[exchange]
        print(f"  {exchange:<5} {len(codes):>2} 个: {' '.join(codes)}")
    print("\n全表：")
    for v in varieties:
        print(f"  {v.code:<3} {v.name_cn:<6} {v.exchange:<5} product_id={v.product_id}")
