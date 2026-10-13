#!/usr/bin/env python3
"""Xtream-mellanhand: lägger till saknade EPG-id i kanallistan och skickar allt annat vidare till leverantören.

- player_api.php?action=get_live_streams  -> leverantörens lista, med epg_channel_id ifyllt där det saknas
- player_api.php (övrigt)                 -> vidarebefordras oförändrat
- xmltv.php                               -> egen EPG (serve/epg-full.xml.gz: valda länder + NHL-tablåer)
- /live/ /movie/ /series/ /timeshift/ …   -> omdirigeras till leverantören (videon går aldrig via mellanhanden)

Bara det användarnamn som står i .env släpps igenom, så att servern inte blir en öppen proxy.
Användning:  python3 proxy.py [--port 8080]
"""
import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from analyze import load_env
from epgcommon import ROOT, UA

SERVE = ROOT / "serve"
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

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if not self.authorized(url.path, query):
            return self.send(403, b'{"error":"forbidden"}')

        if url.path.startswith(REDIRECT_PREFIXES):
            return self.send(302, extra={"Location": UPSTREAM + self.path})

        if url.path == "/xmltv.php":
            epg = SERVE / "epg-full.xml.gz"
            if not epg.exists():
                epg = SERVE / "epg-fix.xml.gz"
            if not epg.exists():
                return self.send(503, b"EPG not ready", "text/plain")
            return self.send(200, epg.read_bytes(), "application/xml", {"Content-Encoding": "gzip"})

        if url.path == "/player_api.php":
            status, body, ctype = self.upstream()
            if status == 200 and query.get("action", [""])[0] == "get_live_streams":
                try:
                    streams = json.loads(body)
                    ids = Ids.get()
                    for s in streams:
                        if not (s.get("epg_channel_id") or "").strip():
                            tid = ids.get(str(s.get("stream_id")))
                            if tid:
                                s["epg_channel_id"] = tid
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
