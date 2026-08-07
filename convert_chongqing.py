#!/usr/bin/env python3
r"""
One-off converter: the organised Chongqing report
(C:\Users\admin\重庆轨道交通线路信息.md) -> data/chongqing.json in the
importer's format (see data_format.md). Mirrors convert_chengdu.py.

The .md is the finished product (15 records: 1-6, 9, 10, 18, 环线 L,
空港线 KG, 国博线 E, 江跳线 JT, 璧铜线 BT, 重庆云巴 SS). We parse it directly:
  * the 路网概况 line table  -> line code, Chinese name, colour (hex)
  * each "### <line>" section's station table (| # | 站名 | English | 换乘 |)
    -> ordered station names + English names
  * directions: exactly what the source's 运行方向 bullets state — linear lines
    get 开往<末站> / 开往<首站> (app-uniform wording), 环线 gets 内环 / 外环
    (same convention as Beijing/Shanghai ring lines). The through-running /
    express 交路 in the 结构 notes (4—环线—5 直通快速列车, 5号线↔江跳线贯通,
    10号线快速车) are NOT modelled as directions — the source does not list
    them as 运行方向.
  * stations de-duped by NAME (project-wide convention). The source's
    line-suffixed same-name stations (歇台子(1)/(5)/(18)、杨家坪(2)/(18)、
    观音桥(3)/(9)、玉带山(4)/(环线)) are physically separate stations per the
    source's 备注 3, and the suffixes keep them separate here — do NOT strip
    them. English name taken from first sighting; conflicts printed as warnings.
  * 云巴 SS has no official colour (source leaves it blank) -> fallback grey.

Prefix is "cq-" (Chongqing); registry: bj/sh/sz(苏州)/szn(深圳)/pa/cd(成都).

Run once:  python convert_chongqing.py
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = r"C:\Users\admin\重庆轨道交通线路信息.md"
OUT = os.path.join(ROOT, "data", "chongqing.json")

LOOP_CODES = {"L"}                      # 环线 is a closed loop
FALLBACK_COLOR = "#4b5563"

# non-numeric codes -> (line id suffix, English name); numeric -> "Line N"
CODE_ID = {"L": "loop", "KG": "kg", "E": "e", "JT": "jt", "BT": "bt", "SS": "ss"}
NAME_EN = {
    "L": "Loop Line",
    "KG": "Konggang Line (Line 3 Airport Branch)",
    "E": "Guobo Line (Line 6 Branch)",
    "JT": "Jiangtiao Line",
    "BT": "Bitong Line",
    "SS": "Chongqing Yunba",
}


def line_id(code):
    return f"cq-l{code}" if code.isdigit() else f"cq-{CODE_ID[code]}"


def cells(row):
    return [c.strip() for c in row.strip().strip("|").split("|")]


def parse_overview(text):
    """路网概况 line table -> {code: (chinese name, '#rrggbb')}."""
    out = {}
    for raw in text.splitlines():
        if raw.startswith("###"):
            break                       # overview lives above the line sections
        if not raw.lstrip().startswith("|"):
            continue
        c = cells(raw)
        if len(c) != 8 or not re.fullmatch(r"[0-9A-Za-z]+", c[0]):
            continue
        color = c[3].strip("`")
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            color = FALLBACK_COLOR      # 云巴 SS: source has no official colour
        name = re.sub(r"\*\*|\s", "", c[1])
        out[c[0]] = (name, color)
    return out


def section_code(title):
    """'1 号线' -> '1'; '环线（L）' -> 'L'; '重庆云巴（SS）' -> 'SS'."""
    m = re.match(r"^(\d+)\s*号线", title)
    if m:
        return m.group(1)
    m = re.search(r"（([A-Z]+)）", title)
    return m.group(1) if m and m.group(1) in CODE_ID else None


def parse_sections(text):
    """-> [(code, [(station name, english), …], [运行方向 bullet texts])] in doc order."""
    secs = []
    cur = None
    for raw in text.splitlines():
        h = re.match(r"^###\s+(.+?)\s*$", raw)
        if h:
            code = section_code(h.group(1))
            cur = (code, [], []) if code else None
            if cur:
                secs.append(cur)
            continue
        if cur is None:
            continue
        if raw.lstrip().startswith("- **运行方向"):
            cur[2].append(raw)
            continue
        if not raw.lstrip().startswith("|"):
            continue
        c = cells(raw)
        if len(c) == 4 and c[0].isdigit():      # skips the →闭合 row and headers
            cur[1].append((c[1], c[2] if c[2] != "—" else ""))
    return secs


def check_directions(code, seq, bullets):
    """Warn if the doc's 运行方向 termini disagree with the station table ends."""
    ends = []
    for b in bullets:
        body = b.split("：", 1)[-1]
        body = re.sub(r"（[^）]*）", "", body)
        parts = [p.strip() for p in body.split("→") if p.strip()]
        if len(parts) >= 2:
            ends.append(parts[-1])
    if code in LOOP_CODES:
        return
    want = [seq[-1], seq[0]]
    if ends[:2] != want:
        print(f"  ! line {code}: 运行方向 termini {ends[:2]} != table ends {want}")


def build():
    with open(SRC, encoding="utf-8") as f:
        text = f.read()

    overview = parse_overview(text)
    sections = parse_sections(text)

    line_recs = []
    station_lines = {}      # name -> [line_id, …]
    station_en = {}         # name -> english
    order = []              # first-seen station names
    per_line = []           # for the summary print

    for code, rows, bullets in sections:
        if not rows:
            print(f"  ! line {code}: no station table, skipped")
            continue
        lid = line_id(code)
        seq = [nm for nm, _ in rows]
        check_directions(code, seq, bullets)

        name_cn, color = overview.get(code, (code, FALLBACK_COLOR))
        name_en = NAME_EN.get(code, f"Line {code}")
        dirs = ["内环", "外环"] if code in LOOP_CODES else \
               [f"开往{seq[-1]}", f"开往{seq[0]}"]
        line_recs.append({"id": lid, "name": name_cn, "name_en": name_en,
                          "color": color, "directions": dirs})
        per_line.append((lid, name_cn, seq, dirs))

        for nm, en in rows:
            if nm not in station_lines:
                station_lines[nm] = []
                order.append(nm)
            if en:
                if station_en.get(nm, en) != en:
                    print(f"  ! station {nm}: English '{station_en[nm]}' vs '{en}'")
                station_en.setdefault(nm, en)
            if lid not in station_lines[nm]:
                station_lines[nm].append(lid)

    name_to_id = {}
    station_list = []
    for i, nm in enumerate(order, 1):
        sid = f"cq-{i:03d}"
        name_to_id[nm] = sid
        station_list.append({
            "id": sid, "name_cn": nm, "name_en": station_en.get(nm, ""),
            "alias": [], "lines": station_lines[nm],
        })

    # demo seed: 沙坪坝 (1号线 #14 / 9号线 #3 / 环线)
    spb = name_to_id["沙坪坝"]
    seed = [{
        "station": spb, "from_line": "cq-l1", "from_dir": "开往璧山",
        "to_line": "cq-l9", "to_dir": "开往花石沟",
        "author_email": "shancheng@example.com", "author_nick": "山城通勤",
        "anon": True, "position_type": "car", "car_number": 3,
        "description": "1号线往璧山方向坐第3节车厢，下车后左手边就是9号线换乘扶梯，比走到车尾少绕大半个站台。",
        "likes": 15, "dislikes": 2, "version": 1, "days_ago": 9,
        "comments": [{"nick": "匿名", "days_ago": 3, "text": "沙坪坝站是地下枢纽，换乘要上下好几层，赶时间的多留两分钟。"}],
    }]

    doc = {
        "city": {
            "id": "chongqing", "country_id": "cn",
            "country_cn": "中国", "country_en": "China",
            "name_cn": "重庆", "name_en": "Chongqing",
            "alias": ["chongqing", "cq", "重庆市", "渝", "山城"],
            "timezone": "Asia/Shanghai",
        },
        "system": {"id": "chongqing-rail-transit", "name_cn": "重庆轨道交通",
                   "name_en": "Chongqing Rail Transit"},
        "lines": line_recs,
        "stations": station_list,
        "seed_answers": seed,
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    print(f"wrote {OUT}")
    print(f"  lines:    {len(line_recs)}")
    print(f"  stations: {len(station_list)} (deduped by name)")
    print(f"  station rows total: {sum(len(s) for _, _, s, _ in per_line)}")
    inter = [s for s in station_list if len(s["lines"]) > 1]
    print(f"  interchanges (>=2 lines): {len(inter)}")
    print(f"  seed: 沙坪坝 ({spb}) lines={station_lines['沙坪坝']}")
    for lid, name_cn, seq, dirs in per_line:
        print(f"    {lid:<8} {name_cn:<12} stations={len(seq):>3}  "
              f"{seq[0]}…{seq[-1]}  dirs={'/'.join(dirs)}")


if __name__ == "__main__":
    build()
