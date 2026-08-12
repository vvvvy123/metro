#!/usr/bin/env python3
"""
Metro Transfer — static site data generator (stdlib only, no pip install).

Turns data/*.json into the static JSON that web/index.html fetches when there
is no backend, so the whole app can be hosted as plain files (read-only:
votes/comments/uploads live in the visitor's own localStorage).

    python gen_static.py            # write web/data/*.json
    python gen_static.py --check    # report staleness only, exit 1 if stale

Design notes:
    * Zero third-party dependencies, like everything else here.
    * Reuses import_city.validate() and import_city.dir_id() rather than
      reimplementing them, so direction ids are byte-identical to the ones the
      backend puts in metro.db (bj-l2-内环, pa-m1-开往château-de-vincennes).
      Importing import_city has no side effects — it never opens the DB.
    * Output shape matches the frontend's in-memory `OFF` store, NOT the API's
      response shape, because the static loader feeds `OFF` directly.
    * Deterministic: the `gen` stamp is a content hash of data/*.json, not a
      timestamp, so --check is meaningful and reruns are byte-stable.
"""
import argparse
import hashlib
import io
import json
import os
import sys

import import_city                      # for validate() / dir_id() only
from import_city import DataError, dir_id, validate

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "web", "data")

# Chinese in tracebacks would die on the Windows console's GBK codec.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def city_ids():
    return sorted(f[:-5] for f in os.listdir(DATA_DIR) if f.endswith(".json"))


def content_stamp(ids):
    """Hash of every source file, so `gen` changes only when the data does."""
    h = hashlib.sha256()
    for cid in ids:                      # ids are sorted -> stable
        with open(os.path.join(DATA_DIR, f"{cid}.json"), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12]


def build_city(cid):
    """One data/<city>.json -> the payload web/data/city-<city>.json holds."""
    with open(os.path.join(DATA_DIR, f"{cid}.json"), encoding="utf-8") as f:
        doc = json.load(f)
    validate(doc, cid)                   # same failure mode as the importer
    c = doc["city"]

    # `city` is deliberately NOT stamped onto every line/station record — the
    # loader injects it (3353 stations x '"city":"..."' would cost ~60 KB).
    lines = []
    for l in doc["lines"]:
        lines.append({
            "id": l["id"],
            "name": l["name"],
            "color": l.get("color", "#4b5563"),   # import_city.py:168
            "dirs": [{"id": dir_id(l["id"], d), "name": d}
                     for d in l.get("directions", [])],
        })
    lines.sort(key=lambda x: x["name"])           # matches server.py:249 ORDER BY name

    stations = []
    for st in doc["stations"]:
        row = {"id": st["id"], "name_cn": st["name_cn"], "lines": list(st["lines"])}
        # name_en is empty for 1416 of 3353 stations; omit the key instead of
        # shipping '"name_en":""'. The loader defaults it. alias is [] for ALL
        # stations, so it is never emitted (offline code already guards it).
        if st.get("name_en"):
            row["name_en"] = st["name_en"]
        stations.append(row)
    stations.sort(key=lambda x: x["name_cn"])     # matches server.py:240 ORDER BY name_cn

    answers = []
    for i, a in enumerate(doc.get("seed_answers", []), 1):
        answers.append({
            # never collides with a real device id ("dev-..."), so the loader can
            # use it as `owner` and is_mine stays false / no delete affordance.
            "id": f"seed-{cid}-{i}",
            "station": a["station"],
            "from_line": a["from_line"],
            "from_dir": dir_id(a["from_line"], a["from_dir"]),   # import_city.py:191
            "to_line": a["to_line"],
            "to_dir": dir_id(a["to_line"], a["to_dir"]),         # import_city.py:192
            "author": a["author_nick"],
            "is_anon": bool(a.get("anon", True)),
            "position_type": a["position_type"],
            "car_number": a.get("car_number"),
            "custom_text": a.get("custom_text", ""),
            "description": a.get("description", ""),
            "version": a.get("version", 1),
            # kept RELATIVE on purpose: the loader resolves it with the existing
            # isoDaysAgo(), so the ranking score does not drift with build date.
            "days_ago": a.get("days_ago", 0),
            "likes": a.get("likes", 0),
            "dislikes": a.get("dislikes", 0),
            "comments": [{"user": cm.get("nick", "匿名"),
                          "days_ago": cm.get("days_ago", 0),
                          "text": cm["text"]} for cm in a.get("comments", [])],
        })

    manifest_row = {"id": c["id"], "name_cn": c["name_cn"], "name_en": c["name_en"],
                    "alias": c.get("alias", [])}         # searchCities matches on alias
    payload = {"city": manifest_row, "lines": lines,
               "stations": stations, "answers": answers}
    return manifest_row, payload


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--check", action="store_true",
                    help="report only, do not write; exit 1 if stale")
    args = ap.parse_args()

    ids = city_ids()
    if not ids:
        sys.exit(f"no city json found in {DATA_DIR}")

    files, rows = {}, []
    n_lines = n_dirs = n_stations = n_answers = 0
    for cid in ids:
        row, payload = build_city(cid)
        rows.append(row)
        files[f"city-{cid}.json"] = dumps(payload)
        n_lines += len(payload["lines"])
        n_dirs += sum(len(l["dirs"]) for l in payload["lines"])
        n_stations += len(payload["stations"])
        n_answers += len(payload["answers"])

    files["cities.json"] = dumps({"gen": content_stamp(ids), "cities": rows})

    total = sum(len(v.encode("utf-8")) for v in files.values())
    print(f"  cities   : {len(ids)} ({', '.join(ids)})")
    print(f"  lines    : {n_lines}   directions: {n_dirs}")
    print(f"  stations : {n_stations}   seed answers: {n_answers}")
    print(f"  payload  : {total/1024:.1f} KB across {len(files)} files "
          f"(largest {max(len(v.encode('utf-8')) for v in files.values())/1024:.1f} KB)")

    if args.check:
        stale = []
        for name, text in sorted(files.items()):
            path = os.path.join(OUT_DIR, name)
            if not os.path.exists(path):
                stale.append(f"{name} (missing)")
                continue
            with open(path, encoding="utf-8") as f:
                if f.read() != text:
                    stale.append(name)
        if stale:
            print(f"  STALE    : {len(stale)} file(s) -> {', '.join(stale)}")
            sys.exit(1)
        print("  on disk  : up to date")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    for name, text in sorted(files.items()):
        with open(os.path.join(OUT_DIR, name), "w",
                  encoding="utf-8", newline="") as f:      # UTF-8, no BOM
            f.write(text)
    print(f"wrote {len(files)} files to {OUT_DIR}")


if __name__ == "__main__":
    try:
        main()
    except DataError as e:
        sys.exit(f"data error: {e}")
