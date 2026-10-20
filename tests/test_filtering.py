from datetime import date, datetime
from zoneinfo import ZoneInfo

from goszakup.filtering import (geo_classification, is_goszakup_platform,
                                lot_geography, period_matches, status_is_open)


NOW = datetime(2026, 10, 15, 10, 30, tzinfo=ZoneInfo("Asia/Almaty"))
DAY = date(2026, 10, 15)


def test_boundaries_and_timezone():
    base = {"startDate": "2026-10-14T23:59:59+05:00", "endDate": "2026-10-17T00:00:00+05:00"}
    assert period_matches(base, now=NOW, start_day=DAY, days=2, mode="active_now")
    assert period_matches(base, now=NOW, start_day=DAY, days=2, mode="overlaps_window")
    assert not period_matches(base, now=NOW, start_day=DAY, days=2, mode="starts_in_window")
    assert not period_matches(base, now=NOW, start_day=DAY, days=2, mode="ends_in_window")
    assert not period_matches({"startDate": "2026-10-14T00:00:00+05:00",
                               "endDate": "2026-10-15T00:00:00+05:00"},
                              now=NOW, start_day=DAY, days=2, mode="overlaps_window")
    assert not period_matches({"startDate": "2026-10-17T00:00:00+05:00",
                               "endDate": "2026-10-18T00:00:00+05:00"},
                              now=NOW, start_day=DAY, days=2, mode="overlaps_window")


def test_repeated_submission_and_status():
    announcement = {"startDate": "2026-10-01", "endDate": "2026-10-02",
                    "repeatStartDate": "2026-10-15T09:00:00+05:00",
                    "repeatEndDate": "2026-10-16T09:00:00+05:00"}
    assert period_matches(announcement, now=NOW, start_day=DAY, days=2, mode="active_now")
    assert status_is_open("Опубликован (прием заявок)")
    assert status_is_open("Опубликовано (прием ценовых предложений)")
    assert not status_is_open("Отменен")
    assert not status_is_open("Приостановлен")


def test_delivery_geography_not_customer_address():
    assert geo_classification(["550000000", "552210000"]) == "match"
    assert geo_classification(["750000000"]) == "exclude"
    assert geo_classification(["550000000", "750000000"]) == "review"
    lot = {"plnPointKatoList": ["550000000"],
           "customerNameRu": "Заказчик Павлодар",
           "Plans": [{"isActive": 1, "isDeleted": 0,
                      "PlansKato": [{"refKatoCode": "750000000", "isActive": 1, "isDeleted": 0}]}]}
    assert lot_geography(lot)[0] == "review"
    lot["plnPointKatoList"] = ["750000000"]
    assert lot_geography(lot)[0] == "exclude"
    lot["plnPointKatoList"] = [""]
    assert lot_geography(lot)[0] == "exclude"
    lot["Plans"][0]["PlansKato"][0]["refKatoCode"] = "552210000"
    assert lot_geography(lot)[0] == "match"


def test_platform_is_verified_on_lot_and_announcement():
    for system_id in (1, 2, 3):
        assert is_goszakup_platform({"systemId": system_id})
        assert is_goszakup_platform({"systemId": system_id, "TrdBuy": {"systemId": 3}},
                                     with_announcement=True)
    for system_id in (None, 4, "3"):
        assert not is_goszakup_platform({"systemId": system_id})
    assert not is_goszakup_platform({"systemId": 3, "TrdBuy": {"systemId": 4}},
                                    with_announcement=True)

