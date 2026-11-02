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
MAX_SOURCES = 5                                 # huvudkanal + upp till fyra alternativ per match
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
    (("MSG",), r"^US\| MSG HD"), (("MSG 2", "MSG2"), r"^US\| MSG 2 HD"), (("MSGSN",), r"^TV\| MSG SPORTSNET"),
    (("NBCSP",), r"^US\| NBC SPORTS PHILADELFIA"), (("NBCSCA",), r"^US\| NBC SPORTS CALIFORNIA$"),
    (("SN-PIT",), r"^US\| SPORTSNET PITTSBURGH"), (("MNMT",), r"^US\| MONUMENTAL SPORTS NETWORK"),
    (("CHSN",), r"^US\| CHICAGO SPORTS NETWORK HD"), (("DSN",), r"^US\| DETROIT SPORTSNET"),
    (("ALT",), r"^US\| ALTITUDE SPORTS HD"), (("ALT2",), r"^US\| ALTITUDE SPORTS 2"),
    (("KONG",), r"^TV\| SEATTLE, WA KONG"), (("KING",), r"^US\| NBC 5 SEATTLE"),
    (("KTVD",), r"^TV\| DENVER, CO KTVD.*\(D11\)"), (("KUSA",), r"^US\| NBC 9 DENVER"),
    (("KSTP",), r"^US\| ABC 5 MINNEAPOLIS"), (("KTTV",), r"^US\| FOX 11 LOS ANGELES"),
    (("Utah16",), r"^TV\| SALT LAKE CITY, UT KUPX"), (("KDFI",), r"KDFI"),
]
# Matchfeeds hos leverantören; bästa källan först. Kategorier blandas för att reserverna ska vara oberoende.
SOURCE_ORDER = [r"^US\| NHL PACKAGE", r"^US\| ESPN", r"^CA\|", r"^US\|", r"NHL|DAZN|SOCCER", r"^SWEDEN", r"."]
OTHER_SPORT = re.compile(r"\b(NFL|NBA|MLB|WNBA|NCAA|RUGBY|BASKETBALL|FOOTBALL|SOCCER:|AHL|OHL|WHL|QMJHL|JR)\b")
LOWER_LEAGUE_CAT = re.compile(r"AHL|OHL|WHL|QMJHL|HOCKEY LEAGUE|FLO |NFL|NBA|MLB|NCAA", re.I)
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def virtual_id(slot, variant=0):
    return BASE_ID + slot * 10 + variant


def epg_id(slot):
    return f"nhl{slot:02d}.game"


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


def find_tv(streams, cats):
    """Leverantörens kanal för varje tv-kanal i TV_CHANNELS: helst med EPG-id och i HD."""
    out = []
    for i, (codes, rx) in enumerate(TV_CHANNELS):
        cands = [s for s in streams if re.search(rx, s.get("name", "").strip())
                 and not re.search(r"PPV|PACKAGE", cats.get(s.get("category_id"), ""))]
        if cands:
            best = min(cands, key=lambda s: (not (s.get("epg_channel_id") or "").strip(), not archive_days(s),
                                             "RAW" in clean(s["name"]), len(s["name"])))
            out.append({"id": TV_BASE_ID + i, "real": best["stream_id"], "name": best["name"].strip(),
                        "epg": (best.get("epg_channel_id") or "").strip(), "icon": best.get("stream_icon") or "",
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
    # Huvudkanalen ligger i "DAGENS MATCHER", alternativen i "RESERVKANALER".
    for g in stored().get("games", []):
        names = g.get("sources") or [f'Ingen kanal hittad: {g["game"]}']
        archive = g.get("archive", [])
        icons = g.get("icons", [])
        for v, name in enumerate(names[:MAX_SOURCES]):
            category = CATEGORY_B if v else CATEGORY_ID
            out.append({
                "num": virtual_id(g["slot"], v), "name": name.strip(), "stream_type": "live",
                "stream_id": virtual_id(g["slot"], v), "epg_channel_id": epg_id(g["slot"]),
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


def carries_game(stream_name, category, game):
    """Är det här kanalen för matchen? Båda lagen måste nämnas, och ett eventuellt datum måste stämma."""
    if re.match(r"\s*(END|ENDED)\b", stream_name, re.I) or "NO EVENT" in stream_name.upper() or "•" in stream_name:
        return False
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
    start = datetime.fromisoformat(game["startTimeUTC"].replace("Z", "+00:00"))
    dates = dates_in(stream_name)
    allowed = {(d.month, d.day) for d in (start, start.astimezone(SE_TZ), start - timedelta(hours=5))}
    return not dates or bool(dates & allowed)


def sources_for(game, streams, cats, tv=()):
    """Upp till tre kanaler för en match: först tv-kanalen som sänder den, sedan matchströmmar."""
    codes = {b["network"].strip() for b in game.get("tvBroadcasts", [])}
    real = {t["real"] for t in tv if codes & set(t["codes"])}
    broadcast = [s for s in streams if s["stream_id"] in real]
    hits = [s for s in streams if carries_game(s.get("name", ""), cats.get(s.get("category_id"), ""), game)]

    def rank(s):
        cat = cats.get(s.get("category_id"), "")
        return next(i for i, rx in enumerate(SOURCE_ORDER) if re.search(rx, cat))
    hits.sort(key=rank)
    picked, used = list(broadcast), set()
    for s in hits:                               # först en per kategori …
        cat = s.get("category_id")
        if cat not in used:
            used.add(cat)
            picked.append(s)
    picked += [s for s in hits if s not in picked]   # … sedan resten
    for team in (game["homeTeam"], game["awayTeam"]):  # lagkanalerna sist, som reserv
        full = clean(f'{team["placeName"]["default"]} {team["commonName"]["default"]}')
        picked += [s for s in streams if s.get("name", "").startswith("NHL TEAM|")
                   and clean(s["name"].split("|", 1)[1]) == full]
    return picked[:MAX_SOURCES]


def title_of(game):
    away, home = game["awayTeam"], game["homeTeam"]
    return f'{away["placeName"]["default"]} {away["commonName"]["default"]} @ {home["placeName"]["default"]} {home["commonName"]["default"]}'


def broadcast_text(game, tv):
    names = {c: t["name"].split("|", 1)[-1].strip() for t in tv for c in t["codes"]}
    return ", ".join(names.get(b["network"].strip(), b["network"].strip()) for b in game.get("tvBroadcasts", []))


def epg_elements(schedule=None):
    """(kanalposter, programposter) för de fasta kanalerna, för hela veckans spelschema."""
    if schedule is None:                         # i går också, så att tablån räcker för catch-up
        yesterday = (datetime.fromisoformat(hockey_date()) - timedelta(days=1)).date().isoformat()
        schedule = {**fetch_schedule(yesterday), **fetch_schedule(hockey_date())}
    tv = stored().get("tv", [])
    main_icon = {g["slot"]: (g.get("icons") or [""])[0] for g in stored().get("games", [])}
    channels = []
    for slot in range(1, MAX_GAMES + 1):
        ch = ET.Element("channel", {"id": epg_id(slot)})
        ET.SubElement(ch, "display-name").text = epg_id(slot)
        ET.SubElement(ch, "icon", {"src": main_icon.get(slot) or NHL_LOGO})
        ch.tail = "\n"
        channels.append(ch)

    def prog(slot, start, stop, title, desc=None):
        p = ET.Element("programme", {"start": start.strftime("%Y%m%d%H%M%S +0000"),
                                     "stop": stop.strftime("%Y%m%d%H%M%S +0000"), "channel": epg_id(slot)})
        ET.SubElement(p, "title", {"lang": "en"}).text = title
        if desc:
            ET.SubElement(p, "desc", {"lang": "en"}).text = desc
        p.tail = "\n"
        return p

    progs = []
    for date, games in schedule.items():
        day_start = datetime.fromisoformat(date).replace(hour=DAY_START_UTC, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        for slot in range(1, MAX_GAMES + 1):
            if slot > len(games):
                progs.append(prog(slot, day_start, day_end, "No game"))
                continue
            game = games[slot - 1]
            title = title_of(game)
            start = datetime.fromisoformat(game["startTimeUTC"].replace("Z", "+00:00"))
            stop = min(start + timedelta(hours=GAME_HOURS), day_end)
            local = start.astimezone(SE_TZ)
            desc = f"{local:%a %d %b %H:%M} svensk tid. TV: {broadcast_text(game, tv)}"
            if start > day_start:
                progs.append(prog(slot, day_start, start, f"{local:%H:%M}  {title}", desc))
            progs.append(prog(slot, start, stop, title, desc))
            if stop < day_end:
                progs.append(prog(slot, stop, day_end, f"Ended: {title}"))
    return channels, progs


def build(streams, cats, schedule=None):
    """Dagens koppling mellan fasta kanaler och riktiga kanaler."""
    schedule = schedule or fetch_schedule(hockey_date())
    games = schedule.get(hockey_date(), [])
    tv = find_tv(streams, cats)
    mapping, report = {t["id"]: t["real"] for t in tv}, []
    mapping = {str(k): v for k, v in mapping.items()}
    for slot, game in enumerate(games[:MAX_GAMES], 1):
        sources = sources_for(game, streams, cats, tv)
        for v, s in enumerate(sources):
            mapping[str(virtual_id(slot, v))] = s["stream_id"]
        report.append({"slot": slot, "game": title_of(game), "start": game["startTimeUTC"],
                       "sources": [s["name"] for s in sources], "archive": [archive_days(s) for s in sources],
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
    data = build(api(env, "get_live_streams"), cats)
    SERVE.mkdir(exist_ok=True)
    tmp = SERVE / "nhl.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(MAP_FILE)
    print(f'{len(data["tv"])} tv-kanaler: ' + ", ".join(t["name"] for t in data["tv"]))
    print(f'{data["date"]}: {len(data["games"])} matcher, {sum(bool(g["sources"]) for g in data["games"])} med källa')
    for g in data["games"]:
        print(f'  NHL {g["slot"]:02d} {g["start"]} {g["game"]}')
        for n in g["sources"]:
            print(f"      {n[:90]}")


if __name__ == "__main__":
    main()
