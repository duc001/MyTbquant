"""data_storage_rule 表数据管理（库表设计 §2.2）。

数据存储规则表（元数据面，manual 维护、无外部拉取源）：一张表一行记录，table_ref 为业务唯一键。
**本表主要为 data_state_check 服务**：它是每张被核对表的声明依据（数据源 / 窗口 / 口径标记），
检查程序据本表标记推导"应有条数 / 终态 / 正常·异常"，结论写入 data_state_check（1:1 经 table_ref 关联）；
本表自身不记状态、不每日变动——无任何日期/时序字段，只在新增表 / 改取数口径时人工维护，
每日变动一律记入 data_state_check（状态面）与 data_pull_detail（拉取流水）。
对外方法：
  fetch()      空方法（本表为声明表，无外部源，不做拉取）
  check()      全局检查——校验所有规则行
  check_row()  指定行检查——校验单条 table_ref 规则
  read()       列表查询（全部规则）
  read_line()  单行查询（按 table_ref）
check()/check_row() 成功返回 SUCCESS（'success'）、失败返回失败信息字符串；
返回值由调用方判断并写入 data_state_check，本模块不代写该表。
表结构由 schema.apply_schema() 负责，本模块只读写本表数据、不建表。
"""
import logging
import sqlite3

from ..schema import DB_PATH

logger = logging.getLogger(__name__)

TABLE_REF = "data_storage_rule"
SUCCESS = "success"  # check()/check_row() 成功返回值，调用方以此判定
_COLS = ("table_ref, table_name, date_type, data_source, pull_method, "
         "pull_count, store_mode, required_fields, status, remark")
# 必须非空的声明字段（缺任一则规则不完整、不可用）；table_ref 为业务键故不在此列
_REQUIRED_FIELDS = ("table_name", "date_type", "data_source", "pull_method", "store_mode")
# 枚举字段的合法取值（空值由必填校验负责，此处只管"非空但取值非法"）
_ENUM_FIELDS = {
    "date_type": ("bar", "static"),  # 本库无 tick 档
    "data_source": ("tbpy", "openvlab", "manual"),
}


class DataStorageRule:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def fetch(self):
        """空方法：本表为声明表（人工维护），无外部数据源，不做拉取。"""
        pass

    @staticmethod
    def _row_problems(row):
        """校验单条规则行：status 可用 / 必填字段非空 / 枚举字段取值合法，返回问题列表。"""
        problems = []
        ref = row["table_ref"]
        if row["status"] != 1:
            problems.append(f"规则 {ref} 不可用(status={row['status']})")
        for f in _REQUIRED_FIELDS:
            if row[f] in (None, ""):
                problems.append(f"规则 {ref} 字段 {f} 为空")
        for f, allowed in _ENUM_FIELDS.items():
            val = row[f]
            if val not in (None, "") and val not in allowed:
                problems.append(f"规则 {ref} 字段 {f} 非法取值({val})")
        return problems

    def _finish(self, label, problems):
        """收口检查结果：无问题记 info 返回 SUCCESS；有则逐条记 warning 并返回失败信息串。"""
        if not problems:
            logger.info("[%s] %s 通过", TABLE_REF, label)
            return SUCCESS
        for p in problems:
            logger.warning("[%s] %s", TABLE_REF, p)
        return "; ".join(problems)

    def check(self):
        """全局检查：逐条校验所有规则（status 可用/必填非空/枚举取值合法）。
        成功返回 'success'，失败返回失败信息字符串（供调用方写入 data_state_check）。"""
        problems = []
        for row in self.read():
            problems.extend(self._row_problems(row))
        return self._finish("全局检查", problems)

    def check_row(self, table_ref):
        """指定行检查：校验某条 table_ref 规则是否存在、status 可用、必填非空、
        枚举取值合法。成功返回 'success'，失败返回失败信息字符串。"""
        row = self.read_line(table_ref)
        if row is None:
            return self._finish(f"检查 {table_ref}", [f"规则 {table_ref} 不存在"])
        return self._finish(f"检查 {table_ref}", self._row_problems(row))

    def read(self):
        """列表查询：返回全部规则行（按 table_ref 排序）。返回 list[dict]。"""
        conn = self._connect()
        try:
            rows = [dict(r) for r in conn.execute(
                f'SELECT {_COLS} FROM data_storage_rule ORDER BY table_ref')]
            logger.debug("[%s] read() -> %d 行", TABLE_REF, len(rows))
            return rows
        finally:
            conn.close()

    def read_line(self, table_ref):
        """单行查询：返回某条 table_ref 规则 dict，不存在返回 None。"""
        conn = self._connect()
        try:
            row = conn.execute(
                f'SELECT {_COLS} FROM data_storage_rule WHERE table_ref=?',
                (table_ref,)).fetchone()
            result = dict(row) if row else None
            logger.debug("[%s] read_line(%s) -> %s", TABLE_REF, table_ref,
                         "命中" if result else "未命中")
            return result
        finally:
            conn.close()


if __name__ == "__main__":
    from ..logutil import setup_logging
    setup_logging()  # 幂等：文件(DEBUG) + 控制台(INFO)

    m = DataStorageRule()
    # —— 播种测试数据：覆盖 正常bar / 正常static / status=0无效 / 必填字段缺失 ——
    conn = m._connect()
    try:
        with conn:
            conn.executemany(
                """INSERT INTO data_storage_rule
                   (table_ref, table_name, date_type, data_source, pull_method,
                    pull_count, store_mode, required_fields, status, remark,
                    create_time, update_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                   ON CONFLICT(table_ref) DO UPDATE SET
                     table_name=excluded.table_name, date_type=excluded.date_type,
                     data_source=excluded.data_source, pull_method=excluded.pull_method,
                     pull_count=excluded.pull_count, store_mode=excluded.store_mode,
                     required_fields=excluded.required_fields, status=excluded.status,
                     remark=excluded.remark""",
                [
                    ("_demo_bar", "主连日线(演示)", "bar", "tbpy", "post_close", 120,
                     "incremental_upsert", "main_symbol,bar_date,close", 1, "正常bar规则"),
                    ("_demo_static", "品种信息(演示)", "static", "manual", "low_freq", None,
                     "full_window_rewrite", "product_cd", 1, "正常static规则"),
                    ("_demo_off", "已停用规则(演示)", "bar", "tbpy", "post_close", 120,
                     "incremental_upsert", "symbol,bar_date", 0, "无效规则(status=0)"),
                    ("_demo_incomplete", "缺字段规则(演示)", "bar", "", "post_close", 120,
                     "incremental_upsert", "symbol", 1, "data_source 缺失"),
                    ("_demo_badenum", "非法枚举规则(演示)", "tick", "wind", "post_close", 120,
                     "incremental_upsert", "symbol", 1, "date_type/data_source 取值非法"),
                ],
            )
        logger.info("已播种测试数据 5 条：_demo_bar/_demo_static/_demo_off/"
                    "_demo_incomplete/_demo_badenum")

        logger.info("---- read() 全部 ----")
        for r in m.read():
            logger.info("  %-18s | %s | %s/%s | %s | status=%s",
                        r["table_ref"], r["table_name"], r["date_type"],
                        r["data_source"], r["store_mode"], r["status"])
        logger.info("---- read_line 命中 / 未命中 ----")
        logger.info("  命中 _demo_bar   -> %s", m.read_line("_demo_bar"))
        logger.info("  未命中 _demo_none -> %s", m.read_line("_demo_none"))

        logger.info("---- check_row 逐条 ----")
        for ref in ("_demo_bar", "_demo_off", "_demo_incomplete", "_demo_badenum",
                    "_demo_none"):
            logger.info("  %-18s -> %s", ref, m.check_row(ref))

        logger.info("---- check() 全局 ----")
        logger.info("  -> %s", m.check())
    finally:
        with conn:
            conn.execute("DELETE FROM data_storage_rule WHERE table_ref LIKE '_demo%'")
        conn.close()
        logger.info("已清理测试数据；清理后 read() 剩余 %d 行", len(m.read()))