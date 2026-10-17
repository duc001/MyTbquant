"""global_config 表数据管理（库表设计 §2.1）。

全局配置表（manual 维护、无外部拉取源）：
  fetch()空方法（无外部源）/ check()全局检查 + check_row()指定行检查 / read()列表查询 + read_line()单行查询。
表结构由 schema.apply_schema() 负责创建与迁移，本模块只读写数据、不建表（只要结果，不管过程）。
"""
import os
import sqlite3
import sys

try:  # 包内导入
    from ..schema import DB_PATH
    from .data_state_check import DataStateCheck
except ImportError:  # 脚本直接运行
    _src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, _src)
    sys.path.insert(0, os.path.join(_src, "fetchers"))
    from schema import DB_PATH
    from data_state_check import DataStateCheck

TABLE_REF = "global_config"


class GlobalConfig:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

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
        if row["status"] != 1:
            problems.append(f"配置 {tag} 不可用(status={row['status']})")
        return problems

    def check(self):
        """全局检查：逐条校验表中所有配置（存在/status 可用/value 非空），返回是否通过
        （True=通过 / False=不通过）；具体问题打印到日志，表级状态经 DataStateCheck 回写。"""
        problems = []
        for row in self.read():
            problems.extend(self._row_problems(row))
        for p in problems:
            print(f"[{TABLE_REF}] {p}")
        status = "normal" if not problems else "abnormal"
        # 表级状态回写 data_state_check：调用其管理类方法，不直接操作该表
        DataStateCheck().record(TABLE_REF, status, "; ".join(problems))
        return not problems

    def check_row(self, group, key):
        """指定行检查：校验某条 (group,key) 是否存在、status 可用、value 非空。
        返回是否通过（True=通过 / False=不通过）；具体问题打印到日志。"""
        row = self.read_line(group, key)
        if row is None:
            print(f"[{TABLE_REF}] 配置 {group}.{key} 不存在")
            return False
        problems = self._row_problems(row)
        for p in problems:
            print(f"[{TABLE_REF}] {p}")
        return not problems

    def read(self, group=None):
        """查询（列表）：无参→全部配置行；给 group→该组全部行。返回 list[dict]。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cols = '"group", key, value, remark, status'
        try:
            if group is None:
                return [dict(r) for r in conn.execute(
                    f'SELECT {cols} FROM global_config ORDER BY "group", key')]
            return [dict(r) for r in conn.execute(
                f'SELECT {cols} FROM global_config WHERE "group"=? ORDER BY key', (group,))]
        finally:
            conn.close()

    def read_line(self, group, key):
        """查询（单行）：返回某条 (group,key) 配置 dict，不存在返回 None。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                'SELECT "group", key, value, remark, status FROM global_config'
                ' WHERE "group"=? AND key=?', (group, key)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


if __name__ == "__main__":
    m = GlobalConfig()
    print("check():", m.check())
    print("check_row(path, db):", m.check_row("path", "db"))