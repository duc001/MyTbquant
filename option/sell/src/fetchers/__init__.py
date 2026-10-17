"""阶段一：取数单元（fetchers）。

约定：一取数单元对应 data_storage_rule 的一个 table_ref（一表一单元）；
同一表的多源（tbpy 主源 + openvlab 补源）经共享 helper 组合，源逻辑不散落各文件。
每个 fetcher 暴露 load_<table>(...) 之类的入口，由上层驱动器按 data_storage_rule 调度。
"""
