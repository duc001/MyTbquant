#!/usr/bin/env python
"""期权卖出策略命令面入口（§11.2）：`python sellopt.py <命令>`。

薄封装——把 argv 转交 `sell_option.strategy.cli.main`（命令实现与可注入运行时都在 cli 模块内）。
命令：scan / check M [--dual] / exit / collect-oi -c M / refresh [--all] / scope / report；
全局 `--session open|closed` 可强制覆盖时段（默认逐品种自动探测）。

等价入口：`python -m sell_option.strategy.cli` 会跑 cli 的**真实数据自检**（非命令面）；真实命令走本脚本。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sell_option.logutil import setup_logging  # noqa: E402

setup_logging()  # 启动即初始化统一日志（单文件 + 控制台），须在导入 cli 之前，确保任何运行期 print 都被捕获

from sell_option.strategy.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
