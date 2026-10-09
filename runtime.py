"""tbpy 运行时辅助模块。

封装"连接/断开"等通用逻辑，并处理不同 tbpy 版本的行为差异：
按文档 init() 返回 bool，但实测 tbpy 3.12.3 在目标客户端(如 tbquant3.exe)
未启动时会直接抛出 SystemError/RuntimeError。这里统一捕获，向上返回 bool，
避免调用方脚本因未启动平台而崩溃。

连接状态进程内幂等：同一进程多次 connect 只真正 init 一次，并统一记录日志，
便于在统一日志里观察"与 tbpy 的交互"（成功 / 失败原因）。
"""
import logging
import tbpy

logger = logging.getLogger(__name__)

_connected = False


def connect(product_key: str) -> bool:
    """连接到量化平台，成功返回 True，失败返回 False（并记录原因）。

    幂等：进程内已连接则直接返回 True，不再重复 init；成功/失败均记入统一日志。
    """
    global _connected
    if _connected:
        return True
    try:
        ok = tbpy.init(product_key)
    except Exception as exc:  # noqa: BLE001 - 平台未启动时可能抛 SystemError
        # SystemError 信息晦涩，其 __cause__/__context__ 通常是更明确的 RuntimeError
        root = exc.__cause__ or exc.__context__
        detail = str(root) if root is not None else str(exc)
        logger.error("tbpy.init 异常: %s", detail)
        logger.error("    请先启动对应的 TBQuant 客户端并登录，再运行本程序。")
        return False

    if not ok:
        reason = ""
        try:
            reason = tbpy.get_last_err()
        except Exception:  # noqa: BLE001
            pass
        logger.error("tbpy.init 失败: %s", reason)
        return False
    _connected = True
    logger.info("tbpy.init 成功 · product_key=%s", product_key)
    return True


def disconnect() -> None:
    """安全断开连接，忽略退出阶段的异常。"""
    global _connected
    try:
        tbpy.exit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("tbpy.exit 异常(可忽略): %s", exc)
    _connected = False
