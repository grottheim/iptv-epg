#!/bin/sh
# Bygger EPG-filerna för de länder mellanhanden använder direkt på servern (serve/epg/), ett land i taget
# för att hålla nere minnet. iptv-epg.org spärrar GitHubs servrar, så GitHub Pages saknar numera den källan.
# Körs av iptv-epgbuild.timer; make_playlist.py använder filerna före GitHub Pages när de är färska.
set -u
cd /opt/iptv || exit 1
curl -sfL -o mapping.tmp https://raw.githubusercontent.com/grottheim/iptv-epg/main/mapping.json \
    && mv mapping.tmp mapping.json
mkdir -p serve/epg
status=0
for c in se hockey ca us; do
    python3 build_epg.py --countries "$c" --out serve/epg || status=1
done
exit $status
