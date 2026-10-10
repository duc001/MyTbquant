# 期权卖权策略 · 本地 DB 设计说明

> 本文档由代码（`sell_option/fetchers/*`、`strategy/*`）中的建表语句与模块 docstring 提取整理，
> 记录本策略使用的全部本地 SQLite 表：**表定义（DDL）+ 字段说明 + 用途 + 读写方**。

## 1. 总览

本策略的全部本地持久化都是 **SQLite 单文件库**，位于 `sell_option/data/`（已在 `.gitignore` 中忽略，
不进版本库，由各 fetcher 首次访问时自动建库建表）。

设计动机：

- **绕开 tbpy 实时取数限制**：tbpy 硬限「每 60s 仅 1 次历史查询」。所有行情都先落本地缓存，
  首跑写入、盘后重跑秒回（`ON CONFLICT` 幂等），从而把盘后批量取数压缩成少数几次 `get_history_n`。
- **替代 `tbpy.is_holiday()`**：tbpy 假日日历对部分日期误报为假期，会把真实交易日误判休市。
  交易日历改为本地 `calendar.db.manual_calendar` 用户维护表。
- **持仓簿独立落盘**：开/平仓记录存 `positions.db`，与行情缓存解耦，供盘后风控（§7.4）与 B/C 块读取。

### 1.1 库文件清单

| 文件 | 大小量级 | 内容 | 维护方式 |
|---|---|---|---|
| `data/calendar.db` | ~60 KB | 交易日历 / 交易时段 / FOMC 缓存 | **用户维护** + 自动写 FOMC |
| `data/spot.db` | ~68 KB | 现货/指数 收盘价格 | 自动写（D08） |
| `data/premium.db` | ~96 KB | 期权收盘权利金 + OI | 自动写（D09/D10/D14） |
| `data/daily.db` | ~4.7 MB | 后复权主连日线序列 | 自动写（D12） |
| `data/iv_snapshot.db` | ~56 KB | 每日 IV 快照（含百分位） | 自动写（D13） |
| `data/positions.db` | ~12 KB | 持仓簿（开/平仓） | 开/平仓动作写 |

### 1.2 统一约定（行情缓存库通用）

`spot.db` / `premium.db` / `daily.db` / `iv_snapshot.db` 遵循同一范式：

- **复合主键 `(symbol, pull_date)`**（IV 表为 `(variety, snapshot_date)`）：
  - `pull_date` = **数据拉取日**（本次运行日期）；
  - `bar_date` = **数据所属交易日**（如某根日线的收盘日，一般 = pull_date，跨夜盘边界时不同）。
    读取方一律按归属交易日严格查询，避免「`load_latest` 模糊回退」把脏日期当真。
  - `source` = 数据来源标记（如 `tbpy` / `cache`），用于自检与溯源。
- **幂等写入**：`INSERT ... ON CONFLICT(symbol, pull_date) DO UPDATE`（或 `INSERT OR REPLACE`），
  保证重跑不重复、不报错、秒回。
- **整序列存 JSON**：`daily.db.index_daily` 的 `data_json` / `close_json` 存放整条日线序列（非单值），
  供 `#3 市况`（ADX/斜率）、`#5 安全垫`（预期移动）、`#10 形态`（跳空/贴边/波动扩张）回算。

---

## 2. `calendar.db` — 日历 / 时段 / 事件缓存

### 2.1 `manual_calendar`（交易日历，用户维护）

```sql
CREATE TABLE manual_calendar (
    date    TEXT PRIMARY KEY,   -- 自然日 YYYY-MM-DD
    is_open INTEGER,            -- 0=休市 / 1=交易日；缺失→默认交易日
    note    TEXT
);
```

- **用途**：权威交易日历，**替代 `tbpy.is_holiday()`**。所有「某日是否可交易」判定（market_session、
  `_trading_day`、D15 长假日历、`is_closed_date` 等）统一读此表，不再触发 tbpy 调用。
- **读取方**：`market_session.is_closed_date`、`_trading_day.get_closed_dates`、`d15_calendar`、
  D02/D08/D12 等取数前的「是否交易日」判断。
- **写入方**：用户手动维护（如补班日标 0、异常日校正），通过 `add_calendar_date(date, is_open, note)` 幂等覆盖。
- **缺失语义**：无记录 → 默认交易日（由 `is_trading_time` 判具体时段兜底），不会误造长假。

### 2.2 `session_hours`（交易时段表，用户维护）

```sql
CREATE TABLE session_hours (
    exchange TEXT NOT NULL,
    product  TEXT NOT NULL DEFAULT '*',
    day      TEXT,   -- JSON 序列：[(start,end),...] 日盘时段
    night    TEXT,   -- JSON 序列：[(start,end),...] 夜盘时段
    note     TEXT,
    PRIMARY KEY (exchange, product)
);
```

- **用途**：各交易所/品种的**日盘、夜盘交易时段**定义，供 `market_session` 判定「当前是否处于可交易时段」。
  `day` / `night` 存 JSON 化的时间段对（如 `[("09:00","10:15"),("10:30","11:30"),("13:30","15:00")]`）。
- **读取方**：`session_hours.load()` → `market_session` 的时段判定（与 `manual_calendar` 并列为纯本地依据）。
- **写入方**：用户通过种子脚本（如 `seed_*`）播种标准时段，可按券商实际调整；`(exchange, product)` 复合主键覆盖更新。

### 2.3 `fomc_cache`（FOMC 日程缓存，自动写）

```sql
CREATE TABLE fomc_cache (
    pull_date   TEXT NOT NULL PRIMARY KEY,  -- 拉取日
    updated     TEXT,                       -- 落地时间
    events_json TEXT                        -- JSON 序列：[{date,name,codes},...]
);
```

- **用途**：缓存 FOMC（及同类宏观事件）日程全集，供 **#6 事件窗口** 提醒（D15）。
- **读取方**：`d15_calendar`（事件/长假提醒，D 块）。
- **写入方**：`d15_calendar.FomcCacheRepo.save()` 拉取后幂等落盘；落盘失败静默不阻断。

---

## 3. `spot.db` — 现货 / 指数收盘价格（D08）

```sql
CREATE TABLE spot_price (
    symbol    TEXT NOT NULL,
    pull_date TEXT NOT NULL,
    price     REAL,        -- 收盘价（真实价口径，D08 绝对价）
    source    TEXT,
    bar_date  TEXT,
    PRIMARY KEY (symbol, pull_date)
);
```

- **用途**：现货/指数收盘价格缓存。**真实价口径**（§二 串价铁律：与指数相对指标分列，混用即缺陷）。
- **读取方**：opening 真实价（选腿基准、#5 安全垫距离、#8 肥瘦 R 净）；report A 总表「真实价」列。
- **写入方**：`d08_spot_price`（`DEFAULT_SPOT_DB`），按 ≤20/批 + 62s 冷却汇总 `get_history_n` 后写入。

---

## 4. `premium.db` — 期权收盘权利金 + OI（D09 / D10 / D14）

```sql
CREATE TABLE premium (
    symbol    TEXT NOT NULL,
    pull_date TEXT NOT NULL,
    close     REAL,    -- 收盘权利金
    source    TEXT,
    bar_date  TEXT,
    oi        REAL,    -- 持仓量（Open Interest）
    PRIMARY KEY (symbol, pull_date)
);
```

- **用途**：期权合约**收盘权利金 + 持仓量**缓存，是选腿与资金/收益判据的数据底座。
  - `close` → 选腿、#7 保证金估算、#8 肥瘦 R/W、#9 净 credit；
  - `oi` → **#2 持仓量 OI**（卖腿/保护腿 OI 过薄判定，盘后仍可取，休市粗筛可用）。
- **读取方**：opening（`OpeningData.premium`）、#2/#7/#8/#9 检查、report A 总表对应列。
- **写入方**：
  - `d09_d10_premium.PremiumCacheRepo`（收盘权利金 `close`）；
  - `d14_contract_oi` / `_combo_oi_premium` 合并取数时一并写入 `oi` 列。
  - **优化**：`close` 与 `oi` 均已缓存则盘后重跑**秒回**，避免重复消耗 tbpy 60s 令牌。

---

## 5. `daily.db` — 后复权主连日线（D12）

```sql
CREATE TABLE index_daily (
    symbol     TEXT NOT NULL,
    pull_date  TEXT NOT NULL,
    bar_date   TEXT,
    data_json  TEXT,   -- 整条日线序列（开高低收/量等）JSON
    close_json TEXT,   -- 收盘价序列 JSON（便于 #3/#5/#10 回算）
    source     TEXT,
    PRIMARY KEY (symbol, pull_date)
);
```

- **用途**：**指数口径**（后复权主连日线，相对指标）。与 `spot.db` 真实价口径**分列**（§二 铁律）。
  提供 #3 市况（ADX/趋势斜率）、#5 安全垫（预期移动 = 2×ATR 钝化）、#10 形态（跳空/区间贴边/ATR 扩张）的回算原料。
- **读取方**：opening 指数口径（`index_daily`）、相关检查、report A 总表「指数」列。
- **写入方**：`d12_index_daily`（`DEFAULT_DAILY_DB`），按 ≤20/批 + 62s 冷却 `get_history_n` 写入；
  老库曾用 `cache_date`，已迁移为 `pull_date`（ALTER 安全、幂等）。

---

## 6. `iv_snapshot.db` — 每日 IV 快照（D13）

```sql
CREATE TABLE iv_snapshot (
    variety          TEXT NOT NULL,   -- 品种代码（如 SR / CF）
    snapshot_date    TEXT NOT NULL,   -- 快照日
    atmv_current     REAL,            -- 当前 ATM 波动率
    atmv_percentile  REAL,            -- IV 百分位（#4 充分性判据）
    atmv_1dchg       REAL,            -- 较昨日变化
    exp              TEXT,            -- 主力月 / 到期信息
    PRIMARY KEY (variety, snapshot_date)
);
```

- **用途**：每日 IV 快照，**#4 IV 分位与方向** 的判据来源（真实 IV 分位、是否仍在升）。
- **读取方**：opening #4 检查；live 取数缺百分位时**回退**本表（避免 #4 被静默失效）。
- **写入方**：`d13_iv_snapshot.IvSnapshotRepo`（盘后落地当日全品种快照）。

---

## 7. `positions.db` — 持仓簿（D16）

```sql
CREATE TABLE positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date        TEXT,     -- 开仓日
    code              TEXT,     -- 品种代码
    exchange          TEXT,
    structure         TEXT,     -- 单腿 / 双卖铁鹰
    short_symbol      TEXT,     -- 卖腿合约
    short_strike      REAL,
    short_premium     REAL,     -- 卖腿权利金
    short_bid         REAL,
    short_ask         REAL,
    prot_symbol       TEXT,     -- 保护腿合约
    prot_strike       REAL,
    prot_premium      REAL,     -- 保护腿权利金
    max_loss          REAL,     -- 单笔最大亏损（#8 预算口径）
    expiry_date       TEXT,     -- 最后交易日 YYYY-MM-DD（不变量；DTE 现算不入库）
    margin            REAL,     -- 占用保证金（#7）
    status            TEXT DEFAULT 'open',   -- open / closed
    exit_date         TEXT,     -- 平仓日
    exit_short_premium REAL,    -- 平仓时卖腿权利金
    exit_prot_premium  REAL,    -- 平仓时保护腿权利金
    realized_pnl      REAL,     -- 已实现盈亏
    exit_reason       TEXT,     -- 平仓原因（止盈/止损/破位/到期）
    note              TEXT
);
```

- **用途**：**持仓簿**，开源与平仓**共用一表**，`status` 区分 `open` / `closed`。
  是盘后持仓处置（B 块）与组合风控（C 块 §7.4）的唯一数据源。
- **读取方**：
  - `d16_positions.fetch_d16()` → 当前 `open` 持仓 → `PositionBook`；
  - `strategy.position`（笔数 / 每品种 / 同持品种数 是否触线）；
  - report B（逐持仓续持/止盈/止损/破位/到期）、C（组合风控）、D（提醒与待人工）。
- **写入方**：开仓动作（opening 下单后写入 `open` 行）、平仓动作（closing 写入 `exit_*` 并置 `status='closed'`）。
- **容错**：`fetch_d16` 先探文件存在 + 全列探测，损坏/缺列→返回 `loaded=False` + 错误信息，
  绝不静默空仓（report A 会标「positions.db 缺失/损坏 → B 块未跑」）。空表 = 没开仓，不是 MISSING。

---

## 8. 非 DB 的本地缓存（补充）

以下为 JSON 落盘（非 SQLite），供选腿/月份解析，列出以备完整：

- `data/option_index.json`：期权合约索引（`_option_index_builder` 连 tbpy `get_symbols` 解析分组后落盘；主进程只读不连）。
- `data/main_month.json`：各品种主力月（`_main_month_builder` 连 tbpy `get_main_instrument` 落盘）。

两者均在独立「用完即退」子进程生成，规避 tbpy 长连接占用；主流程只读 JSON。

---

## 9. 维护要点速查

| 表 | 谁写 | 谁读 | 是否用户维护 |
|---|---|---|---|
| `manual_calendar` | 用户（`add_calendar_date`） | market_session / _trading_day / D15 / 各 fetcher | **是** |
| `session_hours` | 用户（种子脚本） | market_session | **是** |
| `fomc_cache` | D15 自动 | D15 / D 块 | 否（自动） |
| `spot_price` | D08 自动 | opening / report | 否 |
| `premium` | D09/D10/D14 自动 | opening / 检查 / report | 否 |
| `index_daily` | D12 自动 | opening / 检查 / report | 否 |
| `iv_snapshot` | D13 自动 | #4 检查 | 否 |
| `positions` | 开/平仓动作 | D16 / position / report B·C·D | 否（业务写） |

> 设计原则：**行情 = 只读缓存（可丢弃重拉）；持仓 = 业务真相（不可丢）**。`.db` 均在 `data/` 下被 git 忽略，
> 行情库删除可自动重建；`positions.db` 含真实持仓，需单独备份。
