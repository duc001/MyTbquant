"""OI(持仓量)实时订阅通路验证器 —— 针对【期权】open_int。只读、不下单、限时自动退出。

为什么需要它：
    #5 判定要期权 OI(≥500手)，但 OI 无法离线取——get_history_n 的 open_int 返回空、
    get_current_tick 无 oi 字段、subscribe_bar 在【休市日零 bar(连历史回放 flag=0 都不触发)】。
    故 OI 只能在【交易时段】靠 subscribe_bar→on_bar 的 bar.open_int 实时获取。本工具用于
    盘中验证"期权 bar 是否真带 open_int 值"。

2026-09-27(周日休市)首测结论：
    · subscribe_bar 对四所期权(DCE/SHFE/CZCE/GFEX)订阅全部返回 OK —— 机制通路已验证；
    · 但 is_holiday=True 时零 bar 到达，连 m 标的期货对照也 0 bar —— 值无法验证(inconclusive)；
    · 必须【工作日盘中】重跑本工具才能确认 open_int 是否被填充。

运行(盘中)：
    python examples/probe_oi.py                 # 从 option_index 自动挑四所主力月平值附近 call + m 期货对照
    python examples/probe_oi.py m2611-C-3200.DCE rb2611C3100.SHFE   # 指定合约
    $env:OI_SECS=30; python examples/probe_oi.py                    # 自定义最长运行秒数
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

# 允许直接运行本文件（python examples/probe_oi.py）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tbpy  # noqa: E402
import config  # noqa: E402
import runtime  # noqa: E402

INDEX = Path(__file__).resolve().parents[1] / "sell_option" / "data" / "option_index.json"
MAX_SECS = int(os.getenv("OI_SECS", "20"))
# 覆盖四交易所的抽样品种：m=DCE, rb=SHFE, AP=CZCE, lc=GFEX
_SAMPLE_CODES = ["m", "rb", "AP", "lc"]


def pick_test_symbols() -> list[tuple[str, str]]:
    """从索引挑真实挂牌合约：每品种取最早月(近主力)、行权价中位的 call。返回 [(标签, symbol)]。"""
    d = json.loads(INDEX.read_text(encoding="utf-8"))
    varieties = d["varieties"]
    picks: list[tuple[str, str]] = []
    for code in _SAMPLE_CODES:
        entry = varieties.get(code)
        if not entry:
            continue
        contracts = entry["contracts"]
        months = sorted({c["month"] for c in contracts})
        if not months:
            continue
        front = months[0]
        calls = sorted([c for c in contracts if c["month"] == front and c["cp"] == "C"],
                       key=lambda c: c["strike"])
        if not calls:
            continue
        mid = calls[len(calls) // 2]        # 行权价中位≈平值附近，流动性较好
        picks.append((f"{code}{front}C{mid['strike']:g}", mid["symbol"]))
        if code == "m":                      # m 标的期货做对照（期货必有 OI）
            picks.append((f"m{front}标的期货", f"m{front}.{entry.get('exchange', 'DCE')}"))
    return picks


class OiProbe(tbpy.IStrategy):
    """订阅若干合约日线，累积 open_int，到时自动退出。只读、不订阅账户、不下单。"""

    def __init__(self, symbols: list[tuple[str, str]]):
        super().__init__("OiProbe")
        self._symbols = symbols
        self._stat: dict[str, dict] = {}
        self._t0 = dt.datetime.now()

    def on_init(self, context: tbpy.Context):
        begin = dt.datetime.now() - dt.timedelta(days=60)
        for label, sym in self._symbols:
            self._stat[sym] = {"label": label, "n": 0, "flags": set(),
                               "last_time": None, "last_oi": None, "last_close": None}
            ret = context.subscribe_bar(symbol=sym, frequency="1d",
                                        begin_time=begin, sliding_window=80)
            print(f"[oi] 订阅 {sym:<22} [{label}] -> {'OK' if ret is None else f'FAIL({ret})'}")
        context.create_timer(interval_millsecs=2000)
        print(f"[oi] 已订阅 {len(self._symbols)} 个合约，最长 {MAX_SECS}s，等待 on_bar…")

    def on_bar(self, context: tbpy.Context, bars, symbol: str, flag: int):
        st = self._stat.get(symbol)
        if st is None:
            return
        st["flags"].add(flag)               # flag=0 历史回放 / flag=1 实时
        for b in bars:
            st["n"] += 1
            st["last_time"] = getattr(b, "time", None)
            st["last_oi"] = getattr(b, "open_int", None)
            st["last_close"] = getattr(b, "close", None)
        if flag == 1 and bars:
            b = bars[-1]
            print(f"[oi] 实时 {symbol} t={b.time} C={b.close} OI={b.open_int}")

    def on_timer(self, context: tbpy.Context, timer_id: int, interval_millsecs: int):
        if (dt.datetime.now() - self._t0).seconds >= MAX_SECS:
            print("[oi] 到时退出事件循环。")
            tbpy.exit()

    def report(self, holiday: bool, trading: bool):
        print("\n" + "=" * 80)
        print(f"{'标签':<16}{'symbol':<24}{'bars':>5}{'flags':>7}"
              f"{'最后bar时间':>16}{'OI':>10}{'收盘':>10}")
        got_oi = False
        for sym, st in self._stat.items():
            flags = "".join(str(f) for f in sorted(st["flags"])) or "-"
            lt = st["last_time"]
            lts = f"{lt:%Y-%m-%d}" if isinstance(lt, (dt.datetime, dt.date)) else str(lt)
            oi = st["last_oi"]
            if isinstance(oi, (int, float)) and oi:
                got_oi = True
            oi_s = f"{oi:.0f}" if isinstance(oi, (int, float)) else str(oi)
            cl = st["last_close"]
            cl_s = f"{cl:.1f}" if isinstance(cl, (int, float)) else str(cl)
            print(f"{st['label']:<16}{sym:<24}{st['n']:>5}{flags:>7}{lts:>16}{oi_s:>10}{cl_s:>10}")
        print("=" * 80)

        total_bars = sum(st["n"] for st in self._stat.values())
        print("\n=== 结论 ===")
        if total_bars == 0:
            if holiday or not trading:
                print("零 bar + 当前休市/非交易时段 → 本次【无法验证】OI 值。")
                print("请【工作日盘中】(日盘 09:00-15:00 或夜盘)重跑本工具。")
            else:
                print("盘中仍零 bar → 检查合约是否有效/周期/回溯范围，或平台行情权限。")
        elif got_oi:
            print("已收到 bar 且 open_int 非空 → 【期权 OI 可经 subscribe_bar 获取】，通路确认。")
        else:
            print("收到 bar 但 open_int 为空/0 → 期权 OI 可能不被推送，需进一步排查(周期/权限/合约)。")


def run(symbols: list[tuple[str, str]]) -> int:
    print("=== 测试合约 ===")
    for label, sym in symbols:
        print(f"  {label:<16} {sym}")

    if not runtime.connect(config.PRODUCT_KEY):
        print("tbpy 未连接：请先启动并登录 TBQuant3 客户端")
        return 1

    holiday = trading = False
    try:
        probe_sym = symbols[0][1] if symbols else "m.DCE"
        try:
            from sell_option.fetchers.calendar_cache import is_closed_date
            holiday = is_closed_date(dt.date.today())
        except Exception:  # noqa: BLE001
            holiday = None
        try:
            from sell_option.fetchers.session_hours import is_trading_time
            trading = bool(is_trading_time(probe_sym))
        except Exception:  # noqa: BLE001
            trading = False
        print(f"\n休市日(manual_calendar)={holiday}  is_trading_time({probe_sym})={trading}")
    except Exception as e:  # noqa: BLE001
        print(f"交易时段查询异常：{type(e).__name__}: {e}")

    probe = OiProbe(symbols)
    try:
        tbpy.exe()          # 阻塞，直到 on_timer 调 tbpy.exit()
    except KeyboardInterrupt:
        print("\n[oi] 中断退出")
    finally:
        probe.report(holiday, trading)
        del probe
        runtime.disconnect()
    return 0


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    if argv:                                   # 用户显式指定合约
        symbols = [(s, s) for s in argv]
    else:                                      # 从索引自动挑
        symbols = pick_test_symbols()
    if not symbols:
        print("无可测合约：请先构建 option_index（python -m sell_option.fetchers.d06_option_chain）")
        return 1
    return run(symbols)


if __name__ == "__main__":
    raise SystemExit(main())
