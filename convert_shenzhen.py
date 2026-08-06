#!/usr/bin/env python3
r"""
One-off converter: the organised Shenzhen report
(C:\Users\admin\深圳地铁线路站点.md) -> data/shenzhen.json in the importer's
format (see data_format.md). Mirrors convert_suzhou.py.

The .md is the finished product (16 operating lines: 1, 2&8, 3, 4, 5, 6,
6支线, 7, 9, 10, 11, 12, 13, 14, 16, 20). We parse it directly:
  * each line's "**站序**：A—B—C…" line  -> ordered station names
  * directions: the source has no 上行/下行, only 方向A/方向B by the two ends of
    the 站序; we render them the app-uniform way: 开往<末站> / 开往<首站>.
  * stations de-duped by NAME (same name == same physical station; the report's
    备注 explicitly counts same-named interchange stations once per line, i.e.
    the source itself treats name-identity as station-identity).

Prefix is "szn-" (Shenzhen) so it never collides with Suzhou's "sz-".
2号线 & 8号线 run through as one line on the source page (赤湾↔溪涌) and are
kept as one line entry (szn-l2-8), matching the source. One data quirk fixed:
深圳北 (lines 5/6) / 深圳北站 (line 4) are the same hub -> normalised to 深圳北站.

Run once:  python convert_shenzhen.py
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = r"C:\Users\admin\深圳地铁线路站点.md"
OUT = os.path.join(ROOT, "data", "shenzhen.json")

# same physical hub written two ways in the source
NAME_FIX = {"深圳北": "深圳北站"}

# official-ish Shenzhen Metro line colours, keyed by our line id
LINE_COLOR = {
    "szn-l1": "#00a651", "szn-l2-8": "#e60012", "szn-l3": "#0072bc",
    "szn-l4": "#d20962", "szn-l5": "#a3238e", "szn-l6": "#52c1d6",
    "szn-l6b": "#52c1d6", "szn-l7": "#ef7fb0", "szn-l9": "#8c6239",
    "szn-l10": "#f7a800", "szn-l11": "#6d4a99", "szn-l12": "#008e9b",
    "szn-l13": "#00a3a6", "szn-l14": "#c8a063", "szn-l16": "#85c440",
    "szn-l20": "#b0afae",
}


def label_to_id_name(label):
    """'2号线 & 8号线' -> (szn-l2-8, name, name_en); '6号线支线' -> szn-l6b; …"""
    label = label.strip()
    if "支线" in label:
        n = re.search(r"(\d+)", label).group(1)
        return f"szn-l{n}b", f"{n}号线支线", f"Line {n} Branch"
    nums = re.findall(r"(\d+)", label)
    if "&" in label or len(nums) > 1:
        return "szn-l" + "-".join(nums), label, "Line " + " & ".join(nums)
    n = nums[0]
    return f"szn-l{n}", f"{n}号线", f"Line {n}"


def fix(name):
    return NAME_FIX.get(name, name)


def build():
    with open(SRC, encoding="utf-8") as f:
        text = f.read()

    # walk sections; capture each line's 站序 (the authoritative ordered list)
    lines = []                 # [(lid, name, name_en, [station names in order])]
    cur = None                 # (lid, name, name_en)
    for raw in text.splitlines():
        h = re.match(r"^###\s+(.+?)\s*$", raw)
        if h:
            cur = label_to_id_name(h.group(1))
            continue
        if cur is None:
            continue
        m = re.match(r"^\*\*站序\*\*[：:]\s*(.+?)\s*$", raw)
        if not m:
            continue
        seq = [fix(s.strip()) for s in re.split(r"[—–]", m.group(1)) if s.strip()]
        lines.append((*cur, seq))
        cur = None

    # build line records + collect stations (dedupe by name, keep first-seen order)
    line_recs = []
    stations = {}              # name -> [line_id, …]
    order = []                 # first-seen station names
    for lid, name, name_en, seq in lines:
        line_recs.append({
            "id": lid, "name": name, "name_en": name_en,
            "color": LINE_COLOR.get(lid, "#4b5563"),
            "directions": [f"开往{seq[-1]}", f"开往{seq[0]}"],
        })
        for nm in seq:
            rec = stations.setdefault(nm, [])
            if nm not in order:
                order.append(nm)
            if lid not in rec:
                rec.append(lid)

    name_to_id = {}
    station_list = []
    for i, nm in enumerate(order, 1):
        sid = f"szn-{i:03d}"
        name_to_id[nm] = sid
        station_list.append({
            "id": sid, "name_cn": nm, "name_en": "",
            "alias": [], "lines": stations[nm],
        })

    # demo seed: 车公庙 (1/7/9/11) — 1号线往机场东 换 11号线往碧头
    cgm = name_to_id["车公庙"]
    seed = [{
        "station": cgm, "from_line": "szn-l1", "from_dir": "开往机场东",
        "to_line": "szn-l11", "to_dir": "开往碧头",
        "author_email": "penglai@example.com", "author_nick": "鹏城通勤",
        "anon": True, "position_type": "car", "car_number": 5,
        "description": "1号线往机场东方向坐第5节车厢，下车正对11号线换乘通道，不用穿过整个站厅。",
        "likes": 23, "dislikes": 2, "version": 1, "days_ago": 10,
        "comments": [{"nick": "匿名", "days_ago": 6, "text": "11号线站台深，换乘扶梯挺长的，预留点时间。"}],
    }]

    doc = {
        "city": {
            "id": "shenzhen", "country_id": "cn",
            "country_cn": "中国", "country_en": "China",
            "name_cn": "深圳", "name_en": "Shenzhen",
            "alias": ["shenzhen", "szn", "深圳市", "鹏城"],
            "timezone": "Asia/Shanghai",
        },
        "system": {"id": "shenzhen-metro", "name_cn": "深圳地铁", "name_en": "Shenzhen Metro"},
        "lines": line_recs,
        "stations": station_list,
        "seed_answers": seed,
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    print(f"wrote {OUT}")
    print(f"  lines:    {len(line_recs)}")
    print(f"  stations: {len(station_list)} (deduped by name)")
    inter = [s for s in station_list if len(s["lines"]) > 1]
    print(f"  interchanges (>=2 lines): {len(inter)}")
    print(f"  seed: 车公庙 ({cgm}) lines={stations['车公庙']}")
    for lid, name, name_en, seq in lines:
        print(f"    {lid:<9} {name:<12} stations={len(seq)}  {seq[0]}…{seq[-1]}")


if __name__ == "__main__":
    build()
