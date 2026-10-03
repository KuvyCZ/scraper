# Hledač flipů — byty a domy v Šumperku a okolí → Telegram.
# python:3.11-slim má i arm64 variantu -> jede na Synology DS223j (ARM).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

# Nejdřív jen requirements -> cache vrstvy se netrhá při změně kódu.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY flip/ flip/

# Perzistentní úložiště DB – sem se mountuje volume z NASu (DB_PATH=data/flip.db).
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Výchozí: běží pořád, každý zdroj ve vlastním intervalu. Jednorázově: `python main.py once`.
CMD ["python", "main.py"]
