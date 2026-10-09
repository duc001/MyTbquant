"""D11 · 合约乘数（contract_unit），从边界表"乘数"列直接读取。

需求源 §6.1 D11：元数据，供给 #7 保证金 / #8 封顶预算 / #9 肥瘦 的一切金额换算。

设计：直接从边界表读（品种规格值，不随行情变化），不调 tbpy。
历史曾有过"每运行实测"的防御策略，但 2026-09-28 核对交易所官方数据后 47 品种全对齐，
回归简单路径：按表取值即可。

串价铁律 N/A：本单元取合约规格（非价格），不涉及主连指数冒充。

运行自检：``python -m sell_option.fetchers.d11_contract_multiplier``
"""
from __future__ import annotations

import sys
from pathlib import Path

try:  # 包方式
    from .boundary_table import load_boundary_table
except ImportError:  # 脚本直跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.boundary_table import load_boundary_table


class MultiplierSet:
    """D11 产物：品种 → 乘数值（来自边界表）。供 #7/#8/#9 金额换算取用。"""

    def __init__(self, by_code: dict[str, int]):
        self._by_code = by_code

    def get(self, code: str) -> int:
        """取品种乘数；未知品种 → KeyError。"""
        try:
            return self._by_code[code]
        except KeyError:
            raise KeyError(f"品种 {code!r} 不在边界表内（共 {len(self._by_code)} 品种）") from None

    def contract_unit(self, code: str) -> int:
        """快捷取某品种乘数（#7/#8/#9 直接调用）。"""
        return self.get(code)

    def codes(self) -> list[str]:
        return list(self._by_code)

    def __getitem__(self, code: str) -> int:
        return self._by_code[code]

    def __contains__(self, code: str) -> bool:
        return code in self._by_code

    def items(self):
        return self._by_code.items()

    def __len__(self) -> int:
        return len(self._by_code)


def fetch_d11() -> MultiplierSet:
    """从边界表"乘数"列构建品种→乘数映射；非正整数/重复 → 抛 ValueError。"""
    rows = load_boundary_table()
    by_code: dict[str, int] = {}
    for n, row in enumerate(rows, start=1):
        code, raw = row["标的编码"], row["乘数"]
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"边界表第 {n} 行乘数非整数：{raw!r}（品种 {code}）") from None
        if value <= 0:
            raise ValueError(f"边界表第 {n} 行乘数须为正数，收到 {value}（品种 {code}）")
        if code in by_code:
            raise ValueError(f"边界表品种代码重复：{code}（第 {n} 行）")
        by_code[code] = value
    return MultiplierSet(by_code)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    table = fetch_d11()
    print(f"D11 合约乘数：共 {len(table)} 品种（从边界表读取）")

    by_mult: dict[int, list[str]] = {}
    for code, value in table.items():
        by_mult.setdefault(value, []).append(code)
    for value in sorted(by_mult):
        print(f"  乘数 {value:>4}: {', '.join(by_mult[value])}")
