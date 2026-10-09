"""逻辑单元层（§七）检查单元契约与元数据约定。

**每个检查项 ＝ `checks/` 下一个独立可复用单元模块 `cX_*.py`**，统一暴露：
- `RULE_ID`：规则编号字符串（"#1" .. "#10"）；
- `RULE_NAME`：中文名（"盘口价差" 等，入报告）；
- `BLOCKING`：bool，该检查 FAIL 是否淘汰候选（#2 持仓量OI / #3 市况 / #6 事件为 False：FAIL 降级 WARN、不淘汰）；
- `evaluate(*data_products, th) -> CheckResult`：**只吃数据层产物 + 阈值集**的纯函数——
  不自行取数、不读 config、无副作用；各检查的数据入参随其所需而不同（#1 吃 PremiumSet、#2 吃 OiSet，
  #5 吃 Spot+IV+DTE…），这是"按需依赖数据层"的可复用体现。

**依赖单向**：逻辑单元层只依赖数据层（`fetchers` 产物 + `thresholds`），**绝不依赖策略聚合层**——
因此每个 cX 单元都能脱离聚合层单独 import、单独由 `tests/` 传入构造好的数据层产物做离线单元测试（业务代码的 `__main__` 一律连真实数据，禁假源模拟）。

聚合层（`strategy/`）通过【适配器】把各检查需要的数据层产物分发过去（见 strategy.opening.OPENING_CHECKS），
逻辑单元本身对聚合层一无所知。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import CheckResult


@runtime_checkable
class CheckUnit(Protocol):
    """逻辑单元模块的元数据契约（模块对象即可满足：具备下列模块级属性）。

    evaluate 的具体数据入参各检查不同（按需依赖数据层产物），故此处只约束元数据 + 返回类型，
    不固定 evaluate 签名；聚合层用适配器统一成 (gathered, th) -> CheckResult 后再编排。"""

    RULE_ID: str
    RULE_NAME: str
    BLOCKING: bool


__all__ = ["CheckUnit", "CheckResult"]
