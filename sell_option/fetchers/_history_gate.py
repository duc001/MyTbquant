"""进程级串行令牌：包裹 ``tbpy.get_history_n``，遵守「1 次成功 / 60s」硬性全局限流。

实测结论（对 tbpy 探针验证，2026-10-02）
--------------------------------------
1. 限流是**硬性、进程级全局**的「1 次*成功* / 60s」：任一 ``get_history_n`` 成功后，紧接着
   换不同合约、同合约不同字段、第三合约的调用**全部**被限流，与合约/字段无关。
2. 被限流时的真实签名：返回空 dict（各 field 为 len=0 数组），且 ``get_last_err()`` 返回
   ``查询历史失败,查询限制,间隔=60000ms,合约数量=20``（含「查询限制 / 间隔=60000」）。
3. ``get_last_err()`` 是**粘滞**的：成功调用之后它仍可能滞留上一段 throttle 文本 → 判定
   throttle 必须以「结果是否为空」为先决（有数据即成功，忽略粘滞 err）。
4. 间隔精确 60000ms。

设计（最优编排）
----------------
- **成功后才排冷却**：任一次成功占用 60s 额度，立即把 ``_COOLDOWN_UNTIL`` 设为
  ``now + GAP``，使下一调用在真正 <60s 时才等待；若中间自然过去了 ≥60s 真实时间则不空等。
- **空结果不清冷却**：真无数据（空数组 + 无 throttle err）未占用额度，下一调用可立即发，
  不白等 60s（这点是纯盲睡 62s 做不到的）。
- **限流签名兜底退避**：万一在冷却判断之外仍撞上 throttle（err 含限流特征），退避一个窗口
  并重试，避免把一次调用白白 throttle 成 MISSING。
- 全局锁保证退避期间其它取数器不会也来撞额度；重试耗尽仍限流则如实返回空交下游判 MISSING。

用法
----
把 ``tbpy.get_history_n(a, b, **kw)`` 换成 ``gated_get_history_n(tbpy, a, b, **kw)``。
``tbpy`` 作为首个参数传入，本模块不在导入期依赖 tbpy，便于离线 / 测试。

环境变量
--------
- ``SELLOPT_HISTORY_GAP``    ：成功后的冷却秒（默认 ``62``，含 2s 余量覆盖时钟差）。
- ``SELLOPT_HISTORY_RETRY``  ：撞限流后的最大重试次数（默认 ``2``）。
- ``SELLOPT_HISTORY_DEBUG``  ：非 0 打印等待 / 退避信息。
"""
from __future__ import annotations

import os
import threading
import time

_LOCK = threading.Lock()
_COOLDOWN_UNTIL = 0.0  # 上次成功后，最早允许再发"成功"请求的时刻（time.monotonic()）
_MIN_GAP = float(os.getenv("SELLOPT_HISTORY_GAP", "62"))    # 成功后冷却秒
_MAX_RETRY = int(os.getenv("SELLOPT_HISTORY_RETRY", "2"))   # 撞限流重试次数
_DEBUG = bool(os.getenv("SELLOPT_HISTORY_DEBUG", ""))

# 限流判定签名（实测 err 文本「查询历史失败,查询限制,间隔=60000ms,合约数量=20」）。
# 只取"限流专属"令牌，避免把其它失败（网络等）也当成限流去退避。
_THROTTLE_HINTS = ("查询限制", "间隔=60000", "限流", "频繁", "过快")


def _has_data(result) -> bool:
    """结果是否真正含数据（非空数组 / 标量），而非空 dict（各 field len=0）。

    tbpy 被限流或真无数据时都返回 ``{"field": <len=0 数组>}``——这是"无数据"，
    不应算作"成功占用额度"。只有至少一个 field 带非空数组 / 标量才算有数据。
    """
    if not result:
        return False
    if isinstance(result, dict):
        for v in result.values():
            if hasattr(v, "__len__"):
                if len(v) > 0:
                    return True
            else:  # 标量（int/float/str）
                return True
        return False
    return True  # 列表 / 标量等非 dict：非空即视为有数据


def _is_throttle(tbpy, result) -> bool:
    """结果无数据 且 err 含限流特征 → 视为被 throttle。

    注意：``get_last_err()`` 粘滞，成功调用后也可能滞留 throttle 文本；故先以
    ``_has_data`` 断定成功——有数据即成功，不需看 err。
    """
    if _has_data(result):
        return False  # 有数据 = 成功（忽略粘滞 err）
    err = ""
    try:
        err = (tbpy.get_last_err() or "") if tbpy is not None else ""
    except Exception:  # noqa: BLE001
        err = ""
    if not err:
        return False  # 空且无错 → 真无数据，非限流
    return any(h in err for h in _THROTTLE_HINTS)


def gated_get_history_n(tbpy, *args, **kwargs):
    """遵守 60s 全局限流的 ``tbpy.get_history_n`` 代理。入参与原接口完全一致。"""
    global _COOLDOWN_UNTIL
    with _LOCK:
        # 成功后的冷却期内才等待；平时（或已自然过去 ≥60s）直接发
        wait = _COOLDOWN_UNTIL - time.monotonic()
        if wait > 0:
            if _DEBUG:
                print(f"[history-gate] 冷却等待 {wait:.1f}s …", flush=True)
            time.sleep(wait)

        r = tbpy.get_history_n(*args, **kwargs)
        if _is_throttle(tbpy, r):
            # 兜底：撞上限流 → 退避一个窗口并重试
            for _ in range(_MAX_RETRY):
                if _DEBUG:
                    print(f"[history-gate] 检测到限流，退避 {_MIN_GAP:.0f}s 后重试", flush=True)
                time.sleep(_MIN_GAP)
                r = tbpy.get_history_n(*args, **kwargs)
                if not _is_throttle(tbpy, r):
                    break

        # 成功占用额度 → 排冷却；空结果（真无数据/被限流耗尽）未占用 → 清冷却，下一调用可立即发
        if _has_data(r):
            _COOLDOWN_UNTIL = time.monotonic() + _MIN_GAP
        else:
            _COOLDOWN_UNTIL = 0.0
        return r


def reset_gate() -> None:
    """测试 / 重连后清空冷却状态（可选）。"""
    global _COOLDOWN_UNTIL
    with _LOCK:
        _COOLDOWN_UNTIL = 0.0
