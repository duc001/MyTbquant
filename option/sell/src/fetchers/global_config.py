"""global_config 表数据管理（库表设计 §2.1）。

全局配置表：只能手工配置入库，无外部数据源。
对外方法：
  fetch()      空方法（本表无外部源，不做拉取）
  check()      全局检查——校验表中所有配置行
  check_row()  指定行检查——校验单条 (group, key)
  read()       列表查询（全部 / 按 group）
  read_line()  单行查询（按 group, key）
check()/check_row() 成功返回 SUCCESS（'success'）、失败返回失败信息字符串；
返回值由调用方判断并写入 data_state_check，本模块不代写该表。
表结构由 schema.apply_schema() 负责，本模块只读写本表数据、不建表。
"""
import logging
import sqlite3

from ..schema import DB_PATH

logger = logging.getLogger(__name__)

TABLE_REF = "global_config"
SUCCESS = "success"                              # check()/check_row() 成功返回值，调用方以此判定
_COLS = '"group", key, value, remark, status'   # 对外暴露的配置字段


class GlobalConfig:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def fetch(self):
        """空方法：本表数据无法从外部拉取（仅手工配置），无拉取逻辑。"""
        pass

    @staticmethod
    def _row_problems(row):
        """校验单条配置行：status 可用 / value 非空，返回问题列表。"""
        problems = []
        tag = f"{row['group']}.{row['key']}"
        if row["value"] in (None, ""):
            problems.append(f"配置 {tag} 为空")
        if row["status"] not in (1,):
            problems.append(f"配置 {tag} 不可用(status={row['status']})")
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
        """全局检查：逐条校验表中所有配置（存在/status 可用/value 非空）。
        成功返回 'success'，失败返回失败信息字符串（供调用方写入 data_state_check）。"""
        problems = []
        for row in self.read():
            problems.extend(self._row_problems(row))
        return self._finish("全局检查", problems)

    def check_row(self, group, key):
        """指定行检查：校验某条 (group,key) 是否存在、status 可用、value 非空。
        成功返回 'success'，失败返回失败信息字符串。"""
        row = self.read_line(group, key)
        if row is None:
            return self._finish(f"检查 {group}.{key}", [f"配置 {group}.{key} 不存在"])
        return self._finish(f"检查 {group}.{key}", self._row_problems(row))

    def read(self, group=None):
        """列表查询：无参→全部配置行；给 group→该组全部行。返回 list[dict]。"""
        conn = self._connect()
        try:
            if group is None:
                rows = [dict(r) for r in conn.execute(
                    f'SELECT {_COLS} FROM global_config ORDER BY "group", key')]
            else:
                rows = [dict(r) for r in conn.execute(
                    f'SELECT {_COLS} FROM global_config WHERE "group"=? ORDER BY key', (group,))]
            logger.debug("[%s] read(group=%s) -> %d 行", TABLE_REF, group, len(rows))
            return rows
        finally:
            conn.close()

    def read_line(self, group, key):
        """单行查询：返回某条 (group,key) 配置 dict，不存在返回 None。"""
        conn = self._connect()
        try:
            row = conn.execute(
                f'SELECT {_COLS} FROM global_config WHERE "group"=? AND key=?',
                (group, key)).fetchone()
            result = dict(row) if row else None
            logger.debug("[%s] read_line(%s.%s) -> %s", TABLE_REF, group, key,
                         "命中" if result else "未命中")
            return result
        finally:
            conn.close()


if __name__ == "__main__":
    from ..logutil import setup_logging
    setup_logging()  # 幂等：文件(DEBUG) + 控制台(INFO)
    m = GlobalConfig()
    # —— 播种测试数据：覆盖 正常 / 停用(status=0) / 空值 / 另一组 ——
    conn = m._connect()
    try:
        with conn:
            conn.executemany(
                """INSERT INTO global_config ("group", key, value, remark, status, create_time, update_time)
                   VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                   ON CONFLICT("group", key) DO UPDATE SET
                     value=excluded.value, remark=excluded.remark, status=excluded.status""",
                [
                    ("_demo", "good", "value1", "正常配置", 1),
                    ("_demo", "disabled", "value2", "已停用", 0),
                    ("_demo", "empty", "", "空值配置", 1),
                    ("_other", "good2", "value3", "另一组正常配置", 1),
                ],
            )
        logger.info("已播种测试数据：_demo(good/disabled/empty) + _other(good2)，共 4 行")

        logger.info("---- read() 全部 ----")
        for r in m.read():
            logger.info("  %s.%s = %r status=%s | %s",
                        r["group"], r["key"], r["value"], r["status"], r["remark"])
        logger.info("---- read('_demo') 该组 ----")
        for r in m.read("_demo"):
            logger.info("  %s.%s = %r", r["group"], r["key"], r["value"])
        logger.info("---- read_line 命中 / 未命中 ----")
        logger.info("  命中 _demo.good  -> %s", m.read_line("_demo", "good"))
        logger.info("  未命中 _demo.none -> %s", m.read_line("_demo", "none"))

        logger.info("---- check_row 逐条 ----")
        for k in ("good", "disabled", "empty", "none"):
            logger.info("  _demo.%-9s -> %s", k, m.check_row("_demo", k))

        logger.info("---- check() 全局 ----")
        logger.info("  -> %s", m.check())
    finally:
        # 清理测试数据，保持配置表干净（不留测试残留）
        with conn:
            conn.execute('DELETE FROM global_config WHERE "group" IN (\'_demo\', \'_other\')')
        conn.close()
        logger.info("已清理测试数据；清理后 read() = %s", m.read())