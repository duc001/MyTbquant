# tbpy 方法参考（真实请求 / 返回示例）

> 本文档每个方法都附 **真实调用示例** 与 **本次实测的真实返回**（2026-10-09 跑 `examples/probe_tbpy.py` 抓得，TBQuant3 客户端已登录）。
> 所有合约代码、数字、字符串均为 tbpy 实际返回，未作美化。
>
> 约定：
> - 业务代码**不直接**调 `tbpy.init/exit`，统一经 `runtime.connect(config.PRODUCT_KEY)` / `runtime.disconnect()`。
> - `get_history_n` 有**进程级全局「1 次成功 / 60s」**限流，业务一律经 `gated_get_history_n` 代理（见 §6）。
> - ndarray = numpy 数组；`time` 字段是 **object 数组，元素为字符串** `"YYYY-MM-DD 00:00:00"`。

---

## 1. `tbpy.init(product_key) -> bool` ／ `runtime.connect(product_key) -> bool`

**请求示例（我们统一用封装）**
```python
from runtime import connect, disconnect
ok = connect(config.PRODUCT_KEY)   # config.PRODUCT_KEY 来自 config.py
```
**返回示例**：`True`（已连）/ `False`（客户端未启动或已初始化）。

**说明**：`runtime.connect` 进程内幂等（已连直接返回）；底层 `tbpy.init`，平台未启动会抛异常被捕获为 `False`。`disconnect()` 底层 `tbpy.exit()`，在 `atexit` 统一释放防实例堆积。

---

## 2. `tbpy.get_symbols() -> list[str]`

**请求示例**
```python
import tbpy
syms = tbpy.get_symbols()   # 必须在「用完即退」独立子进程里调（见 §5 坑位）
```
**返回示例（真实，约 11 万条）**
```python
['SR611C4600.CZCE', 'm2701.DCE', 'AP701.CZCE', 'CF501.CZCE', ...]  # 全部 "代码.交易所"
```
**返回结构**：字符串列表，每条形如 `"<商品码><月份><C/P><行权价>.<交易所>"` 或 `"<主力码>.<交易所>"`。

**⚠ 坑位**：一次性物化 ~11 万对象会**损坏进程堆**，之后同进程 `get_instrument`/`get_main_instrument` 返回垃圾甚至 access violation。**必须**放独立子进程（`_option_index_builder.py`），主进程只读其落盘的 `data/option_index.json`。

---

## 3. `tbpy.get_main_instrument(underlying_symbol: str) -> Instrument`

**请求示例**
```python
inst = tbpy.get_main_instrument(underlying_symbol="SR.CZCE")
sym = inst.symbol          # 每属性只读一次，防堆损坏
```
**返回示例（真实）**
```python
Instrument(symbol='SR701.CZCE')     # 即 get_main_instrument 的 .symbol
```
**返回结构**：`Instrument` 对象；本仓库只用 `.symbol`（主力合约代码）。

**⚠ 坑位**：**未连接状态裸调会 access violation（0xC00000005）**——必须 `connect` 后调用，且放独立子进程（`_main_month_builder.py`）。

---

## 4. `tbpy.get_instrument(symbol: str) -> Instrument | None`

**请求示例**
```python
inst = tbpy.get_instrument(symbol="SR611C4600.CZCE")
name = inst.sec_name        # 合约名（非空判有效）
exp  = inst.delisted_date   # 到期日
```
**返回示例（真实）**
```python
Instrument(
    symbol='SR611C4600.CZCE',
    sec_name='白糖期权SR611C4600',
    delisted_date='2026-10-13 00:00:00',
)
```
**返回结构**：`Instrument`；用到属性 `.symbol` / `.sec_name` / `.delisted_date`（datetime 或字符串）。

**说明**：本仓库用 `.sec_name` 非空判合约有效（`d06.make_tbpy_validator`），用 `.delisted_date` 补期权链到期月（`d06._option_delisted_date`）。**坑位**：在 `get_symbols` 同进程内不可靠（堆已损），到期日补齐走「干净进程」。

---

## 5. `tbpy.get_history_n(symbol, period, count, *, fields=..., timeout=..., flag=...) -> dict`

**请求参数**

| 参数 | 类型 | 必填 | 说明 | 本仓库取值 |
|---|---|---|---|---|
| `symbol` | `str` 或 `list[str]` | ✓ | 单合约，或**批量 ≤20** | 单：`"SR701.CZCE"`；多：`["SR611C4600.CZCE","SR701.CZCE"]` |
| `period` | `str` | ✓ | K 线周期 | `"1d"` |
| `count` | `int` | ✓ | 取最近 N 根 | `1`（最新 1 根）/ `3`（多根） |
| `fields` | `list[str]` | ✓ | 字段名 | `"close","time"` / `"position"`(OI) / `"open","high","low","close","time"` |
| `timeout` | `str` | | 超时 | `"30s"` |
| `flag` | `QuoteFlag` | | 复权标记（仅 index 用） | `tbpy.QuoteFlag.RolloverBackWard`（后复权） |

**请求示例（真实调用）**
```python
# 单合约·主连期货·多字段多根
r = tbpy.get_history_n("SR701.CZCE", "1d", 3,
                       fields=["open","high","low","close","time"], timeout="30s")

# 单合约·期权·取收盘权利金 + OI(position)
r = tbpy.get_history_n("SR611C4600.CZCE", "1d", 1,
                       fields=["close","position","time"], timeout="30s")

# 多合约（≤20）
r = tbpy.get_history_n(["SR611C4600.CZCE","SR701.CZCE"], "1d", 1,
                       fields=["close","time"], timeout="30s")
```

**返回结构（两种，按 symbol 数判定）**
- **单合约** → 扁平 `{field: ndarray}`
- **多合约** → 嵌套 `{symbol: {field: ndarray}}`
- 数值字段 `dtype=float64`，`position`(OI) 为 `int64`，`time` 为 `object` 数组（元素是字符串 `"YYYY-MM-DD 00:00:00"`）。

**返回示例（本次真实返回）**
```python
# ① 单合约 SR701.CZCE，count=3 → 扁平
{
  "open":   array([5485., 5464., 5492.]),      # float64
  "high":   array([5504., 5502., 5554.]),
  "low":    array([5455., 5442., 5488.]),
  "close":  array([5475., 5491., 5546.]),
  "time":   array(['2026-10-08 00:00:00',
                   '2026-10-09 00:00:00',
                   '2026-10-12 00:00:00'], dtype=object),
}

# ② 单合约 SR611C4600.CZCE，count=1，含 OI → 扁平
{
  "close":    array([625.]),        # float64，最新收盘权利金 625
  "position": array([7]),           # int64，持仓量 OI = 7（流动性低）
  "time":     array(['2026-08-31 00:00:00'], dtype=object),  # 该期权最新日线停在 8-31
}

# ③ 多合约 count=1 → 嵌套
{
  "SR611C4600.CZCE": {"close": array([625.]),  "time": array(['2026-08-31 00:00:00'], dtype=object)},
  "SR701.CZCE":      {"close": array([5546.]), "time": array(['2026-10-12 00:00:00'], dtype=object)},
}
```

**限流时的真实返回（紧跟成功调用 <60s）**
```python
# 上一次 get_history_n 成功后 62s 内再调：
tbpy.get_history_n(["SR611C4600.CZCE","SR701.CZCE"], "1d", 1, fields=["close","time"])
# → {}        （空 dict）
tbpy.get_last_err()
# → '查询历史失败,查询限制,间隔=60000ms,合约数量=20'
```
> 单合约被限流则返回 `{field: <len0 ndarray>}`（非空 dict、空数组）。两者都「无数据」。
> **判定限流以「返回是否有数据」为先决**，`get_last_err()` 粘滞不可盲信。

**调用点**：`d08.close_batch` / `d09.combo_oi_close_batch` / `d12.history_batch` / `d14.oi_batch`，均经 `gated_get_history_n` 代理。

---

## 6. `tbpy.get_current_tick(symbol: str) -> Tick | None`

**请求示例**
```python
t = tbpy.get_current_tick(symbol="SR701.CZCE")
if t is not None:
    last = t.last
    bidasks = t.bidasks          # 盘口列表
    bid = bidasks[0].bid_p       # 买一（部分版本为元组 bidasks[0][0]）
    ask = bidasks[0].ask_p       # 卖一
```
**返回示例（真实，休市期）**
```python
None        # 休市 / 无行情时返回 None
```
**返回结构**：`Tick` 对象（`None` = 无数据）。用到属性 `.last`（最新价 float）、`.bidasks`（盘口列表，期权 `bidasks[0]` 为买一卖一，取 `.bid_p`/`.ask_p`）。

**说明**：盘中实时价**不缓存**，每次强制刷 tick；盘后回退 `get_history_n` 的 `close`。本次抓样本时为周五夜盘休市，故返回 `None`（真实行为，非异常）。

---

## 7. `tbpy.get_last_err() -> str`

**请求示例**
```python
err = tbpy.get_last_err()
```
**返回示例（真实）**
```python
""                                  # 最近一次操作成功后：空
"查询历史失败,查询限制,间隔=60000ms,合约数量=20"   # 紧跟成功调用被限流时
"SR2701C5600.CZCE 无效合约"          # 合约代码过期/无效时
```
**说明**：返回**最近一次操作**的错误文本；成功后被清空。连接失败诊断、限流判定（配合「是否有数据」）时使用。粘滞，勿单独作为成功/失败判据。

---

## 8. `tbpy.QuoteFlag.RolloverBackWard`

**请求示例**
```python
try:
    flag = tbpy.QuoteFlag.RolloverBackWard
except Exception:
    flag = None
r = tbpy.get_history_n(sym, "1d", count, fields=[...], timeout="30s",
                       **({"flag": flag} if flag else {}))   # 仅 d12 后复权主连日线用
```
**真实可用枚举（本次 `dir(tbpy.QuoteFlag)` 抓得）**
```
CCData, KCutEquidistant, KCutShowEndTime, KCutTradeTime, KCutTradeTimeNormal,
Null, OnlyDay, OnlyNight, RolloverBackWard, ...
```
**说明**：连续合约复权/切片标记；本仓库仅 `d12` 取后复权主连日线传 `RolloverBackWard`。取不到则 `flag=None` 按普通合约取数。

---

## 9. 我们封装的辅助（必读，非 tbpy 原生）

### 9.1 `gated_get_history_n(tbpy, *args, **kwargs) -> dict`
位置：`sell_option/fetchers/_history_gate.py`。**入参与 `tbpy.get_history_n` 完全一致**，仅多一个首参 `tbpy`。行为：成功后排冷却（`SELLOPT_HISTORY_GAP` 默认 62s）+ 限流签名兜底退避（`SELLOPT_HISTORY_RETRY` 默认 2）+ 进程锁。**所有 `get_history_n` 都应经它**。

### 9.2 `runtime.connect` / `runtime.disconnect`
见 §1。

---

## 10. 实跑坑位速查（已逐一验证）

| 现象 | 真实根因 | 处置 |
|---|---|---|
| `get_symbols()` 后 `get_instrument` 返回垃圾 | ~11 万对象物化**损坏堆** | 独立子进程（`_option_index_builder`），主进程只读 JSON |
| `get_main_instrument` 裸调崩 `0xC0000005` | **未连接**时裸调 access violation | `connect` 后 + 独立子进程 |
| `get_history_n` 返回 `{}` / 空数组，err 含 `间隔=60000` | **60s 全局限流** | 经 `gated_get_history_n`；成功才排冷却，勿盲睡 62s |
| 成功调用后 `get_last_err()` 仍有 throttle 文本 | err **粘滞** | 判定限流先以「返回是否有数据」为准 |
| `get_current_tick` 返回 `None` | 休市/无行情 | 判 `None` 后回退 `get_history_n` 的 close |
| `connect` 返回 `False` 但 tbpy 实际可用 | 本进程已 `init`（「已初始化」） | 判 `get_last_err()` 含「已初始化」即按已连接 |

---

## 11. 策略 / 交易回调 API（示例 `strategies/dual_ma.py`）

> 以下为 TBQuant **策略模式**接口（继承 `tbpy.IStrategy`、由平台事件驱动）。本仓库核心为离线数据管线，此 API 仅作模板示例。

**请求/返回（真实代码签名）**
```python
class DualMAStrategy(tbpy.IStrategy):
    def __init__(self, fast_length, slow_length, account_id, symbol,
                 bar_frequency="10s", order_volume=1):
        super().__init__("DualMAStrategy")

    def on_init(self, context: tbpy.Context):
        context.subscribe_tick(symbol=self._symbol)          # -> int|None（None=成功）
        context.subscribe_bar(symbol=self._symbol,
                              frequency="10s",
                              begin_time=datetime.datetime.now() - datetime.timedelta(minutes=self._slow_length),
                              sliding_window=self._slow_length + 1)   # -> int|None
        self._account = context.subscribe_account(account_id=self._account_id)  # -> tbpy.Account
        self._timer_id = context.create_timer(interval_millsecs=5000)          # -> int
        self._tick = tbpy.get_current_tick(symbol=self._symbol)                # -> Tick|None
        self._pos  = self._account.get_position(symbol=self._symbol)            # -> Position

    def on_bar(self, context, bars, symbol, flag): ...   # flag=0 历史 / 1 实时
    def on_tick(self, context, tick: tbpy.Tick): ...
    def on_position(self, context, position: tbpy.Position): ...
    def on_order(self, context, order: tbpy.Order): ...
    def on_fill(self, context, fill: tbpy.Fill): ...
    def on_timer(self, context, timer_id, interval_millsecs): ...
```

**`tbpy.Account` 下单 / 查询（真实方法）**
```python
self._account.get_status()                       # -> AccountStatus（判 == tbpy.AccountStatus.OnService）
self._account.get_position(symbol=...)           # -> Position
self._account.buy(symbol=, volume=, price=)      # 开多 -> order_id 列表
self._account.sell(symbol=, volume=, price=)     # 平多
self._account.buy2cover(symbol=, volume=, price=)# 平空
self._account.sell2short(symbol=, volume=, price=)# 开空
self._account.cancel_order(order_id=)            # 撤单
```

**数据类型属性（真实字段名）**
```python
tbpy.Tick:      .last, .bidasks              # bidasks[0].bid_p / .ask_p
tbpy.Position:  .s_can_cover_volume, .l_current_volume, .l_active_volume,
                .l_active_close_volume, .l_can_sell_volume,
                .s_current_volume, .s_active_volume, .s_active_close_volume
tbpy.Order:     .order_id, .status           # tbpy.OrderStatus.NewReject/AllFill/Canceled/CanceledFill
tbpy.Fill:      .symbol, .fill_volume, .fill_price
```

---

## 12. 复现上述真实样本

```bash
# 需先启动并登录 TBQuant3 客户端
cd d:\PythonProject\MyTbquant
.venv\Scripts\python.exe examples/probe_tbpy.py
# 输出见 sell_option/logs/probe_tbpy.txt（含 60s 限流间隔的两次 62s 等待）
```
> 探针会自动在两次 `get_history_n` 间 `sleep(62)` 避开限流，依次真实抓取：主连 OHLC(count=3)、期权(含 OI)、多合约嵌套结构。

---

## 13. 批量分页（20/批 + 62s 冷却）完整示例脚本

**脚本**：`examples/example_batch_history.py`（已实跑，2026-10-09 真实输出见下）。完整复刻各 fetcher（d08/d09/d12/d14）的取数编排：
1. 合约列表分块，每块 **≤20**（tbpy 单次查询上限）；
2. 块间 `sleep(62)`（>60s 限流窗口）后，经 `gated_get_history_n` 拉取；gated 内部还会在「成功」后自动排冷却 + 限流兜底退避；
3. 解析返回：单合约扁平 `{field:ndarray}` / 多合约嵌套 `{symbol:{field:ndarray}}`。

**核心编排（脚本节选）**
```python
BATCH_MAX = 20
COOLDOWN = 62

for i, ch in enumerate(chunk(syms, BATCH_MAX)):
    if i > 0:
        time.sleep(COOLDOWN)                       # 块间冷却，避 60s 限流
    r = gated_get_history_n(
        tbpy,
        ch if len(ch) > 1 else ch[0],              # 多合约传 list，单合约传 str
        "1d", 1, fields=["close", "time"], timeout="30s")
    if not r:
        continue                                   # 限流/无数据 → 跳过（不冒充）
    multi = len(ch) > 1
    for s in ch:
        data = r.get(s) if multi else r
        close = data.get("close")
        # ⚠ numpy 空数组不能写 `if close`，必须 len(close) 判断，否则抛 ambiguous
        c = float(close[-1]) if close is not None and len(close) else None
```

**运行**
```bash
cd d:/PythonProject/MyTbquant
.venv/Scripts/python.exe examples/example_batch_history.py
```

**真实输出（本次实跑，25 合约 → 2 批）**
```
[batch] 共 25 个合约 → 2 批（每批≤20）
  批 1: 取到 13/20 个
  批 2: 取到 5/5 个
[batch] 完成，共取到 18/25 个合约收盘
  AP612C6700.CZCE: close=483.5  bar=2026-10-09 00:00:00
  AP612C6800.CZCE: close=396.0  bar=2026-10-09 00:00:00
  AP612C6900.CZCE: close=306.5  bar=2026-10-09 00:00:00
  AP612C7000.CZCE: close=251.5  bar=2026-10-09 00:00:00
  AP612C7100.CZCE: close=165.0  bar=2026-10-09 00:00:00
  ...（其余 AP 看涨期权，沿行权价递减）
```
> 25 个里只取到 18 个：未被取到的 7 个是**流动性不足、近期无日线**的虚值合约，tbpy 返回空数组（`len(close)==0`）→ 按 `None` 跳过。这正是「两路皆无 → MISSING，绝不冒充」的真实体现。

