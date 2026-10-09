"""D06 外部备源适配器：新浪（备源①）+ openvlab（备源②）+ get_instrument 单合约复核。

需求源 §6.3 F：外部源合约 symbol 须按 D02 边界表模板回拼、并经 tbpy get_instrument
单合约复核有效后方可选腿；串价铁律只约束价格类，链清单是元数据、多源降级不禁。

**实测结论（2026-09-27 探针，务必据实理解两备源的能力边界）**：
- 新浪 `OptionService.getOptionData`（product：DCE/SHFE/GFEX 用 `{code}_o`、CZCE 用 `{code}`）
  **只返回主力月单月 T 型盘口**：`data.up`（看涨）/`data.down`（看跌）每行末列是简码
  `mC2300`/`mP2300`——**无月份、无到期日、无完整 symbol**。故新浪无法独立重建 D06 要的
  "行权价×到期日×C/P 完整多月链"；它只能提供"主力月行权价阶梯"，月份/到期日/回拼/复核
  仍须借 tbpy 侧（month_resolver + get_instrument）。tbpy 全down 时新浪数据无法复核→不可用。
- openvlab（www.openvlab.cn）的数据接口是**公开 HTTP JSON**，直接 GET 即可，无需浏览器/登录/token
  （用户实证 2026-09-27：公共资源已够用，Chromium/Playwright 不需要）。实测 `/api/product-exps`
  覆盖我方 **47/47** 品种的全部到期月份 + 精确到期日 expDate（与 tbpy 期权 delisted_date 完全吻合），
  `/api/ctamap-all` 覆盖 47/47 的平值隐波/IV 百分位/skew；**但两接口均无行权价阶梯**，故 openvlab
  无法独立产出完整期权链（fetch_chain 返回空）。其真实价值：① D06 到期日的 tbpy-无关备份/交叉验证源；
  ② 作新浪备源的 month_resolver（新浪出行权价阶梯 + openvlab 出该月到期日 → 纯 HTTP 单月真链）。

两备源均 `authoritative=False`：即便拿到也只是降级补充，编排器会明确标注 complete=False。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Callable

try:  # 包方式（python -m / import）：相对导入
    from .models import ChainContract
except ImportError:  # 脚本直跑（PyCharm 右键 Run）：补项目根后绝对导入
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sell_option.fetchers.models import ChainContract

# 新浪 T 型盘口末列简码：{品种}{C/P}{行权价}（无月份），如 mC2300 / apP5600
_SINA_CODE_RE = re.compile(r"^([A-Za-z]{1,2})([CP])(\d+)$")
_SINA_URL = (
    "https://stock.finance.sina.com.cn/futures/api/openapi.php/"
    "OptionService.getOptionData?type=futures&product={product}&exchange={exch}&pinzhong={pz}"
)
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
       "Referer": "https://stock.finance.sina.com.cn/"}

# openvlab 公开数据接口（纯 HTTP JSON，无需登录/浏览器；Referer 用其行情页）
_OLV_BASE = "https://www.openvlab.cn"
_OLV_PRODUCT_EXPS = _OLV_BASE + "/api/product-exps"   # 品种→到期月份+expDate+front_exp+涨跌停+tick
_OLV_CTAMAP = _OLV_BASE + "/api/ctamap-all"           # 品种→平值隐波/IV百分位/skew/rv22/price
_OLV_HEADERS = {"User-Agent": _UA["User-Agent"], "Referer": _OLV_BASE + "/market",
                "Accept": "application/json, text/plain, */*"}


@dataclass(frozen=True)
class SinaLadder:
    """新浪单月 T 型盘口解析结果（无月份/到期日，仅 C/P+行权价阶梯）。"""

    code: str
    exchange: str
    rungs: tuple[tuple[str, float], ...]   # (cp, strike) 去重升序

    def strikes(self, cp: str) -> list[float]:
        return sorted(s for c, s in self.rungs if c == cp)


def _sina_product(code: str, exchange: str) -> tuple[str, str]:
    """按交易所拼新浪 product/exchange 参数：CZCE 用裸码，其余加 _o 后缀（探针实测）。"""
    exch = exchange.lower()
    product = code.lower() if exchange == "CZCE" else f"{code.lower()}_o"
    return product, exch


class SinaChainSource:
    """备源①新浪：抓主力月 T 型盘口。**结构性局限见模块 docstring**——只能出行权价阶梯，
    月份/到期日须由 month_resolver 注入（一般来自 tbpy 侧），否则无法组装合法 ChainContract。"""

    name = "sina"
    authoritative = False    # 单月、无到期日，非完整链

    def __init__(self, timeout: int = 25,
                 month_resolver: Callable[[str, str], tuple[str, "object"] | None] | None = None):
        self.timeout = timeout
        # month_resolver(code, exchange) -> (month4, expiry_date) | None；None 则 fetch_chain 返回空
        self.month_resolver = month_resolver

    def fetch_ladder(self, code: str, exchange: str) -> SinaLadder | None:
        """抓并解析新浪单月盘口；网络/结构异常返回 None（不抛，交编排器降级）。"""
        product, exch = _sina_product(code, exchange)
        url = _SINA_URL.format(product=product, exch=exch, pz=code.lower())
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                obj = json.loads(r.read().decode("utf-8", "replace"))
            data = obj["result"]["data"]
        except Exception:  # noqa: BLE001 - 网络/JSON/键缺失一律视为该源拿不到
            return None
        rungs: dict[tuple[str, float], None] = {}
        for side in ("up", "down"):       # up=看涨 down=看跌
            for row in data.get(side, []) or []:
                if not isinstance(row, list) or not row:
                    continue
                m = _SINA_CODE_RE.match(str(row[-1]))   # 末列简码提 C/P+行权价
                if not m:
                    continue                # 表头行（mC / mP0）跳过
                strike = float(m.group(3))
                if strike <= 0:
                    continue
                rungs[(m.group(2).upper(), strike)] = None
        if not rungs:
            return None
        return SinaLadder(code, exchange, tuple(rungs))

    def fetch_chain(self, code: str, exchange: str) -> list[ChainContract]:
        """组装 ChainContract：新浪出行权价阶梯 + resolver 出月份/到期日 + D02 回拼 symbol。
        无 resolver 或 resolver 拿不到月份 → 返回空（新浪单靠自己无法定位月份/到期日）。"""
        if self.month_resolver is None:
            return []
        ladder = self.fetch_ladder(code, exchange)
        if ladder is None:
            return []
        resolved = self.month_resolver(code, exchange)
        if resolved is None:
            return []
        month4, expiry = resolved
        try:  # D02 模板回拼（延迟导入避免与主源模块成环）
            from .d02_symbol_template import fetch_d02
            templates = fetch_d02()
        except ImportError:
            from sell_option.fetchers.d02_symbol_template import fetch_d02
            templates = fetch_d02()
        out: list[ChainContract] = []
        for cp, strike in ladder.rungs:
            try:
                sym = templates.option_symbol(code, exchange, month4, cp, strike)
            except (ValueError, KeyError, AssertionError):
                continue
            out.append(ChainContract(code=code, exchange=exchange, underlying_month=month4,
                                     expiry=expiry, cp=cp, strike=strike, symbol=sym))
        return out


@dataclass(frozen=True)
class OpenvlabIV:
    """openvlab ctamap 单品种主力月 IV 快照（供 #4 IV 分位 / #5 安全垫；不属 D06 链清单）。

    字段实测（2026-09-27 /api/ctamap-all）：atmv_current=平值隐波现值%、
    atmv_percentile=IV 百分位(openvlab 自算约一年窗口)、atmv_1dchg=IV 日变(#4 方向初判据)；三者同属 D13。
    """

    code: str
    exchange: str
    exp: str                        # 主力到期月 YYYYMM
    atmv_current: float | None      # 平值隐波 %（D13 现值）
    atmv_percentile: float | None   # 平值隐波百分位（D13 百分位）
    atmv_1dchg: float | None        # 平值隐波日变（D13 日变 · #4 方向初判据：<0 视为回落）
    skew_current: float | None = None
    skew_percentile: float | None = None
    rv22: float | None = None              # 22 日实现波动率
    price: float | None = None
    expiry_date: dt.date | None = None     # 主力月到期日（与 tbpy/product-exps 交叉验证用）


class OpenvlabSource:
    """备源②openvlab：**纯 HTTP 直连公开接口**（无需浏览器/登录/token）。

    实测（2026-09-27）：`/api/product-exps` 覆盖我方 47/47 品种的全部到期月份 + 精确到期日
    expDate（与 tbpy 期权 delisted_date 完全吻合）；`/api/ctamap-all` 覆盖 47/47 的 IV。
    **但两接口都不含行权价阶梯**，故 openvlab 无法独立建链（fetch_chain 返回空）；其价值在于：
    ① 为 D06 提供 tbpy-无关的到期日备份/交叉验证（expiry_map）；② 作新浪备源的 month_resolver
    （resolve_front：新浪出行权价阶梯 + openvlab 出主力月到期日 → 纯 HTTP 单月真链）。

    网络/结构异常不抛，记 last_error 并返回空（交编排器降级）；绝不伪造合约或到期日。
    """

    name = "openvlab"
    authoritative = False

    def __init__(self, timeout: int = 25):
        self.timeout = timeout
        self.last_error: str | None = None
        self._exps: dict[str, dict] | None = None     # code_upper -> product-exps row
        self._ctamap: dict[str, dict] | None = None   # code_upper -> ctamap row

    def _get_result(self, url: str):
        req = urllib.request.Request(url, headers=_OLV_HEADERS)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
        if obj.get("code") != 0:
            raise ValueError(f"openvlab 返回码 {obj.get('code')} msg={obj.get('message')}")
        return obj["result"]

    def product_exps(self) -> dict[str, dict]:
        """懒加载 /api/product-exps，按 product_und 大写建索引；失败返回空 dict。"""
        if self._exps is None:
            try:
                rows = self._get_result(_OLV_PRODUCT_EXPS)
                self._exps = {str(r.get("product_und", "")).upper(): r for r in rows}
            except Exception as e:  # noqa: BLE001
                self.last_error = f"product-exps 抓取失败：{type(e).__name__}: {e}"
                self._exps = {}
        return self._exps

    def expiry_map(self, code: str) -> dict[str, dt.date]:
        """品种 → {4 位月份 '2611': 到期日}（openvlab exp YYYYMM 取后 4 位；expDate YYYYMMDD）。"""
        row = self.product_exps().get(code.upper())
        if not row:
            return {}
        out: dict[str, dt.date] = {}
        for e in row.get("exps", []) or []:
            exp, d = str(e.get("exp", "")), str(e.get("expDate", ""))
            if len(exp) == 6 and len(d) == 8:
                try:
                    out[exp[2:]] = dt.date(int(d[:4]), int(d[4:6]), int(d[6:]))
                except ValueError:
                    continue
        return out

    def resolve_front(self, code: str, exchange: str) -> tuple[str, dt.date] | None:
        """作 SinaChainSource.month_resolver：返回主力月 (month4, 到期日)；拿不到返回 None。

        front_exp（YYYYMM）优先；缺失则取到期日最近的月份。"""
        row = self.product_exps().get(code.upper())
        emap = self.expiry_map(code)
        if not row or not emap:
            return None
        front = str(row.get("front_exp", ""))
        if len(front) == 6 and front[2:] in emap:
            return front[2:], emap[front[2:]]
        nearest = min(emap, key=lambda k: emap[k])   # 到期日最近月
        return nearest, emap[nearest]

    def ctamap(self) -> dict[str, dict]:
        """懒加载 /api/ctamap-all，按 prodUnd 大写建索引；失败返回空 dict。"""
        if self._ctamap is None:
            try:
                rows = self._get_result(_OLV_CTAMAP)
                self._ctamap = {str(r.get("prodUnd", "")).upper(): r for r in rows}
            except Exception as e:  # noqa: BLE001
                self.last_error = f"ctamap 抓取失败：{type(e).__name__}: {e}"
                self._ctamap = {}
        return self._ctamap

    def iv(self, code: str) -> OpenvlabIV | None:
        """单品种 IV 快照（供 #4/#5）；拿不到返回 None。"""
        r = self.ctamap().get(code.upper())
        if not r:
            return None

        def _f(k: str) -> float | None:
            try:
                return float(r.get(k))
            except (TypeError, ValueError):
                return None

        exp_d: dt.date | None = None
        try:
            exp_d = dt.date.fromisoformat(str(r.get("expiry_date", "")))
        except ValueError:
            exp_d = None
        return OpenvlabIV(code=code, exchange=str(r.get("exchange", "")), exp=str(r.get("exp", "")),
                          atmv_current=_f("atmv_current"), atmv_percentile=_f("atmv_percentile"),
                          atmv_1dchg=_f("atmv_1dchg"),
                          skew_current=_f("skew_current"), skew_percentile=_f("skew_percentile"),
                          rv22=_f("rv22"), price=_f("price"), expiry_date=exp_d)

    def fetch_chain(self, code: str, exchange: str) -> list[ChainContract]:
        """openvlab 公开接口无行权价阶梯 → 无法独立建链，如实返回空（价值在 expiry_map/resolve_front/iv）。"""
        return []


def revalidate_via_tbpy(symbols: list[str], is_valid: Callable[[str], bool]) -> list[str]:
    """§6.3 F：外部源合约 symbol 须经 tbpy get_instrument 单合约复核有效后方可用。

    `is_valid(symbol)` 由调用方注入（内部 get_instrument + sec_name 非空判定，单合约查询
    不触发 get_symbols 堆问题）；此处只做过滤，返回复核通过的 symbol 子集。
    """
    return [s for s in symbols if is_valid(s)]
