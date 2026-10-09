"""内部 · 隔离子进程构建 main_month.json（各品种主力合约月，供 D07 is_main 展示标记）。

**为什么单独一个子进程**：tbpy.get_main_instrument 在**未连接状态下裸调会 access violation**
（2026-10-02 实测：d07 自检主进程直调 2/2 必崩 0xC0000005；connect 后调用则正常返回）。
主进程直调还须与 D08/D12 的 connect→用→exit 交错共享全局连接态，任何一环 exit 后再调即踩雷。
故原生调用只在这种"用完即退"的独立子进程里做；主进程（d07_candidate_months）
只读它落盘的 JSON，全程不碰 get_main_instrument——与 _option_index_builder 同一隔离范式（§11.3.6）。

本子进程职责：
  connect → 逐品种 get_main_instrument（每品种 1 次、inst.symbol 只读一次）→ 解析月份
  （expand_month 补全 4 位）→ 原子落盘 data/main_month.json → 断开 → 退出。

一般由 d07 provider 自动 spawn；也可手动运行重建：
    .venv\\Scripts\\python.exe -m sell_option.fetchers._main_month_builder
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

# 允许 -m 与脚本直跑两种方式（与其余 dXX 单元一致的双兼容导入）
try:
    from .d01_variety_list import fetch_d01
    from .models import expand_month
except ImportError:  # 脚本直跑：补项目根后绝对导入
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.d01_variety_list import fetch_d01
    from sell_option.fetchers.models import expand_month

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "main_month.json"
_DIGITS_RE = re.compile(r"\d+")


def _month_from_symbol(symbol: str, today) -> str | None:
    """'m2701.DCE'/'AP701.CZCE' → 4 位标的月（'2701'/'2701'）；解析不出 → None。"""
    body = symbol.rsplit(".", 1)[0]
    runs = _DIGITS_RE.findall(body)
    if not runs:
        return None
    digits = runs[-1]
    month = digits[-4:] if len(digits) >= 4 else (digits[-3:] if len(digits) == 3 else "")
    return expand_month(month, today) if month else None


def _atomic_write_json(path: Path, payload: dict) -> None:
    """先写临时文件再 replace，避免半截 JSON 覆盖上一份可用缓存。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build(today_iso: str, path: Path = DEFAULT_PATH) -> dict:
    """连 tbpy、逐品种取主力月、原子落盘。返回落盘 payload。

    连接失败即抛（不落盘、不写半成品）；单品种拿不到只记 None、不阻断其余品种。
    """
    import datetime as dt
    import tbpy  # 延迟导入：仅在真正要连平台时才需要

    try:
        import config
        from runtime import connect, disconnect
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import config
        from runtime import connect, disconnect

    if not connect(config.PRODUCT_KEY):
        raise RuntimeError("tbpy 未连接：请先启动并登录 TBQuant3 客户端")

    today = dt.date.fromisoformat(today_iso)
    months: dict[str, dict] = {}
    try:
        for v in fetch_d01():
            code, exchange = v.code, v.exchange
            entry = {"symbol": None, "month": None}
            try:
                inst = tbpy.get_main_instrument(underlying_symbol=f"{code}.{exchange}")
                sym = inst.symbol if inst is not None else None  # 每属性只读一次（§11.3.6）
                if sym:
                    entry["symbol"] = sym
                    entry["month"] = _month_from_symbol(sym, today)
            except Exception:  # noqa: BLE001 - 单品种失败不影响其余
                pass
            months[code] = entry
    finally:
        disconnect()

    payload = {
        "build_date": today_iso,
        "generator": "tbpy.get_main_instrument",
        "variety_count": len(months),
        "resolved": sum(1 for e in months.values() if e["month"]),
        "months": months,
    }
    _atomic_write_json(path, payload)
    return payload


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    import datetime as dt

    today_iso = dt.date.today().isoformat()
    print(f"[main_month builder] 隔离子进程构建，build_date={today_iso}")
    payload = build(today_iso)
    print(f"[main_month builder] 落盘 {DEFAULT_PATH}")
    print(f"  品种 {payload['variety_count']} 个 · 主力月解析成功 {payload['resolved']} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
