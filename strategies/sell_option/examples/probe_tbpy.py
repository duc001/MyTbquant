"""一次性探针：真实调用 tbpy 各方法，把请求与返回原样 dump 出来，供重写参考文档。
带看门狗：75s 强制 os._exit，避免客户端无响应时卡死。
绝不动 get_symbols（会物化 ~11 万对象损坏堆）。
"""
import sys
from pathlib import Path
_HERE = Path(__file__).resolve().parent
if (_HERE / 'sell_option' / '__init__.py').exists():
    _UNIT = _HERE
else:
    _UNIT = _HERE.parent
_STRATEGIES = _UNIT.parent
_ROOT = _STRATEGIES.parent
for _p in (str(_STRATEGIES), str(_UNIT), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import json
import os
import sys
import time
import threading
from pathlib import Path

# 把项目根加入 sys.path（脚本在 sell_option/ 下，config.py/runtime.py 在根）

# 看门狗：超时强制退出
def _watchdog(sec: int):
    def _t():
        import time
        time.sleep(sec)
        print("WATCHDOG_TIMEOUT")
        os._exit(1)
    threading.Thread(target=_t, daemon=True).start()

def _safe(obj, n=3):
    """把 ndarray / 对象 简化成可序列化的可读形式。"""
    import numpy as np
    if isinstance(obj, np.ndarray):
        return {"__ndarray__": True, "dtype": str(obj.dtype), "len": len(obj),
                "head": [str(x) for x in obj[:n]]}
    if isinstance(obj, (list, tuple)):
        return [_safe(x, n) for x in obj[:n]]
    if isinstance(obj, dict):
        return {str(k): _safe(v, n) for k, v in list(obj.items())[:n]}
    return str(obj)

def main():
    _watchdog(220)
    try:
        import config
        from runtime import connect, disconnect
        import tbpy
    except Exception as e:
        print("IMPORT_FAIL", repr(e)); sys.exit(2)

    out = {}
    if not connect(config.PRODUCT_KEY):
        print("NO_CONNECT")
        # 仍尝试直接 init 看错误
        try:
            ok = tbpy.init(config.PRODUCT_KEY)
            out["init_raw"] = ok
            err = tbpy.get_last_err()
            out["init_err"] = err
        except Exception as e:
            out["init_exc"] = repr(e)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(3)

    try:
        import json as _json
        idx_path = Path(__file__).resolve().parent / "data" / "option_index.json"
        sr_option = None
        if idx_path.exists():
            idx = _json.loads(idx_path.read_text(encoding="utf-8"))
            sr = idx.get("varieties", {}).get("SR")
            if sr and sr.get("contracts"):
                sr_option = sr["contracts"][0]["symbol"]  # 真实 SR 期权代码
        out["sr_option_symbol_used"] = sr_option

        main_sym = "SR701.CZCE"  # 由本次 get_main_instrument 取得

        # === 调用 A：get_history_n 主连期货·多字段多根 ===
        r2 = tbpy.get_history_n(main_sym, "1d", 3,
                                fields=["open", "high", "low", "close", "time"],
                                timeout="30s")
        out["get_history_n_future_ohlc"] = _safe(r2, 5)

        # 错开 60s 限流（成功占用额度，等一个窗口再发）
        time.sleep(62)

        # === 调用 B：get_history_n 单合约·期权(含 OI) ===
        if sr_option:
            r1 = tbpy.get_history_n(sr_option, "1d", 1,
                                    fields=["close", "position", "time"], timeout="30s")
            out["get_history_n_single_option"] = _safe(r1)
            # get_instrument 不受 60s 限流（不同方法），紧接即可
            inst2 = tbpy.get_instrument(symbol=sr_option)
            out["get_instrument_option"] = {
                "type": type(inst2).__name__,
                "symbol": getattr(inst2, "symbol", None),
                "sec_name": getattr(inst2, "sec_name", None),
                "delisted_date": str(getattr(inst2, "delisted_date", None)),
            }

        # 错开 60s 限流
        time.sleep(62)

        # === 调用 C：get_history_n 多合约(≤20, 期权+期货混) ===
        syms = [s for s in ([sr_option] if sr_option else [])] + [main_sym]
        r3 = tbpy.get_history_n(syms, "1d", 1,
                                fields=["close", "time"], timeout="30s")
        out["get_history_n_multi"] = _safe(r3)

        # --- get_current_tick 主连（休市期返回 None，真实行为） ---
        t = tbpy.get_current_tick(symbol=main_sym)
        out["get_current_tick"] = {
            "type": type(t).__name__,
            "repr_head": repr(t)[:300],
            "last": getattr(t, "last", None) if t else None,
            "bidasks_head": _safe(getattr(t, "bidasks", None), 1) if t else None,
        }

        # --- get_main_instrument ---
        inst = tbpy.get_main_instrument(underlying_symbol="SR.CZCE")
        out["get_main_instrument"] = {
            "type": type(inst).__name__,
            "symbol": getattr(inst, "symbol", None),
        }

        # --- QuoteFlag 探测 ---
        try:
            out["QuoteFlag_keys"] = [k for k in dir(tbpy.QuoteFlag) if not k.startswith("_")]
        except Exception as e:
            out["QuoteFlag_err"] = repr(e)

        out["get_last_err"] = tbpy.get_last_err()
    finally:
        try:
            disconnect()
        except Exception:
            pass

    print("RESULT_JSON_BEGIN")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print("RESULT_JSON_END")

if __name__ == "__main__":
    main()
