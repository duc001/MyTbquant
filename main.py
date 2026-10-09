"""MyTbquant 项目统一入口。

用法:
    python main.py            # 等价于 check
    python main.py check      # 连通性 + 只读数据查询（安全，可反复运行）
    python main.py option     # 期权数据查询（A元数据/B行情/C标的，只读安全）
    python main.py collect    # 实时收集具体合约K线（subscribe_bar，只读安全，限时退出）
    python main.py smoke      # 策略接线冒烟测试（限时自动退出，休市时不会下单）
    python main.py strategy   # 运行双均线策略（会真实下单，需二次确认）

    任意命令都可加 -p/--product 切换产品、-a/--account 切换账户，例如:
        python main.py check    -p tbquant3          # 连 TBQuant3
        python main.py strategy -a sim2              # 用仿真账户2
        python main.py strategy -p tbquant3 -a sim2  # 同时指定
"""
import argparse
import sys

from sell_option.logutil import setup_logging


def cmd_check() -> int:
    """运行只读数据查询示例。"""
    from examples.query_data import run

    return run()


def cmd_option() -> int:
    """运行期权数据查询示例（只读）。"""
    from examples.option_data import run

    return run()


def cmd_collect(args) -> int:
    """运行实时 K 线收集器（subscribe_bar，只读，不下单）。"""
    from examples.collect_bars import run

    return run(frequency=args.freq, max_secs=args.secs)


def cmd_smoke() -> int:
    """运行策略接线冒烟测试（限时自动退出）。"""
    from examples.smoke_strategy import run

    return run()


def cmd_strategy() -> int:
    """运行双均线策略（真实下单，需二次确认）。"""
    import tbpy

    import config
    import runtime
    from strategies.dual_ma import DualMAStrategy

    print("=" * 60)
    print("警告: 双均线策略会向【真实账户】发送委托，可能产生实际成交！")
    answer = input(f"确认用账户 {config.ACCOUNT_ID} / 产品 {config.PRODUCT_KEY} 运行吗？(输入 yes 继续): ")
    if answer.strip().lower() != "yes":
        print("已取消。")
        return 0

    if not runtime.connect(config.PRODUCT_KEY):
        return 1

    # 交易必须用“可下单的具体合约”：优先取该品种主力合约（指数如 rb000.SHFE 不可交易）
    main_inst = tbpy.get_main_instrument(underlying_symbol=config.UNDERLYING_SYMBOL)
    if main_inst is not None:
        symbol = main_inst.symbol
    elif config.SYMBOL:
        symbol = config.SYMBOL
        print(f"    未取到主力合约，回退到 config.SYMBOL={symbol}（若为指数将无法下单）")
    else:
        print(f"    无法确定可交易合约: {tbpy.get_last_err()}")
        runtime.disconnect()
        return 1

    # 注意: 必须持有 strategy 引用，避免被垃圾回收导致事件不再回调
    strategy = DualMAStrategy(
        config.FAST_LENGTH, config.SLOW_LENGTH, config.ACCOUNT_ID, symbol,
        bar_frequency=config.BAR_FREQUENCY, order_volume=config.ORDER_VOLUME,
    )
    print(
        f"启动策略: symbol={symbol} account={config.ACCOUNT_ID} "
        f"fast={config.FAST_LENGTH} slow={config.SLOW_LENGTH} freq={config.BAR_FREQUENCY}"
    )
    print("进入事件循环，按 Ctrl+C 停止...")
    try:
        tbpy.exe()  # 阻塞，进入事件驱动循环
    except KeyboardInterrupt:
        print("\n收到中断，正在退出策略...")
    finally:
        del strategy
        runtime.disconnect()
    return 0


def main(argv=None) -> int:
    setup_logging()  # 统一日志：单文件 + 控制台
    parser = argparse.ArgumentParser(description="MyTbquant - tbpy 开发与测试入口")
    parser.add_argument(
        "command",
        nargs="?",
        default="check",
        choices=["check", "option", "collect", "smoke", "strategy"],
        help="check=只读数据查询(默认); option=期权数据查询(只读); collect=实时收集合约K线(只读); smoke=策略接线冒烟测试; strategy=运行双均线策略(会真实下单)",
    )
    parser.add_argument(
        "-p",
        "--product",
        choices=["tbquant", "tbquant3", "tbquant3.easy"],
        default=None,
        help="临时切换要连接的产品，覆盖 config.PRODUCT_KEY / 环境变量 TBPY_PRODUCT",
    )
    parser.add_argument(
        "-a",
        "--account",
        default=None,
        help="切换账户：填预设别名(sim1/sim2)或原始 account_id，覆盖 config.ACCOUNT_ID",
    )
    parser.add_argument(
        "-f",
        "--freq",
        default="1d",
        help="[collect] K线周期(1d/1m/5m/…)，默认 1d",
    )
    parser.add_argument(
        "-t",
        "--secs",
        type=int,
        default=20,
        help="[collect] 最长运行秒数(到时自动退出)，默认 20",
    )
    args = parser.parse_args(argv)

    # 命令行优先级最高：切产品只影响 tbpy.init 连哪个客户端，切账户只影响订阅/下单用哪个账户
    if args.product or args.account:
        import config

        if args.product:
            config.PRODUCT_KEY = args.product
        if args.account:
            config.ACCOUNT_ID = config.resolve_account(args.account)

    if args.command == "check":
        return cmd_check()
    if args.command == "option":
        return cmd_option()
    if args.command == "collect":
        return cmd_collect(args)
    if args.command == "smoke":
        return cmd_smoke()
    if args.command == "strategy":
        return cmd_strategy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
