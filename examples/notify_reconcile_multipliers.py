"""精确对账：交易所官方乘数 vs 边界表"乘数"列（逐品种打印）"""
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# 交易所官方乘数——严格按用户提供、仅含本策略 47 品种中的有交集部分 ──
OFFICIAL = {
    # SHFE (用户表格)
    "cu": 5, "al": 5, "zn": 5, "pb": 5, "ni": 1, "sn": 1, "ao": 20,
    "ad": 10, "au": 1000, "ag": 15, "rb": 10, "wr": 10, "hc": 10,
    "ss": 5, "ru": 10, "br": 5, "bu": 10, "fu": 10, "sp": 10,

    # DCE (用户表格)
    "a": 10, "b": 10, "m": 10, "y": 10, "p": 10, "c": 10, "cs": 10,
    "jd": 5, "lh": 16, "i": 100, "j": 100, "jm": 60, "l": 5, "bz": 30,

    # CZCE (用户表格)
    "sr": 10, "cf": 5, "ta": 5, "ma": 10, "fg": 20, "ap": 10, "cj": 5,
    "cy": 5, "jr": 20, "pm": 50, "ri": 20, "oi": 10, "sm": 5, "pf": 5,
    # RM(菜粕), SA(纯碱), SF(硅铁), PK(花生), PX(对二甲苯), SH(烧碱) 不在用户 CZCE 表中

    # GFEX (用户表格)
    "si": 5, "ps": 3,
}

try:
    from sell_option.fetchers.boundary_table import load_boundary_table
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sell_option.fetchers.boundary_table import load_boundary_table

rows = load_boundary_table()

print("品种\t\t表内乘数\t官方乘数\t状态")
print("-" * 70)

diffs = []
ok = 0
matched = 0
for row in rows:
    code = row["标的编码"]
    table_mult = int(row["乘数"])
    official_mult = OFFICIAL.get(code)
    
    if official_mult is None:
        status = "⚠ 用户表未提供此项"
    elif table_mult == official_mult:
        status = "✅ OK"
        ok += 1
    else:
        status = f"❌ 差异! 表={table_mult} 官方={official_mult}"
    
    matched += 1
    print(f"{code}\t{table_mult}\t{official_mult}\t{status}")
    
    diffs.append((code, table_mult, official_mult, status))

print(f"\n合计: 对账 {len(OFFICIAL)} 个品种中有交集 → 一致 {ok}/{len(OFFICIAL)}, "
      f"未提供 {len(OFFICIAL)-ok}, 一致率={ok/len(OFFICIAL)*100:.0f}%")

if any(s.startswith("❌") for _, _, _, s in diffs):
    print("\n❌ 确认差异:")
    for c, t, o, s in diffs:
        if s.startswith("❌"):
            print(f"  {c}: 边界表={t} vs 官方={o}")
else:
    print("\n✅ 所有可核对的品种与边界表一致——无需再跑 tbpy！")
