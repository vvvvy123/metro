#!/usr/bin/env python3
r"""
MetroMan-format converter that takes the report's 运行方向 bullets as authoritative.

    metrodata/台北捷运线路信息.md      -> data/taipei.json     (prefix tp-)
    metrodata/香港地铁线路信息.md      -> data/hongkong.json   (prefix hk-)
    metrodata/天津地铁线路信息.md      -> data/tianjin.json    (prefix tj-)
    metrodata/大连地铁线路信息.md      -> data/dalian.json     (prefix dl-)
    metrodata/合肥轨道交通线路信息.md  -> data/hefei.json      (prefix hf-)
    metrodata/长春轨道交通线路信息.md  -> data/changchun.json  (prefix cc-)

Written for the branch networks (first three), but it is the better choice for
plain ones too, so 大连/合肥/长春 are here rather than in convert_metroman.py:
every line still gets the hard stated-vs-table-ends check below, where the
sibling script only prints a warning. 大连/合肥/长春 have no branch, no loop and
no through-running, so all 20 of their lines take the 2-direction path.

Why this is not convert_metroman.py
    That script DERIVES the two directions from the station table's ends
    (开往<末站> / 开往<首站>) and merely prints a warning when the report's
    运行方向 bullets disagree. That is wrong for all three cities here:

        台北 O 中和新蘆線   3 directions (大橋頭 splits 蘆洲 / 迴龍)
        台北 V 淡海輕軌     3 directions (濱海沙崙 splits 崁頂 / 漁人碼頭)
        香港 TK 將軍澳綫    3 directions (將軍澳 splits 寶琳 / 康城)
        香港 ER 東鐵綫      3 directions (上水 splits 羅湖 / 落馬洲)
        天津 4 号线         4 directions (西站—东南角 not yet connected, so the
                                         line runs as two independent segments)

    MetroMan flattens every branch into one linear table, so the ends of that
    table cannot express the real service pattern. Here the bullets are the
    source of truth and a direction list may hold any N, which is what the
    schema has always allowed (see data_format.md).

    For the 2-direction lines the derived pair is still computed and compared
    as a SET — a genuine safety net, since a typo in a bullet would otherwise
    sail through. 天津 1/2 号线 legitimately list the two in the opposite order
    from the table ends; order is reported, not corrected.

Other differences from the sibling script, all forced by these sources
    * section -> code: `（BR）` (台北), `（Island Line，I）` (香港, code last),
      `（Y，第一階段）` (台北, code first), `1 号线` / `6 号线二期` / `Z4 线`
      (天津, no code in the title at all). Resolution is code-in-parens, then
      exact title match, then title-before-parens — and the result must be a
      BIJECTION with the 线路清单 table, so a title this script fails to
      recognise is a hard error instead of a silently dropped line.
    * 天津 `6Ⅱ` (U+2161 ROMAN NUMERAL TWO) would be rejected by the sibling's
      `[0-9A-Za-z]+` code filter, dropping 9 stations and 渌水道's interchange.
      Line codes are therefore not charset-filtered at all; the 8-cell row is
      recognised by its `#rrggbb` colour cell. ID_OVERRIDE keeps the id ASCII.
    * 换乘 cells here can read `—（站外換乘 R、BL）` (no in-station transfer) or
      `Y ※` (footnote marker). Parsed for cross-checking only — station links
      come from name de-duplication, as everywhere else in this project.
    * 台北/香港 keep TRADITIONAL characters (the local official spelling), so
      台北車站(A) / 三重(A) / 新北產業園區(A) / 頂埔(LB) / 板橋(Y) stay separate
      stations: the suffixes are part of the official names and those four are
      out-of-station transfers between different operators' station bodies.
      Same convention as 重庆's 歇台子(1)/(5).

Run:  python convert_metroman_branch.py           # all three
      python convert_metroman_branch.py taipei    # just one
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
FALLBACK_COLOR = "#4b5563"

# Line ids are URL path segments and end up inside every direction id, so keep
# them ASCII even when the official code is not.
ID_OVERRIDE = {"tianjin": {"6Ⅱ": "tj-l6ii"}}

CITIES = {
    "taipei": {
        "src": os.path.join(ROOT, "metrodata", "台北捷运线路信息.md"),
        "prefix": "tp",
        "city": {"name_cn": "台北", "name_en": "Taipei",
                 "alias": ["taipei", "tp", "臺北", "台北市", "臺北市"],
                 "timezone": "Asia/Taipei"},
        "system": {"id": "taipei-metro", "name_cn": "台北捷運", "name_en": "Taipei Metro"},
        # No English in the section titles, so spell all 12 out.
        "name_en": {
            "BR": "Wenhu Line", "R": "Tamsui-Xinyi Line", "RB": "Xinbeitou Branch Line",
            "G": "Songshan-Xindian Line", "GB": "Xiaobitan Branch Line",
            "O": "Zhonghe-Xinlu Line", "BL": "Bannan Line", "Y": "Circular Line",
            "A": "Taoyuan Airport MRT", "V": "Danhai Light Rail",
            "K": "Ankeng Light Rail", "LB": "Sanying Line",
        },
        "seed": {
            "station": "台北車站",
            "from_code": "R", "from_dir": "開往淡水",
            "to_code": "BL", "to_dir": "開往南港展覽館",
            "email": "taipei@example.com", "nick": "台北通勤",
            "car": 3, "likes": 18, "dislikes": 1, "days_ago": 7,
            "desc": "淡水信義線往淡水方向坐第3節車廂，下車後往前走的樓梯直接下到板南線往南港展覽館的月台，不用繞到站廳層。",
            "comment": "台北車站地下街出口很多，換乘跟著月台編號走比跟著出口指示牌快。",
        },
    },
    "hongkong": {
        "src": os.path.join(ROOT, "metrodata", "香港地铁线路信息.md"),
        "prefix": "hk",
        "city": {"name_cn": "香港", "name_en": "Hong Kong",
                 "alias": ["hongkong", "hong kong", "hk", "香港特別行政區", "香港特别行政区"],
                 "timezone": "Asia/Hong_Kong"},
        "system": {"id": "hongkong-mtr", "name_cn": "港鐵", "name_en": "MTR"},
        "name_en": {},          # every section title carries the English name
        "seed": {
            "station": "金鐘",
            "from_code": "I", "from_dir": "開往柴灣",
            "to_code": "TW", "to_dir": "開往荃灣",
            "email": "hongkong@example.com", "nick": "港鐵通勤",
            "car": 4, "likes": 22, "dislikes": 2, "days_ago": 9,
            "desc": "港島綫往柴灣方向坐第4節車廂，下車即對正上荃灣綫月台的扶手電梯，比走到車頭少繞半個月台。",
            "comment": "金鐘四線交匯，荃灣綫月台早上很擠，提早一節車廂等會好上車。",
        },
    },
    "tianjin": {
        "src": os.path.join(ROOT, "metrodata", "天津地铁线路信息.md"),
        "prefix": "tj",
        "city": {"name_cn": "天津", "name_en": "Tianjin",
                 "alias": ["tianjin", "tj", "天津市", "津门"],
                 "timezone": "Asia/Shanghai"},
        "system": {"id": "tianjin-metro", "name_cn": "天津地铁", "name_en": "Tianjin Metro"},
        "name_en": {"6Ⅱ": "Line 6 Phase II", "JJ": "Jinjing Line"},
        "seed": {
            "station": "营口道",
            "from_code": "1", "from_dir": "开往双桥河",
            "to_code": "3", "to_dir": "开往小淀",
            "email": "tianjin@example.com", "nick": "天津通勤",
            "car": 2, "likes": 15, "dislikes": 1, "days_ago": 5,
            "desc": "1号线往双桥河方向坐第2节车厢，下车后左手边就是3号线往小淀的换乘通道，不用穿过整个站厅。",
            "comment": "营口道是天津换乘量最大的站，滨江道商圈出口人多，换乘走里侧通道快一些。",
        },
    },
    "dalian": {
        "src": os.path.join(ROOT, "metrodata", "大连地铁线路信息.md"),
        "prefix": "dl",
        "city": {"name_cn": "大连", "name_en": "Dalian",
                 "alias": ["dalian", "dl", "大连市"],
                 "timezone": "Asia/Shanghai"},
        "system": {"id": "dalian-metro", "name_cn": "大连地铁", "name_en": "Dalian Metro"},
        # 大连's English station names are translated rather than transliterated
        # (开发区 = Development Zone, 通世泰 = Tostem); the line names are not,
        # so the "Line <n>" fallback is correct for all six.
        "name_en": {},
        "seed": {
            "station": "西安路",
            "from_code": "1", "from_dir": "开往河口",
            "to_code": "2", "to_dir": "开往海之韵",
            "email": "dalian@example.com", "nick": "大连通勤",
            "car": 2, "likes": 14, "dislikes": 1, "days_ago": 6,
            "desc": "1号线往河口方向坐第2节车厢，下车后右手边的楼梯直接上到2号线往海之韵的月台，比走站厅层换乘少绕一半路。",
            "comment": "西安路站连着几个商场，早高峰站厅层人特别多，走月台端头的通道会快不少。",
        },
    },
    "hefei": {
        "src": os.path.join(ROOT, "metrodata", "合肥轨道交通线路信息.md"),
        "prefix": "hf",
        "city": {"name_cn": "合肥", "name_en": "Hefei",
                 "alias": ["hefei", "hf", "合肥市", "庐州"],
                 "timezone": "Asia/Shanghai"},
        "system": {"id": "hefei-rail-transit", "name_cn": "合肥轨道交通",
                   "name_en": "Hefei Rail Transit"},
        "name_en": {},
        "seed": {
            "station": "合肥南站",
            "from_code": "1", "from_dir": "开往九联圩",
            "to_code": "5", "to_dir": "开往贵阳路",
            "email": "hefei@example.com", "nick": "合肥通勤",
            "car": 4, "likes": 16, "dislikes": 1, "days_ago": 4,
            "desc": "1号线往九联圩方向坐第4节车厢，下车后直走就是5号线往贵阳路的换乘通道，不用上到站厅再折回来。",
            "comment": "合肥南站是全网唯一的三线换乘站，1/4/5 三条线的通道分得很开，看清导向牌上的线路号再走。",
        },
    },
    "changchun": {
        "src": os.path.join(ROOT, "metrodata", "长春轨道交通线路信息.md"),
        "prefix": "cc",
        "city": {"name_cn": "长春", "name_en": "Changchun",
                 "alias": ["changchun", "cc", "长春市"],
                 "timezone": "Asia/Shanghai"},
        "system": {"id": "changchun-rail-transit", "name_cn": "长春轨道交通",
                   "name_en": "Changchun Rail Transit"},
        # 地铁 (1/2/6) and 轻轨 (3/4/8) share one network, one fare and one
        # numbering scheme, so the 制式 column is metadata, not a line-name part.
        "name_en": {},
        "seed": {
            "station": "长春站",
            "from_code": "1", "from_dir": "开往红嘴子",
            "to_code": "3", "to_dir": "开往长影世纪城",
            "email": "changchun@example.com", "nick": "长春通勤",
            "car": 3, "likes": 13, "dislikes": 1, "days_ago": 8,
            "desc": "1号线往红嘴子方向坐第3节车厢，下车后跟着轻轨指示牌走，换3号线往长影世纪城不用出站厅绕行。",
            "comment": "长春站的1号线和3号线一个是地铁一个是轻轨，站台高差比较大，带箱子的话找电梯会省力。",
        },
    },
}


# --------------------------------------------------------------------------
# Markdown parsing
# --------------------------------------------------------------------------
def cells(row):
    return [c.strip() for c in row.strip().strip("|").split("|")]


def norm(s):
    """Compare-only normalisation: full-width parens/spaces vs half-width."""
    return (s.replace("（", "(").replace("）", ")")
             .replace("　", "").replace(" ", ""))


def parse_overview(text):
    """线路清单 table -> {code: (chinese name, '#rrggbb')} in document order.

    Rows are recognised by CONTAINING a `#rrggbb` cell rather than by the shape
    of the code, because 天津's `6Ⅱ` is not [0-9A-Za-z]; and the column count is
    not fixed, because 长春 carries an extra 制式 column for its 地铁/轻轨 mix
    (9 cells vs 8 elsewhere). Requiring the colour at index >= 2 keeps 标识 and
    线路 in front of it. Every other table in these reports has 2, 3 or 4 cells
    and no colour cell, so this stays unambiguous. The 预留色值 notes are
    blockquote prose, not table rows, so they are excluded by the `|` test.
    """
    out = {}
    for raw in text.splitlines():
        if not raw.lstrip().startswith("|"):
            continue
        c = cells(raw)
        color = next((x.strip("`") for x in c[2:]
                      if re.fullmatch(r"#[0-9A-Fa-f]{6}", x.strip("`"))), None)
        if color is None:
            continue
        code = re.sub(r"\*\*|\s", "", c[0])
        if code in out:
            raise SystemExit(f"duplicate line code '{code}' in 线路清单")
        out[code] = (re.sub(r"\*\*|\s", "", c[1]), color)
    return out


def paren_tokens(title):
    """'港島綫（Island Line，I）' -> ['Island Line', 'I']; '文湖線（BR）' -> ['BR']."""
    m = re.search(r"（([^（）]*)）", title)
    if not m:
        return []
    return [t.strip() for t in re.split(r"[，,、]", m.group(1)) if t.strip()]


def title_code(title, overview):
    """Resolve a '### ...' title to its 线路清单 code.

    Three rules, in order, because the three reports label sections differently:
      1. a code inside the parens   —  文湖線（BR） / 港島綫（Island Line，I）
                                       / 環狀線（Y，第一階段） / 津静线（JJ）
      2. the whole title equals a 线路清单 name  —  9 号线（津滨轻轨）
      3. the part before the parens does  —  1 号线 / 6 号线二期（…8 号线） / Z4 线
    Rule 3 is EXACT equality, not a prefix test: '6 号线' and '6 号线二期' are
    two different lines that share a prefix, and prefix matching would merge
    them and lose 9 stations plus 渌水道's interchange.
    """
    for tok in paren_tokens(title):
        if tok in overview:
            return tok
    by_name = {norm(n): code for code, (n, _) in overview.items()}
    for cand in (title, re.sub(r"（[^（）]*）\s*$", "", title)):
        code = by_name.get(norm(cand))
        if code:
            return code
    return None


def header_name_en(title, code):
    """'港島綫（Island Line，I）' -> 'Island Line'. Chinese annotations rejected."""
    for tok in paren_tokens(title):
        if tok != code and re.fullmatch(r"[A-Za-z][A-Za-z .\-']*", tok):
            return tok
    return None


def parse_transfer_cell(cell):
    """换乘 cell -> set of line codes reachable INSIDE the station.

    '—' / '—（站外換乘 R、BL）' -> empty: an out-of-station walk between two
    differently-named stations is not an in-station interchange, and the whole
    point of those four 台北 rows is that they must NOT be merged.
    'Y ※' -> {'Y'} (footnote marker stripped).
    """
    cell = cell.strip()
    if not cell or cell.startswith("—"):
        return set()
    return {t.strip(" ※*") for t in re.split(r"[、,，]", cell) if t.strip(" ※*")}


def parse_sections(text, overview):
    """-> [(code, title, [(name, english, {transfer codes})], [stated dirs])]."""
    secs, cur, seen = [], None, {}
    for raw in text.splitlines():
        h = re.match(r"^###\s+(.+?)\s*$", raw)
        if h:
            title = h.group(1)
            code = title_code(title, overview)
            cur = None
            if code is None:
                continue                      # 线路清单 / 数据校正说明 / 换乘站汇总
            if code in seen:
                raise SystemExit(
                    f"two sections resolve to line '{code}': '{seen[code]}' and '{title}'")
            seen[code] = title
            cur = (code, title, [], [])
            secs.append(cur)
            continue
        if cur is None:
            continue
        # Two bullet layouts in the wild, both accepted:
        #   flat   (台北/香港/天津)  `- **運行方向①**：開往南港展覽館`
        #   nested (大连/合肥/长春)  `- **运行方向**：` then `  - **方向①**：开往河口`
        # The nested parent has an EMPTY body, so keying off a non-empty body is
        # what separates the header from a real direction — matching only
        # `运行方向` would append "" for the parent and miss all the children.
        if re.match(r"^-\s*\*\*(?:[运運]行)?方向", raw.lstrip()):
            body = raw.split("：", 1)[-1].strip()
            # drop a trailing "（起点 → 终点，方位）" that only restates the sequence;
            # 天津's "（北段）/（南段）" has no arrow and is deliberately kept.
            body = re.sub(r"（[^（）]*→[^（）]*）\s*$", "", body).strip()
            if body:
                cur[3].append(body)
            continue
        if not raw.lstrip().startswith("|"):
            continue
        c = cells(raw)
        if len(c) == 4 and c[0].isdigit():
            cur[2].append((c[1], c[2] if c[2] != "—" else "",
                           parse_transfer_cell(c[3])))

    missing = set(overview) - seen.keys()
    if missing:                               # never silently drop a line
        raise SystemExit(f"no section found for line(s): {', '.join(sorted(missing))}")
    return secs


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------
def build(city_id, cfg):
    with open(cfg["src"], encoding="utf-8") as f:
        text = f.read()
    pfx = cfg["prefix"]
    overview = parse_overview(text)
    sections = parse_sections(text, overview)
    override = ID_OVERRIDE.get(city_id, {})
    warnings = []

    def line_id(code):
        if code in override:
            return override[code]
        return f"{pfx}-l{code}" if code.isdigit() else f"{pfx}-{code.lower()}"

    line_recs, per_line = [], []
    station_lines, station_en, order = {}, {}, []
    dirs_by_code = {}

    for code, title, rows, stated in sections:
        if not rows:
            raise SystemExit(f"line {code}: no station table")
        lid = line_id(code)
        seq = [nm for nm, _, _ in rows]
        names = set(seq)

        if not stated:
            raise SystemExit(f"line {code}: no 运行方向 bullets")
        # Every stated terminus must be a station of this line. The（北段）/（南段）
        # segment annotation is not part of the station name, so strip it for the
        # membership test only — 台北's 台北車站(A) / 頂埔(LB) use HALF-width
        # parens and are real name suffixes, which is why only （） is stripped.
        for d in stated:
            term = re.sub(r"^开往|^開往", "", re.sub(r"（[^（）]*）\s*$", "", d))
            if term not in names:
                raise SystemExit(
                    f"line {code}: direction '{d}' ends at '{term}', not a station on it")
        if len(set(stated)) != len(stated):
            raise SystemExit(f"line {code}: duplicate direction {stated}")

        # Safety net for the ordinary case: a 2-direction line must still be the
        # two ends of the table, or a bullet has a typo. Compared as a set —
        # 天津 1/2 号线 list them in the opposite order on purpose.
        prefix_cn = "開往" if stated[0].startswith("開往") else "开往"
        derived = [f"{prefix_cn}{seq[-1]}", f"{prefix_cn}{seq[0]}"]
        if len(stated) == 2:
            if {norm(s) for s in stated} != {norm(d) for d in derived}:
                raise SystemExit(f"line {code}: stated {stated} != table ends {derived}")
            if [norm(s) for s in stated] != [norm(d) for d in derived]:
                warnings.append(f"line {code}: directions listed in the reverse "
                                f"order from the table ends ({' / '.join(stated)})")
        else:
            warnings.append(f"line {code}: branch line, {len(stated)} directions "
                            f"({' / '.join(stated)})")

        name_cn, color = overview[code]
        name_en = (cfg["name_en"].get(code) or header_name_en(title, code)
                   or (f"Line {code}" if re.fullmatch(r"[A-Za-z]?\d+", code) else name_cn))
        dirs_by_code[code] = list(stated)
        line_recs.append({"id": lid, "name": name_cn, "name_en": name_en,
                          "color": color, "directions": list(stated)})
        per_line.append((lid, code, name_cn, seq, list(stated)))

        for nm, en, _ in rows:
            if nm not in station_lines:
                station_lines[nm] = []
                order.append(nm)
            if en:
                if station_en.get(nm, en) != en:
                    warnings.append(f"station {nm}: English '{station_en[nm]}' vs '{en}'")
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
    from_lid, to_lid = line_id(s["from_code"]), line_id(s["to_code"])
    for lid_, code_, d_ in ((from_lid, s["from_code"], s["from_dir"]),
                            (to_lid, s["to_code"], s["to_dir"])):
        if d_ not in dirs_by_code[code_]:
            raise SystemExit(f"seed: '{d_}' is not a direction of line {code_}")
        if lid_ not in station_lines[s["station"]]:
            raise SystemExit(f"seed: {lid_} does not serve {s['station']}")
    seed = [{
        "station": st_id,
        "from_line": from_lid, "from_dir": s["from_dir"],
        "to_line": to_lid, "to_dir": s["to_dir"],
        "author_email": s["email"], "author_nick": s["nick"],
        "anon": True, "position_type": "car", "car_number": s["car"],
        "description": s["desc"], "likes": s["likes"], "dislikes": s["dislikes"],
        "version": 1, "days_ago": s["days_ago"],
        "comments": [{"nick": "匿名", "days_ago": max(1, s["days_ago"] - 2),
                      "text": s["comment"]}],
    }]

    city = {"id": city_id, "country_id": "cn", "country_cn": "中国",
            "country_en": "China", **cfg["city"]}
    doc = {"city": city, "system": cfg["system"], "lines": line_recs,
           "stations": station_list, "seed_answers": seed}
    out = os.path.join(ROOT, "data", f"{city_id}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    inter = [x for x in station_list if len(x["lines"]) > 1]
    n_rows = sum(len(q) for _, _, _, q, _ in per_line)
    print(f"wrote {out}")
    print(f"  lines: {len(line_recs)}  stations: {len(station_list)} (deduped by name)  "
          f"rows: {n_rows}  interchanges: {len(inter)}  "
          f"directions: {sum(len(d) for _, _, _, _, d in per_line)}")
    print(f"  seed: {s['station']} ({st_id}) lines={station_lines[s['station']]} "
          f"{s['from_dir']} → {s['to_dir']}")
    for lid, code, name_cn, seq, dirs in per_line:
        print(f"    {lid:<9} {code:<4} {name_cn:<12} stations={len(seq):>3}  "
              f"{seq[0]}…{seq[-1]}  dirs={' / '.join(dirs)}")
    for w in warnings:
        print(f"  ! {w}")


if __name__ == "__main__":
    wanted = sys.argv[1:] or list(CITIES)
    for cid in wanted:
        if cid not in CITIES:
            sys.exit(f"unknown city '{cid}' — known: {', '.join(CITIES)}")
        print(f"== {cid}")
        build(cid, CITIES[cid])
