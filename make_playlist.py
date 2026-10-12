#!/usr/bin/env python3
"""Skapar EPG-fix: en M3U-spellista med de kanaler som saknar EPG-id hos leverantören, med rätt tvg-id,
plus en egen EPG-fil som även innehåller genererade tablåer för NHL-matchkanalerna.

Spellistan innehåller dina inloggningsuppgifter och sparas därför bara lokalt (serve/).
Bör köras några gånger per dygn, eftersom matchkanalernas namn byts varje dag.

Användning:  python3 make_playlist.py [land ...]     (standard: se hockey)
"""
import gzip
import json
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from analyze import api, load_env
from epgcommon import ROOT, UA, category_matcher

BASE_URL = "https://grottheim.github.io/iptv-epg/"
PORT = 8765
SERVE = ROOT / "serve"

# Matchkanaler vars namn byts efter dagens matcher.
GAME_CATS = re.compile(r"^US\| (NHL PACKAGE|VIAPLAY NHL PPV)$")
NHL_SLOT = re.compile(r"^NHL(?: LIVE)?\s*\|\s*\d+\s*-\s*(?:(\d{1,2})(?::(\d\d))?\s*([ap]m)\s+)?(.*)$", re.I)
VIAPLAY_SLOT = re.compile(r"^NHL (.+?) - (.+?) \| \w{3} (\d{1,2} \w{3}) (\d{1,2}:\d\d) \|.*VIAPLAY PPV", re.I)
ET_TZ = ZoneInfo("America/New_York")      # NHL-tider i namnen ("7pm") antas vara östkusttid
SE_TZ = ZoneInfo("Europe/Stockholm")       # Viaplay-tider antas vara svensk tid
GAME_HOURS = 3


def lan_address():
    for iface in ("en0", "en1"):
        r = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True)
        if r.stdout.strip():
            return r.stdout.strip()
    return "localhost"


def programme(channel, start, stop, title, desc=None):
    p = ET.Element("programme", {"start": start.strftime("%Y%m%d%H%M%S %z"),
                                 "stop": stop.strftime("%Y%m%d%H%M%S %z"), "channel": channel})
    ET.SubElement(p, "title", {"lang": "en"}).text = title
    if desc:
        ET.SubElement(p, "desc", {"lang": "en"}).text = desc
    ET.SubElement(p, "category").text = "Sport"
    p.tail = "\n"
    return p


def game_times(name, now):
    """(titel, start) för en matchkanal; start är None om tid saknas. None om kanalen är tom."""
    m = VIAPLAY_SLOT.match(name)
    if m:
        dates = []
        for year in (now.year - 1, now.year, now.year + 1):
            try:
                dates.append(datetime.strptime(f"{m[3]} {year} {m[4]}", "%d %b %Y %H:%M").replace(tzinfo=SE_TZ))
            except ValueError:
                pass
        return f"{m[1].title()} – {m[2].title()}", min(dates, key=lambda d: abs(d - now))
    m = NHL_SLOT.match(name)
    if m and m[4].strip():
        if not m[1]:
            return m[4].strip(), None
        hour = int(m[1]) % 12 + (12 if m[3].lower() == "pm" else 0)
        start = now.astimezone(ET_TZ).replace(hour=hour, minute=int(m[2] or 0), second=0, microsecond=0)
        return m[4].strip(), start
    return None


def fetch_xmltv(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    return ET.fromstring(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)


def main():
    countries = sys.argv[1:] or ["se", "hockey"]
    env = load_env()
    # Färska kanaldata, eftersom matchkanalernas namn ändras.
    raw_cats = api(env, "get_live_categories")
    streams = api(env, "get_live_streams")
    (ROOT / "cache" / "live_categories.json").write_text(json.dumps(raw_cats, ensure_ascii=False))
    (ROOT / "cache" / "live_streams.json").write_text(json.dumps(streams, ensure_ascii=False, indent=1))
    cats = {c["category_id"]: c["category_name"] for c in raw_cats}
    mapping = json.loads((ROOT / "mapping.json").read_text())

    in_countries = category_matcher(countries)
    by_name = {}
    for tid, t in mapping.items():               # kategorifiltret avgör vilka länder som tas med
        for n in t.get("names", []):
            by_name.setdefault(n, tid)

    now = datetime.now(SE_TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    epg_url = f"http://{lan_address()}:{PORT}/epg-fix.xml.gz"
    lines = [f'#EXTM3U url-tvg="{epg_url}" x-tvg-url="{epg_url}"']
    used_ids, gen_ch, gen_pr = set(), [], []

    def add(s, tid, group):
        name = s.get("name", "")
        lines.append(f'#EXTINF:-1 tvg-id="{tid}" tvg-name="{name}" tvg-logo="{s.get("stream_icon") or ""}" '
                     f'group-title="EPG-fix {group}",{name}')
        lines.append(f'{env["XTREAM_URL"]}/live/{env["XTREAM_USER"]}/{env["XTREAM_PASS"]}/{s["stream_id"]}.ts')

    for s in streams:
        name = s.get("name", "")
        group = cats.get(s.get("category_id"), "")
        if name.startswith("#") or (s.get("epg_channel_id") or "").strip():
            continue
        tid = by_name.get(name)
        if tid and in_countries(group):
            add(s, tid, group)
            used_ids.add(tid)
        elif "hockey" in countries and GAME_CATS.match(group) and (NHL_SLOT.match(name) or "VIAPLAY PPV" in name):
            tid = f"match{s['stream_id']}.hockey"
            add(s, tid, group)
            ch = ET.Element("channel", {"id": tid})
            ET.SubElement(ch, "display-name").text = name
            ch.tail = "\n"
            gen_ch.append(ch)
            game = game_times(name, now)
            if game and game[1] is None:
                gen_pr.append(programme(tid, day_start, day_start + timedelta(days=1), game[0]))
            elif game and game[1] + timedelta(hours=GAME_HOURS) > now - timedelta(hours=12):
                title, start = game
                local = start.astimezone(ET_TZ)
                when = f"{local:%a %b %d}, {local:%I:%M %p}".replace(" 0", " ") + " ET"
                gen_pr.append(programme(tid, min(day_start, start - timedelta(hours=12)), start,
                                        f"Up next: {title}", f"Game starts {when}."))
                gen_pr.append(programme(tid, start, start + timedelta(hours=GAME_HOURS), title,
                                        f"NHL Hockey: {title}. Starts {when} (estimated {GAME_HOURS} h)."))
            else:                                  # tom kanal eller gammalt namn
                gen_pr.append(programme(tid, day_start, day_start + timedelta(days=2), "No game scheduled"))

    # Egen EPG-fil: de kopplade kanalernas tablåer från GitHub Pages plus de genererade matchtablåerna.
    tv = ET.Element("tv", {"generator-info-name": "IPTV EPG-fix"})
    progs = []
    for c in countries:
        url = BASE_URL + ("epg.xml.gz" if c == "se" else f"epg-{c}.xml.gz")
        try:
            root = fetch_xmltv(url)
        except Exception as e:
            print(f"VARNING: kunde inte hämta {url}: {e}")
            continue
        for el in root:
            if (el.get("id") if el.tag == "channel" else el.get("channel")) in used_ids:
                el.tail = "\n"
                (tv if el.tag == "channel" else progs).append(el)
    tv.extend(gen_ch)
    tv.extend(progs)
    tv.extend(gen_pr)

    SERVE.mkdir(exist_ok=True)
    tmp = SERVE / "epg-fix.tmp"
    with gzip.open(tmp, "wb") as f:
        ET.ElementTree(tv).write(f, encoding="utf-8", xml_declaration=True)
    tmp.replace(SERVE / "epg-fix.xml.gz")
    tmp = SERVE / "playlist.tmp"
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(SERVE / "playlist.m3u")
    print(f"{now:%Y-%m-%d %H:%M}  {len(used_ids)} kopplade EPG-id + {len(gen_ch)} matchkanaler, "
          f"{len(progs) + len(gen_pr)} program  ({epg_url})")


if __name__ == "__main__":
    main()
