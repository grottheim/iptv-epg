"""Gemensamma hjälpfunktioner och inställningar för EPG-skripten."""
import gzip
import re
import shutil
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UA = "Mozilla/5.0 EPG-builder"

IPTVEPG = "https://iptv-epg.org/files/epg-{}.xml.gz"
EPGSHARE = "https://epgshare01.online/epgshare01/epg_ripper_{}.xml.gz"

# Land -> vilka leverantörskategorier som hör dit (regex) och vilka källfiler som används.
# Källorna står i prioritetsordning; per kanal väljs ändå den som räcker längst framåt.
COUNTRIES = {
    "se": {"cats": r"^SWEDEN ", "iptvepg": ["se"], "epgshare": ["SE1"]},
    "no": {"cats": r"^NORWAY ", "iptvepg": ["no"], "epgshare": ["NO1"]},
    "dk": {"cats": r"^DENMARK ", "iptvepg": ["dk"], "epgshare": ["DK1"]},
    "fi": {"cats": r"^(FINLAND |FI\|)", "iptvepg": ["fi"], "epgshare": ["FI1"]},
    "uk": {"cats": r"^UK\|", "iptvepg": ["gb"], "epgshare": ["UK1"]},
    "ie": {"cats": r"^IR\| IRELAND", "iptvepg": ["gb"], "epgshare": ["IE1", "UK1"]},
    "de": {"cats": r"^DE\|", "iptvepg": ["de"], "epgshare": ["DE1"]},
    "at": {"cats": r"^AT\|", "iptvepg": ["at", "de"], "epgshare": ["AT1", "DE1"]},
    "ch": {"cats": r"^CH\| (SWITZERLAND|DAZN|SFL)", "iptvepg": ["ch", "de"], "epgshare": ["CH1", "DE1"]},
    "fr": {"cats": r"^FR\|", "iptvepg": ["fr"], "epgshare": ["FR1"]},
    "be": {"cats": r"^BE\|", "iptvepg": ["be", "nl", "fr"], "epgshare": ["BE2", "NL1", "FR1"]},
    "nl": {"cats": r"^(NL\||EU\| NETHERLANDS)", "iptvepg": ["nl"], "epgshare": ["NL1"]},
    "lu": {"cats": r"^EU\| LUXEMBOURG", "iptvepg": ["lu"], "epgshare": ["LU1"]},
    "es": {"cats": r"^ES\|", "iptvepg": ["es"], "epgshare": ["ES1"]},
    "pt": {"cats": r"^PT\|", "iptvepg": ["pt"], "epgshare": ["PT1"]},
    "it": {"cats": r"^IT\|", "iptvepg": ["it"], "epgshare": ["IT1"]},
    "gr": {"cats": r"^(GR|CY)\|", "iptvepg": ["gr"], "epgshare": ["GR1", "CY1"]},
    "mt": {"cats": r"^MT\|", "iptvepg": ["mt"], "epgshare": ["MT1"]},
    "pl": {"cats": r"^PL\|", "iptvepg": ["pl"], "epgshare": ["PL1"]},
    "cz": {"cats": r"^CZ\|", "iptvepg": ["cz"], "epgshare": ["CZ1", "SK1"]},
    "hu": {"cats": r"^HU\|", "iptvepg": ["hu"], "epgshare": ["HU1"]},
    "ro": {"cats": r"^RO\|", "iptvepg": ["ro"], "epgshare": ["RO1", "RO2"]},
    "bg": {"cats": r"^BG\|", "iptvepg": ["bg"], "epgshare": ["BG1"]},
    "hr": {"cats": r"^HR\|", "iptvepg": ["hr"], "epgshare": ["HR1"]},
    "rs": {"cats": r"^(RS|CG)\|", "iptvepg": ["rs", "me"], "epgshare": ["RS1"]},
    "ba": {"cats": r"^BH\|", "iptvepg": ["ba"], "epgshare": ["BA1"]},
    "exyu": {"cats": r"^EXYU\|", "iptvepg": ["rs", "hr", "ba"], "epgshare": ["RS1", "HR1", "BA1"]},
    "mk": {"cats": r"^MK\|", "iptvepg": ["mk"], "epgshare": []},
    "si": {"cats": r"^SL\|", "iptvepg": ["si"], "epgshare": []},
    "al": {"cats": r"^(AL|KO)\|", "iptvepg": ["al"], "epgshare": ["AL1"]},
    "lv": {"cats": r"^LA\|", "iptvepg": ["lv"], "epgshare": ["LV1"]},
    "lt": {"cats": r"^LT\|", "iptvepg": ["lt"], "epgshare": ["LT1"]},
    "ua": {"cats": r"^UKR\|", "iptvepg": ["ua"], "epgshare": ["UA1"]},
    "by": {"cats": r"^BY\|", "iptvepg": ["by"], "epgshare": []},
    "ru": {"cats": r"^RU\|", "iptvepg": ["ru"], "epgshare": []},
    "tr": {"cats": r"^TR\|", "iptvepg": ["tr"], "epgshare": ["TR1", "TR3"]},
    "us": {"cats": r"^US\|", "iptvepg": ["us"], "epgshare": ["US2", "US_LOCALS1", "US_SPORTS1"]},
    "ca": {"cats": r"^CA\|", "iptvepg": ["ca"], "epgshare": ["CA2", "US2"]},
    # Lagkanaler för NHL, AHL och kanadensiska juniorligor. NHL-lagen ligger i en "PACKAGE"-kategori,
    # därför gäller inte SKIP_CATS här.
    "hockey": {"cats": r"^(NA\| HOCKEY LEAGUE|US\| NHL PACKAGE)", "iptvepg": ["us", "ca"], "epgshare": [],
               "noskip": True},
}

# Kategorier utan riktig tablå (evenemang, 24/7-loopar, radio, repriser …).
SKIP_CATS = re.compile(
    r"PPV|24/7|24-7|RADIO|REPLAY|REPETICI|EVENT|ONBOARD|SERIES|PACKAGE|PLAY\b|PLAYER|"
    r"PRIME|NETFLIX|TUBI|PLUTO|CINEMANIA|ESPN PLUS|FLO |STORE|RED BUTTON|TEAM|"
    r"LEAGUE ONE|LEAGUE TWO|CHAMPIONSHIP|NATIONAL LEAGUE|VIDIO|MC VIDEO|SDAROT|CONCERTS",
    re.I)


def category_matcher(countries):
    """Funktion som avgör om en kategori hör till något av länderna (med hänsyn till SKIP_CATS)."""
    rules = [(re.compile(COUNTRIES[c]["cats"]), COUNTRIES[c].get("noskip", False)) for c in countries]
    return lambda cat: any(rx.search(cat) and (noskip or not SKIP_CATS.search(cat)) for rx, noskip in rules)


def sources_for(country):
    """[(nyckel, url)] för ett land."""
    cfg = COUNTRIES[country]
    return ([(f"iptvepg-{c}", IPTVEPG.format(c)) for c in cfg["iptvepg"]] +
            [(f"epgshare-{c}", EPGSHARE.format(c)) for c in cfg["epgshare"]])


def all_sources():
    seen = {}
    for c in COUNTRIES:
        for key, url in sources_for(c):
            seen.setdefault(key, url)
    return seen


def download(url, dest):
    """Laddar ner (och packar upp) en XMLTV-fil till disk utan att hålla allt i minnet."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    with open(tmp, "rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    if gz:
        with gzip.open(tmp) as src, open(dest, "wb") as f:
            shutil.copyfileobj(src, f, 1 << 20)
        tmp.unlink()
    else:
        tmp.replace(dest)
    with open(dest, "rb") as f:
        if not f.read(200).lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
            raise RuntimeError(f"{url} gav inte XML")


SOURCE_PREFIX = re.compile(r"^([A-Z]{2,5} - |\[[^\]]+\]\s*)")
_QUALITY = (r"\b(fhd|uhd|hd|sd|lq|low|hevc|h265|4k\+?|8k|raw|gold|vip|super|backup|"
            r"\d{3,4}p|480|720|1080|50fps|60fps)\b")


def norm(name):
    """'SWE| TV 4 HITS HD' -> 'tv4hits'"""
    name = (name or "").replace("\\u0026", "&")
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"^[a-z0-9+\- ]{1,12}[|:]\s*", "", s)    # 'swe| ', 'uk-nowtv| '
    s = re.sub(r"\.[a-z]{2}(@\w+)?$", "", s)              # id-suffix '.se'
    s = s.replace("&", " and ").replace("+", " plus ")
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(_QUALITY, " ", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return re.sub(r"^bbc([1-4])", lambda m: "bbc" + ["one", "two", "three", "four"][int(m[1]) - 1], s)


_CALL_WORDS = {"WEST", "WILD", "KIDS", "WORLD", "WOMEN", "WINE", "WAVE", "CITY", "CINE", "CLUB",
               "COZI", "CNBC", "CPAC", "CRIME", "COMEDY", "KIDZ", "WOW", "CBS", "CNN", "CMT", "CBSN"}
NETWORKS = ["ABC", "CBS", "NBC", "FOX", "CW", "MNT", "MYNETWORK", "PBS", "ION", "TELEMUNDO", "UNIVISION",
            "UNIMAS", "CTV", "CBC", "GLOBAL", "CITYTV", "GRIT", "REWIND", "METV", "COZI", "ANTENNA", "BOUNCE",
            "ESTRELLA", "AZTECA", "COMET", "CHARGE", "LAFF", "DABL", "START", "QUEST", "TBD", "INDEPENDENT"]
_NET_RX = re.compile(r"\b(" + "|".join(NETWORKS) + r"|MY NETWORK)\b")


def stations_in_name(name):
    """Nätverk och stationer i ett leverantörsnamn.
    'US| CW 6 SAN DIEGO CA (KFMB)' -> ('CW', [('KFMB', None)]);  '... (WSVN-D2)' -> ('ABC', [('WSVN', 2)])"""
    up = name.upper()
    m = _NET_RX.search(up)
    net = ("MNT" if m[1] in ("MY NETWORK", "MYNETWORK") else m[1]) if m else None
    found = []
    for tok in re.findall(r"[A-Z0-9\-]+", up):
        t = re.fullmatch(r"([KWC][A-Z]{2,3})(?:-(?:DT|TV|LD|CD|LP|D)(\d*))?", tok)
        if t and t[1] not in _CALL_WORDS and t[1] not in NETWORKS:
            found.append((t[1], int(t[2]) if t[2] else None))
    return net, found


def station_of_id(cid):
    """(anropssignal, nätverk, sidokanal) ur ett käll-id, eller None.
    'WSVN-DT2.us_locals1' -> ('WSVN', None, 2);  'ABCWSVNDT2.us' -> ('WSVN', 'ABC', 2);  'NBCWCAU.us' -> ('WCAU', 'NBC', 1)"""
    base = (cid or "").split(".")[0]
    m = re.fullmatch(r"([KWC][A-Z]{2,3})-(?:DT|TV|LD|CD|LP|D|TDT)(\d*)", base.upper())
    if m:
        return (m[1], None, int(m[2] or 1)) if m[1] not in _CALL_WORDS else None
    m = re.fullmatch(r"([A-Za-z]*?)([KWC][A-Z]{2,3})(?:DT(\d+))?", base)
    if m and m[2] not in _CALL_WORDS:
        net = m[1].upper() or None
        return m[2], ("MNT" if net == "MYNETWORK" else net), int(m[3] or 1)
    return None
