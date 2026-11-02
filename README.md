# Flip finder: Olomoucký kraj

Watches **flats and houses for sale** in the bigger towns of Olomoucký kraj, scores each one on how good a
**flip** it looks (buy → renovate → sell), and sends them to **Telegram**.
Personal, non-commercial use.

**Towns:** about 40 towns of roughly 2,000+ people in all five okresy (Olomouc, Prostějov,
Přerov, Šumperk, Jeseník); small villages are left out. The list, with approximate
populations, is in `flip/towns.py` — add or remove a town there.
On Bazoš the town is picked by **postal code (PSČ)**, not by the town name; change the list
with `BAZOS_PSC` in `.env` (see `.env.example`).

**Sites:** Sreality · Realingo · Bazoš · reality-sumperk.cz

## What you get on Telegram

- **🏠 Every new listing** in your towns up to the budget (default 5 M Kč), with its score.
- **💸 Pricier places too**, when their price per m² is at least 20 % below the town's typical
  price (`OVER_BUDGET_DISCOUNT_PCT`; 0 = stick to the budget).
- **🔥** for strong ones: score ≥ 65, or an estimated flip margin ≥ 20 %.
- **📉 Price drop** when a listing you've already seen gets ≥ 3 % cheaper, including when the
  same property shows up cheaper on another site.
- **First run:** instead of 50+ messages you get one summary with the top 10 (🔥 ones also in full).
- **⚠️ A warning** if a site fails 3 runs in a row (sent once, not every run).
- **🔎 Similar nearby button** under each listing: tap it and the bot replies with comparable
  listings in the same town (same type, similar size), their price per m² and how this one compares.
  Works while the app is running (`python main.py`), only in your own chat.
- **📊 Weekly town prices:** once a week, a table of the typical asking price per m² in each town
  (flats and houses, renovated vs. to renovate) with the change since last week. Each week is also
  added to `data/town_prices.csv`, which opens in Excel. Change with `TOWN_REPORT_DAYS`.

The same property listed on several sites is sent **once**, with "also on …" links.

Example:
```
🔥 91/100 · Flat 3+1 · Šumperk
72 m²
1 290 000 Kč · 17 917 Kč/m² (-46 % vs median, 11 similar in Šumperk)
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
- **Comparison area:** the listing's **own town** first (Olomouc and a village have very different
  prices). If there are fewer than 5 similar listings there, it widens to the watched towns of the
  same okres, then to the whole kraj. Sreality stores the whole kraj for exactly this reason.
- **`python main.py towns`** prints each town's typical price per m² (flats and houses), plus
  renovated vs. to-renovate where there are enough listings — a quick view of where flips can work.
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
python main.py towns              # typical price per m² in each town
python main.py report             # send the weekly town-price table to Telegram now
```

### Synology NAS (Container Manager)

GitHub builds a ready-made image after every change to `main`
(`.github/workflows/docker.yml` → `ghcr.io/kuvycz/scraper:latest`, ARM and x86).
The NAS only downloads it, so there is no ZIP upload and no local Build.

1. In File Station create `docker/flip-scraper` with an empty `data/` folder inside, and put
   `docker-compose.yml`, `update-on-nas.sh` and `.env` (from `.env.example`, with the
   Telegram token and chat id filled in) into it.
2. Container Manager → **Project → Create** → point it at the folder → it pulls the image and starts.
   It runs permanently and survives NAS restarts.
3. Control Panel → **Task Scheduler** → Create → Scheduled Task → User-defined script,
   user `root`, daily, script `sh /volume1/docker/flip-scraper/update-on-nas.sh`.
   It downloads a new version when there is one and restarts the scraper with it
   (your data stays). To update right away: select the task → **Run**.
   After each update the bot says "✅ Scraper is now running version …".

The GitHub package has to be **public** once, so the NAS can download it without a password:
GitHub → your profile → Packages → `scraper` → Package settings → Change visibility → Public.

### Getting the data: Telegram commands

Write these to the bot (it only answers your own `TELEGRAM_CHAT_ID`):

| Command | What you get back |
|---|---|
| `/data` | `listings_<date>.csv` (all listings in your towns, opens in Excel), `town_prices.csv` (weekly prices per town) and `flip_<date>.db` (the full database, e.g. to attach for Claude) |
| `/status` | how many listings are stored, when each site last ran, which version is running |
| `/help` | the list of commands |

The bot answers between site checks, so during a long run (the first Sreality pass) a reply
can take a few minutes.

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
| `flip/export.py` | `/data`, `/status`, `/help` commands and the "updated" message |
| `tests/` | `python -m unittest discover -s tests -t .` (no extra dependencies) |
| `legacy/` | the old Olomoucký-kraj monitor, archived and not used |
