from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from goszakup.api import Client
from goszakup.collector import collect
from goszakup.config import Config


def test_collect_filters_before_full_details_and_resumes(monkeypatch, tmp_path):
    now = datetime.now(ZoneInfo("Asia/Almaty"))
    config = Config("fake-token", "https://ows.goszakup.gov.kz/v3/graphql", "Asia/Almaty",
                    "active_now", 2, 100, 1, 0, 2, tmp_path)
    calls = []
    monkeypatch.setattr(Client, "reference_statuses", lambda self: [
        {"id": 240, "name_ru": "Опубликован (прием ценовых предложений)"}])
    monkeypatch.setattr(Client, "reference_kato_codes", lambda self: ["550000000", "552210000"])

    def pages(self, query, root, filter_value, page_size, start_after=None):
        calls.append((root, filter_value))
        if root == "TrdBuy":
            assert filter_value == {"kato": ["550000000", "552210000"], "refBuyStatusId": [240]}
            yield [{"id": 1, "systemId": 3, "refBuyStatusId": 240,
                    "startDate": (now-timedelta(hours=1)).isoformat(),
                    "endDate": (now+timedelta(hours=1)).isoformat()},
                   {"id": 2, "systemId": 4, "refBuyStatusId": 240,
                    "startDate": (now-timedelta(hours=1)).isoformat(),
                    "endDate": (now+timedelta(hours=1)).isoformat()}], 2
        elif filter_value.get("trdBuyId"):
            assert filter_value == {"trdBuyId": [1]}
            yield [
                {"id": 11, "systemId": 3, "trdBuyId": 1, "plnPointKatoList": ["550000000"],
                 "Plans": [{"isQvazi": 0, "PlansKato": [{"refKatoCode": "550000000"}]}]},
                {"id": 12, "systemId": 3, "trdBuyId": 1, "plnPointKatoList": ["750000000"],
                 "Plans": [{"isQvazi": 0, "PlansKato": [{"refKatoCode": "750000000"}]}]},
                {"id": 13, "systemId": 3, "trdBuyId": 1, "plnPointKatoList": ["550000000"],
                 "Plans": [{"isQvazi": 1, "PlansKato": [{"refKatoCode": "550000000"}]}]},
                {"id": 14, "systemId": 3, "trdBuyId": 1, "plnPointKatoList": ["550000000"],
                 "Plans": [{"isQvazi": 0, "PlansKato": [{"refKatoCode": "550000000"}]}]},
                {"id": 15, "systemId": 4, "trdBuyId": 1, "plnPointKatoList": ["550000000"],
                 "Plans": [{"isQvazi": 0, "PlansKato": [{"refKatoCode": "550000000"}]}]},
            ], 14
        else:
            assert filter_value == {"id": [11, 14]}
            rows = []
            for lot_id in (11, 14):
                rows.append({"id": lot_id, "systemId": 3, "trdBuyId": 1, "amount": 50,
                             "plnPointKatoList": ["550000000"],
                             "Plans": [{"isQvazi": 0, "PlansKato": [{"refKatoCode": "550000000"}]}],
                             "TrdBuy": {"id": 1, "systemId": 3, "refBuyStatusId": 240,
                                        "startDate": (now-timedelta(hours=1)).isoformat(),
                                        "endDate": (now+timedelta(hours=1)).isoformat(),
                                        "Cancel": [{"id": 1}] if lot_id == 14 else []}})
            yield rows, 14

    monkeypatch.setattr(Client, "pages", pages)
    lots, review, stats = collect(config, now.date())
    assert [lot["id"] for lot in lots] == [11]
    assert [lot["id"] for lot, _ in review] == [14]
    assert stats["candidates"] == 4
    assert stats["excluded_candidates"] == 2
    assert stats["excluded_outside_region"] == 1
    assert stats["excluded_quasi"] == 1
    assert stats["lots_after_exclusions"] == 2
    previous_calls = len(calls)
    lots2, _, _ = collect(config, now.date(), resume=True)
    assert [lot["id"] for lot in lots2] == [11]
    assert len(calls) == previous_calls

