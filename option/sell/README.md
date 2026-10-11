# option.sell

从零重写的期权卖方策略系统（Python 包路径 `option.sell`）。

旧系统 `strategies/sell_option/` 经运行发现较多问题，已停止维护、原样保留作参考，
本目录为全新起点。采用两层结构 `option/`（策略父命名空间）+ `sell/`（卖方子策略），
便于以后扩展 `option.buy` 等。

## 目录结构
- `src/`        —— 源码（重写从这里开始，包内 `from option.sell ...`）
- `data/`       —— 运行时数据（行情缓存 / 持仓 / 报表）
- `docs/`       —— 设计文档

> 当前为空骨架，逐步填充。
