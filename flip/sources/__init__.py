"""flip/sources/ — 4 nezávislé zdroje se stejným rozhraním:

    fetch_listings(cfg: FlipConfig, known_ids: set[str], refresh_ids: set[str] = ...) -> FetchResult
    (refresh_ids = známé inzeráty, jejichž detail se má přečíst znovu)

Pád jednoho zdroje (síť, redesign webu) nesmí shodit ostatní — to řeší pipeline.
"""

from flip.sources import bazos, realingo, reality_sumperk, sreality

SOURCES = {
    "sreality": sreality,
    "realingo": realingo,
    "bazos": bazos,
    "reality_sumperk": reality_sumperk,
}

DISPLAY_NAMES = {
    "sreality": "Sreality",
    "realingo": "Realingo",
    "bazos": "Bazoš",
    "reality_sumperk": "Reality Šumperk",
}
