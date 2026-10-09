"""D03 · 主力指数合约映射（品种 → 888/9888 后复权主连合约）。

需求源 §6.1 D03：静态参数，权威源 = 边界表「主力指数样例」列，
供给 #3（ADX＋斜率）· #10（否决信号）指数日线的取数对象。

性质与 D02 相反：DCE 老/新品种后缀分裂（9888/888）**无字面规律、只能查表**
（边界表 §二.5 重点坑），故本单元不推导规则，逐品种直接返回可合约 symbol；
000/9000 加权口径不采用（§二.4.4）。

校验（读表即体检）：后缀只允许 888/9888；代码前缀/交易所与行内其它列自洽；
品种不重复。异常＝静态表损坏，属全局前置故障，直接抛错。

运行自检：``python -m sell_option.fetchers.d03_index_symbol``
"""
from __future__ import annotations

import re

try:  # 包方式（python -m / import）：相对导入
    from .boundary_table import load_boundary_table
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.boundary_table import load_boundary_table

# 主力指数合约：代码 + 888/9888 + .交易所（§二.4.3）
_INDEX_RE = re.compile(r"^([A-Za-z]{1,2})(888|9888)\.([A-Z]{3,6})$")


class IndexSymbolTable:
    """D03 产物：品种 → 后复权主连日线合约 的查表器。"""

    def __init__(self, by_code: dict[str, str]):
        self._by_code = by_code  # 标的编码 -> 指数合约 symbol（如 "m" -> "m9888.DCE"）

    def get(self, code: str) -> str:
        """取品种的指数日线合约；未知品种 → KeyError（名单以表为准，D01 同源）。"""
        try:
            return self._by_code[code]
        except KeyError:
            raise KeyError(f"品种 {code!r} 不在边界表内（共 {len(self._by_code)} 品种）") from None

    def items(self):
        return self._by_code.items()

    def __len__(self) -> int:
        return len(self._by_code)

    def __contains__(self, code: str) -> bool:
        return code in self._by_code


def fetch_d03() -> IndexSymbolTable:
    """从边界表「主力指数样例」列构建品种→指数合约映射；畸形/重复 → 抛 ValueError。"""
    rows = load_boundary_table()
    by_code: dict[str, str] = {}
    for n, row in enumerate(rows, start=1):
        code, exchange, raw = row["标的编码"], row["交易所编码"], row["主力指数样例"]
        m = _INDEX_RE.match(raw)
        if m is None:
            raise ValueError(f"边界表第 {n} 行主力指数样例非法（须为 代码+888/9888.交易所）：{raw!r}")
        s_code, s_exch = m.group(1), m.group(3)
        if s_code != code or s_exch != exchange:
            raise ValueError(f"边界表第 {n} 行：主力指数样例 {raw!r} 与标的编码/交易所列不符")
        if code in by_code:
            raise ValueError(f"边界表品种代码重复：{code}（第 {n} 行）")
        by_code[code] = raw
    return IndexSymbolTable(by_code)


if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    table = fetch_d03()
    print(f"D03 主力指数映射：共 {len(table)} 品种")
    cont9888 = sorted(c for c, s in table.items() if s.endswith("9888." + s.rsplit(".", 1)[1]))
    print(f"  其中 9888（DCE 老品种）{len(cont9888)} 个: {' '.join(cont9888)}")
    print(f"  其余 888 共 {len(table) - len(cont9888)} 个")

    print("\n全表：")
    for code, symbol in sorted(table.items()):
        print(f"  {code:<3} -> {symbol}")
