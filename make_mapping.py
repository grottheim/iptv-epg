#!/usr/bin/env python3
"""Kopplar leverantörens kanaler till externa EPG-källor och skriver mapping.json.

Kör efter analyze.py (använder cache/live_streams.json och cache/live_categories.json).
Användning:  python3 make_mapping.py [land ...]     (utan argument: alla länder)
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter

from epgcommon import (COUNTRIES, ROOT, SOURCE_PREFIX, category_matcher, callsign_of_id, callsigns_in_name,
                       download, norm, sources_for)

# Namn som inte går att matcha automatiskt, per land: normaliserat leverantörsnamn -> källnamn.
ALIASES = {
    "se": {
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
        "horseandcountry": "horseandcountrytv",
        "dwenglish": "dw",
        "bbcworldnews": "bbcnews",
        "bloombergtveurope": "bloombergtv",
        "cnbceurope": "cnbc",
    },
}

ALIASES["hockey"] = {"wbscrantonpenguins": "wilkesbarrescrantonpenguins", "utahhockeyclub": "utahmammoth"}

# Regionala varianter som ska få samma tablå som huvudkanalen.
REGIONAL = {
    "se": (re.compile(r"^svt1(dalarna|gavleborg|jamtland|jonkoping|norrbotten|skane|smaland|"
                      r"varmland|vast|vasterbotten|vasternorrland|orebro|ost)$"), "svt1"),
}

_loaded = {}


def load_source(key, url):
    if key in _loaded:
        return _loaded[key]
    path = ROOT / "cache" / f"src_{key}.xml"
    by_name, ids, calls = {}, set(), {}
    try:
        if not path.exists():
            print(f"  hämtar {key} …")
            download(url, path)
        for _, el in ET.iterparse(path):
            if el.tag == "channel":
                cid = el.get("id")
                ids.add(cid)
                for dn in el.findall("display-name"):
                    by_name.setdefault(norm(SOURCE_PREFIX.sub("", dn.text or "")), cid)
                by_name.setdefault(norm(cid), cid)
                cs = callsign_of_id(cid)
                if cs:
                    calls.setdefault(cs, cid)
                el.clear()
            elif el.tag == "programme":
                el.clear()
    except Exception as e:
        print(f"  VARNING: {key} kunde inte läsas: {e}")
    by_name.pop("", None)
    _loaded[key] = (by_name, ids, calls)
    return _loaded[key]


def main():
    countries = sys.argv[1:] or list(COUNTRIES)
    streams = json.loads((ROOT / "cache" / "live_streams.json").read_text())
    cats = {c["category_id"]: c["category_name"] for c in
            json.loads((ROOT / "cache" / "live_categories.json").read_text())}

    out = ROOT / "mapping.json"
    targets = json.loads(out.read_text()) if sys.argv[1:] and out.exists() else {}
    targets = {k: v for k, v in targets.items() if v.get("country") not in countries}
    missing = {}

    for country in countries:
        in_country = category_matcher([country])
        mine = [s for s in streams
                if in_country(cats.get(s.get("category_id"), ""))
                and not s.get("name", "").startswith("#")]
        if not mine:
            continue
        print(f"{country}: {len(mine)} kanaler")
        srcs = [(key, load_source(key, url)) for key, url in sources_for(country)]
        aliases = ALIASES.get(country, {})
        regional = REGIONAL.get(country)
        stat = Counter()
        for s in mine:
            name = s.get("name", "")
            n = norm(name)
            if regional and regional[0].match(n):
                n = regional[1]
            n = aliases.get(n, n)
            eid = (s.get("epg_channel_id") or "").strip()

            cs = callsigns_in_name(name) if country in ("us", "ca") else set()
            found = []
            for key, (by_name, ids, calls) in srcs:
                sid = eid if eid in ids else by_name.get(n)
                if not sid:
                    sid = next((calls[c] for c in sorted(cs) if c in calls), None)
                if sid:
                    found.append([key, sid])
            if not found:
                stat["utan träff"] += 1
                missing.setdefault(country, set()).add(name)
                continue
            stat["kopplade"] += 1
            # Kanaler utan id från leverantören får källans id, så de kan kopplas manuellt i UHF.
            tid = eid or found[0][1]
            t = targets.setdefault(tid, {"name": re.sub(r"^[\w\-+]+\s*[|:]\s*", "", name).strip(),
                                         "country": country, "sources": found, "streams": 0})
            t["streams"] += 1
            if not eid and name not in t.setdefault("names", []):
                t["names"].append(name)          # exakta leverantörsnamn för namnmatchning i UHF
            if not eid:
                t.setdefault("stream_ids", []).append(str(s["stream_id"]))
        print(f"   {dict(stat)}")

    out.write_text(json.dumps(targets, ensure_ascii=False, indent=1))
    (ROOT / "cache" / "unmatched.json").write_text(
        json.dumps({k: sorted(v) for k, v in missing.items()}, ensure_ascii=False, indent=1))
    print(f"\n{len(targets)} EPG-kanaler -> {out.name}  (omatchade: cache/unmatched.json)")


if __name__ == "__main__":
    main()
