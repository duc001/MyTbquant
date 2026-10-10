"""统一日志（§运维·单文件日志）。

设计目标（用户诉求）：
- **按日期拆分的统一日志目录**：所有输出（包括存量代码的 `print`）统一归档到
  `<sell_option>/logs/app-YYYY-MM-DD.log`（每天一个文件），并复制一份到控制台（实时可见）。
- **统一格式**，每条都带：时间 · 级别 · 记录器名 · 模块.函数:行号 · 正文。
- **可执行洞察**：含「运行什么 / 类·方法 / 请求参数 / 输出参数 / 关键信息 / 流转」。

实现要点：
1. `setup_logging()` 幂等，挂一个 FileHandler（单文件）+ 一个控制台 Handler（用原始 stderr，
   避免递归）；并把 `sys.stdout/stderr` 重定向到日志——存量 `print` 会自动进同一文件，
   且**保留真实调用者位置**（模块/函数/行号），无需改动 46 个文件的打印。
2. `get_logger(name)`：按模块取 logger（推荐 `get_logger(__name__)`）。
3. `log_call` 装饰器：自动记录函数「进入·请求参数 → 返回·输出参数 · 耗时」，异常也记。
4. `flow(span, **kv)` 上下文：标注一段流程的开始/结束（流转信息）。

使用：在入口（`sellopt.py` / `main.py`）最前面 `from sell_option.logutil import setup_logging; setup_logging()`。
"""
from __future__ import annotations

import contextlib
import functools
import inspect
import io
import logging
import sys
import time
from datetime import date as _date
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径与格式
# ---------------------------------------------------------------------------
_LOG_DIR = Path(__file__).resolve().parent / "logs"


def _daily_log_file() -> Path:
    """按日期拆分的日志文件名：`app-YYYY-MM-DD.log`（每天一个文件）。"""
    return _LOG_DIR / f"app-{_date.today():%Y-%m-%d}.log"

FMT = "%(asctime)s | %(levelname)-5s | %(name)s | %(module)s.%(funcName)s:%(lineno)d | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

_CONFIGURED = False


# ---------------------------------------------------------------------------
# 参数/结果摘要（避免把巨大对象整段塞进日志）
# ---------------------------------------------------------------------------
def _summ(obj, limit: int = 400) -> str:
    """把任意对象压成可读摘要；列表/字典截断并标注长度；超长截断。"""
    try:
        if isinstance(obj, (list, tuple)) and len(obj) > 6:
            inner = ", ".join(_summ(x, 80) for x in obj[:6])
            return f"{type(obj).__name__}[{len(obj)}]({inner}, …)"
        if isinstance(obj, dict):
            ks = list(obj.keys())
            shown = ks[:6]
            body = ", ".join(f"{k}={_summ(obj[k], 80)}" for k in shown)
            tail = "…}" if len(obj) > 6 else "}"
            return "{" + body + tail
        r = repr(obj)
    except Exception:  # noqa: BLE001 - repr 也可能抛（极少）
        r = "<unrepr>"
    if r is None:
        return "None"
    if len(r) > limit:
        r = r[:limit] + f"…(+{len(r) - limit})"
    return r


# ---------------------------------------------------------------------------
# stdout/stderr → 日志（保留真实调用者位置）
# ---------------------------------------------------------------------------
def _find_echo_handler():
    """在调用栈中找『正把日志写回被重定向的 stdout/stderr』的 StreamHandler。

    返回该 handler（回环源头），找不到返回 None。仅匹配 stream 指向被重定向
    流（即本模块安装的 `_StreamToLogger`）的 handler；指向真实文件 / 原始
    stderr 的正常 handler 不会被误伤。"""
    f = sys._getframe(1)  # 本函数的调用方（即 _StreamToLogger.write）
    seen = 0
    while f is not None and seen < 64:
        seen += 1
        if f.f_code.co_name == "emit":
            self_ = f.f_locals.get("self")
            if isinstance(self_, logging.StreamHandler) and \
               getattr(self_, "stream", None) in (sys.stdout, sys.stderr):
                return self_
        f = f.f_back
    return None


def _neutralize_handler(h: logging.Handler) -> None:
    """把回环 handler 从所属 logger 摘除并改指原始 stderr，永久停止回环。"""
    try:
        h.stream = sys.__stderr__  # 即便残留引用也只写原始 stderr，不再灌入日志
    except Exception:  # noqa: BLE001
        pass
    root = logging.getLogger()
    if h in root.handlers:
        root.removeHandler(h)
    for lg in list(logging.Logger.manager.loggerDict.values()):
        if h in getattr(lg, "handlers", ()):
            lg.removeHandler(h)


class _StreamToLogger(io.TextIOBase):
    """把 `print()` 输出转发到 logger，使存量 print 也进统一日志文件。

    通过 `sys._getframe` 跳过本模块帧，记录【真正的 print 调用位置】。

    回环自愈：若某 logging handler 把日志又写回被重定向的 stdout/stderr
    （例如脚本在 `setup_logging()` 之后又 `addHandler(StreamHandler(sys.stdout))`，
    捕获到的正是本重定向流），`write` 会侦测并摘除该 handler，内容直接落原始
    stderr——既不会无限循环，也不会出现嵌套重复的日志行。"""

    def __init__(self, logger: logging.Logger, level: int):
        self._logger = logger
        self._level = level
        self._buf = ""

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    def write(self, s: str) -> int:
        if not s:
            return 0
        # 回环自愈：某 handler 把日志写回被重定向的 stdout/stderr → 摘除并直出原始 stderr
        echo = _find_echo_handler()
        if echo is not None:
            _neutralize_handler(echo)
            try:
                sys.__stderr__.write(s)
            except Exception:  # noqa: BLE001
                pass
            return len(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                _redirect_emit(self._logger, self._level, line.rstrip())
        return len(s)

    def flush(self) -> None:
        if self._buf:
            line = self._buf
            self._buf = ""
            if line.strip():
                _redirect_emit(self._logger, self._level, line.rstrip())


def _redirect_emit(logger: logging.Logger, level: int, msg: str) -> None:
    """构造一条「位置指向真实调用者」的日志记录。"""
    f = sys._getframe(1)  # 调用方 = _StreamToLogger.write（位于本文件）
    while f is not None and f.f_code.co_filename == __file__:
        f = f.f_back
    pathname = f.f_code.co_filename if f else "<unknown>"
    lineno = f.f_lineno if f else 0
    func = f.f_code.co_name if f else "<unknown>"
    try:
        record = logger.makeRecord(
            logger.name, level, pathname, lineno, msg, None, None, func
        )
    except Exception:  # noqa: BLE001 - 兜底
        logger.log(level, msg)
        return
    logger.handle(record)


def _emit_loc(logger: logging.Logger, level: int, msg: str, *,
              pathname: str, lineno: int, func: str) -> None:
    """构造一条「位置指向指定文件/行/函数」的日志，绕过 findCaller。

    用于装饰器/上下文：让日志的 `模块.函数:行号` 落在【被装饰函数】或
    【真实调用处】，而不是本模块内部的 wrapper。
    """
    try:
        record = logger.makeRecord(
            logger.name, level, pathname, lineno, msg, None, None, func
        )
    except Exception:  # noqa: BLE001 - 兜底
        logger.log(level, msg)
        return
    logger.handle(record)


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
def get_logger(name: str | None = None) -> logging.Logger:
    """按名称取 logger（推荐 `get_logger(__name__)`）。"""
    return logging.getLogger(name)


def setup_logging(level: int = logging.DEBUG, *,
                  console: bool = True,
                  capture_stdout: bool = True,
                  log_file: Path | None = None) -> logging.Logger:
    """初始化统一日志（幂等）。

    - 单文件（按日期拆分）：`<sell_option>/logs/app-YYYY-MM-DD.log`（每天一个，追加）。
    - 控制台：输出到原始 stderr（与文件同格式），级别 INFO（文件为 DEBUG，更全）。
    - 重定向 stdout/stderr：存量 print 也归档进同一文件，且带真实调用位置。
    """
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        return root
    _CONFIGURED = True

    root.setLevel(level)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    target = log_file or _daily_log_file()

    fmt = logging.Formatter(FMT, datefmt=DATEFMT)

    fh = logging.FileHandler(target, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler(sys.__stderr__)  # 用原始 stderr，避免递归
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        root.addHandler(ch)

    if capture_stdout:
        sys.stdout = _StreamToLogger(logging.getLogger("app.stdout"), logging.INFO)
        sys.stderr = _StreamToLogger(logging.getLogger("app.stderr"), logging.ERROR)

    root.info("=" * 72)
    root.info(f"统一日志启动 · 文件={target} · 命令即将执行")
    return root


def log_call(_fn=None, *, level: int = logging.INFO):
    """装饰器：自动记录函数「进入·请求参数 → 返回·输出参数 · 耗时」，异常也记。

    用法：`@log_call` / `@log_call()` / `@log_call(level=logging.DEBUG)`。
    """

    def deco(fn):
        logger = logging.getLogger(fn.__module__)
        qual = fn.__qualname__
        src = inspect.getsourcefile(fn) or "<unknown>"
        srcline = fn.__code__.co_firstlineno

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            _emit_loc(logger, level,
                      f">>> 进入 {qual} · 请求参数=({_summ((args, kwargs))})",
                      pathname=src, lineno=srcline, func=fn.__name__)
            try:
                res = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                ms = (time.perf_counter() - t0) * 1000
                _emit_loc(logger, logging.ERROR,
                          f"!!! 异常 {qual} 抛出 {type(e).__name__}: {e} (耗时 {ms:.0f}ms)",
                          pathname=src, lineno=srcline, func=fn.__name__)
                raise
            ms = (time.perf_counter() - t0) * 1000
            _emit_loc(logger, level,
                      f"<<< 返回 {qual} · 输出参数={_summ(res)} (耗时 {ms:.0f}ms)",
                      pathname=src, lineno=srcline, func=fn.__name__)
            return res

        return wrapper

    if _fn is not None and callable(_fn):
        return deco(_fn)
    return deco


@contextlib.contextmanager
def flow(span: str, level: int = logging.INFO, **kv):
    """上下文：标注一段流程的开始/结束（流转信息）。`**kv` 记录该段关键入参。

    日志位置落在调用 `with flow(...)` 的真实代码行（而非本模块），便于定位流转段落。
    """
    logger = logging.getLogger("app.flow")
    f = sys._getframe(1)
    # 跳过 @contextmanager 包装（contextlib）与本模块自身，定位真正写 `with flow(...)` 的代码行
    while f is not None and (f.f_code.co_filename == __file__
                            or "contextlib" in f.f_code.co_filename):
        f = f.f_back
    pathname = f.f_code.co_filename if f else "<unknown>"
    lineno = f.f_lineno if f else 0
    func = f.f_code.co_name if f else "<unknown>"
    tag = " ".join(f"{k}={_summ(v)}" for k, v in kv.items())
    _emit_loc(logger, level, f">>> 开始 {span}" + (f" · 入参={tag}" if tag else ""),
              pathname=pathname, lineno=lineno, func=func)
    t0 = time.perf_counter()
    try:
        yield
    except Exception as e:  # noqa: BLE001
        ms = (time.perf_counter() - t0) * 1000
        _emit_loc(logger, logging.ERROR,
                  f"!!! 中断 {span} {type(e).__name__}: {e} (耗时 {ms:.0f}ms)",
                  pathname=pathname, lineno=lineno, func=func)
        raise
    ms = (time.perf_counter() - t0) * 1000
    _emit_loc(logger, level, f"<<< 结束 {span} (耗时 {ms:.0f}ms)",
              pathname=pathname, lineno=lineno, func=func)
