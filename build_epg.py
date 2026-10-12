#!/usr/bin/env python3
"""Bygger en sammanslagen XMLTV-fil utifrån mapping.json. Behöver inga inloggningsuppgifter.

För varje kanal väljs den källa som räcker längst framåt i tiden.
Användning:  python3 build_epg.py [--out docs/epg.xml.gz]
"""
import argparse
import gzip
import json
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from epgcommon import ROOT, SOURCES, download


def parse_time(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y%m%d%H%M%S %z")
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "epg.xml.gz"))
    args = ap.parse_args()

    mapping = json.loads((ROOT / "mapping.json").read_text())
    wanted = defaultdict(set)                    # källa -> källans kanal-id som behövs
    for t in mapping.values():
        for key, sid in t["sources"]:
            wanted[key].add(sid)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
    progs = defaultdict(list)                    # (källa, källans id) -> [programme]
    with tempfile.TemporaryDirectory() as tmp:
        for src in SOURCES:
            path = Path(tmp) / f"{src['key']}.xml"
            try:
                download(src["url"], path)
            except Exception as e:                # en trasig källa ska inte stoppa bygget
                print(f"VARNING: {src['key']} kunde inte hämtas: {e}")
                continue
            for _, el in ET.iterparse(path):
                if el.tag == "programme":
                    cid = el.get("channel")
                    stop = parse_time(el.get("stop"))
                    if cid in wanted[src["key"]] and stop and stop > cutoff:
                        progs[(src["key"], cid)].append(el)
                    else:
                        el.clear()
                elif el.tag == "channel":
                    el.clear()

    tv = ET.Element("tv", {"generator-info-name": "IPTV EPG merge"})
    body = []
    stats = []
    for tid, t in mapping.items():
        best = max((progs.get(tuple(s), []) for s in t["sources"]),
                   key=lambda p: max((parse_time(x.get("stop")) for x in p),
                                     default=datetime.min.replace(tzinfo=timezone.utc)))
        ch = ET.SubElement(tv, "channel", {"id": tid})
        ET.SubElement(ch, "display-name").text = t["name"]
        for p in sorted(best, key=lambda x: x.get("start")):
            p.set("channel", tid)
            body.append(p)
        stats.append((len(best), tid))
    tv.extend(body)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wb") as f:
        ET.ElementTree(tv).write(f, encoding="utf-8", xml_declaration=True)

    empty = [tid for n, tid in stats if n == 0]
    print(f"{len(mapping)} kanaler, {len(body)} program -> {out}")
    if empty:
        print("Utan program just nu:", ", ".join(empty))


if __name__ == "__main__":
    main()
