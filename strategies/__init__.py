"""策略包：存放可复用的 tbpy 策略类（继承 tbpy.IStrategy）。"""
from strategies.dual_ma import DualMAStrategy

__all__ = ["DualMAStrategy"]
