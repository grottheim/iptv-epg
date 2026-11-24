#!/usr/bin/env python3
"""NHL-kanaler med tablå från NHL:s officiella spelschema.

Matcherna ligger hos leverantören på kanaler vars namn byts varje dag. Det här skriptet letar upp
kanalerna som har dagens matcher och skriver serve/nhl.json (virtuell kanal -> riktig kanal), som
mellanhanden använder vid uppspelning. Varje match får en huvudkanal och upp till fyra alternativ, döpta
efter de kanaler de visar.

Körs av en timer på servern.  Användning:  python3 nhl.py
"""
import json
import re
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from epgcommon import ROOT, UA

SERVE = ROOT / "serve"
MAP_FILE = SERVE / "nhl.json"
SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule/{}"

CATEGORY_ID, CATEGORY_B, CATEGORY_TV = "990001", "990002", "990003"
TV_BASE_ID = 991000000
MAX_GAMES = 16                                  # NHL har 32 lag
BASE_ID = 990000000
MAX_TV = 6                                      # tv-kanaler per match (gruppen DAGENS MATCHER)
MAX_RESERVES = 4                                # matchströmmar per match (gruppen RESERVKANALER)
MAX_SOURCES = 10                                # tv-kanaler + reserver per match (variant 0–9)
GAME_HOURS = 3.5
DAY_START_UTC = 10                              # ett NHL-dygn räknas från kl. 10 UTC, då inga matcher pågår
SE_TZ = ZoneInfo("Europe/Stockholm")
HISTORY_DAYS = 3
NHL_LOGO = "http://103.176.90.154/picons/logos/USA/NHL.png"   # leverantörens NHL-logga, för matcher utan kanal                                # så många dygns kopplingar sparas, för catch-up

# Andra namn på lagen än de NHL:s schema anger.
EXTRA_NAMES = {
    "VGK": ["VEGAS"], "TOR": ["LEAFS"], "NYR": ["NY RANGERS"], "NYI": ["NY ISLANDERS"],
    "UTA": ["UTAH HOCKEY CLUB", "UTAH HC"], "STL": ["ST LOUIS"], "MTL": ["MONTREAL"],
}
# Tv-kanaler som sänder NHL: NHL:s nätverkskod(er) -> leverantörens kanalnamn (regex). Ordningen blir
# ordningen i gruppen "NHL - TV-KANALER". Strömningstjänster (Prime, ESPN+, HBO MAX …) saknas, eftersom de
# bara finns som matchströmmar hos leverantören.
TV_CHANNELS = [
    (("SN", "SNO"), r"^CA\| SPORTSNET ONTARIO"), (("SNE",), r"^CA\| SPORTSNET EAST"),
    (("SNW",), r"^CA\| SPORTSNET WEST"), (("SNP",), r"^CA\| SPORTSNET PACIFIC"),
    (("SN1",), r"^CA\| SPORTSNET ONE"), (("SN360",), r"^CA\| SPORTSNET 360"),
    (("TSN1",), r"^CA\| TSN ?1\b"), (("TSN2",), r"^CA\| TSN ?2\b"), (("TSN3",), r"^CA\| TSN ?3\b"),
    (("TSN4",), r"^CA\| TSN ?4\b"), (("TSN5",), r"^CA\| TSN ?5\b"),
    (("CBC",), r"^CA\| CBC TORONTO"), (("CITY",), r"^CA\| CITY TV TORONTO"),
    (("TVAS", "TVAS-D"), r"^CA\| TVA SPORTS HD"), (("TVAS2",), r"^CA\| TVA SPORTS 2"),
    (("RDS",), r"^CA\| RDS HD"), (("RDS2",), r"^CA\| RDS 2"), (("RDSI",), r"^CA\| RDS INFO HD"),
    (("TNT",), r"^US\| TNT HD$"), (("truTV",), r"^US\| TRUTV HD"), (("NHLN",), r"^US\| NHL NETWORK"),
    (("ESPN",), r"^US\| ESPN HD$"), (("ESPN2",), r"^US\| ESPN ?2 HD$"),
    (("NESN",), r"^US\| NESN HD$"), (("NESN+",), r"^US\| NESN PLUS"),
    (("MSG",), r"^US\| MSG HD"), (("MSG 2", "MSG2"), r"^US\| MSG 2 HD"), (("MSGSN",), r"^(TV\| MSG SPORTSNET|US\| MSG PLUS)"),
    (("NBCSP",), r"^US\| NBC SPORTS PHILADELFIA"), (("NBCSCA",), r"^US\| NBC SPORTS CALIFORNIA$"),
    (("SN-PIT",), r"^US\| SPORTSNET PITTSBURGH"), (("MNMT",), r"^US\| MONUMENTAL SPORTS NETWORK"),
    (("CHSN",), r"^US\| CHICAGO SPORTS NETWORK HD"), (("DSN",), r"^US\| DETROIT SPORTSNET"),
    (("ALT",), r"^US\| ALTITUDE SPORTS HD"), (("ALT2",), r"^US\| ALTITUDE SPORTS 2"),
    (("KONG",), r"^TV\| SEATTLE, WA KONG"), (("KING",), r"^US\| NBC 5 SEATTLE"),
    (("KTVD",), r"^TV\| DENVER, CO KTVD.*\(D11\)"), (("KUSA",), r"^US\| NBC 9 DENVER"),
    (("KSTP",), r"^US\| ABC 5 MINNEAPOLIS"), (("KTTV",), r"^US\| FOX 11 LOS ANGELES"),
    (("Utah16",), r"^TV\| SALT LAKE CITY, UT KUPX"), (("KDFI",), r"KDFI"),
    # Kod:lag = nätverket bara för det laget (Scripps sänder olika lag på olika lokalkanaler).
    (("SCRIPPS:VGK",), r"^US\| VEGAS34 \(KMCC\)"),
    # Panthers på Scripps: WSFL 39 i Miami (heter numera The Spot South Florida 39). Predators-kanalen
    # WNPX (The Spot Nashville 28) och Lightnings The Spot Tampa Bay finns inte hos leverantören.
    (("SCRIPPS:FLA",), r"^US\| CW 39 WSFL MIAMI"),
]
# Matchfeeds hos leverantören; bästa källan först. Kategorier blandas för att reserverna ska vara oberoende.
# NHL-paketets flöden (NHL | 01 …) visar "Commercial Break" i pauserna och hamnar därför efter de andra.
# PPV-strömmarna i fotbollskategorierna (PPV CA …) startar ofta inte alls och ligger därför sist.
SOURCE_ORDER = [r"^US\| ESPN", r"^CA\|", r"^US\|(?! NHL PACKAGE)", r"DAZN", r"NHL", r"^SWEDEN", r"SOCCER", r"."]
OTHER_SPORT = re.compile(r"\b(NFL|NBA|MLB|WNBA|NCAA|RUGBY|BASKETBALL|FOOTBALL|SOCCER:|AHL|OHL|WHL|QMJHL|JR)\b")
LOWER_LEAGUE_CAT = re.compile(r"AHL|OHL|WHL|QMJHL|HOCKEY LEAGUE|FLO |NFL|NBA|MLB|NCAA", re.I)
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def virtual_id(slot, variant=0):
    return BASE_ID + slot * 10 + variant


def epg_id(slot, variant=0):
    """Eget EPG-id per kanal: delar flera kanaler samma id visar UHF tablån bara på en av dem."""
    return f"nhl{slot:02d}.game" if not variant else f"nhl{slot:02d}-{variant}.game"


def virtual_categories():
    return [{"category_id": CATEGORY_ID, "category_name": "NHL - DAGENS MATCHER", "parent_id": 0},
            {"category_id": CATEGORY_TV, "category_name": "NHL - TV-KANALER", "parent_id": 0},
            {"category_id": CATEGORY_B, "category_name": "NHL - RESERVKANALER", "parent_id": 0}]


def stored():
    try:
        return json.loads(MAP_FILE.read_text())
    except (OSError, ValueError):
        return {}


def archive_days(stream):
    """Antal dygn catch-up som leverantören har för en kanal (0 = ingen)."""
    try:
        return int(stream.get("tv_archive_duration") or 0) if str(stream.get("tv_archive")) == "1" else 0
    except ValueError:
        return 0


def archive_fields(days):
    return {"tv_archive": 1 if days else 0, "tv_archive_duration": days or 0}


# Lokala tv-stationer (inte sportkanaler); de läggs sist bland en matchs tv-kanaler.
LOCAL_TV = {"KONG", "KING", "KTVD", "KUSA", "KSTP", "KTTV", "Utah16", "KDFI", "SCRIPPS:VGK", "SCRIPPS:FLA"}
# Tablå för kanaler där leverantören saknar EPG-id (MSG Sportsnet hette tidigare MSG Plus).
EPG_FALLBACK = {"MSGSN": "MadisonSquareGardenPlus.us", "ALT2": "AltitudeSports2.us", "KTVD": "MYKTVD.us"}


def find_tv(streams, cats):
    """Leverantörens kanal för varje tv-kanal i TV_CHANNELS: helst med EPG-id och i HD."""
    try:                                         # EPG-id som mellanhanden fyller i (make_playlist.py)
        filled = json.loads((SERVE / "epg_ids.json").read_text())
    except (OSError, ValueError):
        filled = {}

    def epg_of(s):
        return (s.get("epg_channel_id") or "").strip() or filled.get(str(s.get("stream_id")), "")
    out = []
    for i, (codes, rx) in enumerate(TV_CHANNELS):
        cands = [s for s in streams if re.search(rx, s.get("name", "").strip())
                 and not re.search(r"PPV|(?<!NHL) PACKAGE", cats.get(s.get("category_id"), ""))]
        if cands:
            best = min(cands, key=lambda s: (not epg_of(s), "RAW" in clean(s["name"]), not archive_days(s),
                                             len(s["name"])))
            out.append({"id": TV_BASE_ID + i, "real": best["stream_id"], "name": best["name"].strip(),
                        "epg": epg_of(best) or EPG_FALLBACK.get(codes[0], ""), "icon": best.get("stream_icon") or "",
                        "codes": list(codes), "tvg": f"nhltv{i:02d}.tv", "archive": archive_days(best)})
    return out


def virtual_streams():
    """Kanallistans poster för NHL-grupperna."""
    out = []
    for tv in stored().get("tv", []):
        out.append({
            "num": tv["id"], "name": tv["name"], "stream_type": "live", "stream_id": tv["id"],
            # Eget EPG-id: med samma id som leverantörens exemplar av kanalen visar UHF tablån bara på ett av dem.
            "stream_icon": tv["icon"], "epg_channel_id": tv.get("tvg", tv["epg"]), "added": "0", "is_adult": 0,
            "category_id": CATEGORY_TV, "category_ids": [int(CATEGORY_TV)], "custom_sid": "",
            "direct_source": "", **archive_fields(tv.get("archive", 0)),
        })
    # Dagens matcher: kanalerna heter som den kanal de visar; tablån (NHL:s schema) visar matchen.
    # Tv-kanalerna som sänder matchen (i NHL:s ordning) ligger i "DAGENS MATCHER", matchströmmarna i
    # "RESERVKANALER". Saknas tv-kanal hamnar den bästa matchströmmen i "DAGENS MATCHER".
    for g in stored().get("games", []):
        names = g.get("labels") or g.get("sources") or [f'Ingen kanal hittad: {g["game"]}']
        archive = g.get("archive", [])
        icons = g.get("icons", [])
        for v, name in enumerate(names[:MAX_SOURCES]):
            category = CATEGORY_ID if v < max(1, g.get("main", 1)) else CATEGORY_B
            out.append({
                "num": virtual_id(g["slot"], v), "name": name.strip(), "stream_type": "live",
                "stream_id": virtual_id(g["slot"], v), "epg_channel_id": epg_id(g["slot"], v),
                "stream_icon": (icons[v] if v < len(icons) else "") or NHL_LOGO,
                "added": "0", "is_adult": 0, "category_id": category, "category_ids": [int(category)],
                "custom_sid": "", "direct_source": "", **archive_fields(archive[v] if v < len(archive) else 0),
            })
    return out


def resolve(stream_id, when=None):
    """Riktig kanal för en virtuell kanal, eller None. Med `when` (catch-up) används kopplingen för det
    NHL-dygnet, så att en match från i går spelas från kanalen som visade den."""
    data = stored()
    if when is not None and int(stream_id) < TV_BASE_ID:   # tv-kanalerna är alltid samma kanal
        return data.get("history", {}).get(hockey_date(when), {}).get(str(stream_id))
    return data.get("map", {}).get(str(stream_id))


def hockey_date(now=None):
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(hours=DAY_START_UTC)).date().isoformat()


def fetch_schedule(date):
    req = urllib.request.Request(SCHEDULE_URL.format(date), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        week = json.load(r)["gameWeek"]
    return {d["date"]: sorted(d["games"], key=lambda g: (g["startTimeUTC"], g["id"])) for d in week}


def clean(text):
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().upper()
    return " " + re.sub(r"[^A-Z0-9]+", " ", s).strip() + " "


def team_names(team):
    """(lagnamn, stad/förkortning): lagnamnet räcker, staden kräver att det står NHL i kanalnamnet."""
    common = clean(team["commonName"]["default"]).strip()
    weak = {team["abbrev"]} | {clean(team["placeName"]["default"]).strip()}
    weak.discard("NEW YORK")                    # delas av två lag
    return {common} | {x for x in EXTRA_NAMES.get(team["abbrev"], []) if " " in x}, weak | set(EXTRA_NAMES.get(team["abbrev"], []))


def has(name, words):
    return any(f" {w} " in name for w in words)


def dates_in(name):
    """Datum som står i ett kanalnamn, som (månad, dag)."""
    up = name.upper()
    found = {(int(m), int(d)) for m, d in re.findall(r"\b\d{4}-(\d\d)-(\d\d)\b", up)}
    found |= {(int(m), int(d)) for d, m in re.findall(r"\b(\d\d)-(\d\d)-\d{4}\b", up)}
    found |= {(MONTHS[m], int(d)) for d, m in re.findall(r"\b(\d{1,2}) (JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b", up)}
    found |= {(MONTHS[m], int(d)) for m, d in re.findall(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC) (\d{1,2})\b", up)}
    return found


# Sportsnet+ (SN+): "CA (SN+ 011) | Los Angeles @ Montreal (2026-11-23 19:00:15)", ibland med prefix som
# "French Language Broadcast: ". Bara städerna står med, och samma kategori har basket ("Utah @ Los Angeles
# Lakers"), så varje sida måste vara exakt lagets stad och/eller lagnamn.
SNPLUS = re.compile(r"\(SN\+ \d+\)\s*\|\s*(?:[^|:@]*:\s*)?(.+?)\s+@\s+(.+?)\s*(?:\(\d{4}-\d\d-\d\d[^)]*\))?\s*$")


def is_team(side, team):
    s = clean(side).strip()
    place = clean(team["placeName"]["default"]).strip()
    common = clean(team["commonName"]["default"]).strip()
    ok = {common, f"{place} {common}", team["abbrev"]} | set(EXTRA_NAMES.get(team["abbrev"], []))
    if place != "NEW YORK":                     # delas av två lag
        ok.add(place)
    return s in ok


def match_date_ok(stream_name, game):
    start = datetime.fromisoformat(game["startTimeUTC"].replace("Z", "+00:00"))
    dates = dates_in(stream_name)
    allowed = {(d.month, d.day) for d in (start, start.astimezone(SE_TZ), start - timedelta(hours=5))}
    return not dates or bool(dates & allowed)


def is_snplus(stream_name):
    return bool(re.search(r"\(SN\+ \d+\)", stream_name))


def carries_game(stream_name, category, game):
    """Är det här kanalen för matchen? Båda lagen måste nämnas, och ett eventuellt datum måste stämma."""
    if re.match(r"\s*(END|ENDED)\b", stream_name, re.I) or "NO EVENT" in stream_name.upper() or "•" in stream_name:
        return False
    m = SNPLUS.search(stream_name)
    if m:
        return (is_team(m[1], game["awayTeam"]) and is_team(m[2], game["homeTeam"])
                and match_date_ok(stream_name, game))
    if LOWER_LEAGUE_CAT.search(category):
        if not re.match(r"^US\| NHL", category):
            return False
    name = clean(stream_name)
    if OTHER_SPORT.search(name):
        return False
    is_nhl = " NHL " in name or "NHL" in category.upper() or " NHL TV " in name
    for team in (game["homeTeam"], game["awayTeam"]):
        strong, weak = team_names(team)
        if not (has(name, strong) or (is_nhl and has(name, weak))):
            return False
    return match_date_ok(stream_name, game)


def original_text(p):
    """Titel och beskrivning utan det som label_programme lagt till (matchen först, vår beskrivning)."""
    title = re.sub(r"^[A-Z]{2,3} @ [A-Z]{2,3} · | · sedan [A-Z]{2,3} @ [A-Z]{2,3} \d\d:\d\d$", "",
                   p.findtext("title") or "")
    desc = p.findtext("desc") or ""
    if "svensk tid." in desc:
        desc = desc.split("\n\n", 1)[1] if "\n\n" in desc else ""
    return f"{title} {desc}"


class ChannelEpg:
    """Tv-gruppens tablåer (nhltvNN.tv) ur förra EPG-bygget, för att se vad kanalerna sänder under en match."""
    path = SERVE / "epg-full.xml.gz"
    mtime, data = None, {}

    @classmethod
    def get(cls):
        try:
            m = cls.path.stat().st_mtime
        except OSError:
            return {}
        if m != cls.mtime:
            import gzip
            data = {}
            try:
                with gzip.open(cls.path) as f:
                    for _, el in ET.iterparse(f):
                        if el.tag == "programme" and el.get("channel", "").startswith("nhltv"):
                            try:
                                data.setdefault(el.get("channel"), []).append((
                                    datetime.strptime(el.get("start"), "%Y%m%d%H%M%S %z"),
                                    datetime.strptime(el.get("stop"), "%Y%m%d%H%M%S %z"),
                                    original_text(el)))
                            except (TypeError, ValueError):
                                pass
                        if el.tag in ("programme", "channel"):
                            el.clear()
            except (OSError, ET.ParseError):
                return cls.data
            cls.data, cls.mtime = data, m
        return cls.data


HOCKEY_WORDS = re.compile(r"\b(NHL|LNH|HOCKEY)\b", re.I)


def shows_other(t, game):
    """Sänder kanalen enligt sin egen tablå något annat än hockey mitt i matchen? (t.ex. basket på en
    regional sportkanal när matchen flyttats till systerkanalen). Okänd tablå räknas inte som annat."""
    start = datetime.fromisoformat(game["startTimeUTC"].replace("Z", "+00:00"))
    mid = start + timedelta(minutes=75)
    texts = [txt for a, b, txt in ChannelEpg.get().get(t.get("tvg"), []) if a <= mid < b]
    if not texts:
        return False
    teams = [clean(x["commonName"]["default"]).strip() for x in (game["homeTeam"], game["awayTeam"])]
    return not any(HOCKEY_WORDS.search(txt) or any(f" {n} " in clean(txt) for n in teams) for txt in texts)


def broadcast_tv(game, tv):
    """Tv-kanalerna (poster ur tv-listan) som sänder matchen: rikstäckande först, sedan hemmalagets och
    bortalagets sportkanaler, lokala tv-stationer sist. Inom varje del gäller NHL:s ordning."""
    def rank(b):
        local = bool(broadcast_keys(game, b) & LOCAL_TV)
        return local, {"N": 0, "H": 1, "A": 2}.get(b.get("market"), 1)
    out = []
    for b in sorted(game.get("tvBroadcasts", []), key=rank):
        out += [t for t in tv if broadcast_keys(game, b) & set(t["codes"]) and t not in out]
    return [t for t in out if not shows_other(t, game)][:MAX_TV]


def sources_for(game, streams, cats, tv=()):
    """(kanaler, antal tv-kanaler) för en match: först tv-kanalerna som sänder den, i samma ordning som
    NHL anger dem, sedan matchströmmar som reserver."""
    by_id = {s["stream_id"]: s for s in streams}
    broadcast = [by_id[t["real"]] for t in broadcast_tv(game, tv) if t["real"] in by_id]
    hits = [s for s in streams if carries_game(s.get("name", ""), cats.get(s.get("category_id"), ""), game)]

    def rank(s):
        cat = cats.get(s.get("category_id"), "")
        if "SOCCER" in cat.upper():                # t.ex. "CA| SOCCER PPV": opålitliga, sist
            return SOURCE_ORDER.index(r"SOCCER")
        return next(i for i, rx in enumerate(SOURCE_ORDER) if re.search(rx, cat))
    hits.sort(key=rank)
    # NHL anger ibland strömningstjänsten Sportsnet+ som sändare; dess engelska ström räknas då som
    # huvudkanal, efter tv-kanalerna.
    if any(b.get("network", "").strip() == "SN+" for b in game.get("tvBroadcasts", [])):
        sn = [s for s in hits if is_snplus(s.get("name", "")) and "FRENCH" not in s["name"].upper()]
        broadcast += sn[:1]
    picked, used = list(broadcast), set()
    for s in hits:                               # först en per kategori …
        cat = s.get("category_id")
        if cat not in used and s not in picked:
            used.add(cat)
            picked.append(s)
    picked += [s for s in hits if s not in picked]   # … sedan resten
    for team in (game["homeTeam"], game["awayTeam"]):  # lagkanalerna sist, som reserv
        full = clean(f'{team["placeName"]["default"]} {team["commonName"]["default"]}')
        picked += [s for s in streams if s.get("name", "").startswith("NHL TEAM|")
                   and clean(s["name"].split("|", 1)[1]) == full]
    return picked[:min(MAX_SOURCES, len(broadcast) + MAX_RESERVES)], len(broadcast)


def short_source(stream, tv_codes=()):
    """Kort namn på kanalen/strömmen, t.ex. "TSN2", "RDS 2", "ESPN+ 097", "Telia SE"."""
    name = stream.get("name", "").strip()
    if tv_codes:                                   # tv-kanal: namnet utan land och bildkvalitet
        short = re.sub(r"^[A-Z]{2,4}\|\s*", "", name)
        short = re.sub(r"\s*\b(F?HD|UHD|4K)\b|[\u1d00-\u1dbf\u2070-\u209f]+", "", short).strip()
        nice = {"SCRIPPS:FLA": "WSFL 39 MIAMI", "Utah16": "UTAH 16 (KUPX)", "KONG": "KONG SEATTLE", "KTVD": "KTVD DENVER"}
        return nice.get(tv_codes[0]) or (short if len(short) <= 26 else tv_codes[0])
    up = name.upper()
    m = re.search(r"\(SN\+ (\d+)\)", up)
    if m:
        return f"SN+ {m[1]}" + (" (franska)" if "FRENCH" in up else "")
    for rx, fmt in ((r"ESPN\+ (?:PPV )?(\d+)", "ESPN+ {}"), (r"TELIA ?(?:PLAY|F)?\s*(SE|NO|DK|FI)\b", "Telia {}"),
                    (r"DISNEY\+ ?(SE|NO|DK|FI)\b", "Disney+ {}"), (r"\b(SE|NO|DK|FI): PLAY\+", "Viaplay {}"),
                    (r"\((SE|NO|DK|FI)\)", "Ström {}"), (r"\b(US|CA): SOCCER PPV", "PPV {}")):
        m = re.search(rx, up)
        if m:
            return fmt.format(m[1])
    if re.match(r"NHL \| \d+", up):
        return "NHL-flöde"
    if up.startswith("NHL TEAM|"):
        return "Lagkanal " + clean(name.split("|", 1)[1]).strip().title()
    return "Ström"


def short_game(game):
    return f'{game["awayTeam"]["abbrev"]} @ {game["homeTeam"]["abbrev"]}'


def label_of(game, stream, tv):
    """Kanalnamn i listan: bara kanalen, t.ex. "TSN2". Matchen står i tablån, som alltid är rätt för dagen."""
    codes = next((t["codes"] for t in tv if t["real"] == stream["stream_id"]), ())
    return short_source(stream, codes)


def title_of(game):
    away, home = game["awayTeam"], game["homeTeam"]
    return f'{away["placeName"]["default"]} {away["commonName"]["default"]} @ {home["placeName"]["default"]} {home["commonName"]["default"]}'


def broadcast_keys(game, b):
    """Koder som en sändning matchar: nätverket, och nätverket:laget för lokala sändningar."""
    net = b["network"].strip()
    team = {"H": game["homeTeam"], "A": game["awayTeam"]}.get(b.get("market"), {}).get("abbrev")
    return {net, f"{net}:{team}"} if team else {net}


def broadcast_text(game, tv):
    names = {c: t["name"].split("|", 1)[-1].strip() for t in tv for c in t["codes"]}
    return ", ".join(next((names[k] for k in broadcast_keys(game, b) if k in names), b["network"].strip())
                     for b in game.get("tvBroadcasts", []))


def game_window(date, game):
    day_start = datetime.fromisoformat(date).replace(hour=DAY_START_UTC, tzinfo=timezone.utc)
    start = datetime.fromisoformat(game["startTimeUTC"].replace("Z", "+00:00"))
    day_end = day_start + timedelta(days=1)
    return day_start, day_end, start, min(start + timedelta(hours=GAME_HOURS), day_end)


def label_programme(p, entry):
    """Sätter matchen först i titeln på ett program från en tv-kanals egen tablå."""
    try:
        start = datetime.strptime(p.get("start", "").strip(), "%Y%m%d%H%M%S %z")
        stop = datetime.strptime(p.get("stop", "").strip(), "%Y%m%d%H%M%S %z")
    except ValueError:
        return
    title = p.find("title")
    if title is None:
        title = ET.SubElement(p, "title")
    # Matchen som programmet överlappar mest (en kanal kan sända två matcher efter varandra).
    games = [entry] + entry.get("others", [])
    overlap = lambda g: (min(stop, g["stop"]) - max(start, g["start"])).total_seconds()
    best = max(games, key=overlap)
    if overlap(best) > 0 and best is not entry:               # kanalen visar en annan match just då
        title.text = f'{best["short"]} · {title.text or ""}'
    elif overlap(best) > 0:                                   # under matchen
        title.text = f'{entry["short"]} · {title.text or ""}'
        desc = p.find("desc")
        if desc is None:
            desc = ET.SubElement(p, "desc")
        desc.text = entry["desc"] + (f"\n\n{desc.text}" if desc.text else "")
    elif start < entry["start"]:                                  # före matchen samma dygn
        title.text = f'{title.text or ""} · sedan {entry["short"]} {entry["local"]}'


def epg_elements(schedule=None):
    """(kanalposter, programposter, tv-tablåer) för de fasta kanalerna, för hela veckans spelschema.

    Matchkanaler som är en tv-kanal (TSN2, RDS …) får den kanalens egen tablå, med matchen först i titeln.
    De byggs av make_playlist.py ur tv-tablåerna; här returneras bara vad som ska kopieras vart
    (lista med {src, target, from, to, start, stop, short, local, desc})."""
    if schedule is None:                         # i går också, så att tablån räcker för catch-up
        yesterday = (datetime.fromisoformat(hockey_date()) - timedelta(days=1)).date().isoformat()
        schedule = {**fetch_schedule(yesterday), **fetch_schedule(hockey_date())}
    tv = stored().get("tv", [])
    icons = {g["slot"]: g.get("icons") or [] for g in stored().get("games", [])}
    channels = []
    for slot in range(1, MAX_GAMES + 1):
        for v in range(MAX_SOURCES):
            ch = ET.Element("channel", {"id": epg_id(slot, v)})
            ET.SubElement(ch, "display-name").text = epg_id(slot, v)
            icon = icons.get(slot, [])[v] if v < len(icons.get(slot, [])) else ""
            ET.SubElement(ch, "icon", {"src": icon or NHL_LOGO})
            ch.tail = "\n"
            channels.append(ch)

    def prog(slot, v, start, stop, title, desc=None):
        return prog_for(epg_id(slot, v), start, stop, title, desc)

    def prog_for(channel, start, stop, title, desc=None):
        p = ET.Element("programme", {"start": start.strftime("%Y%m%d%H%M%S +0000"),
                                     "stop": stop.strftime("%Y%m%d%H%M%S +0000"), "channel": channel})
        ET.SubElement(p, "title", {"lang": "en"}).text = title
        if desc:
            ET.SubElement(p, "desc", {"lang": "en"}).text = desc
        p.tail = "\n"
        return p

    progs, overlay = [], []
    for date, games in schedule.items():
        for slot in range(1, MAX_GAMES + 1):
            if slot > len(games):
                day_start = datetime.fromisoformat(date).replace(hour=DAY_START_UTC, tzinfo=timezone.utc)
                progs += [prog(slot, v, day_start, day_start + timedelta(days=1), "No game")
                          for v in range(MAX_SOURCES)]
                continue
            game = games[slot - 1]
            day_start, day_end, start, stop = game_window(date, game)
            short, title = short_game(game), title_of(game)
            local = start.astimezone(SE_TZ)
            desc = f"{title}. {local:%a %d %b %H:%M} svensk tid. TV: {broadcast_text(game, tv)}"
            entry = {"from": day_start, "to": day_end, "start": start, "stop": stop, "short": short,
                     "local": f"{local:%H:%M}", "desc": desc}
            # Tv-kanalens egen tablå på tv-gruppens kanal, med matchen först i titeln.
            channels_tv = broadcast_tv(game, tv)
            overlay += [{**entry, "src": t["epg"], "target": t["tvg"]} for t in channels_tv if t["epg"]]
            for v in range(MAX_SOURCES):
                if v < len(channels_tv) and channels_tv[v]["epg"]:
                    overlay.append({**entry, "src": channels_tv[v]["epg"], "target": epg_id(slot, v)})
                    continue
                if start > day_start:
                    progs.append(prog(slot, v, day_start, start, f"{local:%H:%M} {short} · {title}", desc))
                progs.append(prog(slot, v, start, stop, f"{short} · {title}", desc))
                if stop < day_end:
                    progs.append(prog(slot, v, stop, day_end, f"Slut: {short}"))
    # Tv-gruppens kanaler utan egen tablå: matcherna de sänder, ur NHL:s schema.
    for t in tv:
        if t["epg"]:
            continue
        ch = ET.Element("channel", {"id": t["tvg"]})
        ET.SubElement(ch, "display-name").text = t["tvg"]
        if t.get("icon"):
            ET.SubElement(ch, "icon", {"src": t["icon"]})
        ch.tail = "\n"
        channels.append(ch)
        for date, games in schedule.items():
            day_start = datetime.fromisoformat(date).replace(hour=DAY_START_UTC, tzinfo=timezone.utc)
            pos = day_start
            for game in games:
                if t not in broadcast_tv(game, tv):
                    continue
                _, day_end, start, stop = game_window(date, game)
                if start < pos:
                    continue
                short, title = short_game(game), title_of(game)
                desc = f"{title}. {start.astimezone(SE_TZ):%a %d %b %H:%M} svensk tid."
                if start > pos:
                    progs.append(prog_for(t["tvg"], pos, start, f"{start.astimezone(SE_TZ):%H:%M} {short}", desc))
                progs.append(prog_for(t["tvg"], start, stop, f"{short} · {title}", desc))
                pos = stop
            if pos < day_start + timedelta(days=1):
                progs.append(prog_for(t["tvg"], pos, day_start + timedelta(days=1), "Ingen NHL-match"))
    # Samma tv-kanal kan sända flera matcher samma dygn (t.ex. Sportsnet): de andra matcherna märks med sitt namn.
    for e in overlay:
        e["others"] = [{"start": o["start"], "stop": o["stop"], "short": o["short"]} for o in overlay
                       if o["src"] == e["src"] and o["from"] == e["from"] and o["short"] != e["short"]]
    return channels, progs, overlay


def build(streams, cats, schedule=None):
    """Dagens koppling mellan fasta kanaler och riktiga kanaler."""
    schedule = schedule or fetch_schedule(hockey_date())
    games = schedule.get(hockey_date(), [])
    tv = find_tv(streams, cats)
    mapping, report = {t["id"]: t["real"] for t in tv}, []
    mapping = {str(k): v for k, v in mapping.items()}
    for slot, game in enumerate(games[:MAX_GAMES], 1):
        sources, main = sources_for(game, streams, cats, tv)
        for v, s in enumerate(sources):
            mapping[str(virtual_id(slot, v))] = s["stream_id"]
        report.append({"slot": slot, "game": title_of(game), "start": game["startTimeUTC"],
                       "sources": [s["name"] for s in sources], "labels": [label_of(game, s, tv) for s in sources], "main": main, "archive": [archive_days(s) for s in sources],
                       "icons": [s.get("stream_icon") or "" for s in sources]})
    today = hockey_date()
    history = {d: m for d, m in stored().get("history", {}).items()
               if d >= (datetime.fromisoformat(today) - timedelta(days=HISTORY_DAYS)).date().isoformat()}
    history[today] = mapping
    return {"date": today, "map": mapping, "games": report, "tv": tv, "history": history}


def main():
    from analyze import api, load_env
    env = load_env()
    cats = {c["category_id"]: c["category_name"] for c in api(env, "get_live_categories")}
    streams = api(env, "get_live_streams")
    data = build(streams, cats)
    SERVE.mkdir(exist_ok=True)
    tmp = SERVE / "nhl.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(MAP_FILE)
    print(f'{len(data["tv"])} tv-kanaler: ' + ", ".join(t["name"] for t in data["tv"]))
    print(f'{data["date"]}: {len(data["games"])} matcher, {sum(bool(g["sources"]) for g in data["games"])} med källa')
    try:                                         # svenska ligor (swe.py) med samma kanallista
        import swe
        swe.update(streams, cats)
    except Exception as e:
        print(f"VARNING: svenska ligorna kunde inte uppdateras: {e}")
    for g in data["games"]:
        print(f'  NHL {g["slot"]:02d} {g["start"]} {g["game"]}')
        for n in g["sources"]:
            print(f"      {n[:90]}")


if __name__ == "__main__":
    main()
