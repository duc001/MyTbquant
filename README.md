说明
    这是pbpy的开发和测试项目，用于测试和开发pbpy的功能。
    我本地已经按照了Tbquant和Tbquant3.并有可以运行的账号。
    tbpy已经下载了，目录： C:\Users\du chuan\Downloads\tbpy-3.12.3-py3-none-any.whl

一. 项目结构
    这是Python的项目，用于Tbquant和TBPY的开发和测试。

    MyTbquant/
    ├── main.py                 统一入口：check(默认)/option/collect/smoke/strategy 子命令
    ├── config.py               全局配置：产品key、账户、合约、策略参数(支持环境变量覆盖)
    ├── runtime.py              运行时辅助：封装连接/断开，兼容平台未启动时 init 抛异常
    ├── strategies/
    │   ├── __init__.py
    │   └── dual_ma.py          双均线策略示例(参考 docs/reference/tbpy教程.md 案例1，会真实下单)
    ├── sell_option/            国内商品期权卖出策略(新实现已按需求文档重建：checks/ 逻辑单元 + strategy/ 聚合层 + fetchers/ 数据层 + thresholds.py 阈值层；sellopt.py 命令入口；旧实现 2026-09-27 删除)
    │   └── data/               运行时数据(日线缓存/快照/持仓/报告，git 忽略)
    ├── examples/
    │   ├── __init__.py
    │   ├── query_data.py       只读数据查询示例(安全可反复运行；案例2 的只读部分)
    │   ├── option_data.py      期权数据查询示例(只读)：合约元数据/实时行情/标的与历史
    │   ├── eg_history.py       EG(乙二醇)历史K线拉取(限流感知重试；只读)
    │   ├── collect_bars.py     subscribe_bar 实时K线收集器(只读，限时退出)
    │   └── smoke_strategy.py   策略接线冒烟测试(限时自动退出，验证 on_init 接线)
    ├── docs/                    设计/参考文档（DB设计说明、tbpy方法参考、需求与边界表、reference/ 外部教程）
    │   ├── DB设计说明.md          本地 SQLite 库表设计
    │   ├── tbpy方法参考.md        tbpy 方法请求/返回示例
    │   ├── 期权卖出策略需求文档.md 需求唯一权威源(WHAT)＋实现参考(HOW)
    │   ├── 品种边界表.md          权威 47 品种边界表
    │   └── reference/            外部教程与配图(tbpy教程.md、Tbquant教程.docx 等)
    ├── pyproject.toml          项目与依赖声明(uv 管理)
    ├── uv.lock                 依赖锁定文件
    └── .venv/                  虚拟环境(Python 3.12)

二. 资料说明
    ./docs/reference/Tbquant教程.docx
    这是Tbquant的教程，用于Tbquant的开发，它有自己的开发语言运行平台，Python的开发平台不能运行Tbquant语言开发的代码。
    ./docs/reference/tbpy教程.md
    这是Tbpy的教程，使用Python开发，可以运行Tbpy的代码。用于连接与Tbqant平台。

三. 环境安装
    本项目使用 uv 管理依赖，Python 版本需 >= 3.12。在项目根目录执行：
    1) 安装本地 tbpy whl 与 numpy：
         uv pip install "C:\Users\du chuan\Downloads\tbpy-3.12.3-py3-none-any.whl"
         uv pip install numpy
       注：tbpy 3.12.3 的 whl 未声明 numpy，但 import tbpy 需要 numpy，须手动补装。
    2) 运行前请先启动并登录 TBQuant 客户端，否则 init 会提示类似"请先启动tbquant.exe!"。

四. 运行方式
    在项目根目录执行：
    1) 只读连通性与数据查询(安全，不下单)：
         python strategies/sell_option/main.py check        (等价于直接 python strategies/sell_option/main.py)
    2) 期权数据查询(只读)：合约元数据 / 实时行情 / 标的主力与历史
         python strategies/sell_option/main.py option       (标的品种由 config.OPTION_UNDERLYING 指定，默认 m.DCE)
    3) 实时收集具体合约K线(subscribe_bar，只读，限时自动退出；休市无行情流)：
         python strategies/sell_option/main.py collect      (默认 eg2611.DCE 1d 收集20s；-f 周期 -t 秒数)
    4) 策略接线冒烟测试(限时自动退出；休市时不会下单)：
         python strategies/sell_option/main.py smoke        (默认约15秒，可用 $env:SMOKE_SECS 调整)
    5) 运行双均线策略(会向账户下单，需输入 yes 二次确认；建议开盘时段运行)：
         python strategies/sell_option/main.py strategy
    配置可用环境变量覆盖，例如(PowerShell)：
         $env:TBPY_PRODUCT    = "tbquant"
         $env:TBPY_ACCOUNT_ID = "tbf_fasot1"
         $env:TBPY_SYMBOL     = "rb000.SHFE"

五. 切换产品 (tbquant / tbquant3 / tbquant3.easy)
    产品 key 只在一处生效：runtime.py 里的 tbpy.init(key)，它决定连接哪个已启动的
    客户端；查询/策略等其余代码完全通用，切换无需改动其它任何地方。
    三种方式(优先级由高到低)：
    1) 命令行临时切换(推荐，一次性)：
         python strategies/sell_option/main.py check    -p tbquant3
         python strategies/sell_option/main.py strategy -p tbquant3
    2) 环境变量(当前终端会话内持续生效)：
         $env:TBPY_PRODUCT = "tbquant3"
         python strategies/sell_option/main.py check
    3) 改默认值(长期)：编辑 config.py 中 PRODUCT_KEY 的默认值。
    注意：切到哪个产品，就必须先启动并登录对应客户端(TBQuant / TBQuant3)；
    且账户 ACCOUNT_ID 通常也需切换为该客户端下已登录的账户(否则订阅账户会失败)。

六. 切换账户 (两个仿真账户，每次只用一个)
    config.py 的 ACCOUNTS 预设了两个仿真账户别名(均 broker_id=482)：
        sim1 -> tbf_fasot1        sim2 -> 66107987
    账户只在“订阅账户/下单”时用到(check 只读查询不依赖具体账户)。切换方式(优先级由高到低)：
    1) 命令行 -a(推荐)：可填别名，也可直接填原始 account_id
         python strategies/sell_option/main.py strategy -a sim2
         python strategies/sell_option/main.py smoke    -a 66107987
    2) 环境变量：
         $env:TBPY_ACCOUNT_ID = "sim2"      (或直接填原始 ID)
         python strategies/sell_option/main.py strategy
    3) 改默认/增删账户：编辑 config.py 的 DEFAULT_ACCOUNT 或 ACCOUNTS 字典。
    -a 可与 -p 组合：python strategies/sell_option/main.py strategy -p tbquant3 -a sim2

七. 期权数据字段速查 (实测 tbpy 3.12.3；完整可运行示例见 examples/option_data.py)
    A. 合约元数据 get_instrument -> Instrument（仅下列字段，无期权专用字段）：
         symbol / exchange / sec_id / sec_name / price_tick / contract_unit /
         big_point_value / listed_date / delisted_date / deal_times
         · 到期日(最后交易日) = delisted_date（可能返回 datetime 或 str，需归一化）
         · 行权价 / 看涨看跌(C/P) / 标的月份 → 无字段，只能从 symbol 解析
         · symbol 带交易所后缀，且格式随交易所不同：
             DCE : m2612-P-2950.DCE (带横线)    SHFE: cu2706C106000.SHFE (无横线)
    B. 实时行情 get_current_tick -> Tick：
         最新价=last  当日高/低=high/low  开盘=open  昨收=pre_close  昨结=pre_settlement
         盘口=bidasks[i] 的 bid_p/bid_v/ask_p/ask_v（BidAsk 对象）
         ✗ Tick 无持仓量(oi)字段
    C. 持仓量 OI：字段名 = open_int（Bar 的属性）
         · get_history_n 取 open_int 实测返回空；OI 只能在实时
           subscribe_bar→on_bar 的 bar.open_int 中拿到（交易时段）
    D. 标的：get_main_instrument / get_second_main_instrument -> Instrument（.symbol 取代码）
         日K：get_history_n(freq='1d', fields=[time,open,high,low,close,volume]) 实测有数据
    ★ get_history_n 全局限流(实测铁证)：一次成功后约 60s 内的后续查询(无论同/异合约)
      会被拦，返回【dict，各字段是长度 0 的 numpy 空数组】array([], dtype=float64)，
      dict 内【无】状态字段；真实原因看 tbpy.get_last_err()：
          '查询历史失败,查询限制,间隔=60000ms,合约数量=20'
      → 空返回时先读 get_last_err，命中限流就等约 60s 再试(密集重试只会持续撞墙)。
    ★ 单合约日线【能取到】：eg2611/eg2701 等具体月份合约的 1d 历史，冷却后首查即有
      数据(从各自上市日起)；此前"单合约日线取不到"是被【限流】误导的错误结论。
      指数(eg000.DCE)适合连续多年日线，单合约只有其存续期内的数据。
    ★ get_history_n 支持后复权：传 flag=tbpy.QuoteFlag.RolloverBackWard 即得服务端后复权
      序列(QuoteFlag 是 IntFlag，可与 OnlyDay/OnlyNight 用 | 组合)。实测主力指数连续(888/9888)
      原始为换月拼接、含台阶假跳空(豆粕 m9888 有 -6.37% 等 ≥3% 跳变)；加此 flag 后台阶抹平
      (max gap 降至 2.66%)、真实市场跳空保留。sell_option 日线口径即用后复权主连(888/9888)。
    ★ 其它通用坑：get_history_n 必须传 timeout(如 '30s')；非交易时段 get_current_tick
      返回 None 属正常；subscribe_bar 休市/盘后无实时流、历史回放(flag=0)也可能不触发。

八. 期权卖出策略 (sell_option/，只读不下单)
    旧实现(tbpy 单源、自建 IV 反解等)与新需求偏差过大，已于 2026-09-27 全部删除：
    examples/smoke_sell_option.py、examples/verify_*.py（命令入口 sellopt.py 与 sell_option/ 新实现已重建保留）。
    现存资产：
      · 期权卖出策略需求文档.md —— 需求唯一权威源(含 §十一 实现参考：tbpy 平台限制等)
      · 品种边界表.md —— 47 品种权威边界与 symbol 编码规则
      · sell_option/ —— 已按需求文档 §一–§十 重建的实现：fetchers/(数据层 D01–D18) /
        thresholds.py(阈值层) / checks/(逻辑单元 #1–#10) / strategy/(opening·assemble·runner·closing·
        position·funnel·report·cli 聚合层) / sellopt.py(命令入口)
        三源分工：tbpy 绝对价 / openvlab IV / 新浪 OI 备源
      · data/ —— 运行时数据(日线缓存/快照/持仓/报告，git 忽略)；旧原始缓存已于 2026-09-27 清除，重建时再生成
        (47 品种日线首拉 ≈3 分钟，限流可接受)

九. 日志规范（统一日志，§运维）
    全项目只有一个日志目录、按日期拆分文件，格式与调用位置统一。
    - 路径：sell_option/logs/app-YYYY-MM-DD.log（每天一个文件，追加；如 app-2026-10-03.log）。
    - 控制台同格式实时输出（供运行期肉眼观察）。
    - 统一格式：时间 | 级别 | logger名 | 模块.函数:行号 | 消息。
    - 存量代码里的 print 会被自动收纳进同一文件，并保留真实调用位置（模块/函数/行号），
      无需改动各业务文件；不要在业务代码里再开第二个日志文件或裸 print 直出。
    - 入口（sellopt.py / main.py）在最早位置调用 setup_logging() 初始化（定义见 sell_option/logutil.py）。
    - 新代码写法：
        · 取 logger：logger = get_logger(__name__)，用 logger.info / logger.debug 记录；
        · 关键函数：加 @log_call()，自动记「进入·请求参数 → 返回·输出参数 · 耗时」，异常也记；
        · 流程段：用 flow("环节名") 上下文管理器标注流转信息。
    - 跨天运行会在零点自动落到新的日期文件，历史按日期归档，互不混淆。


