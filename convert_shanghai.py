#!/usr/bin/env python3
"""
One-off converter: harvested Shanghai metro dump
(C:\\Users\\admin\\shmetro\\shmetro_network.json, 20 lines) -> data/shanghai.json
in the importer's format (see data_format.md). Mirrors convert_beijing.py.

Direction model — each line gets exactly TWO opposing directions:
  * The raw feed uses direction = -1 / +1 (the two physical directions) but
    lists several `toward` labels per side because of branch / short-turn
    services. We collapse to two clean "开往<终点>" labels using the line's
    ordered endpoints (stations[0] / stations[-1]); orientation (which sign
    maps to which end) is read from the -1 side's main terminus.
  * Line 4 is a full loop -> 内圈 / 外圈 (Shanghai's official ring naming).

Stations are de-duped by NAME (same name == same physical station, the metro
reality). Station id derives from the (per-line) station code; the one source
collision — 东陆路 / 东兰路 both coded 800-DLL — is kept as two stations via an
id suffix, and interchanges whose code differs per line (机场 stations, etc.)
still merge because we key on the name.

Run once:  python convert_shanghai.py
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = r"C:\Users\admin\shmetro\shmetro_network.json"
OUT = os.path.join(ROOT, "data", "shanghai.json")

# non "N号线" lines -> (id, name_en)
NAMED = {
    "浦江线":     ("sh-pujiang", "Pujiang Line"),
    "市域机场线": ("sh-airport", "Airport Link Line"),
}

# full loop lines (line_no) -> 内圈/外圈 (官方环线命名)
LOOP_LINES = {4}

# keep English for the stations the original file had
STATION_EN = {
    "人民广场": "People's Square", "世纪大道": "Century Avenue",
    "徐家汇": "Xujiahui",
}


def line_id_en(name, no):
    m = re.match(r"^(\d+)号线", name)
    if m:
        n = int(m.group(1))
        return f"sh-l{n}", f"Line {n}"
    if name in NAMED:
        return NAMED[name]
    return "sh-" + re.sub(r"[^0-9a-zA-Z]+", "-", name).strip("-").lower(), ""


def slug(code):
    return re.sub(r"[^0-9a-zA-Z]+", "-", code).strip("-").lower() or "s"


def directions_for(L):
    """Two clean 开往<终点> labels (or 内圈/外圈 for the ring)."""
    if L["line_no"] in LOOP_LINES:
        return ["内圈", "外圈"]
    sts = L["stations"]
    first, last = sts[0]["name"], sts[-1]["name"]
    # main terminus of the -1 side = the toward with most served stations
    best = {}
    for x in L.get("direction_summary", []):
        s = x["direction"]
        if s not in best or x["served_stations"] > best[s][1]:
            best[s] = (x["toward"], x["served_stations"])
    neg = best.get(-1, (first, 0))[0]
    reverse = (neg == last)  # -1 side heads toward the last station in order
    if reverse:
        return [f"开往{last}", f"开往{first}"]   # dir -1, dir +1
    return [f"开往{first}", f"开往{last}"]


def build():
    with open(SRC, encoding="utf-8") as f:
        raw = json.load(f)

    lines = []
    stations = {}  # name -> {"code": firstcode, "lines": [line_id]}

    for L in raw:
        lid, len_en = line_id_en(L["line_name"], L["line_no"])
        color = L.get("color") or "#4b5563"
        lines.append({
            "id": lid, "name": L["line_name"], "name_en": len_en,
            "color": color, "directions": directions_for(L),
        })
        for s in L["stations"]:
            nm = s["name"]
            rec = stations.setdefault(nm, {"code": s["station_code"], "lines": []})
            if lid not in rec["lines"]:
                rec["lines"].append(lid)

    station_list = []
    name_to_id = {}
    used = set()
    for nm, rec in stations.items():
        base = "sh-" + slug(rec["code"])
        sid, k = base, 2
        while sid in used:          # e.g. 东陆路 / 东兰路 both coded 800-DLL
            sid, k = f"{base}-{k}", k + 1
        used.add(sid)
        name_to_id[nm] = sid
        station_list.append({
            "id": sid, "name_cn": nm, "name_en": STATION_EN.get(nm, ""),
            "alias": [], "lines": rec["lines"],
        })

    rmgc = name_to_id["人民广场"]
    # line 2's +1 terminus (airport) as the re-mapped seed to_dir
    l1_dirs = next(l for l in lines if l["id"] == "sh-l1")["directions"]
    l2_dirs = next(l for l in lines if l["id"] == "sh-l2")["directions"]
    from_dir = next(d for d in l1_dirs if "莘庄" in d)
    to_dir = next(d for d in l2_dirs if "航站楼" in d)
    seed = [{
        "station": rmgc, "from_line": "sh-l1", "from_dir": from_dir,
        "to_line": "sh-l2", "to_dir": to_dir,
        "author_email": "shtongqin@example.com", "author_nick": "申城通勤族",
        "anon": True, "position_type": "car", "car_number": 6,
        "description": "1号线往莘庄方向坐第6节车厢，下车即见通往2号线的换乘通道口。",
        "likes": 31, "dislikes": 3, "version": 1, "days_ago": 18,
        "comments": [{"nick": "匿名", "days_ago": 16, "text": "高峰期这里人巨多，建议错峰。"}],
    }]

    doc = {
        "city": {
            "id": "shanghai", "country_id": "cn",
            "country_cn": "中国", "country_en": "China",
            "name_cn": "上海", "name_en": "Shanghai",
            "alias": ["SH", "shanghai", "sh", "上海市"],
            "timezone": "Asia/Shanghai",
        },
        "system": {"id": "shanghai-metro", "name_cn": "上海地铁", "name_en": "Shanghai Metro"},
        "lines": lines,
        "stations": station_list,
        "seed_answers": seed,
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    print(f"wrote {OUT}")
    print(f"  lines:    {len(lines)}")
    print(f"  stations: {len(station_list)} (deduped by name)")
    print(f"  seed:     {len(seed)}  ({from_dir} -> {to_dir} @人民广场 {rmgc})")
    rm = next(s for s in station_list if s["id"] == rmgc)
    print(f"  人民广场 lines: {rm['lines']}")
    for l in lines:
        print(f"    {l['id']:<11} {l['name']:<7} {l['color']:<9} {l['directions']}")


if __name__ == "__main__":
    build()
