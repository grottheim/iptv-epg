#!/usr/bin/env python3
"""Xtream-mellanhand: lägger till saknade EPG-id i kanallistan och skickar allt annat vidare till leverantören.

- player_api.php?action=get_live_streams  -> leverantörens lista, med epg_channel_id ifyllt där det saknas
- player_api.php (övrigt)                 -> vidarebefordras oförändrat
- xmltv.php                               -> egen EPG (serve/epg-full.xml.gz: valda länder + NHL-tablåer)
- /live/ /movie/ /series/ /timeshift/ …   -> omdirigeras till leverantören (videon går aldrig via mellanhanden)
- /health                                 -> hälsokontroll för övervakningen (ingen inloggning, inga hemligheter)
- NHL 01, NHL 02 …                        -> fasta kanaler som omdirigeras till kanalen med matchen (se nhl.py)

Bara det användarnamn som står i .env släpps igenom, så att servern inte blir en öppen proxy.
Användning:  python3 proxy.py [--port 8080]
"""
import argparse
import json
import re
import time
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import nhl
from analyze import load_env
from epgcommon import ROOT, UA

SERVE = ROOT / "serve"
# Leverantörens tidszon (server_info.timezone) – tiderna i catch-up-adresser anges i den.
PROVIDER_TZ = ZoneInfo("Europe/Amsterdam")
REDIRECT_PREFIXES = ("/live/", "/movie/", "/series/", "/timeshift/", "/streaming/", "/hls/", "/hlsr/",
                     "/get.php", "/images/")

ENV = load_env()
UPSTREAM = ENV["XTREAM_URL"]


class Ids:
    """stream_id -> EPG-id, inläst från serve/epg_ids.json och omläst när filen ändras."""
    path = SERVE / "epg_ids.json"
    mtime, data = None, {}

    @classmethod
    def get(cls):
        try:
            m = cls.path.stat().st_mtime
            if m != cls.mtime:
                cls.data, cls.mtime = json.loads(cls.path.read_text()), m
        except FileNotFoundError:
            pass
        return cls.data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):            # dölj lösenord i loggen
        print(self.address_string(), (fmt % args).replace(ENV["XTREAM_PASS"], "***"), flush=True)

    def send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def authorized(self, path, query):
        user = query.get("username", [""])[0]
        if not user:                               # /live/<user>/<pass>/… och liknande
            parts = path.strip("/").split("/")
            user = parts[1] if len(parts) > 2 else ""
        return user == ENV["XTREAM_USER"]

    def upstream(self):
        req = urllib.request.Request(UPSTREAM + self.path, headers={"User-Agent": self.headers.get("User-Agent", UA)})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "text/plain")

    @staticmethod
    def timeshift_start(text):
        try:
            return datetime.strptime(text, "%Y-%m-%d:%H-%M").replace(tzinfo=PROVIDER_TZ)
        except ValueError:
            return None

    _provider = (0.0, {})                          # (tidpunkt, svar) – leverantörskontroll cachas 10 min

    def health(self):
        """Hälsokontroll utan inloggning: hur färska filerna är och om leverantörskontot fungerar."""
        now = time.time()

        def age(name):
            try:
                return round((now - (SERVE / name).stat().st_mtime) / 60)
            except OSError:
                return None
        checked, info = Handler._provider
        if now - checked > 600:
            try:
                q = urllib.parse.urlencode({"username": ENV["XTREAM_USER"], "password": ENV["XTREAM_PASS"]})
                req = urllib.request.Request(f"{UPSTREAM}/player_api.php?{q}", headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    info = json.load(r).get("user_info", {})
            except Exception as e:
                info = {"error": type(e).__name__}
            Handler._provider = (now, info)
        exp = info.get("exp_date")
        days_left = round((int(exp) - now) / 86400) if str(exp or "").isdigit() else None
        result = {
            "nhl_age_min": age("nhl.json"), "epg_age_min": age("epg-full.xml.gz"),
            "provider_status": info.get("status") or info.get("error"), "account_days_left": days_left,
        }
        problems = []
        if result["nhl_age_min"] is None or result["nhl_age_min"] > 45:
            problems.append("NHL-kopplingen uppdateras inte")
        if result["epg_age_min"] is None or result["epg_age_min"] > 240:
            problems.append("EPG:n uppdateras inte")
        if result["provider_status"] != "Active":
            problems.append("IPTV-kontot svarar inte eller är inte aktivt")
        if days_left is not None and days_left < 7:
            problems.append(f"IPTV-kontot går ut om {days_left} dagar")
        result["problems"] = problems
        self.send(503 if problems else 200, json.dumps(result, ensure_ascii=False).encode())

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/health":
            return self.health()
        if not self.authorized(url.path, query):
            return self.send(403, b'{"error":"forbidden"}')

        if url.path.startswith(REDIRECT_PREFIXES):
            path = self.path
            virtual = re.fullmatch(r"(/live/[^/]+/[^/]+/)(99\d{7})(\.\w+)?", url.path)
            if virtual:                            # NHL-kanal: skicka till kanalen som har matchen nu
                real = nhl.resolve(virtual[2])
                if not real:
                    return self.send(404, b'{"error":"no source"}')
                path = f"{virtual[1]}{real}{virtual[3] or ''}"
            # Catch-up på NHL-kanal: /timeshift/<user>/<pass>/<min>/<start>/<id>.ts
            shift = re.fullmatch(r"(/timeshift/[^/]+/[^/]+/\d+/)([\d:-]+)/(99\d{7})(\.\w+)?", url.path)
            if shift:
                real = nhl.resolve(shift[3], self.timeshift_start(shift[2]))
                if not real:
                    return self.send(404, b'{"error":"no source"}')
                path = f"{shift[1]}{shift[2]}/{real}{shift[4] or ''}"
            # … eller /streaming/timeshift.php?stream=<id>&start=<start>&duration=<min>
            stream = query.get("stream", [""])[0]
            if url.path == "/streaming/timeshift.php" and re.fullmatch(r"99\d{7}", stream):
                real = nhl.resolve(stream, self.timeshift_start(query.get("start", [""])[0]))
                if not real:
                    return self.send(404, b'{"error":"no source"}')
                path = self.path.replace(f"stream={stream}", f"stream={real}")
            return self.send(302, extra={"Location": UPSTREAM + path})

        if url.path == "/xmltv.php":
            epg = SERVE / "epg-full.xml.gz"
            if not epg.exists():
                epg = SERVE / "epg-fix.xml.gz"
            if not epg.exists():
                return self.send(503, b"EPG not ready", "text/plain")
            return self.send(200, epg.read_bytes(), "application/xml", {"Content-Encoding": "gzip"})

        if url.path == "/player_api.php":
            action = query.get("action", [""])[0]
            category = query.get("category_id", [""])[0]
            if action == "get_live_streams" and category in (nhl.CATEGORY_ID, nhl.CATEGORY_B, nhl.CATEGORY_TV):
                only = [x for x in nhl.virtual_streams() if x["category_id"] == category]
                return self.send(200, json.dumps(only).encode())
            status, body, ctype = self.upstream()
            if status == 200 and not action:           # inloggning: låt UHF bygga alla adresser mot mellanhanden
                try:
                    info = json.loads(body)
                    host, _, port = (self.headers.get("Host") or "").partition(":")
                    if host and isinstance(info.get("server_info"), dict):
                        info["server_info"].update({"url": host, "port": port or "80", "server_protocol": "http"})
                        body = json.dumps(info, ensure_ascii=False).encode()
                except ValueError:
                    pass
            if status == 200 and action == "get_live_categories":
                try:
                    body = json.dumps(nhl.virtual_categories() + json.loads(body),
                                      ensure_ascii=False, separators=(",", ":")).encode()
                except ValueError:
                    pass
            if status == 200 and action == "get_live_streams":
                try:
                    streams = json.loads(body)
                    ids = Ids.get()
                    for s in streams:
                        if not (s.get("epg_channel_id") or "").strip():
                            tid = ids.get(str(s.get("stream_id")))
                            if tid:
                                s["epg_channel_id"] = tid
                    if not category:
                        streams = nhl.virtual_streams() + streams
                    body = json.dumps(streams, ensure_ascii=False, separators=(",", ":")).encode()
                    ctype = "application/json"
                except ValueError:
                    pass                           # oväntat svar: skicka vidare som det är
            return self.send(status, body, ctype)

        self.send(404, b'{"error":"not found"}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    print(f"Mellanhand på port {args.port} -> {urllib.parse.urlsplit(UPSTREAM).netloc}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
