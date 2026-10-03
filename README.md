# Realitní scraper se skórováním

Sleduje prodej bytů na **Sreality**, **Bezrealitky** a **iDnes** (start: Olomoucký kraj), skóruje je 0–100 podle
toho, jak vypadají jako příležitost — **priorita = byty PŘED rekonstrukcí** — a
posílá zajímavé nad prahem na **Telegram**. Osobní, nekomerční použití.

> Postaveno jako multi-source agregátor s jednotným schématem `Listing`. Aktivní zdroje:
> **Sreality** (REST v1), **Bezrealitky** (GraphQL, od majitelů) a **iDnes Reality**
> (HTML přes BeautifulSoup). Okresy/dispozice jsou napříč zdroji sjednocené → medián
> cena/m² se pooluje.

## ⚠️ Důležité: API realita

Veřejné `/api/cs/v2/estates` z původního zadání je **mrtvé (404)**. Scraper jede na
ověřeném současném endpointu:

- **Seznam:** `https://www.sreality.cz/api/v1/estates/search` (stránkování `limit`/`offset`)
- **Detail:** `https://www.sreality.cz/api/v1/estates/{hash_id}` (popis, stav, štítek)
- Olomoucký kraj = `locality_region_id=8` (ověřeno z `filter_page`).

Stav nemovitosti je **strukturované pole** `building_condition` (`8 = Před rekonstrukcí`),
takže hlavní signál nestojí jen na keywordech v popisu.

## Moduly

| Soubor | Co dělá |
|---|---|
| `sources/base.py` | `Listing` (jednotné schéma) + abstraktní `Source.fetch()` |
| `sources/sreality.py` | scraper Sreality (search + detail jen pro nové), normalizace na `Listing` |
| `sources/bezrealitky.py` | scraper Bezrealitky (GraphQL `listAdverts`, prodej od majitelů); popis i stav rovnou v seznamu |
| `sources/idnes.py` | scraper iDnes Reality (HTML/BeautifulSoup; seznam + detail jen pro nové); okres ze SEO odkazů detailu |
| `comps.py` | srovnatelné v okolí (GPS okruh → okres → kraj) + ARV a ekonomika rekonstrukce |
| `database.py` | SQLite: `listings`, `price_history`, `scores`; `seen_first/last`; dedup `(source, source_id)` |
| `scoring.py` | skóre 0–100, medián cena/m² per okres→kraj, cold-start s přerozdělením vah |
| `notifier.py` | Telegram přes httpx (HTML), práh, denní heartbeat |
| `config.py` | konfigurace z `.env` (žádné hardcoded hodnoty) |
| `main.py` | orchestrace jednoho běhu |
| `test_api.py` | průzkumný skript: stáhne vzorek a vypíše surová data |

### Datový tok
```
sources/ (sreality…) → Listing → SQLite → scoring → Telegram (nad prahem)
                                   ▲ cron každé 2–4 h (Synology Task Scheduler)
```

## Byty i domy + srovnávací analýza (comps)

`CATEGORY_MAIN_CB=1,2` stahuje **byty i domy**. Typ drží `Listing.property_type`
(`byt`/`dum`) a **segmentuje mediány** — bez toho by Kč/m² domů znečistilo mediány bytů.
U domů se navíc ukládá plocha pozemku (`land_area_m2`, Sreality `estate_area`).

Ke každé notifikaci nad prahem se dopočítá blok *Srovnatelné v okolí*:

```
📐 Srovnatelné v okolí (okres Přerov, 11 ks)
   nabídky: 5 490 000 Kč (32 117 Kč/m² · rozptyl 19 910–39 375)
   tato: 14 857 Kč/m² (-54 % vs medián)
   medián pozemku srovnatelných: 931 m²
   po rekonstrukci ~5 632 200 Kč (z 6 hotových)
   ❌ odhad: −3 500 000 Kč reko → marže -918 376 Kč (-15 %)
```

- **Výběr comps:** stejný typ, plocha ±30 %, geograficky **GPS okruh 15 km → okres → kraj**
  (u domů rozhoduje lokalita víc než plocha, proto okruh před administrativní hranicí).
- **ARV** (cena po rekonstrukci) = medián Kč/m² srovnatelných, které už jsou *hotové*
  („Po rekonstrukci“/„Novostavba“/„Velmi dobrý“), × plocha. Míň než 5 comps → `None` (nehádá se).
- **Marže** = ARV × `SALE_FACTOR` − cena − (`RENOVATION_COST_PER_M2` × plocha). Počítá se jen
  u nemovitostí, které opravu potřebují („Před rekonstrukcí“/„Špatný“/„K demolici“).

> ⚠️ **Comps stojí na NABÍDKOVÝCH cenách, ne realizovaných** — portály prodejní ceny
> nezveřejňují. Je to vodítko „kam se podívat“, ne ocenění. Proto `SALE_FACTOR` (0.92)
> a proto se extrémní výsledky **označují varováním**: ověřeno na datech, že 203m² „byt“
> s 47% marží byl ve skutečnosti **půdní prostor** k vybudování bytu.

### Skórování (váhy)
`stav 30 %` · `cena/m² vs medián 25 %` · `urgentní formulace 20 %` · `pokles ceny 15 %` · `doba inzerce 10 %`
Chybí-li data pro signál → `None` a váha se přerozdělí mezi ostatní (nikdy se nehádá).

## Lokální spuštění (PC)

```powershell
# 1) venv + závislosti
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) ověření API (stáhne vzorek do raw_sample.json)
.\.venv\Scripts\python.exe test_api.py

# 3) jednotlivé moduly mají vlastní smoke testy
.\.venv\Scripts\python.exe -m sources.sreality
.\.venv\Scripts\python.exe scoring.py
.\.venv\Scripts\python.exe notifier.py

# 4) celý běh nasucho (bez tokenu se vynutí dry_run, zprávy se jen logují)
$env:MAX_LISTINGS="10"; $env:DETAIL_LIMIT="10"; $env:SCORE_THRESHOLD="20"
.\.venv\Scripts\python.exe main.py
```

## Konfigurace

Zkopíruj `.env.example` → `.env` a vyplň. Telegram bota založíš přes **@BotFather**
(dostaneš `TELEGRAM_BOT_TOKEN`); `TELEGRAM_CHAT_ID` zjistíš tak, že botovi napíšeš a
otevřeš `https://api.telegram.org/bot<TOKEN>/getUpdates`. Bez těchto dvou se scraper
přepne do `dry_run` (zprávy se jen logují).

Práh (`SCORE_THRESHOLD`) nastav **vysoko (70+)**, ať tě bot nezahltí; po odladění sniž.

## Nasazení na Synology NAS (Container Manager)

NAS běží v **UTC** a má jen 1 GB RAM — proto žádný Selenium, jen httpx. Doporučený
režim je **samoplánující se Projekt**: kontejner běží pořád a scrapuje sám každé
`INTERVAL_HOURS` (žádný Task Scheduler není potřeba).

**1) Nahraj projekt na NAS** (File Station) do sdílené složky, např.
`/docker/realitni-scraper`. Vytvoř v ní podsložku `data/`.

**2) `.env`** — zkopíruj `.env.example` → `.env` a vyplň:
```
TELEGRAM_BOT_TOKEN=...    TELEGRAM_CHAT_ID=...
INTERVAL_HOURS=3          # běh každé 3 h (smyčka)
DB_PATH=data/realitky.db  # přežije restart (mountnutý volume)
SCORE_THRESHOLD=70
```

**3) (Tip) Předplň DB**, ať NAS nestahuje stovky detailů: zkopíruj `realitky.db`
vyrobenou na PC do `data/realitky.db`. Při prvním běhu pak budou všechny inzeráty
„známé" → NAS jen rychle projede seznam a detail tahá jen u nových.

**4) Container Manager → Project → Create** → název, cesta = složka projektu
(automaticky najde `docker-compose.yml`) → **Build**. Projekt se spustí a běží jako
stálá služba (`restart: unless-stopped` přežije i restart NASu).

Logy: Container Manager → kontejner → **Logs**. První běh bez předplněné DB stáhne
detail u všech inzerátů (~stovky × 1,6 s ≈ 20 min); další běhy už jen u nových → rychlé.

> Alternativa (jednorázově přes cron): dej `INTERVAL_HOURS=0` a spouštěj
> `run-on-nas.sh` ze **Control Panel → Task Scheduler** každé 2–4 h.

## Investiční režim (Šumperk) — samostatný proces, 4 nezávislé zdroje

`investment_main.py` je **oddělený modul vedle hlavního monitoringu výše** — vlastní
config sekce v `.env` (`INVESTMENT_*`), vlastní DB (`investment.db`), vlastní Telegram
chat (`TELEGRAM_INVESTMENT_CHAT_ID`, stejný bot token). Hlavní `main.py` se tímto
blokem vůbec nemění a neimportuje ho.

Cíl: najít podhodnocené byty 2+1/2+kk/3+1 40-70 m² v Šumperku (nebo celém okrese —
`INVESTMENT_INCLUDE_DISTRICT=true`) jako příležitost k nájemní investici, se
**skóre jako řazením, ne tvrdým filtrem** — posílá se cokoliv nad nízkým prahem
(`INVESTMENT_SCORE_THRESHOLD`, výchozí 40), ať se hraniční tipy nezahodí omylem.

### 4 nezávislé zdroje

Každý zdroj je samostatný modul (`investment/sources/*_source.py`) s jednotným
rozhraním `fetch_listings(config, known_ids) -> list[Listing]` a **vlastním
intervalem** (`INVESTMENT_*_POLL_INTERVAL_MINUTES`). Pád/redesign JEDNOHO zdroje se
odchytí a zaloguje — orchestrátor pokračuje ostatními zdroji dál. Po 3 selháních
v kuse (`INVESTMENT_SOURCE_FAILURE_ALERT_THRESHOLD`) přijde jen **JEDNO** Telegram
varování, ne jedno za každý neúspěšný běh.

| Zdroj | Modul | Jak | Poznámka |
|---|---|---|---|
| Sreality | `sreality_source.py` | JSON API (`sources/sreality.py`, +`district_ids`) | okresový filtr, ověřeno živě |
| Realingo | `realingo_source.py` | Next.js `__NEXT_DATA__` JSON v HTML | spolehlivější než CSS (hashované třídy) |
| Bazoš | `bazos_source.py` | HTML, prosté procházení kategorie | **žádné URL filtrování** — viz robots.txt níže |
| reality-sumperk.cz | `sumperk_source.py` | HTML, dispozice-scoped kategorie | detail jen pro nové (popis) |

Selektory (CSS/regex) pro HTML zdroje jsou v `investment/sources/selectors/*.json` —
redesign webu je pak úprava configu, ne kódu.

### Ověřeno živě proti skutečným webům (ne odhad ze zadání)

- **Sreality**: `locality_district_id=44` = okres Šumperk (135 inzerátů) — jediná
  (aditivní, zpětně kompatibilní) úprava `sources/sreality.py`: nový volitelný
  parametr `district_ids`, výchozí chování (`region_ids`) beze změny.
- **Realingo**: lokalita jde URL cestou (`/prodej_reality/Šumperk/` nebo
  `/Okres_Šumperk/`), řazení od nejlevnějšího = segment `/nejlevnejsi/` (ověřeno
  vzestupné ceny), stránkování `/{N}_strana/`.
- **Bazoš**: robots.txt **DISALLOWuje** `hledat=`, `hlokalita=`, `humkreis`,
  `order=`, `cenaod=`, `cenado=`, `rubriky=`, `type=`, `category=` — přesně ty
  parametry, co Bazoš normálně používá pro filtrování/třídění. Nejde tedy URL
  vyfiltrovat na Šumperk ani seřadit od nejlevnějšího bez porušení robots.txt.
  Scraper proto prochází **prostou, nefiltrovanou kategorii** `/prodam/byt/{offset}/`
  (stránkování cestou, ne query stringem — to disallow nezakazuje) a lokalitu i
  řazení řeší až po stažení, na naší straně.
- **reality-sumperk.cz**: cesty ze zadání (`/byty/nejlevnejsi/`,
  `/prodej/byty/obec-sumperk/`) na webu **neexistují** (404 při ověření). Reálné
  kategorie jsou dispozice-scoped (`/prodej/byty/byty-2-1/` apod.), stránkování
  `?strana=N`. Formulářové `sortBy=1` ("od nejlevnějšího") POSTuje na `/hledani/` —
  ověřeno, že prosté přidání `?sortBy=1` na kategorii pořadí NEMĚNÍ, řadí se proto
  až po stažení.
- **robots.txt se skutečně respektuje za běhu**, ne jen jednorázově ověřilo v terminálu:
  `investment/scraping_utils.py:RobotsChecker` má VLASTNÍ parser s podporou `*`/`$`
  wildcard patternů — ověřeno živě, že stdlib `urllib.robotparser` tyhle patterny
  neumí a Bazošovo `Disallow: /*hledat=` by tiše ignoroval.

### Cross-source deduplikace

Stejný byt na víc portálech (`investment/dedup.py`) — duplicita = cena do 2 % od
sebe **AND** plocha do 2 m² **AND** stejná dispozice **AND** stejná obec. Nechá se
nejlevnější verze, ostatní URL se přidají do alertu (`Also listed on: …`). Protože
zdroje běží na nezávislých intervalech, dedup porovnává nový kandidát proti
**živému stavu ostatních zdrojů v DB**, ne jen proti dávce z aktuálního běhu.

### Bazoš — nestrukturované popisy

Bazoš nemá strukturovaná pole velikosti/dispozice — parsují se regexem z
titulku+popisu. Když se nepodaří určit ani jedno, inzerát se **přesto nechá projít**
(velikost/dispozice se prostě nekontroluje, benefit of the doubt), jen s
`parse_confidence: low` — poznačeno v alertu (`❓ Low parse confidence`).

**Spuštění (samostatně od hlavního scraperu):**
```powershell
$env:INVESTMENT_ENABLED="true"
.\.venv\Scripts\python.exe investment_main.py
```
Bez `INVESTMENT_ENABLED=true` skript jen zaloguje a skončí — nic se nestane omylem.

Na NASu (Container Manager) běží jako **druhý, nezávislý Projekt/kontejner** vedle
`main.py` — jiná DB (`investment.db`), jiné intervaly, žádná kolize s hlavním scraperem.

⚠️ **`INVESTMENT_PRICE_MAX` (2 800 000 Kč) může být pořád nízký pro obec Šumperk
samotnou** — 2+1 v Šumperku (ne okolí) teď běžně stojí 2,9-3,6 mil. Kč (ověřeno
živě). Levnější nabídky (Maletín 1,9 mil., Lukavice 2,45 mil., Písařov 2,1 mil. …)
jsou v OKOLNÍCH obcích okresu — zvaž `INVESTMENT_INCLUDE_DISTRICT=true`.

## Právní / etické mantinely

- Sreality ToS scraping zakazuje → drž to **osobní a nekomerční**, rozumný rate-limit (≥ 1,5 s) a identifikující User-Agent.
- **GDPR:** neukládají se jména ani telefony prodejců — jen údaje o nemovitosti.

## Další kroky (po odladění jádra)

- Doladit váhy/práh podle reálných dat (skóre se loguje u všech inzerátů).
- ✅ **Bezrealitky** přidáno (GraphQL `listAdverts`, prodej od majitelů).
- ✅ **iDnes Reality** přidáno (HTML/BeautifulSoup, seznam + detail).
- Využít `originalPrice` z Bezrealitky → levný **den-1 signál poklesu ceny** (teď se pokles počítá až z historie v DB).
- **Mezi-portálová deduplikace**: stejný byt na Sreality i iDnes = 2 notifikace. Párovat přes GPS/cenu/plochu.
- iDnes okresy: scraper má mapu zatím jen pro Olomoucký kraj (`_OKRES_NAME` v `sources/idnes.py`) — pro další kraje doplnit.
