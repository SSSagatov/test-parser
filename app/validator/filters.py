"""Conservative post-filtering; missing evidence never matches a requested filter."""

from app.config import Settings
from app.models.domain import LotData, SearchFilters
from app.validator.location import verify_pavlodar
from app.validator.lots import is_active


def contains(value: str | None, requested: str) -> bool:
    return requested.casefold() in (value or "").casefold()


def matches(lot: LotData, filters: SearchFilters, settings: Settings) -> bool:
    for field, requested in (
        ("lot_number", filters.lot_number),
        ("announcement_number", filters.announcement_number),
        ("cpv", filters.cpv),
    ):
        if requested and getattr(lot, field) != requested:
            return False
    if filters.status:
        if filters.status.casefold() == "active":
            if not is_active(lot.status, settings):
                return False
        elif not contains(lot.status, filters.status):
            return False
    for value, requested in (
        (lot.procurement_method, filters.method),
        (
            (lot.customer.name or "") + " " + (lot.customer.bin or "") if lot.customer else None,
            filters.customer,
        ),
    ):
        if requested and not contains(value, requested):
            return False
    if filters.supplier and not any(
        contains((p.name or "") + " " + (p.bin or ""), filters.supplier) for p in lot.suppliers
    ):
        return False
    if filters.keyword and not contains(
        " ".join(filter(None, [lot.title, lot.description, lot.specification_original])),
        filters.keyword,
    ):
        return False
    # English names are accepted as explicit aliases, never customer-address guesses.
    places = " ".join(filter(None, [lot.delivery_place, lot.work_place, lot.service_location]))
    if filters.city and filters.city.casefold() in {"pavlodar", "павлодар", "г. павлодар"}:
        if verify_pavlodar(lot, settings):
            return False
    elif filters.city and not contains(places, filters.city):
        return False
    region = "Павлодар" if (filters.region or "").casefold() == "pavlodar" else filters.region
    if region and not contains(places, region):
        return False
    if filters.budget_min is not None and (lot.budget is None or lot.budget < filters.budget_min):
        return False
    if filters.budget_max is not None and (lot.budget is None or lot.budget > filters.budget_max):
        return False
    if filters.date_from and (
        lot.publication_date is None
        or lot.publication_date.tzinfo is None
        or lot.publication_date < filters.date_from
    ):
        return False
    if filters.date_to and (
        lot.publication_date is None
        or lot.publication_date.tzinfo is None
        or lot.publication_date > filters.date_to
    ):
        return False
    return True
