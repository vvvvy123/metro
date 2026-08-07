#!/usr/bin/env python3
r"""
Converter for the MetroMan-format city reports -> data/<city>.json.

Handles the three reports that share one layout (same generator as the
成都 / 重庆 ones, which have their own scripts):
    C:\Users\admin\武汉地铁线路信息.md   -> data/wuhan.json     (prefix wh-)
    C:\Users\admin\南京地铁线路信息.md   -> data/nanjing.json   (prefix nj-)
    C:\Users\admin\长沙地铁线路信息.md   -> data/changsha.json  (prefix cs-)

One script instead of three near-identical convert_<city>.py files because the
source layout is byte-for-byte the same shape; per-city facts live in CITIES.

What is parsed
    * 路网概况 line table (8 cells) -> line code, Chinese name, hex colour
    * each "### <line>" section's station table (| # | 站名 | English | 换乘 |)
      -> ordered station names + English names
    * directions come from the station table ends, rendered the app-uniform way
      (开往<末站> / 开往<首站>), and are cross-checked against the 运行方向
      bullets the report states; mismatches are printed. None of these three
      networks has a loop line (the reports say so explicitly), so no 内环/外环.
    * stations de-duped by NAME (project-wide convention); English name from
      first sighting, conflicts printed.
    * 换乘站汇总 (2-cell appendix table) is ignored here — the throwaway
      verifier cross-checks it against the generated JSON.

Run:  python convert_metroman.py            # all three
      python convert_metroman.py wuhan      # just one
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
FALLBACK_COLOR = "#4b5563"

CITIES = {
    "wuhan": {
        "src": r"C:\Users\admin\武汉地铁线路信息.md",
        "prefix": "wh",
        "city": {"name_cn": "武汉", "name_en": "Wuhan",
                 "alias": ["wuhan", "wh", "武汉市", "江城"]},
        "system": {"id": "wuhan-metro", "name_cn": "武汉地铁", "name_en": "Wuhan Metro"},
        "name_en": {"YL": "Yangluo Line", "KG": "Optics Valley Skytrain"},
        "seed": {
            "station": "洪山广场", "from_code": "2", "from_end": -1,
            "to_code": "4", "to_end": -1,
            "email": "jiangcheng@example.com", "nick": "江城通勤",
            "car": 5, "likes": 16, "dislikes": 1, "days_ago": 6,
            "desc": "2号线往佛祖岭方向坐第5节车厢，下车后正对4号线换乘楼梯，比走到车头少绕半个站台。",
            "comment": "洪山广场换乘通道上下两层，早高峰人多，建议提前一节车厢等门。",
        },
    },
    "nanjing": {
        "src": r"C:\Users\admin\南京地铁线路信息.md",
        "prefix": "nj",
        "city": {"name_cn": "南京", "name_en": "Nanjing",
                 "alias": ["nanjing", "nj", "南京市", "金陵"]},
        "system": {"id": "nanjing-metro", "name_cn": "南京地铁", "name_en": "Nanjing Metro"},
        "name_en": {"NC": "Ningchu Line"},
        "seed": {
            "station": "新街口", "from_code": "1", "from_end": -1,
            "to_code": "2", "to_end": -1,
            "email": "jinling@example.com", "nick": "金陵通勤",
            "car": 3, "likes": 21, "dislikes": 2, "days_ago": 11,
            "desc": "1号线往中国药科大学方向坐第3节车厢，下车右转直走就是2号线往经天路的换乘口，不用穿整个站厅。",
            "comment": "新街口站出入口特别多，换乘跟着线路色标走，别跟着商场指示牌。",
        },
    },
    "changsha": {
        "src": r"C:\Users\admin\长沙地铁线路信息.md",
        "prefix": "cs",
        "city": {"name_cn": "长沙", "name_en": "Changsha",
                 "alias": ["changsha", "cs", "长沙市", "星城"]},
        "system": {"id": "changsha-metro", "name_cn": "长沙地铁", "name_en": "Changsha Metro"},
        "name_en": {},
        "seed": {
            "station": "五一广场", "from_code": "1", "from_end": -1,
            "to_code": "2", "to_end": -1,
            "email": "xingcheng@example.com", "nick": "星城通勤",
            "car": 4, "likes": 13, "dislikes": 1, "days_ago": 5,
            "desc": "1号线往尚双塘方向坐第4节车厢，下车即到2号线换乘扶梯口，走过去不到一分钟。",
            "comment": "五一广场是长沙最挤的换乘站，周末下午人流很大，留点余量。",
        },
    },
}


def cells(row):
    return [c.strip() for c in row.strip().strip("|").split("|")]


def norm(s):
    """Compare-only normalisation: full-width parens/spaces vs half-width."""
    return (s.replace("（", "(").replace("）", ")")
             .replace("　", "").replace(" ", ""))


def parse_overview(text):
    """路网概况 line table -> {code: (chinese name, '#rrggbb')} in doc order."""
    out = {}
    for raw in text.splitlines():
        if raw.startswith("###"):
            break
        if not raw.lstrip().startswith("|"):
            continue
        c = cells(raw)
        if len(c) != 8 or not re.fullmatch(r"[0-9A-Za-z]+", c[0]):
            continue
        color = c[3].strip("`")
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            color = FALLBACK_COLOR          # e.g. 光谷空轨 / 宁滁线: no wiki colour
        out[c[0]] = (re.sub(r"\*\*|\s", "", c[1]), color)
    return out


def section_code(title, known):
    """'1 号线'->1; 'S1 号线（机场线）'->S1; 'S2 磁浮快线'->S2; '阳逻线（YL）'->YL."""
    for pat in (r"^([A-Za-z]?\d+)\s*号线", r"^([A-Za-z]\d+)\s"):
        m = re.match(pat, title)
        if m and m.group(1) in known:
            return m.group(1)
    m = re.search(r"（([A-Za-z]+)）", title)
    if m and m.group(1) in known:
        return m.group(1)
    return None


def parse_sections(text, known):
    """-> [(code, [(name, english)], [stated direction labels])] in doc order."""
    secs, cur = [], None
    for raw in text.splitlines():
        h = re.match(r"^###\s+(.+?)\s*$", raw)
        if h:
            code = section_code(h.group(1), known)
            if code is None:
                print(f"  ! section '{h.group(1)}' has no code in the line table, skipped")
                cur = None
            else:
                cur = (code, [], [])
                secs.append(cur)
            continue
        if cur is None:
            continue
        if raw.lstrip().startswith("- **运行方向"):
            body = raw.split("：", 1)[-1].strip()
            # drop the trailing "（起点 → 终点，方位）" that restates the sequence
            cur[2].append(re.sub(r"（[^（）]*→[^（）]*）\s*$", "", body).strip())
            continue
        if not raw.lstrip().startswith("|"):
            continue
        c = cells(raw)
        if len(c) == 4 and c[0].isdigit():
            cur[1].append((c[1], c[2] if c[2] != "—" else ""))
    return secs


def build(city_id, cfg):
    with open(cfg["src"], encoding="utf-8") as f:
        text = f.read()
    pfx = cfg["prefix"]
    overview = parse_overview(text)
    sections = parse_sections(text, set(overview))

    def line_id(code):
        return f"{pfx}-l{code}" if code.isdigit() else f"{pfx}-{code.lower()}"

    line_recs, per_line = [], []
    station_lines, station_en, order = {}, {}, []
    seqs = {}

    for code, rows, stated in sections:
        if not rows:
            print(f"  ! line {code}: no station table, skipped")
            continue
        lid = line_id(code)
        seq = [nm for nm, _ in rows]
        seqs[code] = seq
        dirs = [f"开往{seq[-1]}", f"开往{seq[0]}"]
        if stated and [norm(s) for s in stated[:2]] != [norm(d) for d in dirs]:
            print(f"  ! line {code}: stated {stated[:2]} != derived {dirs}")

        name_cn, color = overview.get(code, (code, FALLBACK_COLOR))
        if code.isdigit():
            name_en = f"Line {code}"
        elif re.fullmatch(r"[A-Za-z]\d+", code):
            name_en = f"Line {code}"
        else:
            name_en = cfg["name_en"].get(code, name_cn)
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

    name_to_id, station_list = {}, []
    for i, nm in enumerate(order, 1):
        sid = f"{pfx}-{i:03d}"
        name_to_id[nm] = sid
        station_list.append({"id": sid, "name_cn": nm, "name_en": station_en.get(nm, ""),
                             "alias": [], "lines": station_lines[nm]})

    s = cfg["seed"]
    st_id = name_to_id[s["station"]]
    seed = [{
        "station": st_id,
        "from_line": line_id(s["from_code"]), "from_dir": f"开往{seqs[s['from_code']][s['from_end']]}",
        "to_line": line_id(s["to_code"]), "to_dir": f"开往{seqs[s['to_code']][s['to_end']]}",
        "author_email": s["email"], "author_nick": s["nick"],
        "anon": True, "position_type": "car", "car_number": s["car"],
        "description": s["desc"], "likes": s["likes"], "dislikes": s["dislikes"],
        "version": 1, "days_ago": s["days_ago"],
        "comments": [{"nick": "匿名", "days_ago": max(1, s["days_ago"] - 2), "text": s["comment"]}],
    }]

    doc = {
        "city": {"id": city_id, "country_id": "cn", "country_cn": "中国", "country_en": "China",
                 "timezone": "Asia/Shanghai", **cfg["city"]},
        "system": cfg["system"],
        "lines": line_recs,
        "stations": station_list,
        "seed_answers": seed,
    }
    out = os.path.join(ROOT, "data", f"{city_id}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    inter = [x for x in station_list if len(x["lines"]) > 1]
    print(f"wrote {out}")
    print(f"  lines: {len(line_recs)}  stations: {len(station_list)} (deduped by name)  "
          f"rows: {sum(len(q) for _, _, q, _ in per_line)}  interchanges: {len(inter)}")
    print(f"  seed: {s['station']} ({st_id}) lines={station_lines[s['station']]} "
          f"{seed[0]['from_dir']} → {seed[0]['to_dir']}")
    for lid, name_cn, seq, dirs in per_line:
        print(f"    {lid:<9} {name_cn:<16} stations={len(seq):>3}  {seq[0]}…{seq[-1]}  "
              f"dirs={'/'.join(dirs)}")


if __name__ == "__main__":
    wanted = sys.argv[1:] or list(CITIES)
    for cid in wanted:
        if cid not in CITIES:
            sys.exit(f"unknown city '{cid}' — known: {', '.join(CITIES)}")
        print(f"== {cid}")
        build(cid, CITIES[cid])
