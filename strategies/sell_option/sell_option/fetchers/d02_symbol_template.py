"""D02 · symbol 拼接格式模板（期货月份合约 / 期权）。

需求源 §6.1 D02：静态参数，权威源 = 边界表「期货symbol样例 / 期权样例C / 期权样例P」列
（格式模板，非实盘月份），供给一切 symbol 拼接的取数入口（D07 候选月、D06 链过滤、选腿拼合约）。

实现方式：**不硬编码规则**——逐行解析 47 品种样例，按交易所推导编码格式
（代码大小写 / 月份位数 / 期权是否有横线），并断言同一交易所内全表一致；
改边界表样例＝改模板来源，加载器自动跟随。

口径说明：
- CZCE 月份 3 位只保留"年份个位+月"（如 2701→701），十位年代缺失由交易所/解析侧
  按"最近未来月份"惯例消歧，本单元拼接侧只负责格式正确；
- 主力指数合约（888/9888 老/新品种分裂、无字面规律）属 D03 查表范围，不在本单元。

运行自检：``python -m sell_option.fetchers.d02_symbol_template``（含样例回代再生成测试）
"""
from __future__ import annotations

import re
from dataclasses import dataclass

try:  # 包方式（python -m / import）：相对导入
    from .boundary_table import load_boundary_table
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.boundary_table import load_boundary_table

# 样例解析：期货 = 代码+月份.交易所；期权 = 代码+月份[-]C/P[-]行权价.交易所（边界表 §二.4.2 正则加后缀）
_FUT_RE = re.compile(r"^([A-Za-z]{1,2})(\d{3,4})\.([A-Z]{3,6})$")
_OPT_RE = re.compile(r"^([A-Za-z]{1,2})(\d{3,4})-?([CP])-?(\d+)\.([A-Z]{3,6})$")


@dataclass(frozen=True)
class SymbolFormat:
    """一个交易所的 symbol 编码格式（由该所全部品种样例推导，所内必须统一）。"""

    exchange: str
    code_upper: bool     # 品种代码大小写：CZCE 大写，其余小写
    month_digits: int    # 月份位数：CZCE 3 位，其余 4 位
    option_dash: bool    # 期权月份与 C/P、行权价之间是否有横线：DCE/GFEX 有

    def norm_code(self, code: str) -> str:
        return code.upper() if self.code_upper else code.lower()

    def norm_month(self, month: str) -> str:
        """接受 4 位（"2701"）或本所位数（"701"）；4 位→3 位丢年代十位，3 位升 4 位无据不猜。"""
        if not month.isdigit() or len(month) not in (3, 4):
            raise ValueError(f"月份须为 3 或 4 位数字，收到 {month!r}")
        if len(month) == 4:
            return month[1:] if self.month_digits == 3 else month
        if self.month_digits != 3:
            raise ValueError(f"{self.exchange} 需 4 位月份，收到 3 位 {month!r}（年代无法代猜）")
        return month


class SymbolTemplates:
    """D02 产物：按交易所的 symbol 拼接器。"""

    def __init__(self, by_exchange: dict[str, SymbolFormat]):
        self.by_exchange = by_exchange

    def format_of(self, exchange: str) -> SymbolFormat:
        fmt = self.by_exchange.get(exchange)
        if fmt is None:
            raise KeyError(f"未知交易所 {exchange!r}（模板仅含：{sorted(self.by_exchange)}）")
        return fmt

    def futures_symbol(self, code: str, exchange: str, month: str) -> str:
        """拼期货月份合约 symbol，如 ("m","DCE","2701")→"m2701.DCE"、("AP","CZCE","2701")→"AP701.CZCE"。"""
        fmt = self.format_of(exchange)
        return f"{fmt.norm_code(code)}{fmt.norm_month(month)}.{exchange}"

    def option_symbol(self, code: str, exchange: str, month: str, cp: str, strike: int | float) -> str:
        """拼期权合约 symbol，如 ("a","DCE","2611","C",5100)→"a2611-C-5100.DCE"、
        ("rb","SHFE","2701","C",3500)→"rb2701C3500.SHFE"。cp 接受 c/C/p/P。"""
        fmt = self.format_of(exchange)
        cp = cp.upper()
        if cp not in ("C", "P"):
            raise ValueError(f"期权方向须为 C 或 P，收到 {cp!r}")
        strike_s = str(int(strike)) if float(strike) == int(strike) else str(strike)
        if not strike_s.isdigit() or int(strike_s) <= 0:
            raise ValueError(f"行权价须为正数，收到 {strike!r}")
        mid = f"-{cp}-" if fmt.option_dash else cp
        sym = f"{fmt.norm_code(code)}{fmt.norm_month(month)}{mid}{strike_s}.{exchange}"
        if not _OPT_RE.match(sym):  # 自证产物可被边界表解析正则回收
            raise AssertionError(f"拼出的期权 symbol 不合解析正则：{sym!r}")
        return sym


def _require(match: re.Match | None, raw: str, ctx: str) -> re.Match:
    if match is None:
        raise ValueError(f"边界表样例无法解析（{ctx}）：{raw!r}")
    return match


def fetch_d02() -> SymbolTemplates:
    """从边界表样例列推导各交易所编码模板；同所格式冲突/样例畸形 → 抛 ValueError。"""
    rows = load_boundary_table()
    partial: dict[str, SymbolFormat] = {}
    for n, row in enumerate(rows, start=1):
        code, exchange = row["标的编码"], row["交易所编码"]
        ctx = f"第 {n} 行 {code}"

        m = _require(_FUT_RE.match(row["期货symbol样例"]), row["期货symbol样例"], ctx + " 期货样例")
        s_code, s_month, s_exch = m.group(1), m.group(2), m.group(3)
        if s_exch != exchange or s_code != code:
            raise ValueError(f"{ctx}：期货样例 {row['期货symbol样例']!r} 与标的编码/交易所列不符")
        fut_fmt = SymbolFormat(exchange, s_code.isupper(), len(s_month), option_dash=False)

        opt_fmts = []
        for col in ("期权样例C", "期权样例P"):
            mo = _require(_OPT_RE.match(row[col]), row[col], f"{ctx} {col}")
            if mo.group(1) != code or mo.group(5) != exchange:
                raise ValueError(f"{ctx}：{col} {row[col]!r} 与标的编码/交易所列不符")
            opt_fmts.append(SymbolFormat(exchange, mo.group(1).isupper(),
                                         len(mo.group(2)), "-" in row[col]))
        if opt_fmts[0] != opt_fmts[1]:
            raise ValueError(f"{ctx}：C 与 P 样例推导格式不一致 {opt_fmts}")

        # 汇总本行推导格式：大小写/月份位数取期货样例，横线取期权样例
        current = SymbolFormat(exchange, fut_fmt.code_upper, fut_fmt.month_digits, opt_fmts[0].option_dash)
        prev = partial.get(exchange)
        if prev is not None and prev != current:
            raise ValueError(f"{ctx}：{exchange} 内格式不统一，已推导 {prev}，遇 {current}")
        partial[exchange] = current
    return SymbolTemplates(partial)


if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows 控制台 GBK 兜底
        sys.stdout.reconfigure(encoding="utf-8")

    templates = fetch_d02()
    print(f"D02 编码模板：{len(templates.by_exchange)} 个交易所")
    for exchange, fmt in sorted(templates.by_exchange.items()):
        case = "大写" if fmt.code_upper else "小写"
        dash = "有横线" if fmt.option_dash else "无横线"
        print(f"  {exchange:<5} 代码{case} · 月份{fmt.month_digits}位 · 期权{dash}")

    # 回代测试：用模板 + 样例解析出的字段重新生成每个样例，与边界表逐字比对
    rows = load_boundary_table()
    regen, bad = 0, []
    for row in rows:
        exch = row["交易所编码"]
        mf = _require(_FUT_RE.match(row["期货symbol样例"]), row["期货symbol样例"], "回代期货")
        if templates.futures_symbol(mf.group(1), exch, mf.group(2)) != row["期货symbol样例"]:
            bad.append(row["期货symbol样例"])
        regen += 1
        for col in ("期权样例C", "期权样例P"):
            mo = _require(_OPT_RE.match(row[col]), row[col], "回代期权")
            if templates.option_symbol(mo.group(1), exch, mo.group(2), mo.group(3), int(mo.group(4))) != row[col]:
                bad.append(row[col])
            regen += 1
    print(f"\n回代再生成：{regen} 个 symbol，全部一致" if not bad else f"\n回代不一致 {len(bad)} 个：{bad}")
    if bad:
        raise SystemExit(1)
