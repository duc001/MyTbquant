"""全局配置模块。

集中管理"连接的产品、交易账户、合约、策略参数"等配置，供示例与策略复用。
所有配置项都支持用环境变量覆盖，避免把账户等敏感信息硬编码进代码。

例如临时指定账户(两种方式)：
    python main.py check -a sim2          # 命令行切换(别名或原始 ID)
    $env:TBPY_ACCOUNT_ID = "66107987"     # 或用环境变量
    python main.py check
"""
import os

# ---------------------------------------------------------------------------
# 平台连接
# ---------------------------------------------------------------------------
# 连接的量化平台产品，可选值见 docs/reference/tbpy教程.md 3.1.3：
#   tbquant       -> TBQuant 量化平台
#   tbquant3      -> TBQuant3 量化平台（本项目默认，实测连接更稳定）
#   tbquant3.easy -> 简语言量化平台
# 可用环境变量 TBPY_PRODUCT 覆盖（如临时切回 tbquant）。
PRODUCT_KEY = os.getenv("TBPY_PRODUCT", "tbquant3")

# 交易资金账户（需已在 TBQuant 客户端登录）。两个仿真账户，每次只用一个，可灵活切换。
# 切换优先级：命令行 -a <别名或ID> > 环境变量 TBPY_ACCOUNT_ID > 下方 DEFAULT_ACCOUNT
ACCOUNTS = {
    "sim1": os.getenv("TBPY_ACCOUNT_SIM1", "tbf_fasot1"),  # 仿真账户1 (broker_id=482)
    "sim2": os.getenv("TBPY_ACCOUNT_SIM2", "66107987"),    # 仿真账户2 (broker_id=482)
}
DEFAULT_ACCOUNT = os.getenv("TBPY_ACCOUNT", "sim1")        # 默认使用的账户别名


def resolve_account(name_or_id: str) -> str:
    """把账户别名(sim1/sim2)解析为实际 account_id；传入原始 ID 时原样返回。"""
    return ACCOUNTS.get(name_or_id, name_or_id)


# 当前生效账户 ID：优先用环境变量 TBPY_ACCOUNT_ID(可为别名或原始ID)，否则用 DEFAULT_ACCOUNT
ACCOUNT_ID = resolve_account(os.getenv("TBPY_ACCOUNT_ID", DEFAULT_ACCOUNT))

# ---------------------------------------------------------------------------
# 合约
# ---------------------------------------------------------------------------
# 品种代码（underlying_symbol），用于查询主力/次主力合约
UNDERLYING_SYMBOL = os.getenv("TBPY_UNDERLYING", "rb.SHFE")
# 具体合约代码（symbol），用于行情订阅与查询；留空则运行时自动取主力合约
SYMBOL = os.getenv("TBPY_SYMBOL", "rb000.SHFE")
# 期权标的品种（用于 examples/option_data.py 期权数据示例）：默认豆粕 m.DCE
OPTION_UNDERLYING = os.getenv("TBPY_OPTION_UNDERLYING", "m.DCE")

# ---------------------------------------------------------------------------
# 双均线策略参数
# ---------------------------------------------------------------------------
FAST_LENGTH = int(os.getenv("TBPY_FAST_LENGTH", "5"))    # 短均线周期
SLOW_LENGTH = int(os.getenv("TBPY_SLOW_LENGTH", "10"))   # 长均线周期
BAR_FREQUENCY = os.getenv("TBPY_BAR_FREQUENCY", "10s")   # Bar 周期
ORDER_VOLUME = int(os.getenv("TBPY_ORDER_VOLUME", "1"))  # 每次下单手数

# ---------------------------------------------------------------------------
# 期权卖出策略 · 资金池（数据层 D17；权威源＝本 config，人工确认）
# ---------------------------------------------------------------------------
# 用途 §7.1 #7 保证金：合计保证金占用 ≤ 100% × 资金池（占用比例/单笔上限属阈值配置层，见 d17_capital_pool.py）。
# 口径已确认 ✔（4 万 / 100% / 单笔 1 万）——若改回留空("")则视为未配置，此时 #7 合计检查判 MISSING（数据不足不冒充、绝不假设资金池）。
# 例：$env:SELLOPT_CAPITAL_POOL = "200000"  或把下方默认值改成确认后的资金池总额（元）。
CAPITAL_POOL = os.getenv("SELLOPT_CAPITAL_POOL", "40000")

# ---------------------------------------------------------------------------
# 期权卖出策略 · 预算档（数据层 D18；权威源＝本 config，已确认：3000 元/笔）
# ---------------------------------------------------------------------------
# 用途 §7.1 #8 封顶预算：单笔最大亏损（价差宽度 W − 净权利金 R）× 乘数 ≤ 预算档（见 d18_budget.py）。
# 例：$env:SELLOPT_BUDGET_TIER = "3000"  或把下方默认值改成确认后的单笔最大亏损预算（元）。
BUDGET_TIER = os.getenv("SELLOPT_BUDGET_TIER", "3000")

# ---------------------------------------------------------------------------
# 期权卖出策略 · 全部判定阈值参数集（阈值配置层；单一事实源，改动须人工拍板）
# ---------------------------------------------------------------------------
# 每项支持 env 覆盖（前缀 SELLOPT_T_）；sell_option/thresholds.py 提供类型化访问 + 校验。
# 状态标注：✔＝已确认；〔待〕＝工作默认、数值待人工最终拍板。
SELLOPT_THRESHOLDS = {
    # §7.1 #1 盘口价差 / #2 持仓量OI
    "oi_min":              int(os.getenv("SELLOPT_T_OI_MIN", "100")),               # ✔ 单合约 OI 门槛
    "spread_ratio_max":    float(os.getenv("SELLOPT_T_SPREAD_RATIO", "0.10")),      # ✔ 价差≤权利金×10%
    # §7.1 #3 市况 regime
    "adx_ranging":         float(os.getenv("SELLOPT_T_ADX_RANGING", "25")),         # ✔ ADX<此值=震荡
    "slope_ranging":       float(os.getenv("SELLOPT_T_SLOPE_RANGING", "0.0015")),   # ✔ |斜率|<此值=震荡(0.15%/日)
    # §7.1 #4 IV 分位
    "iv_pct_single":       float(os.getenv("SELLOPT_T_IV_PCT_SINGLE", "60")),       # ✔ 单卖分位门槛
    "iv_pct_double":       float(os.getenv("SELLOPT_T_IV_PCT_DOUBLE", "70")),       # ✔ 双卖分位门槛
    # §7.1 #5 安全垫
    "safety_sigma":        float(os.getenv("SELLOPT_T_SAFETY_SIGMA", "1.0")),       # ✔ 现价→保护腿缓冲≥Nσ（#5 及格底线）
    # §7.1 #6 事件窗口
    "event_warn_days":     int(os.getenv("SELLOPT_T_EVENT_WARN", "5")),             # ✔ 事件临近提醒天数
    # §7.1 #7 保证金（资金池金额=D17、此处仅比例/单笔上限）
    "margin_pool_ratio":   float(os.getenv("SELLOPT_T_MARGIN_RATIO", "1.00")),      # ✔ 合计≤100%×资金池
    "margin_per_trade":    float(os.getenv("SELLOPT_T_MARGIN_PER_TRADE", "10000")), # ✔ 单笔保证金上限(元)
    # §7.1 #9 肥瘦
    "rw_min":              float(os.getenv("SELLOPT_T_RW_MIN", "0.20")),            # 〔待〕R/W≥20%
    # §7.1 #10 否决信号（3 形态任一→否决；原"连N同向"已上移 #3 市况）
    "veto_gap_pct":        float(os.getenv("SELLOPT_T_VETO_GAP", "0.02")),          # 〔待〕真实跳空≥2%否决
    "veto_range_edge_pct": float(os.getenv("SELLOPT_T_VETO_EDGE", "0.02")),         # 〔待〕区间贴边(占跨度)≤2%否决
    "veto_atr_ratio":      float(os.getenv("SELLOPT_T_VETO_ATR", "1.5")),           # 〔待〕波动扩张≥1.5×否决
    # §7.2 结构选择
    "dte_min":             int(os.getenv("SELLOPT_T_DTE_MIN", "20")),               # ✔ DTE区间下限(天)
    "dte_max":             int(os.getenv("SELLOPT_T_DTE_MAX", "180")),              # ✔ DTE区间上限(天)
    "candidate_months":    int(os.getenv("SELLOPT_T_CANDIDATE_N", "3")),            # ✔ 候选月数N
    "otm_pct":             float(os.getenv("SELLOPT_T_OTM_PCT", "0.10")),           # ✔ 虚值比例(10%·仅显示字段，选腿已改近端候选集)
    "prot_strikes":        int(os.getenv("SELLOPT_T_PROT_STRIKES", "4")),            # ✔ 保护腿距短腿期望档数(链浅更虚档不足时收窄到可用档数；取值限1..4，默认4)
    "short_candidates":    int(os.getenv("SELLOPT_T_SHORT_CANDIDATES", "3")),        # ✔ 短腿候选集档数(平值/虚一/虚二；由近及远优先级)
    # §7.2 选腿：保护腿护栏【放行到人工·不据脏数据淘汰】——保护腿权利金 ≥ 短腿×此比例即标记异常，
    # 写入 OpeningData.guard + 日志 + 漏斗 guard_anomaly；run_opening_checks 据此把 #8/#9 降级为待人工
    "prot_premium_max_ratio": float(os.getenv("SELLOPT_T_PROT_PREMIUM_MAX_RATIO", "0.90")),
    # §7.3 平仓规则
    "tp_floor":            float(os.getenv("SELLOPT_T_TP_FLOOR", "0.50")),          # 〔待校准〕50%保底止盈
    "tp_extend":           float(os.getenv("SELLOPT_T_TP_EXTEND", "0.75")),         # 〔待校准〕75%结构完好续持
    "sl_multiplier":       float(os.getenv("SELLOPT_T_SL_MULT", "2.0")),            # 〔待校准〕2×止损
    "breach_default":      float(os.getenv("SELLOPT_T_BREACH_DEF", "0.07")),        # ✔ 破位默认(C=7%)
    "breach_m":            float(os.getenv("SELLOPT_T_BREACH_M", "0.05")),          # ✔ M破位5%
    "breach_sr":           float(os.getenv("SELLOPT_T_BREACH_SR", "0.05")),         # ✔ SR破位5%
    "expiry_warn_dte":     int(os.getenv("SELLOPT_T_EXPIRY_WARN", "5")),            # ✔ 到期≤5提醒
    "expiry_must_dte":     int(os.getenv("SELLOPT_T_EXPIRY_MUST", "3")),            # ✔ 到期≤3必平
    "holiday_warn_days":   int(os.getenv("SELLOPT_T_HOL_WARN", "5")),               # ✔ 长假节前提醒窗
    "long_holiday_min":    int(os.getenv("SELLOPT_T_LONG_HOL", "5")),               # ✔ 连续≥N日=长假
    # §7.4 仓位风控（三值〔待校准〕；max_series＝同持 distinct 品种代码数、无板块归并）
    "max_positions":       int(os.getenv("SELLOPT_T_MAX_POS", "6")),                # 〔待校准〕最多持仓笔数
    "max_per_code":        int(os.getenv("SELLOPT_T_MAX_PER_CODE", "2")),           # 〔待校准〕每品种≤N笔
    "max_series":          int(os.getenv("SELLOPT_T_MAX_SERIES", "3")),             # 〔待校准〕同持≤N品种(distinct)
}

# ---------------------------------------------------------------------------
# 期权卖出策略 · 早筛短回路(FastFail)开关
# ---------------------------------------------------------------------------
# 用途：装配期先用便宜数据(#4 IV/#5 安全垫/#7 保证金/#10 否决)跑阻断检查，命中即早退、
#   不取昂贵的期权权利金/OI(get_history_n，62s/次，全池≈48 分钟)。关闭(False，默认)则所有品种
#   都取全量期权数据，报告每个字段都填满，便于横向比较"淘汰到什么程度"(#4 IV 分位低多少、
#   #9 肥瘦 R/W 差多少…)。代价：冷缓存下首跑全池多花若干分钟(被早筛挡掉的品种也各取一次)；
#   重跑命中 premium.db 缓存秒回——取数已不再是瓶颈。
# 默认 False(关)：用户要横向对比数据，且取数大量命中缓存。
# 如需恢复速度，设 True(或 $env:SELLOPT_EARLY_EXCLUDE = "1")。
EARLY_EXCLUDE_ENABLED = os.getenv("SELLOPT_EARLY_EXCLUDE", "0") == "1"

# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
TIMEOUT = os.getenv("TBPY_TIMEOUT", "30s")  # 数据请求超时时间
