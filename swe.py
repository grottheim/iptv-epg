#!/usr/bin/env python3
"""Svenska ligor (SHL, HockeyAllsvenskan, Allsvenskan i fotboll) som egna kanalgrupper, som NHL-kanalerna.

För varje dags matcher letas kanalerna upp som sänder dem:
- tv-kanaler (TV4 Hockey, TV4 Sport Live …) via deras egen tablå, där matchen står med liga och arena
  (svenska tablåer anger inte lagen), och
- matchströmmar hos leverantören (TV4 Play, Telia, Viaplay …) via lagnamnen i strömmens namn.

Resultatet skrivs till serve/swe.json och används av mellanhanden (proxy.py) som för NHL.
Spelschema: stats.swehockey.se (hockey) och ESPN (fotboll). Körs av nhl.py:s timer.
"""
import gzip
import html
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from epgcommon import ROOT, UA
from nhl import archive_days, archive_fields, clean, dates_in

SERVE = ROOT / "serve"
MAP_FILE = SERVE / "swe.json"
SE_TZ = ZoneInfo("Europe/Stockholm")
DAY_START_LOCAL = 6                             # ett "matchdygn" räknas från kl. 06 svensk tid
HISTORY_DAYS = 3
MAX_GAMES, MAX_TV, MAX_RESERVES = 9, 3, 4
SCHEDULE_MAX_AGE = 6 * 3600                     # spelschemat hämtas om högst var sjätte timme

LEAGUES = [
    {"key": "shl", "name": "SHL", "cat": "990011", "base": 992000000, "sport": "hockey",
     "swehockey": "SHL", "epg_word": r"\bSHL\b", "minutes": 150},
    {"key": "ha", "name": "HOCKEYALLSVENSKAN", "cat": "990012", "base": 993000000, "sport": "hockey",
     "swehockey": "HockeyAllsvenskan", "epg_word": r"HOCKEY ?ALLSVENSKAN", "minutes": 150},
    {"key": "allsv", "name": "ALLSVENSKAN", "cat": "990013", "base": 994000000, "sport": "football",
     "espn": "swe.1", "epg_word": r"(?<!HOCKEY)(?<!HOCKEY )(?<!DAM)ALLSVENSKAN", "minutes": 120},
]
# Ord i lagnamn som inte skiljer lagen åt.
STOP = set("IF IFK FF HC SK IK HK IS BK AIF FC HF SC HOCKEY BOIS GIF FK SPORT CLUB KLUBB VIKINGS IBK".split())
# Ord i arenanamn som inte skiljer arenorna åt.
VENUE_STOP = set("ARENA ARENAN IP STADION HALL HALLEN ISHALL ISSTADION NORD SYD THE OF".split())
TV_CATS = re.compile(r"^SWEDEN (SPORT )?HD & HEVC$")
FOOTBALL_HINT = re.compile(r"(?<!HOCKEY)(?<!HOCKEY )ALLSVENSKAN|FOTBOLL|SOCCER|\bFF\b", re.I)
HOCKEY_HINT = re.compile(r"HOCKEY|\bSHL\b|ETTAN", re.I)


def virtual_id(league, slot, variant=0):
    return league["base"] + slot * 10 + variant


def epg_id(league, slot, variant=0):
    return f'{league["key"]}{slot:02d}.game' if not variant else f'{league["key"]}{slot:02d}-{variant}.game'


def match_day(now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(SE_TZ)
    return (local - timedelta(hours=DAY_START_LOCAL)).date().isoformat()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 " + UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


# ---------- spelschema ----------

def swehockey_schedule(name):
    """Hela säsongens matcher för en liga från stats.swehockey.se (ligans sida hittas via startsidan)."""
    front = fetch("https://stats.swehockey.se/")
    m = re.search(r'Overview/(\d+)[^>]*>\s*' + re.escape(name) + r"\s*<", front)
    if not m:
        raise RuntimeError(f"hittar inte {name} på stats.swehockey.se")
    page = fetch(f"https://stats.swehockey.se/ScheduleAndResults/Schedule/{m[1]}")
    games, date = [], None
    for row in re.findall(r'<tr><td class="td(?:Odd|Normal) (?:standardP|p)addingTop.*?</tr>', page, re.S):
        cells = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split())
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        for c in cells:
            d = re.search(r"\b(\d{4}-\d\d-\d\d)\b", c)
            if d:
                date = d[1]
        clock = next((c for c in cells if re.fullmatch(r"\d\d:\d\d", c)), None)
        teams = next((c for c in cells if re.fullmatch(r".*[A-Za-zÅÄÖåäö].* - .*[A-Za-zÅÄÖåäö].*", c)), None)
        if not (date and clock and teams):
            continue
        home, away = [x.strip() for x in teams.split(" - ", 1)]
        start = datetime.fromisoformat(f"{date}T{clock}").replace(tzinfo=SE_TZ).astimezone(timezone.utc)
        venue = cells[-1] if cells[-1] != teams and not re.search(r"\d - \d|^\d+$", cells[-1]) else ""
        games.append({"start": start.isoformat(), "home": home, "away": away, "venue": venue, "city": ""})
    return games


def espn_schedule(slug, days):
    games = []
    for d in days:
        data = json.loads(fetch(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"
                                f"?dates={d.replace('-', '')}"))
        for e in data.get("events", []):
            c = e["competitions"][0]
            team = {x["homeAway"]: x["team"]["displayName"] for x in c["competitors"]}
            venue = c.get("venue", {})
            games.append({"start": datetime.fromisoformat(e["date"].replace("Z", "+00:00")).isoformat(),
                          "home": team.get("home", ""), "away": team.get("away", ""),
                          "venue": venue.get("fullName", ""), "city": venue.get("address", {}).get("city", "")})
    return games


def schedule(league):
    """Ligans matcher (cachade i serve/swe_schedule_<liga>.json)."""
    cache = SERVE / f'swe_schedule_{league["key"]}.json'
    try:
        if time.time() - cache.stat().st_mtime < SCHEDULE_MAX_AGE:
            return json.loads(cache.read_text())
    except (OSError, ValueError):
        pass
    try:
        if league.get("swehockey"):
            games = swehockey_schedule(league["swehockey"])
        else:
            today = datetime.fromisoformat(match_day())
            days = [(today + timedelta(days=i)).date().isoformat() for i in range(-1, 8)]
            games = espn_schedule(league["espn"], days)
    except Exception as e:                       # källan nere: använd förra schemat
        print(f'VARNING: schemat för {league["name"]} kunde inte hämtas: {e}')
        try:
            return json.loads(cache.read_text())
        except (OSError, ValueError):
            return []
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(games, ensure_ascii=False))
    tmp.replace(cache)
    return games


def games_on(games, day):
    out = [g for g in games if match_day(datetime.fromisoformat(g["start"])) == day]
    return sorted(out, key=lambda g: (g["start"], g["home"]))[:MAX_GAMES]


# ---------- namn ----------

def squash(text):
    """clean() och "HV 71" -> "HV71", så att lag med siffror i namnet känns igen hur de än skrivs."""
    return re.sub(r" ([A-Z]+) (\d+) ", r" \1\2 ", clean(text))


def keys(team):
    """Lagets särskiljande ord (versaler, utan å/ä/ö), t.ex. "Färjestad BK" -> ["FARJESTAD"]."""
    words = [w for w in squash(team).split() if w not in STOP]
    return words or squash(team).split()


def short(team):
    """Kort lagnamn för titlar: "Färjestad BK" -> "Färjestad", "IFK Göteborg" -> "Göteborg"."""
    words = [w for w in team.split() if clean(w).strip() not in STOP]
    return " ".join(words) or team


def title(g):
    return f'{short(g["home"])} - {short(g["away"])}'


def has_team(text, team):
    """Står laget i texten? Ord jämförs på de första sex bokstäverna (Djurgården/Djurgårdens, HV 71/HV71)."""
    flat_words = squash(text).split()
    for k in keys(team):
        if len(k) <= 4:                              # korta namn (AIK, HV71, MODO) måste stå som eget ord
            if k in flat_words:
                if k == "AIK" and "SKELLEFTEA" in flat_words:
                    continue
                return True
        elif any(w[:6] == k[:6] for w in flat_words if len(w) >= 4):
            return True
    return False


# ---------- kanaler ----------

def carries(stream_name, category, g, league):
    """Är strömmen den här matchen? Båda lagen ska stå i namnet, rätt sport och inget annat datum."""
    if re.match(r"\s*(END|ENDED)\b", stream_name, re.I) or "NO EVENT" in stream_name.upper():
        return False
    text = f"{stream_name} {category}"
    if league["sport"] == "hockey" and FOOTBALL_HINT.search(text):
        return False
    if league["sport"] == "football" and HOCKEY_HINT.search(text):
        return False
    if not (has_team(stream_name, g["home"]) and has_team(stream_name, g["away"])):
        return False
    start = datetime.fromisoformat(g["start"])
    dates = dates_in(stream_name)
    return not dates or bool(dates & {(d.month, d.day) for d in (start, start.astimezone(SE_TZ))})


class SwedishEpg:
    """Program för svenska kanaler ur mellanhandens EPG (serve/epg-full.xml.gz), omläst när filen ändras."""
    path = SERVE / "epg-full.xml.gz"
    mtime, data = None, {}

    @classmethod
    def get(cls, wanted):
        try:
            m = cls.path.stat().st_mtime
        except OSError:
            return {}
        if m != cls.mtime:
            data = {}
            try:
                with gzip.open(cls.path) as f:
                    for _, el in ET.iterparse(f):
                        if el.tag == "programme" and el.get("channel") in wanted:
                            try:
                                data.setdefault(el.get("channel"), []).append((
                                    datetime.strptime(el.get("start"), "%Y%m%d%H%M%S %z"),
                                    datetime.strptime(el.get("stop"), "%Y%m%d%H%M%S %z"),
                                    f'{el.findtext("title") or ""} {el.findtext("desc") or ""}'))
                            except (TypeError, ValueError):
                                pass
                        if el.tag in ("programme", "channel"):
                            el.clear()
            except (OSError, ET.ParseError):
                return cls.data
            cls.data, cls.mtime = data, m
        return cls.data


def tv_channels(streams, cats):
    """Svenska tv-kanaler: EPG-id -> leverantörens bästa ström (HD före HEVC och SD)."""
    try:
        filled = json.loads((SERVE / "epg_ids.json").read_text())
    except (OSError, ValueError):
        filled = {}
    best = {}
    for s in streams:
        if not TV_CATS.match(cats.get(s.get("category_id"), "")):
            continue
        eid = (s.get("epg_channel_id") or "").strip() or filled.get(str(s.get("stream_id")), "")
        if not eid:
            continue
        name = s.get("name", "")
        rank = ("HEVC" in name, " HD" not in name, not archive_days(s), len(name))
        if eid not in best or rank < best[eid][0]:
            best[eid] = (rank, s)
    return {eid: s for eid, (_, s) in best.items()}


def venue_words(g):
    words = [w for w in clean(g["venue"]).split() if w not in VENUE_STOP and (len(w) >= 4 or any(c.isdigit() for c in w))]
    return words + ([clean(g["city"]).strip()] if g.get("city") else [])


def tv_for(g, league, tv, epg):
    """Tv-kanalerna som enligt sin tablå sänder matchen: ligan + arenan/staden/hemmalaget vid starttiden."""
    start = datetime.fromisoformat(g["start"])
    word = re.compile(league["epg_word"], re.I)
    places = venue_words(g)
    out = []
    for eid, s in tv.items():
        for a, b, text in epg.get(eid, []):
            if not (start - timedelta(minutes=100) <= a <= start + timedelta(minutes=45)):
                continue
            up = clean(text)
            if word.search(text) and (any(f" {p} " in up for p in places) or has_team(text, g["home"])):
                out.append(s)
                break
    out.sort(key=lambda s: s.get("name", ""))
    return out[:MAX_TV]


def source_rank(s, cats):
    cat = cats.get(s.get("category_id"), "")
    name = s.get("name", "")
    if "SOCCER" in cat.upper():                  # opålitliga PPV-strömmar sist
        return 9
    if cat.startswith("SWEDEN") or "(SE)" in name or " SE " in f" {name} ":
        return 0
    return 1


def label(s, is_tv):
    """Kort kanalnamn: "TV4 HOCKEY", "TV4 Play", "Telia SE" …"""
    name = s.get("name", "").strip()
    if is_tv:
        n = re.sub(r"^[A-Z]{2,4}\|\s*", "", name)
        n = re.sub(r"\s*\b(F?HD|UHD|HEVC|4K)\b", "", n).replace("TV 4", "TV4").strip()
        return n
    up = name.upper()
    for rx, txt in ((r"TV ?4 PLAY", "TV4 Play"), (r"PLAY\+", "Viaplay"), (r"VIAPLAY", "Viaplay"),
                    (r"TELIA ?(?:PLAY|F)?\s*(SE|NO|DK|FI)\b", "Telia {}"), (r"\bMAX\b", "Max"),
                    (r"DISCOVERY", "Discovery+"), (r"SVENSK HOCKEY", "Svensk Hockey"),
                    (r"AFTONBLADET", "Aftonbladet"), (r"EXPRESSEN", "Expressen"), (r"DAZN", "DAZN"),
                    (r"\((SE|NO|DK|FI|NL)\)", "Ström {}")):
        m = re.search(rx, up)
        if m:
            return txt.format(*m.groups()) if m.groups() else txt
    return "Ström"


def numbered(labels):
    """Samma namn flera gånger får nummer: "TV4 Play", "TV4 Play 2"."""
    seen, out = {}, []
    for x in labels:
        seen[x] = seen.get(x, 0) + 1
        out.append(x if seen[x] == 1 else f"{x} {seen[x]}")
    return out


def sources_for(g, league, streams, cats, tv, epg):
    broadcast = tv_for(g, league, tv, epg)
    hits = [s for s in streams if s not in broadcast
            and carries(s.get("name", ""), cats.get(s.get("category_id"), ""), g, league)]
    hits.sort(key=lambda s: source_rank(s, cats))
    picked, used = [], set()
    for s in hits:                               # en per kategori först, så att reserverna är oberoende
        if s.get("category_id") not in used:
            used.add(s.get("category_id"))
            picked.append(s)
    picked += [s for s in hits if s not in picked]
    return broadcast, picked[:MAX_RESERVES]


# ---------- byggen ----------

def build(streams, cats):
    tv = tv_channels(streams, cats)
    epg = SwedishEpg.get(set(tv))
    day = match_day()
    old = stored()
    data = {"date": day, "map": {}, "leagues": {}, "history": {}}
    for league in LEAGUES:
        report = []
        for slot, g in enumerate(games_on(schedule(league), day), 1):
            broadcast, reserves = sources_for(g, league, streams, cats, tv, epg)
            sources = broadcast + reserves
            for v, s in enumerate(sources):
                data["map"][str(virtual_id(league, slot, v))] = s["stream_id"]
            report.append({"slot": slot, "game": title(g), "start": g["start"], "main": len(broadcast),
                           "labels": numbered([label(s, i < len(broadcast)) for i, s in enumerate(sources)]),
                           "sources": [s.get("name", "").strip() for s in sources],
                           "archive": [archive_days(s) for s in sources],
                           "icons": [s.get("stream_icon") or "" for s in sources]})
        data["leagues"][league["key"]] = report
    keep = (datetime.fromisoformat(day) - timedelta(days=HISTORY_DAYS)).date().isoformat()
    data["history"] = {d: m for d, m in old.get("history", {}).items() if d >= keep}
    data["history"][day] = data["map"]
    return data


def stored():
    try:
        return json.loads(MAP_FILE.read_text())
    except (OSError, ValueError):
        return {}


def virtual_categories():
    return [{"category_id": lg["cat"], "category_name": f'{lg["name"]} - DAGENS MATCHER', "parent_id": 0}
            for lg in LEAGUES]


def categories():
    return {lg["cat"] for lg in LEAGUES}


def virtual_streams():
    out = []
    data = stored()
    for league in LEAGUES:
        for g in data.get("leagues", {}).get(league["key"], []):
            labels = g.get("labels") or [f'Ingen kanal hittad: {g["game"]}']
            for v, name in enumerate(labels):
                archive, icons = g.get("archive", []), g.get("icons", [])
                vid = virtual_id(league, g["slot"], v)
                out.append({
                    "num": vid, "name": name, "stream_type": "live", "stream_id": vid,
                    "epg_channel_id": epg_id(league, g["slot"], v),
                    "stream_icon": (icons[v] if v < len(icons) else "") or "",
                    "added": "0", "is_adult": 0, "category_id": league["cat"],
                    "category_ids": [int(league["cat"])], "custom_sid": "", "direct_source": "",
                    **archive_fields(archive[v] if v < len(archive) else 0),
                })
    return out


def owns(stream_id):
    return 992000000 <= int(stream_id) < 995000000


def resolve(stream_id, when=None):
    data = stored()
    if when is not None:
        return data.get("history", {}).get(match_day(when), {}).get(str(stream_id))
    return data.get("map", {}).get(str(stream_id))


def epg_elements():
    """(kanalposter, programposter) för ligornas kanaler, för i går och veckan framåt."""
    channels, progs = [], []
    today = datetime.fromisoformat(match_day())
    days = [(today + timedelta(days=i)).date().isoformat() for i in range(-1, 7)]
    data = stored()
    for league in LEAGUES:
        games = schedule(league)
        icons = {g["slot"]: g.get("icons") or [] for g in data.get("leagues", {}).get(league["key"], [])}
        for slot in range(1, MAX_GAMES + 1):
            for v in range(MAX_TV + MAX_RESERVES):
                ch = ET.Element("channel", {"id": epg_id(league, slot, v)})
                ET.SubElement(ch, "display-name").text = epg_id(league, slot, v)
                icon = icons.get(slot, [])[v] if v < len(icons.get(slot, [])) else ""
                if icon:
                    ET.SubElement(ch, "icon", {"src": icon})
                ch.tail = "\n"
                channels.append(ch)
        for day in days:
            day_start = datetime.fromisoformat(day).replace(hour=DAY_START_LOCAL, tzinfo=SE_TZ).astimezone(timezone.utc)
            day_end = day_start + timedelta(days=1)
            todays = games_on(games, day)
            for slot in range(1, MAX_GAMES + 1):
                g = todays[slot - 1] if slot <= len(todays) else None
                if g:
                    start = datetime.fromisoformat(g["start"])
                    stop = min(start + timedelta(minutes=league["minutes"]), day_end)
                    local = start.astimezone(SE_TZ)
                    desc = f'{league["name"].title() if league["key"] != "shl" else "SHL"}: {g["home"]} - {g["away"]}' \
                           f'{", " + g["venue"] if g["venue"] else ""}. {local:%a %d %b %H:%M} svensk tid.'
                    parts = [(day_start, start, f"{local:%H:%M} {title(g)}"), (start, stop, title(g)),
                             (stop, day_end, f"Slut: {title(g)}")]
                else:
                    desc, parts = None, [(day_start, day_end, "Ingen match")]
                for a, b, text in parts:
                    if b <= a:
                        continue
                    for v in range(MAX_TV + MAX_RESERVES):
                        p = ET.Element("programme", {"start": a.strftime("%Y%m%d%H%M%S +0000"),
                                                     "stop": b.strftime("%Y%m%d%H%M%S +0000"),
                                                     "channel": epg_id(league, slot, v)})
                        ET.SubElement(p, "title", {"lang": "sv"}).text = text
                        if desc:
                            ET.SubElement(p, "desc", {"lang": "sv"}).text = desc
                        p.tail = "\n"
                        progs.append(p)
    return channels, progs


def update(streams, cats):
    data = build(streams, cats)
    SERVE.mkdir(exist_ok=True)
    tmp = SERVE / "swe.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(MAP_FILE)
    return data


def main():
    from analyze import api, load_env
    env = load_env()
    cats = {c["category_id"]: c["category_name"] for c in api(env, "get_live_categories")}
    data = update(api(env, "get_live_streams"), cats)
    for key, games in data["leagues"].items():
        print(f"{key}: {len(games)} matcher {data['date']}")
        for g in games:
            print(f'  {g["start"][11:16]} {g["game"]}: {g["labels"]}')


if __name__ == "__main__":
    main()
