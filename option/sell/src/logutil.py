"""option/sell 统一日志：写入文件 + 打印控制台。

沿用项目约定格式（strategies/sell_option/sell_option/logutil.py）：每条日志含
时间 · 级别 · 记录器名 · 模块.函数:行号 · 正文，便于定位来源。

使用：
- 入口最前面调用一次 `setup_logging()`（幂等）：挂 文件(DEBUG,按日拆分) + 控制台(INFO)。
- 各模块用标准 `logging.getLogger(__name__)` 记日志即可，无需依赖本模块。
"""
import logging
import sys
from datetime import date
from pathlib import Path

# 日志目录：option/sell/logs（与 sell_option/logs 一致，按日期拆成单文件）
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

FMT = "%(asctime)s | %(levelname)-5s | %(name)s | %(module)s.%(funcName)s:%(lineno)d | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

_CONFIGURED = False


def _daily_log_file() -> Path:
    """按日期拆分的日志文件：logs/app-YYYY-MM-DD.log（每天一个，追加）。"""
    return _LOG_DIR / f"app-{date.today():%Y-%m-%d}.log"


def get_logger(name=None):
    """按名称取 logger（等价 logging.getLogger）。"""
    return logging.getLogger(name)


def setup_logging(level=logging.DEBUG, *, console=True, log_file=None):
    """初始化统一日志（幂等，重复调用只生效一次）。

    - 文件：logs/app-YYYY-MM-DD.log（按日拆分、追加），级别 DEBUG，更全。
    - 控制台：输出到原始 stderr，级别 INFO，与文件同格式。
    """
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        return root
    _CONFIGURED = True

    root.setLevel(level)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    target = Path(log_file) if log_file else _daily_log_file()

    fmt = logging.Formatter(FMT, datefmt=DATEFMT)

    fh = logging.FileHandler(target, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler(sys.__stderr__)  # 原始 stderr，避免与重定向互相递归
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        root.addHandler(ch)

    root.info("统一日志启动 · 文件=%s", target)
    return root