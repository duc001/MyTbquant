"""D05 · 组合优惠系数（边界表"实际占用万元 ÷ 单腿保证金万元"反推）。

需求源 §6.1 D05：静态参数，供给 #7 保证金检查，与 D04 同属估算制（A 已定，标注『估算』）：
#7 组合占用估算 = 单腿占用（D04 估算器）× 本系数。

反推式：系数 = 实际占用万元 ÷ 单腿保证金万元。
表内万元列仅两位小数，单行比值带舍入噪声（如 c：0.04/0.22≈0.182），故：
- 按交易所聚合取**中位数**再归整到 0.05 步长（已知档位 0.20 / 1.00 均可被还原）；
- 断言**同所内比值离散度不超容差**，否则说明有行写错，抛错不带病供给。
不硬编码"DCE·GFEX ×0.20"——系数从表推导，改表自动跟随（边界表为唯一权威源）。

运行自检：``python -m sell_option.fetchers.d05_combo_discount``
"""
from __future__ import annotations

from statistics import median

try:  # 包方式（python -m / import）：相对导入
    from .boundary_table import load_boundary_table
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.boundary_table import load_boundary_table

# 同所离散度容差：万元两位小数的舍入噪声 <0.05，取 0.08 留余量；越界＝某行数据损坏
_SPREAD_TOL = 0.08
# 归整步长：交易所组合优惠按档位给（实测全表收敛到 0.20 / 1.00 两档）
_SNAP_STEP = 0.05


class ComboDiscountTable:
    """D05 产物：交易所 → 组合优惠系数，兼 #7 组合占用估算器。"""

    def __init__(self, by_exchange: dict[str, float]):
        self._by_exchange = by_exchange

    def get(self, exchange: str) -> float:
        try:
            return self._by_exchange[exchange]
        except KeyError:
            raise KeyError(f"交易所 {exchange!r} 无优惠系数（表内含：{sorted(self._by_exchange)}）") from None

    def estimate_combined(self, single_leg_margin: float, exchange: str) -> float:
        """#7 组合口径占用估算（元）＝单腿占用（D04 估算器产物）× 系数。"""
        if not isinstance(single_leg_margin, (int, float)) or single_leg_margin <= 0:
            raise ValueError(f"单腿占用须为正数，收到 {single_leg_margin!r}")
        return float(single_leg_margin) * self.get(exchange)

    def items(self):
        return self._by_exchange.items()

    def __len__(self) -> int:
        return len(self._by_exchange)


def _snap(value: float) -> float:
    """归整到 _SNAP_STEP 档位（保留两位小数避免浮点尾差）。"""
    return round(round(value / _SNAP_STEP) * _SNAP_STEP, 2)


def fetch_d05() -> ComboDiscountTable:
    """逐行反推系数、按交易所聚合；离散超限/数值非法 → 抛 ValueError。"""
    rows = load_boundary_table()
    ratios: dict[str, list[tuple[str, float]]] = {}  # 交易所 -> [(品种, 比值)]
    for n, row in enumerate(rows, start=1):
        code, exchange = row["标的编码"], row["交易所编码"]
        try:
            single_leg = float(row["单腿保证金万元"])
            combined = float(row["实际占用万元"])
        except ValueError:
            raise ValueError(f"边界表第 {n} 行 {code} 保证金两列须为数值") from None
        if single_leg <= 0 or combined <= 0:
            raise ValueError(f"边界表第 {n} 行 {code} 保证金两列须为正数：{single_leg}/{combined}")
        if combined > single_leg + 1e-9:
            raise ValueError(f"边界表第 {n} 行 {code} 实际占用({combined}) > 单腿保证金({single_leg})，优惠系数不可能>1")
        ratios.setdefault(exchange, []).append((code, combined / single_leg))

    by_exchange: dict[str, float] = {}
    for exchange, pairs in ratios.items():
        values = [v for _, v in pairs]
        spread = max(values) - min(values)
        if spread > _SPREAD_TOL:
            worst = sorted(pairs, key=lambda p: p[1])
            raise ValueError(
                f"{exchange} 内优惠系数离散度过大（{spread:.3f} > {_SPREAD_TOL}）："
                f"最低 {worst[0][0]}={worst[0][1]:.3f} 最高 {worst[-1][0]}={worst[-1][1]:.3f}，疑有行写错"
            )
        by_exchange[exchange] = _snap(median(values))
    return ComboDiscountTable(by_exchange)


if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    table = fetch_d05()
    rows = load_boundary_table()
    print(f"D05 组合优惠系数：{len(table)} 个交易所")
    ratios: dict[str, list[float]] = {}
    for row in rows:
        r = float(row["实际占用万元"]) / float(row["单腿保证金万元"])
        ratios.setdefault(row["交易所编码"], []).append(r)
    for exchange, coef in sorted(table.items()):
        vs = ratios[exchange]
        print(f"  {exchange:<5} 系数={coef:<5} （逐行反推 {len(vs)} 个，区间 {min(vs):.3f} ~ {max(vs):.3f}）")

    # 全品种端到端演示：单腿估算 × 系数 ≈ 表内实际占用快照
    print(f"\n全品种组合占用估算（表内快照口径）：")
    print(f"{'品种':<4} {'交易所':<6} {'单腿保证金':>12} {'系数':>6} {'组合估算':>12} {'表内实际':>12} {'差异':>8}")
    print("-" * 64)
    for row in rows:
        code = row["标的编码"]
        exch = row["交易所编码"]
        single_leg = float(row["单腿保证金万元"]) * 10000
        coef = table.get(exch)
        est = table.estimate_combined(single_leg, exch)
        ref = float(row["实际占用万元"]) * 10000
        diff_pct = ((est - ref) / ref * 100) if ref else 0
        print(f"{code:<4} {exch:<6} {single_leg:>12,.0f} {coef:>6.2f} {est:>12,.0f} {ref:>12,.0f} {diff_pct:>+7.1f}%")
    print("-" * 64)
