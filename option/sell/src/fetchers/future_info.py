"""取数单元 future_info：解析品种边界表.md → future_info 表（品种级静态参考）。

权威源：品种边界表.md（保证金为「万元」，入库按库表设计 §四 归一为「元」，×10000）。
设计见 docs/库表设计.md §2.5。
"""
import os
import sqlite3
import sys

from ..schema import DB_PATH, apply_schema


def load_future_info(md_path: str, db_path: str = DB_PATH) -> int:
    """解析品种边界表.md【一、边界表数据】表格，写入 future_info。

    列顺序：标的编码/品种/交易所编码/现价/乘数/单腿保证金万元/实际占用万元/
    期货symbol样例/主力指数样例/期权样例C/期权样例P。
    - combo_discount 由交易所推导：DCE·GFEX ×0.20，其余 1.0。
    - 保证金两列由源「万元」×10000 归一为「元」。
    - 幂等：按 product_cd 主键 ON CONFLICT DO UPDATE（真 upsert）。
    """
    apply_schema(db_path)  # 确保表存在
    with open(md_path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    header_idx = next(
        (i for i, ln in enumerate(lines)
         if ln.strip().startswith("|") and "标的编码" in ln),
        None,
    )
    if header_idx is None:
        raise RuntimeError("未找到边界表表头（标的编码）")
    rows = []
    for ln in lines[header_idx + 1:]:
        s = ln.strip()
        if not s.startswith("|"):
            break
        if set(s) <= set("|-: "):  # 分隔行
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 11:
            continue
        rows.append(cells[:11])

    payload = [
        (code, name, exch, float(price), float(mult),
         float(margin) * 10000, float(actual) * 10000,
         (0.20 if exch in ("DCE", "GFEX") else 1.0),
         fut, main, call, put, None)
        for code, name, exch, price, mult, margin, actual, fut, main, call, put in rows
    ]
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.executemany(
                """INSERT INTO future_info
                   (product_cd, product_name, exchange, last_price, multiplier,
                    margin_per_lot, actual_margin_per_lot, combo_discount,
                    fut_symbol_template, main_index_symbol, opt_call_template, opt_put_template, remark)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(product_cd) DO UPDATE SET
                     product_name=excluded.product_name, exchange=excluded.exchange,
                     last_price=excluded.last_price, multiplier=excluded.multiplier,
                     margin_per_lot=excluded.margin_per_lot,
                     actual_margin_per_lot=excluded.actual_margin_per_lot,
                     combo_discount=excluded.combo_discount,
                     fut_symbol_template=excluded.fut_symbol_template,
                     main_index_symbol=excluded.main_index_symbol,
                     opt_call_template=excluded.opt_call_template,
                     opt_put_template=excluded.opt_put_template""",
                payload,
            )
        return len(payload)
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        md = sys.argv[1]
    else:
        # 权威源当前仍在 v1 目录；v2 落地后应迁至 option/sell/ 下
        root = os.path.abspath(__file__)
        for _ in range(5):
            root = os.path.dirname(root)
        md = os.path.join(root, "strategies", "sell_option", "docs", "品种边界表.md")
    n = load_future_info(md)
    print(f"loaded {n} varieties into {DB_PATH}")
