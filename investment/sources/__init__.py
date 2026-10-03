"""investment/sources/ — čtyři nezávislé zdrojové moduly (Sreality, Realingo, Bazoš,
reality-sumperk.cz) sdílející jeden normalizovaný tvar (`Listing` v base.py).

Každý modul exportuje `fetch_listings(config) -> list[Listing]` a nic jiného od
sebe navzájem nevyžaduje — pád jednoho modulu (síť, změna HTML) nesmí shodit
ostatní. To řeší orchestrátor v investment_main.py (try/except per modul).
"""
