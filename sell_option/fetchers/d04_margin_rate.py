"""D04 · 保证金率（边界表"单腿保证金万元"列反推）。

需求源 §6.1 D04：静态参数，供给 #7 保证金检查。**A 已定**：实测 tbpy 无保证金字段，
#7 走估算制——估算单腿占用(元) = 实时现价 × 实测乘数(D11) × 本表保证金率，结果标注『估算』。

反推式：保证金率 = 单腿保证金万元 × 10000 ÷ (现价 × 乘数)。
"现价"列系人工快照、会过期，但分子（保证金万元）与分母（现价×乘数）同取自查表时点，
其商即交易所保证金率参数本身——比值不受价格漂移影响，故反推有效。

校验：三列均须为正数；反推率限定在 [RATE_MIN, RATE_MAX] 合理带内，
越带＝该行数据损坏（如漏/多写一个量级），直接抛错而不是带病供给 #7。

运行自检：``python -m sell_option.fetchers.d04_margin_rate``
"""
from __future__ import annotations

try:  # 包方式（python -m / import）：相对导入
    from .boundary_table import load_boundary_table
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.boundary_table import load_boundary_table

# 保证金率合理带：境内商品期货交易所品种极少出带（实测 47 品种反推≈0.09–0.17），
# 越带基本是表格错位/量级笔误，宁可报错也不带病估算。
RATE_MIN, RATE_MAX = 0.03, 0.50


class MarginRateTable:
    """D04 产物：品种 → 保证金率，兼 #7 估算器。"""

    def __init__(self, by_code: dict[str, float]):
        self._by_code = by_code

    def get(self, code: str) -> float:
        try:
            return self._by_code[code]
        except KeyError:
            raise KeyError(f"品种 {code!r} 不在边界表内（共 {len(self._by_code)} 品种）") from None

    def estimate_leg_margin(self, spot: float, code: str, multiplier: int, volume: int = 1) -> float:
        """#7 估算单腿保证金占用（元）＝实时现价×实测乘数×保证金率×手数。

        现价/乘数必须由调用方传入运行实测值（D08 tick / D11 contract_unit），
        本方法绝不用表内快照现价兜底——那是 §四 口径铁律禁止的。
        """
        for name, v in (("spot", spot), ("multiplier", multiplier), ("volume", volume)):
            if not isinstance(v, (int, float)) or v <= 0:
                raise ValueError(f"估算入参 {name} 须为正数，收到 {v!r}")
        return float(spot) * int(multiplier) * self.get(code) * int(volume)

    def items(self):
        return self._by_code.items()

    def __len__(self) -> int:
        return len(self._by_code)


def _positive(raw: str, col: str, code: str, n: int) -> float:
    try:
        v = float(raw)
    except ValueError:
        raise ValueError(f"边界表第 {n} 行「{col}」非数值：{raw!r}（品种 {code}）") from None
    if v <= 0:
        raise ValueError(f"边界表第 {n} 行「{col}」须为正数，收到 {v}（品种 {code}）")
    return v


def fetch_d04() -> MarginRateTable:
    """逐行反推保证金率；列值非法或反推出带 → 抛 ValueError（静态表损坏＝全局前置故障）。"""
    rows = load_boundary_table()
    by_code: dict[str, float] = {}
    for n, row in enumerate(rows, start=1):
        code = row["标的编码"]
        spot = _positive(row["现价"], "现价", code, n)
        mult = _positive(row["乘数"], "乘数", code, n)
        margin_wan = _positive(row["单腿保证金万元"], "单腿保证金万元", code, n)
        rate = margin_wan * 10000.0 / (spot * mult)
        if not (RATE_MIN <= rate <= RATE_MAX):
            raise ValueError(
                f"边界表第 {n} 行 {code} 反推保证金率 {rate:.4f} 出带 [{RATE_MIN}, {RATE_MAX}]，"
                f"疑为 现价/乘数/单腿保证金 列错位或量级笔误"
            )
        if code in by_code:
            raise ValueError(f"边界表品种代码重复：{code}（第 {n} 行）")
        by_code[code] = round(rate, 4)
    return MarginRateTable(by_code)


if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    table = fetch_d04()
    rates = dict(table.items())
    lo = min(rates.values())
    hi = max(rates.values())
    print(f"D04 保证金率：共 {len(table)} 品种，反推区间 {lo:.3f} ~ {hi:.3f}（#7 估算用，标注『估算』）")
    by_pct: dict[float, list[str]] = {}
    for code, rate in rates.items():
        by_pct.setdefault(round(rate, 2), []).append(code)
    for pct in sorted(by_pct):
        print(f"  {pct:>6.1%}: {' '.join(by_pct[pct])}")

    # 全品种端到端演示（运行时该两处应换成 D08 实时价 / D11 实测乘数）
    rows = load_boundary_table()
    print(f"\n全品种单腿估算保证金（表内快照口径）：")
    print(f"{'品种':<4} {'现价':>10} {'乘数':>6} {'估算保证金':>12} {'表内人工值':>12} {'差异':>8}")
    print("-" * 56)
    for row in rows:
        code = row["标的编码"]
        price = float(row["现价"])
        mult = int(row["乘数"])
        est = table.estimate_leg_margin(price, code, mult)
        ref = float(row["单腿保证金万元"]) * 10000
        diff_pct = ((est - ref) / ref * 100) if ref else 0
        print(f"{code:<4} {price:>10.1f} {mult:>6} {est:>12,.0f} {ref:>12,.0f} {diff_pct:>+7.1f}%")
    print("-" * 56)
