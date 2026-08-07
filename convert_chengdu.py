#!/usr/bin/env python3
r"""
One-off converter: the organised Chengdu report
(C:\Users\admin\成都地铁线路信息.md) -> data/chengdu.json in the importer's
format (see data_format.md). Mirrors convert_shenzhen.py / convert_paris.py.

The .md is the finished product (19 records: metro 1-10, 13, 17, 18, 19, 27,
30, S3 资阳线, plus tram R2 蓉2号线 and its branch RB). We parse it directly:
  * the 路网概况 line table  -> line code, Chinese name, colour (hex)
  * each "### <line>" section's station table (| # | 站名 | English | 换乘 |)
    -> ordered station names + English names
  * directions: exactly what the source's 运行方向 bullets state — linear lines
    get 开往<末站> / 开往<首站> (app-uniform wording), the 7号线 loop gets
    内环 / 外环 (same convention as Beijing/Shanghai ring lines). The branch /
    express 交路 mentioned in the 结构 notes (1号线 支线, 18号线 直达车,
    19号线 双机场直达) are NOT modelled as directions — the source does not
    list them as 运行方向.
  * stations de-duped by NAME (same name == same physical interchange node),
    the project-wide convention. English name taken from first sighting;
    conflicts are printed as warnings.

Prefix is "cd-" (Chengdu); registry: bj/sh/sz(苏州)/szn(深圳)/pa/cq(重庆).

Run once:  python convert_chengdu.py
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = r"C:\Users\admin\成都地铁线路信息.md"
OUT = os.path.join(ROOT, "data", "chengdu.json")

LOOP_CODES = {"7"}                      # 7号线 is a closed loop
FALLBACK_COLOR = "#4b5563"

# English line names for the non-numeric codes (numeric -> "Line N")
NAME_EN = {
    "S3": "Line S3 (Ziyang)",
    "R2": "Tram Line Rong 2",
    "RB": "Tram Line Rong 2 (Branch)",
}


def line_id(code):
    return f"cd-l{code}" if code.isdigit() else f"cd-{code.lower()}"


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
            color = FALLBACK_COLOR
        out[c[0]] = (c[1].replace(" ", ""), color)
    return out


def section_code(title):
    """'1 号线' -> '1'; 'S3 资阳线' -> 'S3'; 'R2 有轨电车蓉 2 号线（主线）' -> 'R2'."""
    m = re.match(r"^(\d+|S3|R2|RB)(?:\s|号线|$)", title)
    return m.group(1) if m else None


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
        sid = f"cd-{i:03d}"
        name_to_id[nm] = sid
        station_list.append({
            "id": sid, "name_cn": nm, "name_en": station_en.get(nm, ""),
            "alias": [], "lines": station_lines[nm],
        })

    # demo seed: 天府广场 (1号线 #7 / 2号线 #15)
    tfgc = name_to_id["天府广场"]
    seed = [{
        "station": tfgc, "from_line": "cd-l1", "from_dir": "开往科学城",
        "to_line": "cd-l2", "to_dir": "开往成都行政学院",
        "author_email": "rongcheng@example.com", "author_nick": "蓉城通勤",
        "anon": True, "position_type": "car", "car_number": 4,
        "description": "1号线往科学城方向坐第4节车厢，下车后正对换乘通道，走到2号线往成都行政学院站台比从车头走近不少。",
        "likes": 18, "dislikes": 1, "version": 1, "days_ago": 8,
        "comments": [{"nick": "匿名", "days_ago": 4, "text": "天府广场通道岔路多，跟着换乘指示牌走别拐去出站口。"}],
    }]

    doc = {
        "city": {
            "id": "chengdu", "country_id": "cn",
            "country_cn": "中国", "country_en": "China",
            "name_cn": "成都", "name_en": "Chengdu",
            "alias": ["chengdu", "cd", "成都市", "蓉城", "蓉"],
            "timezone": "Asia/Shanghai",
        },
        "system": {"id": "chengdu-rail-transit", "name_cn": "成都轨道交通",
                   "name_en": "Chengdu Rail Transit"},
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
    print(f"  seed: 天府广场 ({tfgc}) lines={station_lines['天府广场']}")
    for lid, name_cn, seq, dirs in per_line:
        print(f"    {lid:<8} {name_cn:<18} stations={len(seq):>3}  "
              f"{seq[0]}…{seq[-1]}  dirs={'/'.join(dirs)}")


if __name__ == "__main__":
    build()
