"""基于 subscribe_bar 的实时 K 线收集器（事件驱动，只读安全，不下单）。

为什么需要它：
    get_history_n 能取历史 K 线，但有【全局限流】(约每 60s 一次)，且拿不到实时
    推送、open_int(持仓量)在历史里也常为空。subscribe_bar 则在交易时段【实时】
    推送每根 bar(on_bar flag=1)、并可回放近期历史(flag=0)，适合持续收集某合约
    的实时 K 线与持仓量(OI)。注意：休市/盘后无实时流，历史回放也可能不触发。

机制(参考 strategies/dual_ma.py 的事件框架)：
    on_init  -> context.subscribe_bar(symbol, frequency, begin_time, sliding_window)
    on_bar   -> 按 time 去重累积每根 bar；实时 bar 到达即打印
    on_timer -> 到时 tbpy.exit()，避免休市时无限阻塞
    结束     -> 打印汇总，并可选保存 CSV

运行(只读、不下单，可反复跑)：
    python main.py collect                                   # eg2611.DCE 1d，收集 20s
    python examples/collect_bars.py                            # 同上
    python examples/collect_bars.py eg2611.DCE -f 5m -t 30     # 5 分钟线，收集 30s
    python examples/collect_bars.py eg2611.DCE -f 1d --csv data/eg2611_1d.csv
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
import argparse
import csv
import datetime
import os
import sys

# 允许直接运行本文件（python examples/collect_bars.py）

import tbpy  # noqa: E402

import config  # noqa: E402
import runtime  # noqa: E402

DEFAULT_SYMBOL = "eg2611.DCE"     # 要实时收集的具体合约
DEFAULT_FREQ = "1d"               # K 线周期
DEFAULT_LOOKBACK_DAYS = 400       # begin_time 回溯天数（覆盖 eg2611 上市至今约 10 个月）
DEFAULT_SECS = 20                 # 事件循环最长运行秒数（到时自动退出）
FIELDS = ["time", "open", "high", "low", "close", "volume", "turn_over", "open_int"]


def _estimate_window(frequency: str, lookback_days: int) -> int:
    """按周期粗估 sliding_window：需 >= 区间内 bar 数才能一次拿全历史回放。上限 8000。"""
    per_day = {"1d": 1.0, "1w": 0.2, "1mon": 0.05}.get(frequency)
    if per_day is None:
        num = "".join(c for c in frequency if c.isdigit()) or "1"
        unit = frequency[-1]
        # EG 日盘+夜盘约 6.5 小时 ≈ 390 分钟
        per_day = {"m": 390.0, "h": 6.5, "s": 23400.0}.get(unit, 390.0) / int(num)
    return max(10, min(int(lookback_days * per_day) + 20, 8000))


def _fmt_num(v, width, prec=1):
    return f"{v:>{width}.{prec}f}" if isinstance(v, (int, float)) else "-".rjust(width)


def _fmt_time(t):
    try:
        return f"{t:%Y-%m-%d %H:%M}"
    except (ValueError, TypeError):
        return str(t)


class BarCollector(tbpy.IStrategy):
    """订阅并累积某合约的 K 线，到时自动退出。只读，不订阅账户、不下单。"""

    def __init__(self, symbol, frequency, lookback_days, max_secs, csv_path=None):
        super().__init__("BarCollector")
        self._symbol = symbol
        self._frequency = frequency
        self._lookback_days = lookback_days
        self._max_secs = max_secs
        self._csv_path = csv_path
        self._bars = {}                     # time -> dict，按时间去重
        self._t0 = datetime.datetime.now()

    def on_init(self, context: tbpy.Context):
        window = _estimate_window(self._frequency, self._lookback_days)
        begin = datetime.datetime.now() - datetime.timedelta(days=self._lookback_days)
        ret = context.subscribe_bar(
            symbol=self._symbol,
            frequency=self._frequency,
            begin_time=begin,
            sliding_window=window,
        )
        if ret is not None:
            print(f"[collect] 订阅 bar 失败: {ret}")
            tbpy.exit()
            return
        context.create_timer(interval_millsecs=2000)
        print(f"[collect] 已订阅 {self._symbol} {self._frequency}  "
              f"begin={begin:%Y-%m-%d}  window={window}  最长 {self._max_secs}s")
        print("[collect] 等待 on_bar 回放历史/推送实时…（休市时可能只有历史回放或无数据）")

    def on_bar(self, context: tbpy.Context, bars, symbol: str, flag: int):
        # flag=0 历史回放，flag=1 实时；两者都收集，按 time 去重
        for b in bars:
            t = getattr(b, "time", None)
            if t is None or t in self._bars:
                continue
            self._bars[t] = {f: getattr(b, f, None) for f in FIELDS}
        if flag == 1 and len(bars) > 0:
            b = bars[-1]
            print(f"[collect] 实时 {symbol} {_fmt_time(b.time)}  "
                  f"O={b.open} H={b.high} L={b.low} C={b.close} "
                  f"V={b.volume} OI={b.open_int}")

    def on_timer(self, context: tbpy.Context, timer_id: int, interval_millsecs: int):
        if (datetime.datetime.now() - self._t0).seconds >= self._max_secs:
            print("[collect] 达到设定时长，退出事件循环。")
            tbpy.exit()

    # ---- 结束后汇总/落盘 ----
    def rows(self):
        return [self._bars[t] for t in sorted(self._bars)]

    def save_csv(self, rows):
        if not self._csv_path or not rows:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self._csv_path)), exist_ok=True)
        with open(self._csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for r in rows:
                rr = dict(r)
                rr["time"] = str(rr["time"])
                w.writerow(rr)
        print(f"[collect] 已保存 {len(rows)} 行 -> {self._csv_path}")


def _print_table(rows, max_lines=40):
    n = len(rows)
    half = max_lines // 2
    show = rows if n <= max_lines else rows[:half] + rows[-half:]
    print("-" * 88)
    print(f"{'时间':<18}{'开盘':>9}{'最高':>9}{'最低':>9}{'收盘':>9}{'成交量':>12}{'持仓量':>10}")
    for r in show:
        print(f"{_fmt_time(r['time']):<18}{_fmt_num(r['open'], 9)}{_fmt_num(r['high'], 9)}"
              f"{_fmt_num(r['low'], 9)}{_fmt_num(r['close'], 9)}"
              f"{_fmt_num(r['volume'], 12, 0)}{_fmt_num(r['open_int'], 10, 0)}")
    if n > max_lines:
        print(f"…（中间省略，共 {n} 行）…")
    print("-" * 88)


def _warn_if_market_closed(symbol):
    """休市/盘后时给出明确提示：此时 subscribe_bar 无实时 bar、历史回放也可能不触发。"""
    try:
        try:
            from sell_option.fetchers.calendar_cache import is_closed_date
            holiday = is_closed_date(datetime.date.today())
        except Exception:  # noqa: BLE001
            holiday = None
        try:
            from sell_option.fetchers.session_hours import is_trading_time
            trading = is_trading_time(symbol)
        except Exception:  # noqa: BLE001
            trading = False
    except Exception:  # noqa: BLE001
        return
    if holiday:
        print("[collect] 注意：今日休市(manual_calendar)。")
    if not trading:
        print("[collect] 注意：当前非交易时段 —— subscribe_bar 不会有实时 bar，"
              "历史回放(flag=0)在休市时也可能不触发；请在交易时段运行以收集实时 K 线。")


def run(symbol=DEFAULT_SYMBOL, frequency=DEFAULT_FREQ,
        lookback_days=DEFAULT_LOOKBACK_DAYS, max_secs=DEFAULT_SECS, csv_path=None):
    print("=" * 88)
    print(f"[collect] 平台 key={config.PRODUCT_KEY}  合约={symbol}  周期={frequency}  "
          f"回溯={lookback_days}d  限时={max_secs}s")
    if not runtime.connect(config.PRODUCT_KEY):
        print("请先启动并登录 TBQuant 客户端后重试。")
        return 1

    _warn_if_market_closed(symbol)

    collector = BarCollector(symbol, frequency, lookback_days, max_secs, csv_path)
    try:
        tbpy.exe()          # 阻塞，直到 on_timer 调用 tbpy.exit()
    except KeyboardInterrupt:
        print("\n[collect] 收到中断，退出。")
    finally:
        rows = collector.rows()
        print(f"\n[collect] 共收集 {len(rows)} 根 {symbol} {frequency} bar")
        if rows:
            print(f"[collect] 区间 {_fmt_time(rows[0]['time'])} ~ {_fmt_time(rows[-1]['time'])}")
            _print_table(rows)
            collector.save_csv(rows)
        else:
            print("[collect] 未收到任何 bar：多为休市/盘后无实时行情所致"
                  "（交易时段运行即可收到实时 bar）；若交易时段仍为空，"
                  "再检查周期/回溯范围或该合约是否有数据。")
        del collector
        runtime.disconnect()
    return 0


def _parse_args(argv):
    p = argparse.ArgumentParser(description="subscribe_bar 实时 K 线收集器（只读，不下单）")
    p.add_argument("symbol", nargs="?", default=DEFAULT_SYMBOL,
                   help=f"合约代码（默认 {DEFAULT_SYMBOL}）")
    p.add_argument("-f", "--freq", default=DEFAULT_FREQ, help="K 线周期（1d/1m/5m/…，默认 1d）")
    p.add_argument("-d", "--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                   help="begin_time 回溯天数（默认 400）")
    p.add_argument("-t", "--secs", type=int, default=DEFAULT_SECS,
                   help="最长运行秒数（默认 20，到时自动退出）")
    p.add_argument("--csv", default=None, help="可选：把收集到的 bar 保存为 CSV 的路径")
    return p.parse_args(argv)


if __name__ == "__main__":
    a = _parse_args(sys.argv[1:])
    raise SystemExit(run(a.symbol, a.freq, a.days, a.secs, a.csv))
