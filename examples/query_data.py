"""只读数据查询示例（参考 docs/reference/tbpy教程.md "TBPY使用案例 - 2. 读写数据" 的只读部分）。

本示例只做「连通性检查 + 行情/合约查询」，全程不下单、不写/删任何数据，
因此可以安全地反复运行，用于验证 tbpy 环境与平台连接是否正常。

运行方式（二选一）：
    python main.py check          # 推荐，走统一入口
    python examples/query_data.py # 直接运行本脚本
"""
import datetime
import os
import sys

# 允许脚本被直接运行时也能 import 到项目根目录下的 config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tbpy  # noqa: E402

import config  # noqa: E402
import runtime  # noqa: E402


def _print_tick(tick):
    """友好打印一个 Tick 快照。"""
    if tick is None:
        print("    <None>")
        return
    print(
        f"    {tick.symbol} time={tick.time} last={tick.last} "
        f"open={tick.open} high={tick.high} low={tick.low} vol={tick.volume}"
    )


def _print_history(history):
    """友好打印 get_history_n 返回的 dict{field: ndarray}。"""
    if not history:
        print("    <空>")
        return
    for field, arr in history.items():
        length = len(arr) if hasattr(arr, "__len__") else "NA"
        print(f"    {field}: len={length} -> {arr}")


def _row_count(history) -> int:
    """返回 dict{field: ndarray} 的行数(各字段等长)，空则返回 0。"""
    if not history:
        return 0
    for arr in history.values():
        return len(arr) if hasattr(arr, "__len__") else 0
    return 0


def run() -> int:
    """执行只读查询流程，返回进程退出码（0 表示成功）。"""
    print("=" * 60)
    print(f"[1] 连接平台: key={config.PRODUCT_KEY}")
    if not runtime.connect(config.PRODUCT_KEY):
        print("    排查: 1) 对应的 TBQuant 客户端是否已启动并登录;")
        print("          2) 账户是否已开通 tbpy 权限;")
        print("          3) config.PRODUCT_KEY 是否与实际客户端匹配。")
        return 1

    print(f"    init 成功; is_init={tbpy.is_init()}; tbpy 版本={tbpy.get_version()}")

    # ------------------------------------------------------------------
    print("\n[2] 交易日历/时段信息")
    # 注意: is_trading_time 必须传 symbol，签名为 is_trading_time(symbol, dateTime=None)
    try:
        from sell_option.fetchers.session_hours import is_trading_time
        trading = is_trading_time(config.SYMBOL)
        # 休市日改由本地日历 calendar.db.manual_calendar 判定（不再调 tbpy.is_holiday()）
        try:
            from sell_option.fetchers.calendar_cache import is_closed_date
            holiday = is_closed_date(datetime.date.today())
        except Exception:  # noqa: BLE001
            holiday = None
        print(f"    is_trading_time({config.SYMBOL})={trading}  休市日(manual_calendar)={holiday}")
        if holiday or not trading:
            print("    当前为非交易时段/假日：实时快照 get_current_tick 可能返回 None，属正常现象。")
    except Exception as exc:  # 平台可能不支持，忽略即可
        print(f"    <跳过: {exc}>")

    # ------------------------------------------------------------------
    print(f"\n[3] 查询主力合约: underlying={config.UNDERLYING_SYMBOL}")
    main_inst = tbpy.get_main_instrument(underlying_symbol=config.UNDERLYING_SYMBOL)
    symbol = config.SYMBOL
    if main_inst is not None:
        print(
            f"    主力合约={main_inst.symbol} 名称={main_inst.sec_name} "
            f"交易所={main_inst.exchange} 最小变动价={main_inst.price_tick}"
        )
        symbol = main_inst.symbol  # 后续查询优先用主力合约
    else:
        print(f"    未取到主力合约({tbpy.get_last_err()})，改用 config.SYMBOL={symbol}")

    # ------------------------------------------------------------------
    print(f"\n[4] 查询合约信息 get_instrument: {symbol}")
    inst = tbpy.get_instrument(symbol=symbol)
    if inst is not None:
        print(
            f"    {inst.symbol} 交易单位={inst.contract_unit} 每点价值={inst.big_point_value} "
            f"上市={inst.listed_date} 退市={inst.delisted_date}"
        )
    else:
        print(f"    <None> ({tbpy.get_last_err()})")

    # ------------------------------------------------------------------
    print(f"\n[5] 查询最新历史行情 get_history_n: {symbol} 1m x5")
    history = tbpy.get_history_n(
        symbol=symbol, frequency="1m", count=5, fields=["time", "open", "high", "low", "close", "volume"],
        timeout=config.TIMEOUT,
    )
    _print_history(history)

    # ------------------------------------------------------------------
    print(f"\n[6] 查询实时快照 get_current_tick: {symbol}")
    _print_tick(tbpy.get_current_tick(symbol=symbol))

    # ------------------------------------------------------------------
    print("\n[7] 查询全部合约代码 get_symbols (仅显示前 5 个与总数)")
    symbols = tbpy.get_symbols()
    if symbols:
        print(f"    共 {len(symbols)} 个合约, 示例: {symbols[:5]}")
    else:
        print(f"    <空> ({tbpy.get_last_err()})")

    # ------------------------------------------------------------------
    print("\n[8] 查询已登录账户 get_account_ids")
    try:
        account_ids = tbpy.get_account_ids()
        print(f"    账户列表: {account_ids}")
    except Exception as exc:
        print(f"    <跳过: {exc}>")

    # ------------------------------------------------------------------
    print("\n[9] 按区间查询历史 get_history（日线，最近30天）")
    today = datetime.datetime.now()
    begin = datetime.datetime(today.year, today.month, today.day) - datetime.timedelta(days=30)
    end = datetime.datetime(today.year, today.month, today.day)
    hist = tbpy.get_history(
        symbol=symbol, frequency="1d", begin_time=begin, end_time=end,
        fields=["time", "close"], timeout=config.TIMEOUT,
    )
    if _row_count(hist) > 0:
        _print_history(hist)
    else:
        print("    返回空：区间历史查询可能受“申请历史范围控制权限”限制(见 docs/reference/tbpy教程.md 2.约定)，")
        print("    或该合约在此区间无数据；而最新N条查询 get_history_n 不受此限制，参见上方 [5]。")

    print("\n" + "=" * 60)
    print("只读查询完成，释放连接。")
    runtime.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(run())
