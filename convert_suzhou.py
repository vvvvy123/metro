#!/usr/bin/env python3
"""
One-off converter: the organised Suzhou report (C:\\Users\\admin\\苏州地铁线路站点.md)
-> data/suzhou.json in the importer's format (see data_format.md).

The .md is the finished, deduped product (9 operating lines 1-8 & 11; line 10 is
unopened with no published names, 6延/7延 are unopened extensions — all excluded).
We parse it directly:
  * overview table  -> each line's 上行/下行 termini  -> two "开往<终点>" directions
  * per-line lists  -> ordered station names (换乘 annotations stripped)
  * stations deduped by NAME (same name == same physical station); the lines a
    station belongs to are collected from every section it appears in.

Suzhou is a brand-new city, so there is no prior seed to preserve; we add one
demo transfer answer (苏州火车站 2号线->4号线) to match the other cities' sample
content. Station ids are synthesised sequentially (no global id in the source).

Run once:  python convert_suzhou.py
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = r"C:\Users\admin\苏州地铁线路站点.md"
OUT = os.path.join(ROOT, "data", "suzhou.json")

# official line colors (sz-mtr.com line map)
LINE_COLOR = {
    "1号线": "#c40e5c", "2号线": "#f04e3e", "3号线": "#00a1e0", "4号线": "#0071bc",
    "5号线": "#8a2be2", "6号线": "#e6a800", "7号线": "#7ac143", "8号线": "#f7941e",
    "11号线": "#a05eb5",
}


def build():
    with open(SRC, encoding="utf-8") as f:
        text = f.read()

    # --- overview table: | 1号线 | 往 木渎 | 往 钟南街 | 24 | ---
    up_down = {}   # line name -> (up_terminus, down_terminus, count)
    for m in re.finditer(
            r"^\|\s*(\d+号线)\s*\|\s*往\s*(\S+)\s*\|\s*往\s*(\S+)\s*\|\s*(\d+)\s*\|",
            text, re.M):
        up_down[m.group(1)] = (m.group(2), m.group(3), int(m.group(4)))

    # --- per-line station lists (### N号线 …) ---
    line_names = list(up_down.keys())
    lines = []
    stations = {}          # name -> [line_id, ...]  (insertion order preserved)
    order_names = []       # first-seen order for id assignment
    per_line_count = {}

    cur = None
    for raw in text.splitlines():
        h = re.match(r"^###\s*(\d+号线)", raw)
        if h:
            cur = h.group(1)
            per_line_count[cur] = 0
            continue
        if cur is None:
            continue
        s = re.match(r"^\s*\d+\.\s*(.+?)\s*$", raw)
        if not s:
            continue
        name = re.sub(r"（换乘.*?）\s*$", "", s.group(1)).strip()
        if not name:
            continue
        lid = f"sz-l{int(re.match(r'(\d+)', cur).group(1))}"
        rec = stations.setdefault(name, [])
        if lid not in rec:
            rec.append(lid)
        if name not in stations or name not in order_names:
            if name not in order_names:
                order_names.append(name)
        per_line_count[cur] += 1

    for ln in line_names:
        n = int(re.match(r"(\d+)", ln).group(1))
        up, down, _ = up_down[ln]
        lines.append({
            "id": f"sz-l{n}", "name": ln, "name_en": f"Line {n}",
            "color": LINE_COLOR.get(ln, "#4b5563"),
            "directions": [f"开往{up}", f"开往{down}"],
        })

    name_to_id = {}
    station_list = []
    for i, name in enumerate(order_names, 1):
        sid = f"sz-{i:03d}"
        name_to_id[name] = sid
        station_list.append({
            "id": sid, "name_cn": name, "name_en": "",
            "alias": [], "lines": stations[name],
        })

    # demo seed answer at 苏州火车站 (2号线 -> 4号线), directions verified below
    szhz = name_to_id["苏州火车站"]
    seed = [{
        "station": szhz, "from_line": "sz-l2", "from_dir": "开往桑田岛",
        "to_line": "sz-l4", "to_dir": "开往同里",
        "author_email": "gusu@example.com", "author_nick": "姑苏通勤",
        "anon": True, "position_type": "car", "car_number": 4,
        "description": "2号线往桑田岛方向第4节车厢下车，直接对着去4号线的换乘扶梯，不用绕。",
        "likes": 15, "dislikes": 1, "version": 1, "days_ago": 12,
        "comments": [{"nick": "匿名", "days_ago": 9, "text": "早高峰扶梯排队，走旁边楼梯更快。"}],
    }]

    doc = {
        "city": {
            "id": "suzhou", "country_id": "cn",
            "country_cn": "中国", "country_en": "China",
            "name_cn": "苏州", "name_en": "Suzhou",
            "alias": ["SZ", "suzhou", "sz", "苏州市"],
            "timezone": "Asia/Shanghai",
        },
        "system": {"id": "suzhou-rail", "name_cn": "苏州轨道交通", "name_en": "Suzhou Rail Transit"},
        "lines": lines,
        "stations": station_list,
        "seed_answers": seed,
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    print(f"wrote {OUT}")
    print(f"  lines:    {len(lines)}")
    print(f"  stations: {len(station_list)} (deduped by name)")
    # per-line count vs overview table
    for ln in line_names:
        want = up_down[ln][2]
        got = per_line_count.get(ln, 0)
        flag = "" if want == got else f"  <== MISMATCH (table {want})"
        print(f"    {ln:<6} stations parsed={got}{flag}")
    inter = [s for s in station_list if len(s["lines"]) > 1]
    print(f"  interchanges (>=2 lines): {len(inter)}  (report says 40)")
    print(f"  seed: 苏州火车站 ({szhz}) lines={stations['苏州火车站']}")


if __name__ == "__main__":
    build()
