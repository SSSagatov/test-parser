"""Location is proven only by official delivery/work/service evidence."""

import re

from app.config import Settings
from app.models.domain import LotData
from app.utils.urls import official_url


def verify_pavlodar(lot: LotData, settings: Settings) -> str | None:
    source = lot.location_source_url
    if not official_url(source, settings.official_hosts):
        return "LOCATION_NOT_VERIFIED"
    places = [lot.delivery_place, lot.work_place, lot.service_location]
    if any(re.search(settings.pavlodar_pattern, place, re.IGNORECASE) for place in places if place):
        return None
    return "LOCATION_NOT_VERIFIED"
