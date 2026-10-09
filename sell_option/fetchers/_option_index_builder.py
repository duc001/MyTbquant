"""内部 · 隔离子进程构建 option_index（get_symbols 堆损坏隔离，§11.3.6）。

**为什么单独一个模块**：`tbpy.get_symbols()` 物化 ~11 万条会损坏进程堆——之后同进程
任何原生调用（get_instrument / get_main_instrument）都可能返回垃圾甚至 access violation。
故 get_symbols 只能在这种"用完即退"的独立子进程里调；主进程（d06_option_chain）
只读它落盘的 JSON，全程不碰 get_symbols。

本子进程职责（只做发现，不补到期日）：
  connect → get_symbols()（一次，模块级持有推迟析构）→ 逐条正则解析商品期权 symbol
  → 按 47 品种分组 → 原子落盘 data/option_index.json（symbol + code/month/cp/strike，
  month_expiry 全置 null）→ 退出。

到期日（delisted_date）不在 symbol 字符串里，且堆损坏后 get_instrument 不可靠，
故由主进程在**干净进程**里按 distinct(品种,月)≈313 次 get_instrument 另行补齐并回写。

一般由 d08 自动 spawn；也可手动运行重建：
    .venv\\Scripts\\python.exe -m sell_option.fetchers._option_index_builder
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

# 允许 -m 与脚本直跑两种方式（与其余 dXX 单元一致的双兼容导入）
try:  # 包方式（python -m / import）：相对导入
    from .d01_variety_list import fetch_d01
except ImportError:  # 脚本直跑：补项目根后绝对导入
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.d01_variety_list import fetch_d01

# 商品期货期权 sec_id（边界表 §二.4.2）：代码+月份[-]C/P[-]行权价
_OPT_RE = re.compile(r"^([A-Za-z]{1,2})(\d{3,4})-?([CP])-?(\d+)$")
_VALID_EXCHANGES = {"DCE", "CZCE", "SHFE", "GFEX", "INE"}

DEFAULT_INDEX_PATH = Path(__file__).resolve().parent.parent / "data" / "option_index.json"

# get_symbols 结果必须模块级持有：临时表达式推导会立即析构 ~11 万对象、当场损坏堆（§11.3.6）
_SYMBOLS: list | None = None


def _atomic_write_json(path: Path, payload: dict) -> None:
    """先写临时文件再 replace，避免半截 JSON 覆盖掉上一份可用索引。"""
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


def build_index(today_iso: str, index_path: Path = DEFAULT_INDEX_PATH) -> dict:
    """连 tbpy、get_symbols、解析分组、原子落盘。返回统计 dict（也用于自检打印）。

    失败（未连接 / 空返回）时**不落盘**、抛异常，由调用方决定是否降级。
    """
    global _SYMBOLS
    import tbpy  # 延迟导入：仅在真正要连平台时才需要

    try:  # 子进程内也需要项目根在 path（-m 方式已具备，直跑兜底）
        import config
        from runtime import connect
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import config
        from runtime import connect

    if not connect(config.PRODUCT_KEY):
        raise RuntimeError("tbpy 未连接：请先启动并登录 TBQuant3 客户端")

    _SYMBOLS = tbpy.get_symbols()  # 一次性物化，模块级持有
    if not _SYMBOLS:
        raise RuntimeError("get_symbols 返回空（平台异常或权限不足）")

    # 47 品种：小写码 → (规范码, 交易所)，用于过滤与规范大小写
    canon: dict[str, tuple[str, str]] = {}
    for v in fetch_d01():
        canon[v.code.lower()] = (v.code, v.exchange)

    varieties: dict[str, dict] = {}
    month_sets: dict[str, set[str]] = defaultdict(set)
    total = 0
    for sym in _SYMBOLS:
        if not isinstance(sym, str) or "." not in sym:
            continue
        sec_id, _, exch = sym.partition(".")
        if exch not in _VALID_EXCHANGES:
            continue
        m = _OPT_RE.match(sec_id)
        if not m:
            continue
        key = m.group(1).lower()
        if key not in canon:
            continue
        code, _exch_tbl = canon[key]
        month, cp, strike = m.group(2), m.group(3).upper(), float(m.group(4))
        entry = varieties.setdefault(code, {"exchange": exch, "contracts": []})
        entry["contracts"].append({"symbol": sym, "month": month, "cp": cp, "strike": strike})
        month_sets[code].add(month)
        total += 1

    if not varieties:
        raise RuntimeError("get_symbols 未匹配到任何 47 品种期权（解析正则或平台数据异常）")

    # month_expiry 先全置 null，由主进程干净进程补齐（本子进程堆已损，不做 get_instrument）
    for code, entry in varieties.items():
        entry["contracts"].sort(key=lambda c: (c["month"], c["cp"], c["strike"]))
        entry["month_expiry"] = {mo: None for mo in sorted(month_sets[code])}

    payload = {
        "build_date": today_iso,
        "generator": "tbpy.get_symbols",
        "total_options": total,
        "variety_count": len(varieties),
        "expiry_complete": False,   # 主进程补到期日后置 True
        "varieties": dict(sorted(varieties.items())),
    }
    _atomic_write_json(index_path, payload)
    return {"total": total, "varieties": len(varieties), "months": sum(len(s) for s in month_sets.values())}


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    import datetime as dt

    today_iso = dt.date.today().isoformat()
    print(f"[option_index builder] 隔离子进程构建，build_date={today_iso}")
    stats = build_index(today_iso)
    print(f"[option_index builder] 落盘 {DEFAULT_INDEX_PATH}")
    print(f"  期权合约 {stats['total']} 个 · 品种 {stats['varieties']} 个 · distinct(品种,月) {stats['months']} 个")
    print("  注：到期日由主进程 get_instrument 补齐（本子进程不碰，规避堆损坏）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
