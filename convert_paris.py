#!/usr/bin/env python3
r"""
One-off converter: the organised Paris report
(C:\Users\admin\paris-metro-rer.md) -> data/paris.json in the importer's
format (see data_format.md). 16 métro lines (incl. 3bis/7bis) + 5 RER lines.

Paris does NOT use 上行/下行 — the platform indicator shows only "Direction
<terminus>". So a line's directions are its terminus names. Several lines have
more than two directions (M7 & M13 split into 3; RER A–E have 3–7). The app's
data model stores a *list* of directions per line (the direction table has an
ordinal, and the query wizard renders dirs.map(...)), so we keep every
direction faithfully instead of collapsing to two.

  * directions: hardcoded per line from the report's overview + branch notes
    (reliable; the messy "方向 …" headers are not machine-friendly). Rendered
    app-uniform as 开往<terminus>.
  * stations: parsed generically — every "A → B → C" sequence line inside a
    line's section. Blockquote (>) note lines and the H2-delimited "坑"/"在建"
    sections are skipped so airport/note arrows don't leak in. Tokens are
    cleaned of markdown emphasis and parenthetical annotations, e.g.
    "La Défense (Grande Arche)" -> "La Défense",
    "**Noisy-le-Sec（分叉）**"  -> "Noisy-le-Sec".
  * stations de-duped by NAME across all métro + RER lines (same name == same
    physical node -> correct interchange edges). Métro "Châtelet" and RER
    "Châtelet–Les Halles" have different names in the source and stay distinct,
    matching the report.

Run once:  python convert_paris.py
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = r"C:\Users\admin\paris-metro-rer.md"
OUT = os.path.join(ROOT, "data", "paris.json")

# terminus names per line (order = display order). Rendered as 开往<terminus>.
DIRECTIONS = {
    "pa-m1":  ["Château de Vincennes", "La Défense"],
    "pa-m2":  ["Nation", "Porte Dauphine"],
    "pa-m3":  ["Gallieni", "Pont de Levallois–Bécon"],
    "pa-m3b": ["Porte des Lilas", "Gambetta"],
    "pa-m4":  ["Bagneux–Lucie Aubrac", "Porte de Clignancourt"],
    "pa-m5":  ["Place d'Italie", "Bobigny–Pablo Picasso"],
    "pa-m6":  ["Nation", "Charles de Gaulle–Étoile"],
    "pa-m7":  ["La Courneuve–8 Mai 1945", "Mairie d'Ivry", "Villejuif–Louis Aragon"],
    "pa-m7b": ["Pré-Saint-Gervais", "Louis Blanc"],
    "pa-m8":  ["Pointe du Lac", "Balard"],
    "pa-m9":  ["Mairie de Montreuil", "Pont de Sèvres"],
    "pa-m10": ["Boulogne–Pont de Saint-Cloud", "Gare d'Austerlitz"],
    "pa-m11": ["Rosny–Bois-Perrier", "Châtelet"],
    "pa-m12": ["Mairie d'Issy", "Mairie d'Aubervilliers"],
    "pa-m13": ["Châtillon–Montrouge", "Saint-Denis–Université", "Les Courtilles"],
    "pa-m14": ["Aéroport d'Orly", "Saint-Denis–Pleyel"],
    "pa-rer-a": ["Saint-Germain-en-Laye", "Cergy–Le Haut", "Poissy",
                 "Boissy-Saint-Léger", "Marne-la-Vallée–Chessy"],
    "pa-rer-b": ["Aéroport Charles de Gaulle 2–TGV", "Mitry–Claye",
                 "Robinson", "Saint-Rémy-lès-Chevreuse"],
    "pa-rer-c": ["Pontoise", "Montigny–Beauchamp", "Versailles-Château–Rive Gauche",
                 "Saint-Quentin-en-Yvelines", "Saint-Martin-d'Étampes",
                 "Dourdan-la-Forêt", "Massy–Palaiseau"],
    "pa-rer-d": ["Creil", "Orry-la-Ville–Coye", "Goussainville",
                 "Melun", "Malesherbes", "Corbeil-Essonnes"],
    "pa-rer-e": ["Nanterre-la-Folie", "Chelles-Gournay", "Tournan-en-Brie"],
}

# display order + names + colours (Île-de-France official palette)
LINE_META = {
    "pa-m1":  ("1号线", "Métro 1", "#FFCD00"),
    "pa-m2":  ("2号线", "Métro 2", "#003CA6"),
    "pa-m3":  ("3号线", "Métro 3", "#837902"),
    "pa-m3b": ("3bis号线", "Métro 3bis", "#6EC4E8"),
    "pa-m4":  ("4号线", "Métro 4", "#CF009E"),
    "pa-m5":  ("5号线", "Métro 5", "#FF7E2E"),
    "pa-m6":  ("6号线", "Métro 6", "#6ECA97"),
    "pa-m7":  ("7号线", "Métro 7", "#FA9ABA"),
    "pa-m7b": ("7bis号线", "Métro 7bis", "#6ECA97"),
    "pa-m8":  ("8号线", "Métro 8", "#E19BDF"),
    "pa-m9":  ("9号线", "Métro 9", "#B6BD00"),
    "pa-m10": ("10号线", "Métro 10", "#C9910D"),
    "pa-m11": ("11号线", "Métro 11", "#704B1C"),
    "pa-m12": ("12号线", "Métro 12", "#007852"),
    "pa-m13": ("13号线", "Métro 13", "#6EC4E8"),
    "pa-m14": ("14号线", "Métro 14", "#62259D"),
    "pa-rer-a": ("RER A线", "RER A", "#E2231A"),
    "pa-rer-b": ("RER B线", "RER B", "#7BA3DC"),
    "pa-rer-c": ("RER C线", "RER C", "#FFCE00"),
    "pa-rer-d": ("RER D线", "RER D", "#00814F"),
    "pa-rer-e": ("RER E线", "RER E", "#C04191"),
}
LINE_ORDER = list(LINE_META.keys())


def header_to_id(raw):
    """'### 3bis 号线 — …' -> pa-m3b ; '### RER C — SNCF…' -> pa-rer-c ; else None"""
    m = re.match(r"^###\s+RER\s+([A-E])\b", raw)
    if m:
        return f"pa-rer-{m.group(1).lower()}"
    m = re.match(r"^###\s+(\S+)\s*号线", raw)
    if m:
        tok = m.group(1)
        if "bis" in tok:
            return f"pa-m{tok.replace('bis','')}b"
        if tok.isdigit():
            return f"pa-m{tok}"
    return None


def clean_token(tok):
    """One → token -> a station name, or '' if it's annotation/prose."""
    nm = tok.strip().strip("。，、；：←→ ").strip()
    if not nm or re.search(r"[一-鿿]", nm):   # station names are all Latin
        return ""
    return nm


def build():
    with open(SRC, encoding="utf-8") as f:
        text = f.read()

    per_line = {lid: [] for lid in LINE_META}   # lid -> ordered station names (with dups)
    cur = None
    for raw in text.splitlines():
        if raw.startswith("## ") and not raw.startswith("### "):
            cur = None                            # leave 二/三 line sections on H2 (坑/在建)
            continue
        if raw.startswith("### "):
            cur = header_to_id(raw)
            continue
        if cur is None or raw.lstrip().startswith(">") or raw.lstrip().startswith("|"):
            continue
        # strip markdown emphasis + parenthetical annotations at the LINE level so
        # "La Défense (Grande Arche)" -> "La Défense" and pure-note lines like
        # "（Serge Gainsbourg → … 可换 RER E）" collapse to empty (arrow removed).
        line = raw.replace("**", "").replace("*", "")
        line = re.sub(r"（[^）]*）", "", line)
        line = re.sub(r"\([^)]*\)", "", line)
        if "→" not in line:
            continue
        # drop a leading "方向 X：" / "高架段：" label when it precedes the sequence
        if "：" in line and line.index("：") < line.index("→"):
            line = line.split("：", 1)[1]
        for tok in line.split("→"):
            nm = clean_token(tok)
            if nm:
                per_line[cur].append(nm)

    # lines + stations (dedupe by name, keep first-seen order)
    line_recs = []
    stations = {}      # name -> [line_id, …]
    order = []
    for lid in LINE_ORDER:
        name, name_en, color = LINE_META[lid]
        line_recs.append({
            "id": lid, "name": name, "name_en": name_en, "color": color,
            "directions": [f"开往{t}" for t in DIRECTIONS[lid]],
        })
        seen = set()
        for nm in per_line[lid]:
            if nm in seen:
                continue
            seen.add(nm)
            rec = stations.setdefault(nm, [])
            if nm not in order:
                order.append(nm)
            if lid not in rec:
                rec.append(lid)

    name_to_id = {}
    station_list = []
    for i, nm in enumerate(order, 1):
        sid = f"pa-{i:03d}"
        name_to_id[nm] = sid
        station_list.append({
            "id": sid, "name_cn": nm, "name_en": nm,   # French name for both
            "alias": [], "lines": stations[nm],
        })

    # demo seed: Châtelet (M1/M4/M7/M11/M14) — M1 vers Vincennes 换 M14 vers Orly
    ch = name_to_id["Châtelet"]
    seed = [{
        "station": ch, "from_line": "pa-m1", "from_dir": "开往Château de Vincennes",
        "to_line": "pa-m14", "to_dir": "开往Aéroport d'Orly",
        "author_email": "parisien@example.com", "author_nick": "巴黎地铁通",
        "anon": True, "position_type": "car", "car_number": 3,
        "description": "1号线往Château de Vincennes方向坐第3节车厢，下车沿指示牌走向M14站台最近；换乘通道较长，认准 Direction Aéroport d'Orly 的月台。",
        "likes": 27, "dislikes": 4, "version": 1, "days_ago": 14,
        "comments": [{"nick": "匿名", "days_ago": 8, "text": "Châtelet 站内步行很远，跟着 M14 蓝色指示牌走别乱拐。"}],
    }]

    doc = {
        "city": {
            "id": "paris", "country_id": "fr",
            "country_cn": "法国", "country_en": "France",
            "name_cn": "巴黎", "name_en": "Paris",
            "alias": ["Paris", "paris", "巴黎市", "PAR"],
            "timezone": "Europe/Paris",
        },
        "system": {"id": "paris-transit", "name_cn": "巴黎地铁 / RER", "name_en": "Paris Métro & RER"},
        "lines": line_recs,
        "stations": station_list,
        "seed_answers": seed,
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    # overview station counts for a sanity check
    want = {
        "pa-m1": 25, "pa-m2": 25, "pa-m3": 25, "pa-m3b": 4, "pa-m4": 29,
        "pa-m5": 22, "pa-m6": 28, "pa-m7": 38, "pa-m7b": 8, "pa-m8": 38,
        "pa-m9": 37, "pa-m10": 23, "pa-m11": 19, "pa-m12": 31, "pa-m13": 32,
        "pa-m14": 21, "pa-rer-a": 46, "pa-rer-b": 47, "pa-rer-c": 75,
        "pa-rer-d": 59, "pa-rer-e": 25,
    }
    print(f"wrote {OUT}")
    print(f"  lines:    {len(line_recs)}")
    print(f"  stations: {len(station_list)} (deduped by name across métro+RER)")
    inter = [s for s in station_list if len(s["lines"]) > 1]
    print(f"  interchanges (>=2 lines): {len(inter)}")
    print(f"  seed: Châtelet ({ch}) lines={stations['Châtelet']}")
    for lid in LINE_ORDER:
        got = len(set(per_line[lid]))
        w = want.get(lid)
        flag = "" if w == got else f"  <== overview says {w}"
        print(f"    {lid:<9} parsed={got:<3} dirs={len(DIRECTIONS[lid])}{flag}")


if __name__ == "__main__":
    build()
