"""策略接线冒烟测试（限时自动退出）。

用途：在"不长时间阻塞、不依赖行情"的前提下，用真实账户验证策略的 on_init
是否成功——即 subscribe_tick / subscribe_bar / subscribe_account / create_timer
是否都能正常接线，账户能否正确订阅。

安全性：
    - 到设定时间后由 on_timer 主动调用 tbpy.exit() 退出事件循环，不会无限阻塞；
    - 若在非交易时段运行，无实时 tick/bar 驱动，且 DualMAStrategy 自身有
      "tick 为空 / 非实时 bar / 账户非 OnService 则不下单" 的保护，故不会触发委托。

运行：
    python main.py smoke            # 默认跑 15 秒
    $env:SMOKE_SECS = "20"; python main.py smoke
"""
import datetime
import os
import sys

# 允许脚本被直接运行时也能 import 到项目根目录下的模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tbpy  # noqa: E402

import config  # noqa: E402
import runtime  # noqa: E402
from strategies.dual_ma import DualMAStrategy  # noqa: E402

MAX_RUNTIME_SECS = int(os.getenv("SMOKE_SECS", "15"))


class _SmokeDualMA(DualMAStrategy):
    """在双均线策略基础上增加"限时自动退出"，仅用于接线冒烟测试。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._t0 = datetime.datetime.now()

    def on_init(self, context):
        super().on_init(context)
        print(f"[smoke] on_init 已返回，将在 {MAX_RUNTIME_SECS}s 后自动退出。")

    def on_timer(self, context, timer_id, interval_millsecs):
        super().on_timer(context, timer_id, interval_millsecs)
        elapsed = (datetime.datetime.now() - self._t0).seconds
        print(f"[smoke] 定时器触发，已运行 {elapsed}s（tick={'有' if self._tick else '无'}）")
        if elapsed >= MAX_RUNTIME_SECS:
            print("[smoke] 达到设定时长，主动退出事件循环。")
            tbpy.exit()


def run() -> int:
    """执行冒烟测试，返回进程退出码。"""
    print("=" * 60)
    print(f"[smoke] 连接平台: key={config.PRODUCT_KEY}")
    if not runtime.connect(config.PRODUCT_KEY):
        return 1

    symbol = config.SYMBOL
    print(f"[smoke] 账户={config.ACCOUNT_ID}  合约={symbol}  限时={MAX_RUNTIME_SECS}s")
    print("[smoke] 进入事件循环，验证 on_init 接线...")

    # 必须持有引用，避免策略对象被回收导致事件不再回调
    strategy = _SmokeDualMA(
        config.FAST_LENGTH, config.SLOW_LENGTH, config.ACCOUNT_ID, symbol,
        bar_frequency=config.BAR_FREQUENCY, order_volume=config.ORDER_VOLUME,
    )
    try:
        tbpy.exe()  # 阻塞，直到 on_timer 中调用 tbpy.exit()
    except KeyboardInterrupt:
        print("\n[smoke] 收到中断，退出。")
    finally:
        del strategy
        runtime.disconnect()

    print("[smoke] 冒烟测试结束。")
    return 0


if __name__ == "__main__":
    sys.exit(run())
