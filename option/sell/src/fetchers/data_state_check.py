"""data_state_check 表数据管理（库表设计 §2.3）。

数据状态核对汇总表：由各 fetcher.check() 调用 record() 回写自己的结论（表归属本类，
调用方不直接操作本表）；本模块提供写入 record / 查询 read。
record() 登记该表"已就绪的数据区间 + 当前状态 + 最近一次明细指针"，
结论来源声明见 data_storage_rule（一张表一行，1:1 经 table_ref 关联）。
表结构由 schema.apply_schema() 负责创建，本模块只读写数据、不建表（只要结果，不管过程）。
"""
import logging
import sqlite3
from datetime import datetime

from ..schema import DB_PATH

logger = logging.getLogger(__name__)

TABLE_REF = "data_state_check"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DataStateCheck:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def record(self, table_ref, status, remark="",
               bar_start_date=None, bar_end_date=None, last_detail_id=None):
        """登记 / 更新某表的数据状态结论（由其它 fetcher 的 check() 调用）。

        bar_start_date / bar_end_date：该表已就绪的数据区间（bar 档为 bar_date 区间；
        对无 bar 概念的表如配置表留 None）。last_detail_id：最近一次拉取明细行 id
        （→ data_pull_detail.id，无明细时留 None）。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO data_state_check
                       (table_ref, bar_start_date, bar_end_date, status,
                        last_detail_id, remark, create_time, update_time)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(table_ref) DO UPDATE SET
                         bar_start_date=excluded.bar_start_date,
                         bar_end_date=excluded.bar_end_date,
                         status=excluded.status,
                         last_detail_id=excluded.last_detail_id,
                         remark=excluded.remark, update_time=excluded.update_time""",
                    (table_ref, bar_start_date, bar_end_date, status,
                     last_detail_id, remark, _now(), _now()),
                )
            logger.debug("[%s] record(%s) status=%s 区间=[%s, %s] last_detail_id=%s",
                         TABLE_REF, table_ref, status, bar_start_date, bar_end_date,
                         last_detail_id)
        finally:
            conn.close()

    def read(self, table_ref=None):
        """查询：无参→全部；给 table_ref→单条 dict 或 None。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if table_ref is None:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM data_state_check ORDER BY table_ref")]
                logger.debug("[%s] read() -> %d 行", TABLE_REF, len(rows))
                return rows
            row = conn.execute(
                "SELECT * FROM data_state_check WHERE table_ref=?", (table_ref,)).fetchone()
            result = dict(row) if row else None
            logger.debug("[%s] read(%s) -> %s", TABLE_REF, table_ref,
                         "命中" if result else "未命中")
            return result
        finally:
            conn.close()


if __name__ == "__main__":
    from ..logutil import setup_logging
    setup_logging()  # 幂等：文件(DEBUG) + 控制台(INFO)

    # 只演示查询（record 为真实写入，不在本__main__ 里对正式库执行）
    for r in DataStateCheck().read():
        logger.info("  %-22s | %-8s | 区间=[%s, %s] | last_detail_id=%s | %s",
                    r["table_ref"], r["status"], r["bar_start_date"],
                    r["bar_end_date"], r["last_detail_id"], r["remark"])