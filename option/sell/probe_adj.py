"""探针：验证 tbpy 后复权(RolloverBackWard)值是否随拉取窗口 count 漂移。

思路：同一 symbol 用不同 count（从最新往回拉的根数）拉后复权日线，
对齐【重叠的历史日期】逐个比较 close：
  · 若各重叠日期数值完全一致 → 后复权锚定在固定基准，与 count 无关，存原始+因子即可放心；
  · 若不一致 → 复权随拉取窗口整体漂移（换月锚点随起始根变），坐实"每次换 count 都要重刷"。
另附 raw(不加 flag) 对照：原始价是不变事实，重叠日应完全一致。

真实数据源，无假源。用法：
    python option/sell/probe_adj.py            # 默认 m9888.DCE
    python option/sell/probe_adj.py eg000.DCE
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 项目根

import tbpy  # noqa: E402
import config  # noqa: E402
import runtime  # noqa: E402

SYM = sys.argv[1] if len(sys.argv) > 1 else "m9888.DCE"
FIELDS = ["time", "close"]
COOLDOWN = 62.0  # get_history_n 全局限流 ~60s/次


def pull(count: int, rbw: bool, retries: int = 3):
    kwargs = dict(symbol=SYM, frequency="1d", count=count, fields=FIELDS, timeout="30s")
    if rbw:
        kwargs["flag"] = tbpy.QuoteFlag.RolloverBackWard
    for attempt in range(1, retries + 1):
        h = tbpy.get_history_n(**kwargs)
        times = h.get("time") if isinstance(h, dict) else None
        n = len(times) if times is not None else 0
        if n > 0:
            closes = h.get("close")
            d = {}
            for i in range(n):
                key = str(times[i])[:10]
                c = closes[i] if closes is not None and i < len(closes) else None
                if c is not None:
                    d[key] = float(c)
            return d, n
        try:
            err = tbpy.get_last_err() or ""
        except Exception:
            err = ""
        limited = ("限制" in err) or ("间隔" in err)
        print(f"    count={count} rbw={rbw} 第{attempt}次空 get_last_err='{err}'")
        if attempt < retries:
            time.sleep(COOLDOWN if limited else 5.0)
    return {}, 0


def compare(a, b, label):
    common = sorted(set(a) & set(b))
    diffs = [(k, a[k], b[k]) for k in common if abs(a[k] - b[k]) > 1e-9]
    print(f"\n[{label}]")
    print(f"    重叠 {len(common)} 天 | 不一致 {len(diffs)} 天 | "
          f"{'✅ 完全一致(与 count 无关)' if not diffs else '❌ 存在漂移(随 count 变)'}")
    for k, va, vb in diffs[:12]:
        print(f"      {k}: A={va:.4f}  B={vb:.4f}  Δ={va - vb:+.4f}")
    if diffs:
        ratios = [a[k] / b[k] for k, _, _ in diffs]
        print(f"      比值 A/B: min={min(ratios):.6f} max={max(ratios):.6f} "
              f"(≈常数则=整体缩放, 换月锚随起始根漂移)")
    return len(diffs)


def main():
    print("=" * 74)
    print(f"后复权漂移探针 · symbol={SYM}")
    print("=" * 74)
    if not runtime.connect(config.PRODUCT_KEY):
        print("连接失败：请先启动并登录 TBQuant 客户端后重试。")
        return 2
    try:
        print(f"[1/3] 后复权 count=120 …")
        rbw_short, n1 = pull(120, True)
        time.sleep(COOLDOWN)
        print(f"[2/3] 后复权 count=300 …")
        rbw_long, n2 = pull(300, True)
        time.sleep(COOLDOWN)
        print(f"[3/3] 原始(no flag) count=300 …")
        raw_long, n3 = pull(300, False)

        print(f"\n返回 bar 数：RBW120={n1}  RBW300={n2}  RAW300={n3}")
        print(f"RBW120 首/末: {rbw_short and (min(rbw_short), rbw_short[min(rbw_short)]).__str__()}")

        compare(rbw_short, rbw_long, "核心问题：后复权 近120根 vs 近300根（重叠日）")
        compare(raw_long, raw_long, "对照：原始价（同一次拉取，应恒等，验证比较逻辑本身）")

        print("\n" + "=" * 74)
        print("判读：上面『后复权 120v300』若『不一致>0』→ 复权随拉取窗口漂移，"
              "印证需『存原始价+因子、读取现算』。")
        print("=" * 74)
    finally:
        runtime.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
