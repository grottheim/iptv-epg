#!/usr/bin/env python3
"""Kopplar leverantörens kanaler till externa EPG-källor och skriver mapping.json.

Kör efter analyze.py (använder cache/live_streams.json).
Användning:  python3 make_mapping.py
"""
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

from epgcommon import ROOT, SOURCES, download, norm

# Vilka leverantörskategorier som ska få förbättrad EPG.
CATEGORIES = ["SWEDEN HD & HEVC", "SWEDEN SPORT HD & HEVC"]

# Namn som inte går att matcha automatiskt: normaliserat leverantörsnamn -> normaliserat källnamn.
ALIASES = {
    "svt24barnkanalen": "svtbsvt24",
    "sportkanalen": "tv4sportkanalen",
    "historychannel": "history",
    "historychannel2": "h2",
    "natgeowild": "nationalgeographicwild",
    "nickelodeonjunior": "nickjr",
    "nicktoonsscandinavia": "nicktoons",
    "vsport": "vsport1",
    "vvinter": "vsportvinter",
    "viasatgolf": "vsportgolf",
    "vfotboll": "vsportfotboll",
    "vmotor": "vsportmotor",
    "vsportlive": "vsportlive1",
    "mtv80sultra": "mtv80s",
    "sfkanalen": "sfkanalen",
    "investigationdiscovery": "investigationdiscovery",
    "horseandcountry": "horseandcountrytv",
    "dwenglish": "dw",
    "bbcworldnews": "bbcnews",
    "bloombergtveurope": "bloombergtv",
    "cnbceurope": "cnbc",
    "cnninternational": "cnninternational",
}

SVT_REGION = re.compile(r"^svt1(dalarna|gavleborg|jamtland|jonkoping|norrbotten|skane|smaland|"
                        r"varmland|vast|vasterbotten|vasternorrland|orebro|ost)$")


def load_source(src):
    path = ROOT / "cache" / f"src_{src['key']}.xml"
    if not path.exists():
        print(f"Hämtar {src['url']} …")
        download(src["url"], path)
    by_name, ids = {}, set()
    for _, el in ET.iterparse(path):
        if el.tag == "channel":
            cid = el.get("id")
            ids.add(cid)
            for dn in el.findall("display-name"):
                by_name.setdefault(norm(re.sub(src["prefix"], "", dn.text or "")), cid)
            by_name.setdefault(norm(cid), cid)
            el.clear()
        elif el.tag == "programme":
            el.clear()
    return by_name, ids


def main():
    streams = json.loads((ROOT / "cache" / "live_streams.json").read_text())
    cats = {c["category_id"]: c["category_name"] for c in
            json.loads((ROOT / "cache" / "live_categories.json").read_text())}
    sources = {s["key"]: load_source(s) for s in SOURCES}

    targets = {}          # target-id -> {"name", "sources": [[key, id], ...]}
    missing = []
    for s in streams:
        if cats.get(s.get("category_id")) not in CATEGORIES:
            continue
        name = s.get("name", "")
        if name.startswith("#"):
            continue
        n = norm(name)
        n = "svt1" if SVT_REGION.match(n) else ALIASES.get(n, n)
        eid = (s.get("epg_channel_id") or "").strip()

        found = []
        for key, (by_name, ids) in sources.items():
            sid = eid if eid in ids else by_name.get(n)
            if sid:
                found.append([key, sid])
        if not found:
            missing.append(name)
            continue
        # Kanaler utan id från leverantören får källans id, så de kan kopplas manuellt i UHF.
        tid = eid or found[0][1]
        t = targets.setdefault(tid, {"name": re.sub(r"^\w+\|\s*", "", name), "sources": found})
        t.setdefault("streams", []).append(name)

    out = ROOT / "mapping.json"
    out.write_text(json.dumps(targets, ensure_ascii=False, indent=1))
    print(f"\n{len(targets)} EPG-kanaler kopplade -> {out.name}")
    print(f"{len(missing)} kanaler utan träff i någon källa:")
    for m in sorted(set(missing)):
        print("  ", m)


if __name__ == "__main__":
    main()
