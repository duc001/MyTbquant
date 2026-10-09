"""双均线策略示例（参考 docs/reference/tbpy教程.md "TBPY使用案例 - 1. 双均线交易"）。

策略思路：
    短周期均线上穿长周期均线 -> 平掉空头、开出多头；
    短周期均线下破长周期均线 -> 平掉多头、开出空头。

运行机制：
    通过 on_bar 事件驱动，在每根实时 Bar 完成后计算均线并决策；
    下单价格取最近一次 on_tick 推送的最新价；
    on_timer 定时清理长时间未成交的挂单。

⚠ 风险提示：本策略会向"真实账户"发送委托。请先在模拟账户验证，
   确认风险后再运行；本文件仅提供策略模板，不构成任何投资建议。
"""
import datetime

import tbpy


class DualMAStrategy(tbpy.IStrategy):
    """双均线策略。"""

    def __init__(
        self,
        fast_length: int,
        slow_length: int,
        account_id: str,
        symbol: str,
        bar_frequency: str = "10s",
        order_volume: int = 1,
    ):
        # IStrategy 需要一个策略名作为唯一标识
        super().__init__("DualMAStrategy")
        self._fast_length = fast_length
        self._slow_length = slow_length
        self._account_id = account_id
        self._symbol = symbol
        self._bar_frequency = bar_frequency   # Bar 周期(来自 config.BAR_FREQUENCY)
        self._order_volume = order_volume     # 开仓手数(来自 config.ORDER_VOLUME)

        self._account = None            # type: tbpy.Account | None
        self._tick = None               # type: tbpy.Tick | None
        self._pos = None                # type: tbpy.Position | None
        self._order_dict = {}           # order_id -> 下单时间，用于超时撤单
        self._timer_id = 0

    # ------------------------------------------------------------------
    # 初始化：订阅行情/账户、创建定时器、拉取初始快照
    # ------------------------------------------------------------------
    def on_init(self, context: tbpy.Context):
        ret = context.subscribe_tick(symbol=self._symbol)
        if ret is not None:
            print(f"订阅 tick 失败: {ret}")
            tbpy.exit()
            return

        # 滑窗取 slow_length + 1 根，保证计算"已完成"均线时有足够的历史 Bar
        ret = context.subscribe_bar(
            symbol=self._symbol,
            frequency=self._bar_frequency,
            begin_time=datetime.datetime.now() - datetime.timedelta(minutes=self._slow_length),
            sliding_window=self._slow_length + 1,
        )
        if ret is not None:
            print(f"订阅 bar 失败: {ret}")
            tbpy.exit()
            return

        self._account = context.subscribe_account(account_id=self._account_id)
        if self._account is None:
            print(f"订阅账户失败: {tbpy.get_last_err()}")
            tbpy.exit()
            return

        self._timer_id = context.create_timer(interval_millsecs=5000)
        self._tick = tbpy.get_current_tick(symbol=self._symbol)
        self._pos = self._account.get_position(symbol=self._symbol)
        print("on_init 完成。")

    # ------------------------------------------------------------------
    # Bar 驱动：核心交易逻辑
    # ------------------------------------------------------------------
    def on_bar(self, context: tbpy.Context, bars, symbol: str, flag: int):
        # flag=0 历史数据；flag=1 实时数据。仅在实时数据上交易。
        if flag != 1:
            return
        if self._tick is None or self._account is None:
            return
        if self._account.get_status() != tbpy.AccountStatus.OnService:
            return
        if len(bars) < self._slow_length + 1:
            return

        # 用"已收盘"的 Bar 计算均线（剔除最后一根未完成的 Bar）
        fast_avg = sum(b.close for b in bars[-self._fast_length - 1:-1]) / self._fast_length
        slow_avg = sum(b.close for b in bars[-self._slow_length - 1:-1]) / self._slow_length
        price = self._tick.last

        if fast_avg > slow_avg:
            self._turn_long(symbol, price)
        elif fast_avg < slow_avg:
            self._turn_short(symbol, price)

    def _turn_long(self, symbol: str, price: float):
        """转为多头：先平空，再开多。"""
        if self._pos is None:
            self._push_order_id(self._account.buy(symbol=symbol, volume=self._order_volume, price=price))
            return
        if self._pos.s_can_cover_volume > 0:
            self._push_order_id(
                self._account.buy2cover(symbol=symbol, volume=self._pos.s_can_cover_volume, price=price)
            )
        if self._pos.l_current_volume + self._pos.l_active_volume + self._pos.l_active_close_volume < 2:
            self._push_order_id(self._account.buy(symbol=symbol, volume=self._order_volume, price=price))

    def _turn_short(self, symbol: str, price: float):
        """转为空头：先平多，再开空。"""
        if self._pos is None:
            self._push_order_id(self._account.sell2short(symbol=symbol, volume=self._order_volume, price=price))
            return
        if self._pos.l_can_sell_volume > 0:
            self._push_order_id(
                self._account.sell(symbol=symbol, volume=self._pos.l_can_sell_volume, price=price)
            )
        if self._pos.s_current_volume + self._pos.s_active_volume + self._pos.s_active_close_volume < 2:
            self._push_order_id(self._account.sell2short(symbol=symbol, volume=self._order_volume, price=price))

    # ------------------------------------------------------------------
    # 其余事件
    # ------------------------------------------------------------------
    def on_tick(self, context: tbpy.Context, tick: tbpy.Tick):
        self._tick = tick

    def on_position(self, context: tbpy.Context, position: tbpy.Position):
        self._pos = position

    def on_order(self, context: tbpy.Context, order: tbpy.Order):
        # 委托进入终态后，从超时撤单表中移除
        done = (
            tbpy.OrderStatus.NewReject,
            tbpy.OrderStatus.AllFill,
            tbpy.OrderStatus.Canceled,
            tbpy.OrderStatus.CanceledFill,
        )
        if order.status in done:
            self._order_dict.pop(order.order_id, None)

    def on_fill(self, context: tbpy.Context, fill: tbpy.Fill):
        print(f"成交: {fill.symbol} vol={fill.fill_volume} price={fill.fill_price}")

    def on_timer(self, context: tbpy.Context, timer_id: int, interval_millsecs: int):
        # 撤销挂单超过 10 秒仍未成交的委托
        now = datetime.datetime.now()
        for order_id, send_time in list(self._order_dict.items()):
            if (now - send_time).seconds >= 10 and self._account is not None:
                self._account.cancel_order(order_id=order_id)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _push_order_id(self, order_id_list):
        """记录本次发出的委托号与时间，供 on_timer 超时撤单使用。"""
        if not order_id_list:
            return
        send_time = datetime.datetime.now()
        for order_id in order_id_list:
            self._order_dict[order_id] = send_time
