"""拉取 EG(乙二醇, 大商所 DCE) 近 N 年日线历史数据并打印。

默认拉取 EG 指数 eg000.DCE 的近 2 年日线(约 490 个交易日)。
用指数而非单个主力合约，是因为单个合约存续不足 2 年、无法给出连续 2 年日线。

实测要点(见项目 readme 七)：
  · get_history_n 有【全局限流】：一次成功后约 60s 内的后续查询会被拦，返回各字段
    空 ndarray，原因见 tbpy.get_last_err()='查询历史失败,查询限制,间隔=60000ms,
    合约数量=20'。故本程序空返回时会读 get_last_err，命中限流则等约 60s 再试。
  · 必须传 timeout；区间查询 get_history 受权限限制常返回空，故用 get_history_n。
  · 单合约(eg2611/eg2701)与指数(eg000)的日线【都能取到】；此前"单合约取不到"
    是被限流误导的错误结论。指数适合连续多年，单合约数据从其上市日起。
  · open_int(持仓量)/turn_over 对指数常取不到，会显示为 '-'，属正常。

用法(参数：合约 [年数] [周期])：
    python examples/eg_history.py                    # eg000.DCE 近 2 年日线(默认)
    python examples/eg_history.py eg000.DCE 3        # 指定年数
    python examples/eg_history.py eg2701.DCE 1       # 单个合约日线(从其上市日起)
    python examples/eg_history.py eg2611.DCE 1 5m    # 指定合约+周期(分钟线)
  注：单个合约(如 eg2701，上市 2026-01)存续不足 2 年，日线只从其上市日起；
      要连续多年日线用指数 eg000.DCE。历史查询有全局限流(约 60s 一次)，勿频繁重跑。
"""
import sys
from pathlib import Path
_HERE = Path(__file__).resolve().parent
if (_HERE / 'sell_option' / '__init__.py').exists():
    _UNIT = _HERE
else:
    _UNIT = _HERE.parent
_STRATEGIES = _UNIT.parent
_ROOT = _STRATEGIES.parent
for _p in (str(_STRATEGIES), str(_UNIT), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import datetime
import os
import sys
import time

# 允许直接运行本文件（python examples/eg_history.py）

import tbpy  # noqa: E402

import config  # noqa: E402
import runtime  # noqa: E402

DEFAULT_SYMBOL = "eg000.DCE"     # EG(乙二醇)指数：连续多年日线；单合约存续不足 2 年
UNDERLYING = "eg.DCE"            # EG 品种代码
FIELDS = ["time", "open", "high", "low", "close", "volume", "turn_over", "open_int"]
# 展示列定义：字段 -> (表头, 宽度, 小数位)
_COLS = {
    "open": ("开盘", 9, 1),
    "high": ("最高", 9, 1),
    "low": ("最低", 9, 1),
    "close": ("收盘", 9, 1),
    "volume": ("成交量", 12, 0),
    "turn_over": ("成交额", 16, 0),
    "open_int": ("持仓量", 10, 0),
}


def _num(v, width, prec=1):
    """把数值右对齐格式化；None 显示为 '-'。"""
    return f"{v:>{width}.{prec}f}" if v is not None else "-".rjust(width)


def _w(s):
    """字符串显示宽度（中文按 2 列计）。"""
    return sum(2 if ord(c) > 127 else 1 for c in s)


def _rjust(s, width):
    return " " * max(0, width - _w(s)) + s


def _ljust(s, width):
    return s + " " * max(0, width - _w(s))


def fetch_daily(symbol, years, freq="1d", timeout="30s", retries=2,
                rate_limit_wait=62.0):
    """用 get_history_n 拉取近 years 年 K 线，按日期裁剪后返回 (list[dict], err)。

    关键：get_history_n 有【全局限流】——一次成功后约 60s 内的后续查询(无论同/异
    合约)都会被拦，各字段返回【空 ndarray】，且 tbpy.get_last_err() 给出
    '查询历史失败,查询限制,间隔=60000ms,合约数量=20'。故空返回时先读 get_last_err，
    命中限流就等约 60s 再试(密集重试只会持续撞限流)。
    """
    if freq in ("1d", "1w", "1mon"):
        count = int(years * 250) + 30      # 一年约 245 个交易日，多取再按日期裁剪
    else:
        count = 2000                       # 分钟/小时线：取最近 2000 根
    last_err = ""
    for attempt in range(1, retries + 1):
        hist = tbpy.get_history_n(symbol=symbol, frequency=freq, count=count,
                                  fields=FIELDS, timeout=timeout)
        times = hist.get("time") if isinstance(hist, dict) else None
        n = len(times) if times is not None else 0
        if n > 0:
            cutoff = datetime.datetime.now() - datetime.timedelta(days=int(years * 365))
            rows = []
            for i in range(n):
                t = times[i]
                if t is not None and t < cutoff:
                    continue
                row = {"time": t}
                for f in FIELDS[1:]:
                    arr = hist.get(f)
                    row[f] = arr[i] if (arr is not None and i < len(arr)) else None
                rows.append(row)
            return rows, None
        # 空返回：读出真实原因(get_last_err)，据此决定等待时长
        try:
            last_err = tbpy.get_last_err() or ""
        except Exception:  # noqa: BLE001
            last_err = ""
        limited = ("限制" in last_err) or ("间隔" in last_err)
        if attempt < retries:
            wait = rate_limit_wait if limited else 5.0
            print(f"    [{symbol}] 第 {attempt}/{retries} 次为空；get_last_err="
                  f"'{last_err or '(空)'}' → {'命中限流,冷却' if limited else '预热'} "
                  f"{wait:.0f}s 后重试…")
            time.sleep(wait)
    hint = ("历史查询被限流(约每 60s 放行一次、合约数量上限 20)；请勿频繁重跑，"
            "稍等约 1 分钟再运行即可。"
            if (("限制" in last_err) or ("间隔" in last_err))
            else "该合约此周期可能确无数据(检查合约代码/周期，或改用实时订阅收集)。")
    return None, f"重试 {retries} 次仍为空；get_last_err='{last_err or '(空)'}'。{hint}"


def run(symbol=DEFAULT_SYMBOL, years=2.0, freq="1d"):
    print("=" * 78)
    print(f"拉取 {symbol} 近 {years:g} 年 {freq} 数据")
    if not runtime.connect(config.PRODUCT_KEY):
        print("请先启动并登录 TBQuant 客户端后重试。")
        return 1

    try:
        mi = tbpy.get_main_instrument(underlying_symbol=UNDERLYING)
        if mi is not None:
            print(f"EG 主力合约: {mi.symbol} ({mi.sec_name})")
        else:
            print(f"未取到 {UNDERLYING} 主力合约: {tbpy.get_last_err()}")
    except Exception as e:  # noqa: BLE001
        print("get_main_instrument err:", type(e).__name__, e)

    rows, err = fetch_daily(symbol, years, freq)
    if err:
        print(f"[!] {err}")
    runtime.disconnect()

    if not rows:
        print("=" * 78)
        return 1

    # 只展示实际有数据的列（指数常无 turn_over/open_int）
    active = [f for f in FIELDS[1:] if any(r.get(f) is not None for r in rows)]
    missing = [f for f in FIELDS[1:] if f not in active]

    daily = freq in ("1d", "1w", "1mon")
    time_fmt = "%Y-%m-%d" if daily else "%Y-%m-%d %H:%M"
    date_w = 10 if daily else 16           # 分钟线要显示到时分
    unit = "日线" if daily else freq

    print(f"\n合约 {symbol}  共 {len(rows)} 条 {unit}  "
          f"区间 {rows[0]['time']:{time_fmt}} ~ {rows[-1]['time']:{time_fmt}}")
    print("-" * 78)
    header = _ljust("日期", date_w) + " "
    for f in active:
        name, width, _ = _COLS[f]
        header += _rjust(name, width)
    print(header)
    for r in rows:
        line = f"{r['time']:{time_fmt}}".ljust(date_w) + " "
        for f in active:
            _, width, prec = _COLS[f]
            line += _num(r.get(f), width, prec)
        print(line)
    print("-" * 78)
    if missing:
        print(f"注: 字段 {', '.join(missing)} 全为空 —— get_history_n 对指数常取不到"
              f"成交额/持仓量(OI)，属已知情况。")
    print(f"完成，共打印 {len(rows)} 条。")
    return 0


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SYMBOL
    yrs = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    frq = sys.argv[3] if len(sys.argv) > 3 else "1d"
    raise SystemExit(run(sym, yrs, frq))
