"""data_state_check 表数据管理（库表设计 §2.3）。

数据状态核对汇总表：由各 fetcher.check() 调用 record() 回写自己的结论（表归属本类，
调用方不直接操作本表）；本模块提供写入 record / 查询 read。
表结构由 schema.apply_schema() 负责创建，本模块只读写数据、不建表（只要结果，不管过程）。
"""
import os
import sqlite3
import sys
from datetime import datetime

try:  # 包内导入
    from ..schema import DB_PATH
except ImportError:  # 脚本直接运行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from schema import DB_PATH

TABLE_REF = "data_state_check"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DataStateCheck:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def record(self, table_ref, status, remark=""):
        """登记 / 更新某表的数据状态结论（由其它 fetcher 的 check() 调用）。

        bar_start_date / bar_end_date / last_detail_id 对配置表等无 bar 概念的表留 NULL。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO data_state_check (table_ref, status, remark, create_time, update_time)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(table_ref) DO UPDATE SET
                         status=excluded.status, remark=excluded.remark, update_time=excluded.update_time""",
                    (table_ref, status, remark, _now(), _now()),
                )
        finally:
            conn.close()

    def read(self, table_ref=None):
        """查询：无参→全部；给 table_ref→单条 dict 或 None。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if table_ref is None:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM data_state_check ORDER BY table_ref")]
            row = conn.execute(
                "SELECT * FROM data_state_check WHERE table_ref=?", (table_ref,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


if __name__ == "__main__":
    print("read() 全部:", [(r["table_ref"], r["status"]) for r in DataStateCheck().read()])