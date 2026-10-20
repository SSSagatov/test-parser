from datetime import date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo


PAVLODAR_PREFIX = "55"
KATO_RE = re.compile(r"^\d{9}$")
# Official OWS documentation: 1 and 2 are earlier state procurement modules;
# 3 is the current state procurement system. Other/unknown IDs are not accepted.
GOSZAKUP_SYSTEM_IDS = frozenset({1, 2, 3})


def is_goszakup_platform(record, *, with_announcement=False):
    """Check OWS state-procurement module IDs, not zakup.gov.kz platform IDs."""
    if record.get("systemId") not in GOSZAKUP_SYSTEM_IDS:
        return False
    if with_announcement:
        announcement = record.get("TrdBuy")
        return isinstance(announcement, dict) and announcement.get("systemId") in GOSZAKUP_SYSTEM_IDS
    return True


def parse_api_date(value, tz):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)


def period_matches(announcement, *, now, start_day: date, days: int, mode: str):
    tz = now.tzinfo
    window_start = datetime.combine(start_day, time.min, tz)
    window_end = datetime.combine(start_day + timedelta(days=days), time.min, tz)
    periods = [(announcement.get("startDate"), announcement.get("endDate")),
               (announcement.get("repeatStartDate"), announcement.get("repeatEndDate"))]
    for raw_start, raw_end in periods:
        start = parse_api_date(raw_start, tz)
        end = parse_api_date(raw_end, tz)
        if start is None or end is None or start >= end:
            continue
        overlaps = start < window_end and end > window_start
        if mode == "active_now" and start <= now < end and overlaps:
            return True
        if mode == "starts_in_window" and window_start <= start < window_end:
            return True
        if mode == "ends_in_window" and window_start < end < window_end:
            return True
        if mode == "overlaps_window" and overlaps:
            return True
    return False


def status_is_open(name):
    normalized = (name or "").casefold().replace("ё", "е")
    return bool(re.search(r"прием (?:заявок|ценовых предложений)|дополнени[ея] заявок", normalized)) \
        and not any(word in normalized for word in ("отмен", "приостанов", "заверш"))


def geo_classification(codes):
    if not codes:
        return "review"
    cleaned = [str(code).strip() for code in codes if code is not None]
    if not cleaned or any(not KATO_RE.fullmatch(code) for code in cleaned):
        return "review"
    inside = [code.startswith(PAVLODAR_PREFIX) for code in cleaned]
    if all(inside):
        return "match"
    if any(inside):
        return "review"
    return "exclude"


def lot_geography(lot):
    direct = [str(code).strip() for code in (lot.get("plnPointKatoList") or [])
              if code is not None and str(code).strip()]
    plan_codes = []
    for plan in lot.get("Plans") or []:
        if plan.get("isDeleted") == 1 or plan.get("isActive") == 0:
            continue
        for place in plan.get("PlansKato") or []:
            if place.get("isDeleted") == 1 or place.get("isActive") == 0:
                continue
            plan_codes.append(place.get("refKatoCode"))
    if not plan_codes:
        return "review", "Нет подтверждённых мест поставки в PlansKato"
    classification = geo_classification(plan_codes)
    if classification == "review":
        return "review", "Места поставки отсутствуют, некорректны или в разных регионах"
    if direct and geo_classification(direct) != classification:
        return "review", "КАТО лота противоречит местам поставки пунктов плана"
    return classification, ""

