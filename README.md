# Flip finder: Šumperk area

Watches **flats and houses for sale** in six towns, scores each one on how good a
**flip** it looks (buy → renovate → sell), and sends them to **Telegram**.
Personal, non-commercial use.

**Towns:** Šumperk (incl. Dolní/Horní Temenice) · Nový Malín (incl. Mladoňov, Plechy) ·
Vikýřovice · Rapotín · Dolní Studénky (incl. Králec) · Sudkov. Edit them in `flip/towns.py`.

**Sites:** Sreality · Realingo · Bazoš · reality-sumperk.cz

## What you get on Telegram

- **🏠 Every new listing** in your towns up to the budget (default 5 M Kč), with its score.
- **🔥** for strong ones: score ≥ 65, or an estimated flip margin ≥ 20 %.
- **📉 Price drop** when a listing you've already seen gets ≥ 3 % cheaper, including when the
  same property shows up cheaper on another site.
- **First run:** instead of 50+ messages you get one summary with the top 10 (🔥 ones also in full).
- **⚠️ A warning** if a site fails 3 runs in a row (sent once, not every run).

The same property listed on several sites is sent **once**, with "also on …" links.

Example:
```
🔥 91/100 · Flat 3+1 · Šumperk
72 m²
1 290 000 Kč · 17 917 Kč/m² (-46 % vs median, 11 similar in your towns)
🔨 Před rekonstrukcí, energy G
💰 Flip estimate: sells ~4.18M after reno · reno ~1.8M · costs ~154k → ✅ +597k (+18 %)
   from 6 renovated listings in okres Šumperk; asking prices, not sale prices
📉 9 % cheaper than when first seen
📍 Šumperk, Temenická
🔗 Sreality · also on Bazoš
```

## How the score works (0–100)

| Signal | Weight | Meaning |
|---|---|---|
| Condition | 30 % | Needs renovation? Uses the site's structured condition first, then the text ("k rekonstrukci", "původní stav", umakart…); energy class F/G adds a bit |
| Flip margin | 30 % | `price after renovation × 0.92 − price − renovation − 5 % costs`. Full points at 25 % margin |
| Price vs market | 25 % | Price per m² vs the median of similar listings (same type, similar size). Full points at 40 % below median |
| Motivated seller | 15 % | Price drops, months on the market, "dědictví / rychlé jednání" in the text |

- **Price after renovation** = median price per m² of similar listings that are already renovated × size.
- **Renovation cost:** 25,000 Kč/m² for flats and 30,000 Kč/m² for houses (`RENO_COST_*`).
- **Comparison area:** your six towns first; if there are fewer than 5 similar listings there, it
  widens to the whole okres Šumperk. Sreality stores the whole okres for exactly this reason.
- **Missing data is never guessed.** That signal is skipped and its weight goes to the others.
- **Serious risks halve the score and never get 🔥:** only a share of the property (podíl), an
  auction, or an attic space. They are still shown in the message, together with co-op flats,
  sitting tenants and easements.
- **Excluded entirely:** small cabins (chata), new builds sold "na klíč", plots.
- **Chalupy** are included but tagged, so you check they're livable year-round.

⚠️ All prices are **asking prices**; sites don't publish what properties actually sold for.
For houses, price per m² ignores the land. Treat the numbers as a pointer to what's worth a
look, not as a valuation.

## Run it

```bash
cp .env.example .env              # fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
pip install -r requirements.txt

python main.py check              # test every site live: how many listings, which towns
python main.py test-telegram      # test the bot can reach your chat
python main.py once               # one pass over all sites
python main.py                    # keep running (each site on its own interval)
python main.py top                # best current listings from the database
```

### Synology NAS (Container Manager)

1. Copy the project to the NAS, e.g. `/docker/flip-scraper`, and create a `data/` folder in it.
2. Create `.env` from `.env.example` and fill in the Telegram token and chat id.
3. Container Manager → **Project → Create** → point it at the folder (it finds
   `docker-compose.yml`) → **Build**. It runs permanently and survives NAS restarts.
4. Check it works: `docker compose run --rm scraper python main.py check`.

The first Sreality run fetches details for the whole okres (~10 min). After that it only
fetches new listings and is quick.

## After deploying: please run `check`

House categories on Realingo, Bazoš and reality-sumperk.cz could **not be verified live**
when this was written (flats were verified earlier). `python main.py check` shows, per site, how
many flats and houses it found and in which towns. If one says *"no houses at all"*, fix the
path in `flip/sources/selectors/*.json`; no code change is needed. Realingo also logs any
category codes it doesn't recognise.

## Code map

| Path | What |
|---|---|
| `main.py` | entry point and commands |
| `flip/towns.py` | the six towns and their parts, address matching |
| `flip/sources/*.py` | one module per site + `selectors/*.json` |
| `flip/pipeline.py` | one run: fetch → store → dedupe → score → notify |
| `flip/market.py` | comparable listings, median, price after renovation |
| `flip/scoring.py` | flip score and margin estimate |
| `flip/text.py` | reading condition, risks and house type from listing text |
| `flip/store.py` | SQLite: listings, price history, what was sent, source health |
| `flip/notifier.py` | Telegram messages |
| `tests/` | `python -m unittest discover -s tests -t .` (no extra dependencies) |
| `legacy/` | the old Olomoucký-kraj monitor, archived and not used |
