#!/usr/bin/env python3
"""Skapar en M3U-spellista med de kanaler som saknar EPG-id hos leverantören, med rätt tvg-id.

Spellistan innehåller dina inloggningsuppgifter och sparas därför bara lokalt (serve/playlist.m3u).
Användning:  python3 make_playlist.py [land ...]     (standard: se)
"""
import json
import re
import sys

from analyze import load_env
from epgcommon import COUNTRIES, ROOT, SKIP_CATS

BASE_URL = "https://grottheim.github.io/iptv-epg/"


def main():
    countries = sys.argv[1:] or ["se"]
    env = load_env()
    streams = json.loads((ROOT / "cache" / "live_streams.json").read_text())
    cats = {c["category_id"]: c["category_name"] for c in
            json.loads((ROOT / "cache" / "live_categories.json").read_text())}
    mapping = json.loads((ROOT / "mapping.json").read_text())

    cat_rx = re.compile("|".join(f"(?:{COUNTRIES[c]['cats']})" for c in countries))
    by_name = {}
    for tid, t in mapping.items():               # kategorifiltret nedan avgör vilka länder som tas med
        for n in t.get("names", []):
            by_name.setdefault(n, tid)

    epg = ",".join(BASE_URL + ("epg.xml.gz" if c == "se" else f"epg-{c}.xml.gz") for c in countries)
    lines = [f'#EXTM3U url-tvg="{epg}" x-tvg-url="{epg}"']
    n = 0
    for s in streams:
        name = s.get("name", "")
        tid = by_name.get(name)
        group = cats.get(s.get("category_id"), "")
        if (not tid or (s.get("epg_channel_id") or "").strip()
                or not cat_rx.search(group) or SKIP_CATS.search(group)):
            continue
        lines.append(f'#EXTINF:-1 tvg-id="{tid}" tvg-name="{name}" tvg-logo="{s.get("stream_icon") or ""}" '
                     f'group-title="EPG-fix {group}",{name}')
        lines.append(f'{env["XTREAM_URL"]}/live/{env["XTREAM_USER"]}/{env["XTREAM_PASS"]}/{s["stream_id"]}.ts')
        n += 1

    out = ROOT / "serve" / "playlist.m3u"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{n} kanaler -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
