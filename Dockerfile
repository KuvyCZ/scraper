# Realitní scraper – jednorázový běh (spouští Synology Task Scheduler každé 2–4 h).
# python:3.11-slim má i arm64 variantu -> jede na Synology DS223j (ARM).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

# Nejdřív jen requirements -> cache vrstvy se netrhá při změně kódu.
# httpx má čistě-Python závislosti -> na ARM se nic nekompiluje, build je rychlý.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Perzistentní úložiště DB – sem se mountuje volume z NASu (DB_PATH=data/realitky.db).
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Stáhne -> oskóruje -> notifikuje -> skončí. (Cyklus zajišťuje cron, ne kontejner.)
CMD ["python", "main.py"]
