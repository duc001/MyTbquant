"""期权数据查询示例（只读，安全可反复运行）。

覆盖需求表 A/B/C 三类数据，所有字段名均用真实 tbpy 3.12.3 API + 实盘合约实测核对。

实测关键结论（务必牢记，与直觉不同）：
  A. get_instrument 返回的 Instrument 只有 10 个字段：symbol/exchange/sec_id/
     sec_name/price_tick/contract_unit/big_point_value/listed_date/
     delisted_date/deal_times。**没有** expire_date/strike_price/underlying/
     option_type 等期权专用字段。因此：
        · 到期日(最后交易日) = delisted_date
        · 行权价 / 看涨看跌(C/P) / 标的月份 → 只能从 symbol 字符串解析
        · 交易所 = exchange 字段(同时也是 symbol 的 '.' 后缀)
     期权 symbol 一定带交易所后缀，且格式随交易所不同：
        DCE : m2612-P-2950.DCE    (带短横线)
        SHFE: cu2706C106000.SHFE  (无短横线)
  B. get_current_tick 返回 Tick：最新价=last，当日高低=high/low(Tick 直接带，
     无需日K回退)，盘口=bidasks[i] 的 bid_p/bid_v/ask_p/ask_v(BidAsk 对象，
     字段实测为 ask_p/ask_v/bid_p/bid_v)。
     **Tick 没有 oi/持仓量字段**。持仓量字段名=open_int(Bar 的属性)；实测
     get_history_n 取 open_int 返回空，OI 只能在实时 subscribe_bar→on_bar 的
     bar.open_int 中拿到(交易时段)。
  ★ get_history_n 必须传 timeout(如 '30s')才能可靠返回；不传或连续快速调用
     会拿到空结果。期权历史K也需实时订阅才有，get_history_n 多取不到。
  C. 标的：get_main_instrument/get_second_main_instrument 返回 Instrument，
     用 .symbol 取合约代码；日K用 get_history_n(freq='1d')，Bar 字段为
     open/high/low/close/volume/open_int。

运行：
    python main.py option
    python examples/option_data.py
"""
import datetime
import os
import re
import sys

# 允许直接运行本文件（python examples/option_data.py）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tbpy  # noqa: E402

import config  # noqa: E402
import runtime  # noqa: E402

# 期货期权代码解析：品种(1-2字母) + 月份(3-4位) + C/P + 行权价，短横线可选。
# 适配 DCE(m2612-P-2950) 与 SHFE(cu2706C106000) 两种写法；ETF 期权(纯数字)不适用。
_OPTION_PAT = re.compile(
    r"^(?P<product>[A-Za-z]{1,2})(?P<month>\d{3,4})-?(?P<cp>[CP])-?(?P<strike>\d+)$"
)


def parse_option_symbol(symbol: str):
    """解析期权合约代码。

    成功返回 dict（含 product/month/option_type/strike/exchange/
    underlying_symbol/underlying_product）；非期权或无法解析返回 None。

    例：'m2612-P-2950.DCE' ->
        {product:'m', month:'2612', option_type:'P', strike:2950.0,
         exchange:'DCE', underlying_symbol:'m2612.DCE', underlying_product:'m.DCE'}
    """
    sec_id, _, exchange = symbol.partition(".")
    m = _OPTION_PAT.match(sec_id)
    if not m:
        return None
    product = m.group("product")
    month = m.group("month")
    return {
        "symbol": symbol,
        "product": product,
        "month": month,
        "option_type": m.group("cp"),                          # 'C' 看涨 / 'P' 看跌
        "strike": float(m.group("strike")),                    # 行权价
        "exchange": exchange,
        "underlying_symbol": f"{product}{month}.{exchange}",   # 对应标的期货合约
        "underlying_product": f"{product}.{exchange}",         # 标的品种
    }


def get_option_chain(underlying_product: str):
    """列出某品种(如 'm.DCE')当前挂牌的全部期权合约解析信息(list[dict])。"""
    prod, _, exch = underlying_product.partition(".")
    chain = []
    for sym in tbpy.get_symbols() or []:
        info = parse_option_symbol(sym)
        if info and info["product"] == prod and info["exchange"] == exch:
            chain.append(info)
    return chain


def _as_date(value):
    """把 listed_date/delisted_date 归一化为 datetime.date。

    实测 tbpy 会因连接状态不同，把这些字段返回为 datetime 或 str，故需兼容处理。
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        s = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        try:
            return datetime.datetime.fromisoformat(s).date()
        except ValueError:
            return None
    return None


def _fmt_inst(inst) -> str:
    if inst is None:
        return "<None>"
    return (
        f"{inst.symbol} | sec_id={inst.sec_id} sec_name={inst.sec_name} "
        f"exchange={inst.exchange} price_tick={inst.price_tick} "
        f"contract_unit={inst.contract_unit} big_point_value={inst.big_point_value} "
        f"listed={_as_date(inst.listed_date)} delisted(到期)={_as_date(inst.delisted_date)}"
    )


def _print_history(history) -> None:
    if not history:
        print("    <空>（该合约/周期可能无数据，或受历史范围权限限制）")
        return
    for field, arr in history.items():
        length = len(arr) if hasattr(arr, "__len__") else "NA"
        print(f"    {field}: len={length} -> {arr}")


def run() -> int:
    print("=" * 68)
    print(f"[1] 连接平台: key={config.PRODUCT_KEY}")
    if not runtime.connect(config.PRODUCT_KEY):
        print("    请先启动并登录 TBQuant 客户端后重试。")
        return 1

    underlying_product = config.OPTION_UNDERLYING           # 如 'm.DCE'
    prod = underlying_product.partition(".")[0]

    # ---------- C. 标的主力 / 次主力 ----------
    print(f"\n[2] (C) 标的合约  underlying_product={underlying_product}")
    main_inst = tbpy.get_main_instrument(underlying_symbol=underlying_product)
    second_inst = tbpy.get_second_main_instrument(underlying_symbol=underlying_product)
    main_sym = main_inst.symbol if main_inst is not None else None
    print(f"    主力   get_main_instrument        -> {_fmt_inst(main_inst)}")
    print(f"    次主力 get_second_main_instrument -> {_fmt_inst(second_inst)}")

    # ---------- C. 标的实时 spot + 日K(算 HV) ----------
    if main_sym:
        print(f"\n[3] (C) 标的实时快照(spot)  get_current_tick: {main_sym}")
        t = tbpy.get_current_tick(symbol=main_sym)
        if t is None:
            print("    <None>（非交易时段/假日属正常；开盘后 last 即标的最新价 spot）")
        else:
            print(f"    spot(last)={t.last} high={t.high} low={t.low} volume={t.volume}")

        print(f"\n[4] (C) 标的日K(算 HV)  get_history_n('1d'): {main_sym}")
        h = tbpy.get_history_n(
            symbol=main_sym, frequency="1d", count=5,
            fields=["time", "open", "high", "low", "close", "volume", "open_int"],
            timeout=config.TIMEOUT,
        )
        _print_history(h)

    # ---------- A. 期权链发现 ----------
    print(f"\n[5] (A) 发现期权链  get_symbols + 解析: {underlying_product}")
    chain = get_option_chain(underlying_product)
    print(f"    共 {len(chain)} 个期权合约")
    by_month = {}
    for c in chain:
        by_month.setdefault(c["month"], []).append(c)
    for month in sorted(by_month):
        items = by_month[month]
        calls = [x for x in items if x["option_type"] == "C"]
        puts = [x for x in items if x["option_type"] == "P"]
        strikes = sorted({x["strike"] for x in items})
        rng = f"{strikes[0]:g}~{strikes[-1]:g}" if strikes else "NA"
        print(f"    月份 {month}: calls={len(calls)} puts={len(puts)} 行权价区间 {rng}")

    # ---------- A/B. 取一个样例期权，展示元数据/行情/OI ----------
    sample = None
    if chain:
        main_month = main_sym.partition(".")[0][len(prod):] if main_sym else None
        cand = [c for c in chain if c["month"] == main_month and c["option_type"] == "C"]
        sample = (cand or chain)[0]

    if sample is None:
        print("\n[6] 未发现该品种的期权合约，跳过元数据/行情演示。")
    else:
        print(f"\n[6] (A) 期权元数据  get_instrument: {sample['symbol']}")
        inst = tbpy.get_instrument(symbol=sample["symbol"])
        print(f"    Instrument: {_fmt_inst(inst)}")
        cp_cn = "看涨" if sample["option_type"] == "C" else "看跌"
        print(f"    解析 symbol -> 标的={sample['underlying_symbol']} 类型={cp_cn}"
              f"({sample['option_type']}) 行权价={sample['strike']:g}"
              f" 品种={sample['underlying_product']} 交易所={sample['exchange']}")
        if inst is not None:
            expiry = _as_date(inst.delisted_date)
            if expiry is not None:
                days = (expiry - datetime.date.today()).days
                print(f"    到期日=delisted_date={expiry}  剩余天数={days}")
            else:
                print(f"    到期日=delisted_date={inst.delisted_date!r}（无法解析为日期）")

        print(f"\n[7] (B) 期权实时行情  get_current_tick: {sample['symbol']}")
        ot = tbpy.get_current_tick(symbol=sample["symbol"])
        if ot is None:
            print("    <None>（非交易时段/假日属正常）")
            print("    开盘后字段：最新价=last 当日高/低=high/low "
                  "买一=bidasks[0].bid_p/bid_v 卖一=bidasks[0].ask_p/ask_v")
        else:
            print(f"    last={ot.last} high={ot.high} low={ot.low} volume={ot.volume}")
            if ot.bidasks:
                ba = ot.bidasks[0]
                print(f"    买一 bid_p={ba.bid_p} bid_v={ba.bid_v} | "
                      f"卖一 ask_p={ba.ask_p} ask_v={ba.ask_v}")

        print(f"\n[8] (B) 期权持仓量 OI 字段名=open_int（Tick 无此字段）: {sample['symbol']}")
        oh = tbpy.get_history_n(
            symbol=sample["symbol"], frequency="1d", count=5,
            fields=["time", "close", "volume", "open_int"], timeout=config.TIMEOUT,
        )
        _print_history(oh)
        print("    注：实测 get_history_n 的 open_int(及期权日K)多为空；OI 的可靠来源是"
              "实时 subscribe_bar→on_bar 的 bar.open_int(交易时段)。")

    print("\n" + "=" * 68)
    print("期权数据查询完成，释放连接。")
    runtime.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
