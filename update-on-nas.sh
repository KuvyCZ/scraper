#!/bin/sh
# Automatická aktualizace na Synology: stáhne nejnovější image z GitHubu
# (ghcr.io/kuvycz/scraper:latest) a když je nový, restartuje scraper s ním.
# Když nic nového není, nic se nestane — data (složka data/) zůstávají.
#
# Nastavení (jednou): Ovládací panel -> Plánovač úloh -> Vytvořit -> Naplánovaná úloha
# -> Uživatelem definovaný skript, uživatel root, denně, skript:
#     sh /volume1/docker/flip-scraper/update-on-nas.sh
# Aktualizovat hned: v Plánovači úloh vyber úlohu -> Spustit.

set -e
cd "$(dirname "$0")"

# Synology má docker v /usr/local/bin; Container Manager má buď `docker compose`, nebo `docker-compose`.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
else
  COMPOSE="docker-compose"
fi

echo "$(date '+%Y-%m-%d %H:%M')  checking for a new version…"
$COMPOSE pull
# restartuje jen když se image změnil; kontejner pojmenovaný jinak než projekt -> smazat a znovu
$COMPOSE up -d || { docker rm -f flip-scraper && $COMPOSE up -d; }
docker image prune -f >/dev/null 2>&1 || true   # smaže staré verze, ať nezabírají místo
echo "done"
