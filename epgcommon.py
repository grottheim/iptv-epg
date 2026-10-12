"""Gemensamma hjälpfunktioner för EPG-skripten."""
import gzip
import re
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UA = "Mozilla/5.0 EPG-builder"

# Externa källor i prioritetsordning. Prefix tas bort från display-name vid matchning.
SOURCES = [
    {"key": "iptvepg", "url": "https://iptv-epg.org/files/epg-se.xml.gz", "prefix": r"^SE - "},
    {"key": "epgshare", "url": "https://epgshare01.online/epgshare01/epg_ripper_SE1.xml.gz",
     "prefix": r"^\[[^\]]+\]\s*"},
]


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if not data.lstrip().startswith(b"<"):
        raise RuntimeError(f"{url} gav inte XML")
    dest.write_bytes(data)


_QUALITY = r"\b(fhd|uhd|hd|sd|hevc|h265|4k\+?|raw|50fps|60fps)\b"


def norm(name):
    """'SWE| TV 4 HITS HD' -> 'tv4hits'"""
    name = (name or "").replace("\\u0026", "&")
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"^[a-z]{2,4}\s*[|:]\s*", "", s)          # 'swe| '
    s = re.sub(r"\.(se|dk|no|fi)$", "", s)               # id-suffix
    s = s.replace("&", " and ")
    s = re.sub(_QUALITY, " ", s)
    return re.sub(r"[^a-z0-9]", "", s)
