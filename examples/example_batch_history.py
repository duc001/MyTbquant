"""示例：get_history_n 批量分页（20/批 + 62s 冷却）真实编排。

完整可跑，复刻各 fetcher（d08/d09/d12/d14）的真实取数编排：
  ① 合约列表分块，每块 ≤20（tbpy 单次查询上限）；
  ② 块间 sleep 62s（>60s 限流窗口）后，经 gated_get_history_n 拉取；
     gated 内部还会在「成功」后自动排冷却 + 限流兜底退避，双保险；
  ③ 解析返回：单合约扁平 {field:ndarray} / 多合约嵌套 {symbol:{field:ndarray}}。

需先启动并登录 TBQuant3 客户端。

用法：
    cd d:/PythonProject/MyTbquant
    .venv/Scripts/python.exe examples/example_batch_history.py
"""
from __future__ import annotations

import sys
import time
import json
from pathlib import Path
from datetime import date

# 项目根加入 path（config / runtime / sell_option 包）
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config                 # noqa: E402
import tbpy                   # noqa: E402
from runtime import connect, disconnect  # noqa: E402
from sell_option.fetchers._history_gate import gated_get_history_n  # noqa: E402

BATCH_MAX = 20                # tbpy 单次查询合约上限
COOLDOWN = 62                # 块间冷却秒（> 60s 限流窗口）


def chunk(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_symbols(limit: int = 25) -> list[str]:
    """优先从真实缓存 option_index.json 取样本合约；无则退回硬编码。"""
    p = Path(__file__).resolve().parent / "data" / "option_index.json"
    if p.exists():
        idx = json.loads(p.read_text(encoding="utf-8"))
        syms: list[str] = []
        for v in idx.get("varieties", {}).values():
            for c in v.get("contracts", []):
                syms.append(c["symbol"])
        return syms[:limit]
    return ["SR611C4600.CZCE", "SR701.CZCE", "m2701.DCE"][:limit]


def parse_one(r, sym, multi: bool):
    """从单条返回里取 (close, bar_date)；兼容扁平/嵌套两种结构。"""
    data = r.get(sym) if multi else r
    if not data:
        return None, None
    close = data.get("close")
    t = data.get("time")
    # 注意：numpy 空数组的 bool() 会抛 "ambiguous"，必须用 len() 而非直接做真值判断
    c = float(close[-1]) if close is not None and len(close) else None
    d = str(t[-1]) if t is not None and len(t) else None
    return c, d


def main() -> None:
    if not connect(config.PRODUCT_KEY):
        print("tbpy 未连接：请先启动并登录 TBQuant3 客户端")
        return
    try:
        syms = load_symbols(25)
        n_batches = (len(syms) + BATCH_MAX - 1) // BATCH_MAX
        print(f"[batch] 共 {len(syms)} 个合约 → {n_batches} 批（每批≤{BATCH_MAX}）")

        out: dict[str, tuple[float | None, str | None]] = {}
        for i, ch in enumerate(chunk(syms, BATCH_MAX)):
            if i > 0:
                # 复刻 fetcher：块间显式 sleep 62s（gated 内部另排冷却，双保险）
                time.sleep(COOLDOWN)
            r = gated_get_history_n(
                tbpy,
                ch if len(ch) > 1 else ch[0],   # 多合约传 list，单合约传 str
                "1d", 1,
                fields=["close", "time"], timeout="30s")
            if not r:
                print(f"  批 {i + 1}: 空（限流/无数据，下批重试）")
                continue
            multi = len(ch) > 1
            got = 0
            for s in ch:
                c, d = parse_one(r, s, multi)
                if c is not None:
                    out[s] = (c, d)
                    got += 1
            print(f"  批 {i + 1}: 取到 {got}/{len(ch)} 个")

        print(f"[batch] 完成，共取到 {len(out)}/{len(syms)} 个合约收盘")
        for s, (c, d) in list(out.items())[:12]:
            print(f"  {s}: close={c}  bar={d}")
    finally:
        disconnect()


if __name__ == "__main__":
    main()
