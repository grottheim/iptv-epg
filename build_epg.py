#!/usr/bin/env python3
"""Bygger sammanslagna XMLTV-filer utifrån mapping.json. Behöver inga inloggningsuppgifter.

För varje kanal väljs den källa som räcker längst framåt i tiden. Resultatet blir en fil
per land (docs/epg-<land>.xml.gz) plus en fil med allt (docs/epg-alla.xml.gz).

Användning:  python3 build_epg.py [--days 7] [--out docs]
"""
import argparse
import gzip
import json
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from epgcommon import ROOT, all_sources, download

# Programdelar som behålls; resten (medverkande, bilder, betyg …) tas bort för att hålla nere storleken.
KEEP = {"title", "sub-title", "desc", "category", "episode-num", "date", "new", "live", "premiere"}
BASE_URL = "https://grottheim.github.io/iptv-epg/"
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def parse_time(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y%m%d%H%M%S %z")
    except ValueError:
        return None


def write_xmltv(path, channels, progs):
    with gzip.open(path, "wb", compresslevel=9) as f:
        f.write(b'<?xml version="1.0" encoding="utf-8"?>\n<tv generator-info-name="IPTV EPG merge">\n')
        for el in channels:
            f.write(ET.tostring(el, encoding="utf-8"))
        for el in progs:
            f.write(ET.tostring(el, encoding="utf-8"))
        f.write(b"</tv>\n")


def write_index(out, counts):
    rows = "".join(
        f'<tr><td>{c.upper()}</td><td>{n}</td><td><code>{BASE_URL}epg-{c}.xml.gz</code></td>'
        f'<td>{(out / f"epg-{c}.xml.gz").stat().st_size / 1e6:.1f} MB</td></tr>'
        for c, n in sorted(counts.items()))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    (out / "index.html").write_text(f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>IPTV EPG</title>
<style>body{{font:15px system-ui;margin:16px;background:#fff;color:#111}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}}}}
table{{border-collapse:collapse}}td,th{{padding:4px 10px;text-align:left}}code{{font-size:13px}}</style>
<h1>IPTV EPG</h1><p>Uppdaterad {stamp}. Lägg in adressen för de länder du tittar på som EPG-källa i UHF.</p>
<table><tr><th>Land</th><th>Kanaler</th><th>Adress</th><th>Storlek</th></tr>{rows}</table>
<p>Allt i en fil (tung): <code>{BASE_URL}epg-alla.xml.gz</code></p>""", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7, help="hur många dygn framåt som tas med")
    ap.add_argument("--out", default=str(ROOT / "docs"))
    args = ap.parse_args()

    mapping = json.loads((ROOT / "mapping.json").read_text())
    wanted = defaultdict(set)                    # källa -> källans kanal-id som behövs
    for t in mapping.values():
        for key, sid in t["sources"]:
            wanted[key].add(sid)

    now = datetime.now(timezone.utc)
    start_cut, end_cut = now - timedelta(hours=12), now + timedelta(days=args.days)
    progs = defaultdict(list)                    # (källa, källans id) -> [programme]
    urls = all_sources()
    with tempfile.TemporaryDirectory() as tmp:
        for key in sorted(wanted):
            path = Path(tmp) / f"{key}.xml"
            try:
                download(urls[key], path)
                for _, el in ET.iterparse(path):
                    if el.tag == "programme":
                        cid = el.get("channel")
                        start, stop = parse_time(el.get("start")), parse_time(el.get("stop"))
                        if (cid in wanted[key] and start and stop
                                and stop > start_cut and start < end_cut):
                            for child in list(el):
                                if child.tag not in KEEP:
                                    el.remove(child)
                            el.tail = "\n"
                            progs[(key, cid)].append(el)
                        else:
                            el.clear()
                    elif el.tag == "channel":
                        el.clear()
            except Exception as e:                # en trasig källa ska inte stoppa bygget
                print(f"VARNING: {key}: {e}")
            finally:
                path.unlink(missing_ok=True)
            print(f"  {key}: klar")

    by_country = defaultdict(lambda: ([], []))
    empty = 0
    for tid, t in mapping.items():
        best = max((progs.get(tuple(s), []) for s in t["sources"]),
                   key=lambda p: max((parse_time(x.get("stop")) for x in p), default=EPOCH))
        if not best:
            empty += 1
            continue
        ch = ET.Element("channel", {"id": tid})
        ET.SubElement(ch, "display-name").text = t["name"]
        ch.tail = "\n"
        chans, plist = by_country[t["country"]]
        chans.append(ch)
        for p in sorted(best, key=lambda x: x.get("start")):
            # Samma källkanal kan delas av flera mål-id: kopiera i så fall elementet.
            if p.get("channel") != tid and p.get("_used"):
                p = ET.fromstring(ET.tostring(p))
            p.set("_used", "1")
            p.set("channel", tid)
            plist.append(p)

    for _, plist in by_country.values():
        for p in plist:
            p.attrib.pop("_used", None)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    all_ch, all_pr = [], []
    for country, (chans, plist) in sorted(by_country.items()):
        write_xmltv(out / f"epg-{country}.xml.gz", chans, plist)
        all_ch += chans
        all_pr += plist
    write_xmltv(out / "epg-alla.xml.gz", all_ch, all_pr)
    # Den ursprungliga adressen behålls för Sverige, så att befintliga UHF-inställningar fortsätter fungera.
    if "se" in by_country:
        write_xmltv(out / "epg.xml.gz", *by_country["se"])

    write_index(out, {c: len(v[0]) for c, v in by_country.items()})
    print(f"\n{len(all_ch)} kanaler, {len(all_pr)} program ({empty} kanaler utan data just nu)")
    for f in sorted(out.glob("*.xml.gz")):
        print(f"  {f.name:22} {f.stat().st_size / 1e6:6.1f} MB")


if __name__ == "__main__":
    main()
