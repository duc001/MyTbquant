"""option.db 全部 12 张表的 schema 与初始化。

单一库文件 data/option.db：控制面 7 张（库表设计 §二）+ 业务数据表 5 张（§三）。
账户资金为人工 JSON（option/sell/data/account_capital.json），不建表。
本文件是 docs/库表设计.md 的可执行镜像，字段口径 / 单位 / 约束以该文档为准。
"""
import os
import sqlite3

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DB_DIR, "option.db")

# ============ 控制面（§二，7 张）============

GLOBAL_CONFIG_DDL = """
CREATE TABLE IF NOT EXISTS global_config (      -- KV 配置，业务复合键 (group,key) 为主键，无自增 id
    "group"     TEXT,              -- 组/分组（account/budget/source/schedule/gate/mode/notify/path/threshold）
    key         TEXT,              -- 组内的键名（如 strategy_id / max_risk_degree）
    value       TEXT,              -- 配置值
    remark      TEXT,              -- 说明
    status      INTEGER DEFAULT 1, -- 1=可用/有效 / 0=不可用/无效（停用该配置）
    create_time TEXT,              -- 创建时间 YYYY-MM-DD HH:MM:SS
    update_time TEXT,              -- 最后更新时间
    PRIMARY KEY ("group", key)     -- (group,key) 共同决定唯一 value；同组可多行（如多个账号）
);
"""

DATA_STORAGE_RULE_DDL = """
CREATE TABLE IF NOT EXISTS data_storage_rule (  -- 元数据表（一张表一行，manual 维护、无外部拉取源）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_ref    TEXT UNIQUE,   -- 物理表引用＝唯一键（一张表一行）+ 与 data_state_check 对账连接键
    table_name   TEXT,          -- 表名（人读）
    date_type    TEXT,          -- bar / static（本库无 tick 档）
    data_source  TEXT,          -- 主数据源（tbpy / openvlab / manual）
    pull_method  TEXT,          -- post_close / intraday / event / low_freq
    pull_count   INTEGER,       -- 每标的回溯条数（static 档留空）
    store_mode   TEXT,          -- incremental_upsert / full_window_rewrite
    required_fields TEXT,       -- 必填非空字段清单（逗号分隔列名）
    status       INTEGER DEFAULT 1,  -- 1=有效 / 0=无效
    remark       TEXT,
    create_time  TEXT,
    update_time  TEXT
);
"""

DATA_STATE_CHECK_DDL = """
CREATE TABLE IF NOT EXISTS data_state_check (   -- 就绪登记（状态面，每表一行）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_ref TEXT UNIQUE,                  -- = data_storage_rule.table_ref，1:1 对账
    bar_start_date TEXT,                    -- 覆盖的数据起始交易日
    bar_end_date TEXT,                      -- 覆盖的数据结束交易日（最新已就绪 bar_date）
    status    TEXT,                         -- init→fetching→(normal|abnormal)；仅 normal 放行
    last_detail_id INTEGER,                 -- 最近一次明细引用（→ data_pull_detail.id）
    remark TEXT,
    create_time TEXT, update_time TEXT
);
"""

DATA_PULL_DETAIL_DDL = """
CREATE TABLE IF NOT EXISTS data_pull_detail (   -- 拉取明细流水（每动作一行）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_ref TEXT,                         -- 连接键（= data_storage_rule.table_ref）
    pull_date TEXT,                         -- 拉取日期（动作发生日，非 bar 归属日）
    pull_time TEXT,                         -- 拉取完整时间戳 YYYY-MM-DD HH:MM:SS
    bar_start_date TEXT,                    -- 本次覆盖数据起始交易日
    bar_end_date TEXT,                      -- 本次覆盖数据结束交易日
    expected_n INTEGER,                     -- 应有条数（审计用）
    actual_n INTEGER,                       -- 实拉条数
    status    TEXT,                         -- 本次动作结果状态快照
    err_msg     TEXT,                       -- 明细错误摘要（截断勿逐条罗列）
    remark TEXT,
    create_time TEXT, update_time TEXT,
    UNIQUE (table_ref, pull_time)           -- 防同批次重复 upsert
);
"""

FUTURE_INFO_DDL = """
CREATE TABLE IF NOT EXISTS future_info (        -- 期货品种信息（品种级·固定；权威源 品种边界表.md）
    product_cd        TEXT PRIMARY KEY,  -- 品种代码（含大小写，如 AP/rb/m）
    product_name      TEXT,             -- 品种中文名
    exchange          TEXT,             -- 交易所编码（SHFE/DCE/CZCE/GFEX）
    last_price        REAL,             -- 参考现价（镜像值；权威现价取 contract_daily 最新 bar_date 的 close）
    multiplier        REAL,             -- 乘数 contract_unit（期权与标的期货一致）
    margin_per_lot    REAL,             -- 单腿保证金（元）＝ 现价×乘数×保证金率（源为万元，load 时 ×10000 归一）
    actual_margin_per_lot REAL,         -- 实际占用（元/手）＝ 单腿保证金×组合优惠
    combo_discount    REAL,             -- 组合优惠系数（DCE·GFEX ×0.20，其余 1.0）
    fut_symbol_template TEXT,           -- 期货 symbol 格式模板
    main_index_symbol TEXT,             -- 主力指数合约（后复权主连，直接拉日线，如 rb888.SHFE）
    opt_call_template TEXT,             -- 期权 C 格式模板
    opt_put_template  TEXT,             -- 期权 P 格式模板
    remark            TEXT
);
"""

TRADING_CALENDAR_DDL = """
CREATE TABLE IF NOT EXISTS trading_calendar (   -- 交易日历（静态参考）
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    bar_date            TEXT UNIQUE, -- 交易日 YYYY-MM-DD
    is_trading_day        INTEGER,  -- 1 交易日 / 0 非
    is_month_end          INTEGER,
    is_quarter_end        INTEGER,
    is_year_end           INTEGER,
    prev_bar_date       TEXT,
    next_bar_date       TEXT,
    pre_long_holiday_days   INTEGER,
    post_long_holiday_days  INTEGER,
    holiday_name          TEXT,
    remark                TEXT
);
"""

OPTION_INFO_DDL = """
CREATE TABLE IF NOT EXISTS option_info (         -- 期权合约信息（合约级·滚动，静态参考）
    symbol            TEXT PRIMARY KEY,  -- 期权合约代码 m2611-C-3000.DCE / cu2611C80000.SHFE
    product_cd        TEXT,  -- 标的品种码（→ future_info.product_cd）
    underlying_symbol TEXT,  -- 标的期货合约 m2611.DCE（→ contract_daily.symbol）
    underlying_month  TEXT,  -- 标的月 YYMM 4 位 '2611'（CZCE 3 位按年代补全）
    cp                TEXT,  -- C 认购 / P 认沽
    strike            REAL,  -- 行权价
    listed_date       TEXT,  -- 上市日 YYYY-MM-DD
    delisted_date     TEXT,  -- 最后交易日 YYYY-MM-DD（DTE 基准；过期即失效）
    price_tick        REAL,  -- 最小变动价位（与标的期货不同，必须本表存）
    sec_name          TEXT,  -- 合约中文名
    deal_times        TEXT,  -- 交易时段串（session_hours 交叉校验源）
    UNIQUE (symbol)
);
"""

# ============ 业务数据表（§三，5 张）============

MAIN_CONTRACT_DAILY_DDL = """
CREATE TABLE IF NOT EXISTS main_contract_daily ( -- 主连连续合约后复权日线（仅相对指标）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    main_symbol   TEXT,          -- 主连连续合约代码，如 m9888.DCE / rb888.SHFE
    bar_date   TEXT,          -- 交易日 YYYY-MM-DD
    open         REAL,          -- 开盘价（后复权）
    high         REAL,          -- 最高价（后复权）
    low          REAL,          -- 最低价（后复权）
    close        REAL,          -- 收盘价（后复权）
    volume       REAL,          -- 成交量（手）
    oi           REAL,          -- 持仓量（手，← tbpy position；不用 position 列名以免与账户持仓混淆）
    rollover     REAL,          -- 后复权调整因子（换月系数）
    UNIQUE (main_symbol, bar_date)
);
"""

CONTRACT_DAILY_DDL = """
CREATE TABLE IF NOT EXISTS contract_daily (      -- 月度期货合约不复权日线（绝对价）
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT,          -- 月合约代码，如 m2601.DCE
    product_cd   TEXT,          -- 标的代码（由 symbol 解析派生）
    expire       TEXT,          -- 到期月 YYYY-MM（由 symbol 解析派生）
    bar_date     TEXT,          -- 交易日 YYYY-MM-DD
    open         REAL,          -- 开盘价（不复权）
    high         REAL,          -- 最高价（不复权）
    low          REAL,          -- 最低价（不复权）
    close        REAL,          -- 收盘价（不复权）
    volume       REAL,          -- 成交量（手）
    oi           REAL,          -- 持仓量（手）
    pre_close    REAL,          -- 昨收（openvlab 补源）
    settle       REAL,          -- 结算价（openvlab 补源）
    pre_settle   REAL,          -- 昨结算（openvlab 补源）
    amount       REAL,          -- 成交额（openvlab 补源）
    oi_chg       REAL,          -- 持仓量变化（openvlab 补源）
    data_source  TEXT,          -- 该行来源：tbpy / openvlab / tbpy+openvlab
    UNIQUE (symbol, bar_date)
);
"""

OPTION_DAILY_DDL = """
CREATE TABLE IF NOT EXISTS option_daily (        -- 期权合约不复权日线（权利金真值）
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT,  -- 期权合约代码（→ option_info.symbol）
    product_cd     TEXT,  -- 标的品种码（→ future_info.product_cd）
    bar_date       TEXT,  -- 该行价格所属交易日 YYYY-MM-DD（陈旧判定键）
    open           REAL,
    high           REAL,
    low            REAL,
    close          REAL,  -- 收盘价（不复权）＝权利金主值
    volume         REAL,  -- 成交量（手）
    oi             REAL,  -- 持仓量（手）
    pre_close      REAL,  -- 昨收（openvlab 补源）
    settle         REAL,  -- 结算价（openvlab 补源）
    pre_settle     REAL,  -- 昨结算（openvlab 补源）
    amount         REAL,  -- 成交额（openvlab 补源）
    oi_chg         REAL,  -- 持仓量变化（openvlab 补源）
    data_source    TEXT,  -- 该行来源：tbpy / openvlab / tbpy+openvlab
    UNIQUE (symbol, bar_date)
    -- 不落：iv / greeks（期权合约级，直取 openvlab dto，见库表设计 §七 Q9）
);
"""

OPTION_IV_DDL = """
CREATE TABLE IF NOT EXISTS option_iv (           -- 平值隐含波动率日线（品种 × 到期月 × 交易日，openvlab）
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    product_cd       TEXT,   -- 品种代码
    exp              TEXT,   -- 标的合约到期月 YYMM 4 位如 2611（与源 symbol M2611 / option_info.underlying_month 同格式）
    bar_date         TEXT,   -- 交易日 YYYY-MM-DD（含夜盘归属，非取数日）
    atmv             REAL,   -- 平值隐含波动率 %（全历史有源）
    atmv_1dchg       REAL,   -- 平值隐波日变（可由序列自算）
    atmv_percentile  REAL,   -- IV 百分位 0-100（openvlab 自算，仅当日行有值）
    skew             REAL,   -- 隐波偏度（仅当日行有值）
    skew_percentile  REAL,   -- 偏度百分位（仅当日行有值）
    rv22             REAL,   -- 已实现波动率 %（仅当日行有值）
    price            REAL,   -- 标的现价（仅当日行有值）
    carry            REAL,   -- 展期收益（仅当日行有值）
    valphaT          REAL,   -- 波动率 alpha（仅当日行有值）
    last_time        TEXT,   -- 源数据时点 YYYY-MM-DD HH:MM:SS（仅当日行有值）
    UNIQUE (product_cd, exp, bar_date)
);
"""

ACCOUNT_POSITION_DDL = """
CREATE TABLE IF NOT EXISTS account_position (    -- 账户真实持仓（券商真实数据，tbpy 账户接口）
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       TEXT,    -- 资金账号（多账户并存，勿硬编码）
    broker_id        INTEGER, -- 券商 ID
    symbol           TEXT,    -- 合约代码，如 m2701-P-3300.DCE（期权腿的 strike/cp 不落库，join option_info 取）
    product_cd       TEXT,    -- 标的品种码（由 symbol 解析派生；期货腿亦需）
    expire           TEXT,    -- 到期月 YYYY-MM（由 symbol 解析派生）
    pull_date        TEXT,    -- 归属交易日 YYYY-MM-DD（账户快照即当日拉取）
    side             TEXT,    -- long 权利仓(← l_*) / short 义务仓(← s_*) —— 方向的唯一权威列
    quantity         REAL,    -- 持仓手数（正数）；方向由 side 表达，不重复用负号
    data_source      TEXT,    -- tbpy（账户接口直查）/ manual（人工录入兜底）
    yesterday_volume REAL,    -- 昨仓手数
    can_close_volume REAL,    -- 可平量
    avg_cost         REAL,    -- 开仓均价（按成本价口径 ← l/s_avg_price_o）
    avg_cost_settle  REAL,    -- 均价（按结算价口径 ← l/s_avg_price）
    market_value     REAL,    -- 持仓市值 ← l/s_market_value
    unrealized_pnl   REAL,    -- 浮动盈亏（按成本价口径 ← l/s_float_porfit_o）
    unrealized_pnl_settle REAL, -- 浮动盈亏（按结算价口径 ← l/s_float_porfit，源端拼写 porfit）
    margin_occupied  REAL,    -- 占用保证金 ← l/s_use_margin_amount（期权义务仓实测 0，见 §七 Q10）
    UNIQUE (account_id, symbol, pull_date)
);
"""

_TABLES = [
    ("global_config", GLOBAL_CONFIG_DDL),
    ("data_storage_rule", DATA_STORAGE_RULE_DDL),
    ("data_state_check", DATA_STATE_CHECK_DDL),
    ("data_pull_detail", DATA_PULL_DETAIL_DDL),
    ("future_info", FUTURE_INFO_DDL),
    ("trading_calendar", TRADING_CALENDAR_DDL),
    ("option_info", OPTION_INFO_DDL),
    ("main_contract_daily", MAIN_CONTRACT_DAILY_DDL),
    ("contract_daily", CONTRACT_DAILY_DDL),
    ("option_daily", OPTION_DAILY_DDL),
    ("option_iv", OPTION_IV_DDL),
    ("account_position", ACCOUNT_POSITION_DDL),
]


def init_db(path: str = DB_PATH) -> str:
    """确保数据库文件存在（不建表）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.close()
    return path


def apply_schema(path: str = DB_PATH) -> None:
    """建全部 12 张表（IF NOT EXISTS，幂等）。"""
    init_db(path)
    conn = sqlite3.connect(path)
    try:
        for _name, ddl in _TABLES:
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    p = init_db()
    apply_schema(p)
    print(f"initialized {len(_TABLES)} tables at {p}")
