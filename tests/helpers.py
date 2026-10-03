from flip.config import FlipConfig
from flip.model import Listing


def make(source="sreality", eid="1", *, kind="byt", town="Šumperk", price=2_000_000, area=60.0,
         subtype="2+1", condition=None, description=None, **kw) -> Listing:
    return Listing(
        source=source, external_id=str(eid), url=f"https://example.test/{source}/{eid}", kind=kind,
        subtype=subtype, price=price, area_m2=area, town=town, locality=town,
        building_condition=condition, description=description, **kw,
    )


def config(**kw) -> FlipConfig:
    base = dict(dry_run=True, min_comps=3)
    base.update(kw)
    return FlipConfig(**base)
