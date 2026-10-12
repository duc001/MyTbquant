"""数据库 schema 与初始化。

数据库文件：data/option.db（仅创建空库，不建表）。
product_info 等表结构见 PRODUCT_INFO_DDL，按需调用 apply_schema() 建表（设计见 docs/库表设计.md §3.5）。
"""
import os
import sqlite3

DB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
DB_PATH = os.path.join(DB_DIR, "option.db")

PRODUCT_INFO_DDL = """
CREATE TABLE IF NOT EXISTS product_info (
    product_cd           TEXT PRIMARY KEY,  -- 品种代码（含大小写，如 AP/rb/m）[文件主键]
    product_name         TEXT,             -- 品种中文名（苹果/螺纹钢）
    exchange             TEXT,             -- 交易所编码（SHFE/DCE/CZCE/GFEX）
    last_price           REAL,             -- 现价
    multiplier           REAL,             -- 乘数（contract_unit）
    margin_per_lot       REAL,             -- 单腿保证金万元/手
    actual_margin_per_lot REAL,            -- 实际占用万元/手（= 单腿保证金×组合优惠）
    combo_discount       REAL,             -- 组合优惠系数（DCE·GFEX ×0.20，其余 1.0）
    fut_symbol_template  TEXT,             -- 期货 symbol 格式模板（非实盘月份）
    main_index_symbol    TEXT,             -- 主力指数合约（后复权主连，真实可用）
    opt_call_template    TEXT,             -- 期权 C 格式模板（非实盘月份）
    opt_put_template     TEXT,             -- 期权 P 格式模板（非实盘月份）
    remark               TEXT
);
"""


def init_db(path: str = DB_PATH) -> str:
    """仅确保数据库文件存在（空库，不建任何表）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.close()
    return path


def apply_schema(path: str = DB_PATH) -> None:
    """按 PRODUCT_INFO_DDL 建 product_info 表（当前不自动调用）。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(PRODUCT_INFO_DDL)
        conn.commit()
    finally:
        conn.close()


def load_product_info(md_path: str, db_path: str = DB_PATH) -> int:
    """解析品种边界表.md 的【一、边界表数据】表格，写入 product_info。

    列顺序：标的编码/品种/交易所编码/现价/乘数/单腿保证金万元/实际占用万元/
    期货symbol样例/主力指数样例/期权样例C/期权样例P。
    combo_discount 由交易所推导：DCE·GFEX ×0.20，其余 1.0。
    幂等：每次先清空再导入。
    """
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

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM product_info")  # 幂等重导
        cur = conn.cursor()
        for code, name, exch, price, mult, margin, actual, fut, main, call, put in rows:
            combo = 0.20 if exch in ("DCE", "GFEX") else 1.0
            cur.execute(
                """INSERT INTO product_info
                   (product_cd, product_name, exchange, last_price, multiplier,
                    margin_per_lot, actual_margin_per_lot, combo_discount,
                    fut_symbol_template, main_index_symbol, opt_call_template, opt_put_template, remark)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, name, exch, float(price), float(mult), float(margin),
                 float(actual), combo, fut, main, call, put, None),
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


if __name__ == "__main__":
    db_path = init_db()
    print(f"initialized db at {db_path}")
