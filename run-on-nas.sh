#!/bin/sh
# Tohle volej ze Synology Task Scheduler (Control Panel -> Task Scheduler ->
# Create -> Scheduled Task -> User-defined script), opakování každé 2–4 h.
#
# Jednorázově spustí kontejner: stáhne, oskóruje, notifikuje a skončí.
# DB i .env se berou z adresáře tohoto skriptu (volume ./data přežije běhy).
#
# Cesta k dockeru na Synology bývá /usr/local/bin/docker (jinde /usr/bin/docker).

set -e
cd "$(dirname "$0")"

DOCKER=/usr/local/bin/docker
[ -x "$DOCKER" ] || DOCKER=docker

"$DOCKER" run --rm \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  realitni-scraper:latest
