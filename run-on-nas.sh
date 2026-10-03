#!/bin/sh
# Alternativa ke stále běžícímu kontejneru: volej ze Synology Task Scheduler
# (Control Panel -> Task Scheduler -> Create -> Scheduled Task -> User-defined script),
# opakování např. každou hodinu. Jeden průchod všemi zdroji a konec.
#
# Cesta k dockeru na Synology bývá /usr/local/bin/docker (jinde /usr/bin/docker).

set -e
cd "$(dirname "$0")"

DOCKER=/usr/local/bin/docker
[ -x "$DOCKER" ] || DOCKER=docker

"$DOCKER" run --rm \
  --env-file .env \
  -e DB_PATH=data/flip.db \
  -v "$(pwd)/data:/app/data" \
  flip-scraper:latest python main.py once
