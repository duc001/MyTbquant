"""品种边界表加载器（D01–D03 / D04–D05 / D11 的共用底座）。

`品种边界表.md` §一是权威 47 品种数据表，D01 名单 / D02 symbol 模板 / D03 主力指数
/ D04 保证金率 / D05 组合优惠系数 / D11 乘数 均来自这张表，故解析逻辑只写一遍，
各数据项单元从中取自己那一列。

按 §二.7 取数对接约定实现：**按表头名定位列**（不写死列序，增删列不破坏加载器）、
UTF-8 纯文本、逐行解析 markdown 表格。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

# 边界表路径（唯一权威源；改动须人工）。文档统一收口到 docs/ 下。
BOUNDARY_TABLE_PATH = Path(__file__).resolve().parent.parent.parent / "docs" / "品种边界表.md"

# §一 数据表的表头首尾列，用于定位本表（区别于文档里其它说明性表格）
_HEADER_FIRST = "标的编码"
_HEADER_LAST = "期权样例P"

_SEP_CELL = re.compile(r"^:?-{3,}:?$")  # markdown 表头下分隔行单元格，如 --- / :---:


def _split_row(line: str) -> list[str]:
    """切分 markdown 表行：去掉首尾管道后按 | 拆，逐格去空白。"""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


@lru_cache(maxsize=1)
def load_boundary_table(path: Path = BOUNDARY_TABLE_PATH) -> list[dict[str, str]]:
    """加载 §一 数据表，返回 [{表头名: 单元格文本}]，保留原始文本不做类型转换。

    列值转类型、对账等业务处理由各 D 项单元自行完成；此处只保证"读全、读对位"。
    找不到表 / 行列数与表头不符 → 直接抛错（静态参数缺失属全局故障，不允许静默降级）。
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for i, line in enumerate(lines):
        is_table_line = line.lstrip().startswith("|")
        cells = _split_row(line) if is_table_line else []
        if header is None:  # 尚未定位到表头：只扫描管道行
            if is_table_line and cells and cells[0] == _HEADER_FIRST and cells[-1] == _HEADER_LAST:
                header = cells
            continue
        # 表头已定位：遇到首个非表行（空行/分隔线/下一节）即表结束
        if not is_table_line:
            break
        if all(_SEP_CELL.match(c) for c in cells):  # 表头下分隔行
            continue
        if len(cells) != len(header):
            raise ValueError(
                f"边界表第 {i + 1} 行列数 {len(cells)} 与表头 {len(header)} 不符：{line!r}"
            )
        rows.append(dict(zip(header, cells)))

    if header is None:
        raise ValueError(f"未在 {path} 找到表头为「{_HEADER_FIRST}…{_HEADER_LAST}」的数据表")
    if not rows:
        raise ValueError(f"{path} 的边界表定位成功但没有任何数据行")
    return rows
