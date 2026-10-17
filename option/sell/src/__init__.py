"""option.sell 源码包。

三阶段流水线：fetchers(阶段一取数) → validate(阶段二质检) → report(阶段三报表)；
schema / db 为共享底座。设计见 docs/库表设计.md / docs/需求文档.md。
"""
