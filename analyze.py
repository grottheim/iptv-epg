#!/usr/bin/env python3
"""Analyserar EPG-täckningen för ett Xtream-konto.

Hämtar kanallistan och leverantörens XMLTV, och skriver en rapport över
vilka kanaler som saknar EPG eller har för kort framförhållning.

Användning:  python3 analyze.py [--min-hours 48] [--refresh]
"""
import argparse
import csv
import gzip
import json
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
UA = "Mozilla/5.0 (AppleTV; tvOS) EPG-analyzer"


def load_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit("Saknar .env – kopiera .env.example till .env och fyll i dina uppgifter.")
    env = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("XTREAM_URL", "XTREAM_USER", "XTREAM_PASS"):
        if not env.get(key):
            sys.exit(f"{key} saknas i .env")
    env["XTREAM_URL"] = env["XTREAM_URL"].rstrip("/")
    return env


def fetch(url, dest=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if dest:
        dest.write_bytes(data)
    return data


def api(env, action):
    qs = urllib.parse.urlencode(
        {"username": env["XTREAM_USER"], "password": env["XTREAM_PASS"], "action": action}
    )
    return json.loads(fetch(f"{env['XTREAM_URL']}/player_api.php?{qs}"))


def parse_time(s):
    s = (s or "").strip()
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def scan_xmltv(path):
    """Returnerar {kanal-id: {name, count, last_stop}} samt tidszonsoffsets som förekommer."""
    channels = {}
    progs = defaultdict(lambda: {"count": 0, "last_stop": None})
    offsets = Counter()
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag == "channel":
            name = el.findtext("display-name") or ""
            channels[el.get("id")] = name
            el.clear()
        elif el.tag == "programme":
            cid = el.get("channel")
            stop = parse_time(el.get("stop"))
            p = progs[cid]
            p["count"] += 1
            if stop and (p["last_stop"] is None or stop > p["last_stop"]):
                p["last_stop"] = stop
            raw = (el.get("start") or "").split(" ")
            offsets[raw[1] if len(raw) > 1 else "(ingen)"] += 1
            el.clear()
    return channels, progs, offsets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-hours", type=float, default=48,
                    help="kortare framförhållning än så räknas som 'kort' (standard 48)")
    ap.add_argument("--refresh", action="store_true", help="hämta om även om cache finns")
    args = ap.parse_args()

    env = load_env()
    CACHE.mkdir(exist_ok=True)

    print("Hämtar kategorier och kanaler …")
    raw_cats = api(env, "get_live_categories")
    (CACHE / "live_categories.json").write_text(json.dumps(raw_cats, ensure_ascii=False))
    cats = {c["category_id"]: c["category_name"] for c in raw_cats}
    streams = api(env, "get_live_streams")
    (CACHE / "live_streams.json").write_text(json.dumps(streams, ensure_ascii=False, indent=1))

    xml_path = CACHE / "provider_epg.xml"
    if args.refresh or not xml_path.exists():
        print("Hämtar leverantörens XMLTV (kan ta en stund) …")
        qs = urllib.parse.urlencode({"username": env["XTREAM_USER"], "password": env["XTREAM_PASS"]})
        fetch(f"{env['XTREAM_URL']}/xmltv.php?{qs}", xml_path)
    print(f"Läser XMLTV ({xml_path.stat().st_size / 1e6:.1f} MB) …")
    channels, progs, offsets = scan_xmltv(xml_path)

    now = datetime.now(timezone.utc)
    rows = []
    for s in streams:
        eid = (s.get("epg_channel_id") or "").strip()
        p = progs.get(eid) if eid else None
        hours = round((p["last_stop"] - now).total_seconds() / 3600, 1) if p and p["last_stop"] else None
        if not eid:
            status = "saknar_epg_id"
        elif eid not in channels and not p:
            status = "id_finns_ej_i_epg"
        elif not p or p["count"] == 0:
            status = "inga_program"
        elif hours is None or hours <= 0:
            status = "bara_gammal_data"
        elif hours < args.min_hours:
            status = "kort"
        else:
            status = "ok"
        rows.append({
            "status": status,
            "kategori": cats.get(s.get("category_id"), "?"),
            "kanal": s.get("name", ""),
            "stream_id": s.get("stream_id"),
            "epg_channel_id": eid,
            "antal_program": p["count"] if p else 0,
            "timmar_framåt": hours if hours is not None else "",
        })

    report = ROOT / "epg_rapport.csv"
    with report.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["status"] == "ok", r["kategori"], r["kanal"])))

    total = Counter(r["status"] for r in rows)
    print(f"\n{len(rows)} kanaler, {len(channels)} kanaler i XMLTV\n")
    for st, n in total.most_common():
        print(f"  {st:20} {n:6}  ({n / len(rows):.0%})")

    print("\nKategorier med flest problem:")
    by_cat = defaultdict(Counter)
    for r in rows:
        by_cat[r["kategori"]][r["status"] == "ok"] += 1
    worst = sorted(by_cat.items(), key=lambda kv: -kv[1][False])[:25]
    for cat, c in worst:
        if c[False]:
            print(f"  {c[False]:5} av {c[False] + c[True]:5} saknar EPG  –  {cat}")

    print("\nTidszonsoffsets i leverantörens EPG:", dict(offsets.most_common(5)))
    print(f"\nFull rapport: {report.name}")


if __name__ == "__main__":
    main()
