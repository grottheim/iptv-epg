#!/usr/bin/env python3
"""Bildkontroll av NHL:s tv-kanaler: vilken match visar de egentligen?

Leverantörens kanalnamn och tablåer stämmer inte alltid. Till exempel visade "US| MSG 2 HD" Devils-matchen
från MSG Sportsnet när tablån sa Rangers, och Rangers låg i stället på "TV| MSG ALT-1". Under pågående matcher
tar skriptet därför en stillbild från tv-kanalerna och läser lagförkortningarna på resultatskylten (NYR, BUF …)
med tesseract. Resultatet sparas i serve/verify.json och används av nhl.py:
  - en kanal som bevisligen visar en annan match tas bort från den match NHL:s lista säger,
  - en kanal som bevisligen visar en match läggs till som huvudkanal för den.

Leverantören tillåter bara en ström åt gången. Kontrollen körs därför bara när ingen ström är igång
(active_cons), och mellanhanden (proxy.py) avbryter en pågående stillbild direkt när en kanal startas.
Körs före nhl.py i iptv-nhl.service, var 15:e minut.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nhl  # noqa: E402
from analyze import load_env  # noqa: E402

TIME_BUDGET = 300                   # sekunder per körning
KEEP_CHECKS = 3                     # sparade resultat per kanal
# Resultatskylten sitter oftast i ett hörn: övre vänstra (MSG, Sportsnet, TSN, NBC) eller nertill (ESPN, TNT).
REGIONS = ["iw*0.45:ih*0.2:0:0", "iw:ih*0.25:0:ih*0.75", "iw*0.45:ih*0.2:iw*0.55:0"]
# Ljus text på mörk botten och mörk text på ljus botten görs svart på vitt, som tesseract läser bäst.
THRESHOLDS = [r"gt(val\,170)", r"lt(val\,80)"]
# Hur lagen kan stå på skylten, och vanliga felläsningar (J läses ofta som U). Tvåbokstävers former väger
# lättare, eftersom sådana ord dyker upp i annan text också.
ALIASES = {"NJD": {"NJ", "NU", "NJD"}, "LAK": {"LA", "LAK"}, "TBL": {"TB", "TBL"}, "SJS": {"SJ", "SJS"},
           "CBJ": {"CBJ", "CBU"}, "WSH": {"WSH", "WAS"}, "VGK": {"VGK", "VEG"}, "UTA": {"UTA", "UTAH"}}


def env_url(env, path):
    return f'{env["XTREAM_URL"]}{path}'


def active_streams(env):
    url = env_url(env, f'/player_api.php?username={env["XTREAM_USER"]}&password={env["XTREAM_PASS"]}')
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": nhl.UA}), timeout=20) as r:
        return int(json.load(r)["user_info"].get("active_cons") or 0)


def user_recently_started(minutes=3):
    """Har mellanhanden skickat vidare en ström nyss? (active_cons kan släpa efter.)"""
    try:
        return time.time() - nhl.VIEWING_FILE.stat().st_mtime < minutes * 60
    except OSError:
        return False


def grab(env, sid, out):
    """En stillbild ur strömmen. PID-filen låter mellanhanden avbryta direkt om en kanal startas."""
    url = env_url(env, f'/live/{env["XTREAM_USER"]}/{env["XTREAM_PASS"]}/{sid}.ts')
    p = subprocess.Popen(["ffmpeg", "-nostdin", "-loglevel", "quiet", "-y", "-t", "15", "-i", url,
                          "-frames:v", "1", str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    nhl.GRAB_PID_FILE.write_text(str(p.pid))
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    finally:
        nhl.GRAB_PID_FILE.unlink(missing_ok=True)
    return p.returncode == 0 and out.exists()


def read_tokens(image, work):
    """Ord på 2–4 versaler i bildens hörn."""
    tokens = set()
    for region in REGIONS:
        for th in THRESHOLDS:
            crop = work / "crop.png"
            subprocess.run(["ffmpeg", "-loglevel", "quiet", "-y", "-i", str(image), "-vf",
                            f"crop={region},scale='min(1600,iw*2)':-1,format=gray,lutyuv=y=if({th}\\,0\\,255)", str(crop)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not crop.exists():
                continue
            txt = subprocess.run(["tesseract", str(crop), "-", "--psm", "11"], capture_output=True, timeout=60,
                                 env={**os.environ, "OMP_THREAD_LIMIT": "1"}).stdout.decode("utf-8", "replace")
            tokens |= set(re.findall(r"\b[A-Z]{2,4}\b", txt))
            crop.unlink(missing_ok=True)
    return tokens


def identify(tokens, games):
    """(spel-id, säker) för den pågående match vars lag syns, annars (None, False).
    Säker = båda lagen syns. Två matcher som båda syns (t.ex. en resultatrad) ger inget svar."""
    found = []
    for g in games:
        teams = []
        for side in ("awayTeam", "homeTeam"):
            ab = g[side]["abbrev"]
            forms = ALIASES.get(ab, set()) | {ab}
            hit = tokens & forms
            teams.append(2 if any(len(x) >= 3 for x in hit) else 1 if hit else 0)
        score = sum(teams)
        if score >= 2:
            found.append((g["id"], all(teams)))
    if len(found) != 1:
        return None, False
    return found[0]


def live_games(now):
    games = nhl.fetch_schedule(nhl.hockey_date()).get(nhl.hockey_date(), [])
    out = []
    for g in games:
        start = datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00"))
        if start - timedelta(minutes=5) <= now <= start + timedelta(hours=nhl.GAME_HOURS) \
                and g.get("gameState") not in ("OFF", "FINAL"):
            out.append(g)
    return out


def candidates(games, state):
    """Kanalerna att kontrollera: först de som just nu är kopplade till en pågående match och
    reservkanalerna utan egen kod, sedan övriga tv-kanaler. Inom varje del den som kollats längst sedan först."""
    data = nhl.stored()
    tv = data.get("tv", [])
    streams = json.loads((nhl.ROOT / "cache" / "live_streams.json").read_text())
    pool = [s["stream_id"] for s in streams if nhl.in_pool(s.get("name", ""))]
    live_codes = set()
    for g in games:
        for b in g.get("tvBroadcasts", []):
            live_codes |= nhl.broadcast_keys(g, b)
    first = [t["real"] for t in tv if live_codes & set(t["codes"])] + pool
    rest = [t["real"] for t in tv if t["real"] not in first]

    def last(sid):
        checks = state.get(str(sid), [])
        return checks[-1]["at"] if checks else 0
    return sorted(dict.fromkeys(first), key=last) + sorted(dict.fromkeys(rest), key=last)


def main():
    now = datetime.now(timezone.utc)
    games = live_games(now)
    if not games:
        print("Inga pågående matcher")
        return
    env = load_env()
    try:
        state = json.loads(nhl.VERIFY_FILE.read_text()).get("checks", {})
    except (OSError, ValueError):
        state = {}
    names = {t["real"]: t["name"] for t in nhl.stored().get("tv", [])}
    t0 = time.time()
    checked = 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for sid in candidates(games, state):
            if time.time() - t0 > TIME_BUDGET:
                break
            if user_recently_started() or active_streams(env):
                print("En ström är igång – kontrollen väntar till nästa gång")
                break
            image = work / f"{sid}.png"
            if not grab(env, sid, image):
                continue
            tokens = read_tokens(image, work)
            image.unlink(missing_ok=True)
            game, sure = identify(tokens, games)
            entry = {"at": time.time(), "game": game, "sure": sure, "tokens": sorted(tokens)[:30]}
            state[str(sid)] = (state.get(str(sid), []) + [entry])[-KEEP_CHECKS:]
            short = next((nhl.short_game(g) for g in games if g["id"] == game), "-")
            print(f"{sid:>8} {names.get(sid, '')[:34]:34} {short:10} {'säker' if sure else ''}")
            checked += 1
    # Gamla resultat (mer än ett dygn) rensas.
    state = {k: v for k, v in state.items() if v and time.time() - v[-1]["at"] < 86400}
    tmpf = nhl.VERIFY_FILE.with_suffix(".tmp")
    tmpf.write_text(json.dumps({"checks": state}, indent=1))
    tmpf.replace(nhl.VERIFY_FILE)
    print(f"{checked} kanaler kontrollerade på {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
