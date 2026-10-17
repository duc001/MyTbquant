"""option.db 连接与写入助手。"""
import os
import sqlite3

from .schema import DB_PATH


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """打开（必要时建目录）option.db，返回带 row_factory 的连接。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def upsert(conn: sqlite3.Connection, sql: str, rows) -> None:
    """执行带 ON CONFLICT 的 executemany（单事务）；rows 为参数序列。

    约定：一律用 ON CONFLICT(业务 UNIQUE 键) DO UPDATE 真 upsert（id 不变），
    禁用 INSERT OR REPLACE（会发新 id，打乱"先后"）。见库表设计 §四。
    """
    with conn:
        conn.executemany(sql, rows)
