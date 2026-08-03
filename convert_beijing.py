#!/usr/bin/env python3
"""
One-off converter: official Beijing subway dump (beijing-subway-lines.json,
28 lines with directions/stationsInOrder/accLocation) -> data/beijing.json in
the importer's format (see data_format.md).

- line id: "N号线" -> bj-l{N}; named lines mapped explicitly (reuses bj-apt,
  bj-l1/2/4/5/6/13 so existing references survive).
- direction name: official name minus the trailing （上行/下行）；null names
  are synthesised from stationsInOrder endpoints.
- station id: bj-<accLocation> (dedupes interchanges across lines correctly).
- seed_answers: the original two demo answers, re-mapped onto the new station
  id and the new direction labels so they stay valid.

Run once:  python convert_beijing.py    (writes data/beijing.json)
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(ROOT), "beijing-subway-lines.json")  # C:\Users\admin\...
if not os.path.exists(SRC):
    SRC = r"C:\Users\admin\beijing-subway-lines.json"
OUT = os.path.join(ROOT, "data", "beijing.json")

# named (non "N号线") lines -> (id, name_en)
NAMED = {
    "亦庄T1线":   ("bj-yizhuang-t1",     "Yizhuang T1"),
    "大兴机场线": ("bj-daxing-airport",  "Daxing Airport Express"),
    "西郊线":     ("bj-xijiao",          "Xijiao Line"),
    "S1线":       ("bj-s1",              "S1 Line"),
    "燕房线":     ("bj-yanfang",         "Yanfang Line"),
    "昌平线":     ("bj-changping",       "Changping Line"),
    "房山线":     ("bj-fangshan",        "Fangshan Line"),
    "亦庄线":     ("bj-yizhuang",        "Yizhuang Line"),
    "首都机场线": ("bj-apt",             "Capital Airport Express"),
}

# full loop lines: "开往X（上行）" is misleading on a ring (at any station both
# named termini lie the same way round), so use 内环(clockwise)/外环(counter-cw).
LOOP_LINES = {"2号线", "10号线"}

# preserve English names for the six stations the original file had
STATION_EN = {
    "西直门": "Xizhimen", "东直门": "Dongzhimen", "东单": "Dongdan",
    "建国门": "Jianguomen", "宣武门": "Xuanwumen", "平安里": "Ping'anli",
}


def line_id_en(name):
    m = re.match(r"^(\d+)号线", name)
    if m:
        n = int(m.group(1))
        return f"bj-l{n}", f"Line {n}"
    if name in NAMED:
        return NAMED[name]
    # fallback: slug of the name
    return "bj-" + re.sub(r"[^0-9a-zA-Z]+", "-", name).strip("-").lower(), ""


def clean_dir(nm):
    """drop trailing full-width parenthetical like （上行）/（下行）"""
    if not nm:
        return None
    return re.sub(r"（[^）]*）\s*$", "", nm).strip() or None


def build():
    with open(SRC, encoding="utf-8") as f:
        raw = json.load(f)

    lines = []
    # accLocation -> {"name": cn, "lines": set(line_id)}
    stations = {}

    for L in raw:
        lid, len_en = line_id_en(L["line"])
        color = L.get("color")
        color = f"#{color}" if color else "#4b5563"

        order = L.get("stationsInOrder") or []
        dirs_raw = L.get("directions") or []
        if L["line"] in LOOP_LINES:
            names = ["内环", "外环"]  # 内环=顺时针, 外环=逆时针
        else:
            names = [clean_dir(d.get("name")) for d in dirs_raw]
            # synthesise if missing / non-distinct
            if len(names) < 2 or not all(names) or len(set(names)) < 2:
                if len(order) >= 2:
                    names = [f"开往{order[-1]}", f"开往{order[0]}"]
                else:
                    names = ["方向1", "方向2"]
        lines.append({
            "id": lid, "name": L["line"], "name_en": len_en,
            "color": color, "directions": names[:2],
        })

        # stations: dedupe by accLocation, collect the lines that serve it
        for s in L.get("stations", []):
            acc = s.get("accLocation")
            cn = s.get("name")
            if acc is None or cn is None:
                continue
            rec = stations.setdefault(acc, {"name": cn, "lines": []})
            if lid not in rec["lines"]:
                rec["lines"].append(lid)

    station_list = []
    name_to_id = {}
    for acc, rec in stations.items():
        sid = f"bj-{acc}"
        st = {
            "id": sid, "name_cn": rec["name"],
            "name_en": STATION_EN.get(rec["name"], ""),
            "alias": [], "lines": rec["lines"],
        }
        station_list.append(st)
        # first station with this cn name wins as the canonical lookup
        name_to_id.setdefault(rec["name"], sid)

    xzm = name_to_id["西直门"]
    seed = [
        {
            "station": xzm, "from_line": "bj-l2", "from_dir": "内环",
            "to_line": "bj-l13", "to_dir": "开往东直门",
            "author_email": "laowang@example.com", "author_nick": "通勤老王",
            "anon": True, "position_type": "custom", "custom_text": "车尾第1节",
            "description": "2号线下车后往车尾方向走，上扶梯右转约80米即可到13号线站台。",
            "likes": 42, "dislikes": 5, "version": 2, "days_ago": 7,
            "comments": [
                {"nick": "匿名", "days_ago": 5, "text": "今天扶梯维修，现在改成另一侧了，注意一下。"},
                {"nick": "路人甲", "days_ago": 9, "text": "亲测有效，省了差不多两分钟。"},
            ],
        },
        {
            "station": xzm, "from_line": "bj-l2", "from_dir": "内环",
            "to_line": "bj-l13", "to_dir": "开往东直门",
            "author_email": "xiaolin@example.com", "author_nick": "小林",
            "anon": False, "position_type": "car", "car_number": 3,
            "description": "坐第3节车厢，出门正对楼梯，人少的时候更快。",
            "likes": 18, "dislikes": 2, "version": 1, "days_ago": 88,
            "comments": [],
        },
    ]

    doc = {
        "city": {
            "id": "beijing", "country_id": "cn",
            "country_cn": "中国", "country_en": "China",
            "name_cn": "北京", "name_en": "Beijing",
            "alias": ["BJ", "beijing", "bj", "Peking", "北京市"],
            "timezone": "Asia/Shanghai",
        },
        "system": {"id": "beijing-subway", "name_cn": "北京地铁", "name_en": "Beijing Subway"},
        "lines": lines,
        "stations": station_list,
        "seed_answers": seed,
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    # report
    print(f"wrote {OUT}")
    print(f"  lines:    {len(lines)}")
    print(f"  stations: {len(station_list)} (deduped by accLocation)")
    print(f"  seed:     {len(seed)}")
    # sanity: 西直门 lines
    xz = next(s for s in station_list if s["id"] == xzm)
    print(f"  西直门 ({xzm}) -> lines {xz['lines']}")
    l2 = next(l for l in lines if l["id"] == "bj-l2")
    l13 = next(l for l in lines if l["id"] == "bj-l13")
    print(f"  bj-l2 dirs:  {l2['directions']}")
    print(f"  bj-l13 dirs: {l13['directions']}")


if __name__ == "__main__":
    build()
