"""Narrow announcement search, per-announcement lots, then eligible details."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from hashlib import sha256
import json
import logging
from zoneinfo import ZoneInfo

from .api import APIError, Client
from .filtering import GOSZAKUP_SYSTEM_IDS, is_goszakup_platform, lot_geography, period_matches, status_is_open
from .queries import ANNOUNCEMENTS, LOTS_MIN, LOTS_FULL
from .state import State

LOG = logging.getLogger(__name__)


def batches(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _detailed_batch(client, ids, size):
    output = {}
    for rows, _ in client.pages(LOTS_FULL, "Lots", {"id": ids}, size):
        for row in rows:
            if row["id"] not in ids:
                raise APIError("Фильтр Lots.id вернул посторонний лот")
            output[row["id"]] = row
    if set(output) != set(ids):
        raise APIError("API не вернул подробности для всех отобранных лотов")
    return output


def collect(config, start_day: date, resume=False):
    tz = ZoneInfo(config.timezone)
    signature_data = [start_day.isoformat(), config.date_mode, config.window_days,
                      config.timezone, config.graphql_url, "55", config.page_size]
    signature = sha256(json.dumps(signature_data).encode()).hexdigest()
    path = config.output_dir / f"checkpoint_{start_day}_{config.date_mode}.sqlite3"
    state = State(path, signature, resume)
    client = Client(config)
    try:
        removed = state.prune_platform(GOSZAKUP_SYSTEM_IDS)
        if any(removed.values()):
            LOG.info("Исключены записи с неподтверждённой площадкой из checkpoint: %s", removed)
        now = datetime.fromisoformat(state.get("snapshot_at")) if state.get("snapshot_at") else datetime.now(tz)
        state.set("snapshot_at", now.isoformat())
        statuses = state.get("open_statuses")
        if statuses is None:
            statuses = sorted({r["id"] for r in client.reference_statuses()
                               if isinstance(r.get("id"), int) and
                               status_is_open(r.get("name_ru") or r.get("nameRu"))})
            if not statuses:
                raise APIError("Официальный справочник не дал статусы открытого приёма; выгрузка остановлена")
            state.set("open_statuses", statuses)
        LOG.info("Статусы открытого приёма: %s", statuses)

        if not state.get("announcements_complete", False):
            codes = state.get("region_kato_codes")
            if codes is None:
                LOG.info("Получаю коды Павлодарской области из официального справочника КАТО")
                codes = client.reference_kato_codes()
                state.set("region_kato_codes", codes)
            if not codes or any(not isinstance(code, str) or not code.startswith("55") for code in codes):
                raise APIError("Некорректный сохранённый список кодов КАТО")
            kato_batches = list(batches(codes, 100))
            LOG.info("Получаю объявления с серверными фильтрами kato (%s кодов) и refBuyStatusId", len(codes))
            for batch_index in range(state.get("kato_batch_index", 0), len(kato_batches)):
                filters = {"kato": kato_batches[batch_index], "refBuyStatusId": statuses}
                LOG.info("КАТО: группа %s/%s", batch_index + 1, len(kato_batches))
                for page, cursor in client.pages(ANNOUNCEMENTS, "TrdBuy", filters, config.page_size,
                                                 state.get("announcement_cursor")):
                    selected = []
                    for row in page:
                        if not is_goszakup_platform(row):
                            continue
                        if row.get("refBuyStatusId") not in statuses:
                            continue
                        if not period_matches(row, now=now, start_day=start_day,
                                              days=config.window_days, mode=config.date_mode):
                            continue
                        selected.append(row)
                    state.add_announcements(selected, cursor, scanned=len(page))
                    LOG.info("Объявления: курсор %s, подходит %s", cursor, len(selected))
                state.advance_announcement_batch(batch_index + 1)
            state.set("announcements_complete", True)

        LOG.info("Получаю только лоты отобранных объявлений")
        while pending := state.pending_announcements(25):
            ids = [row_id for row_id, _ in pending]
            collected = []
            scanned = 0
            for page, _ in client.pages(LOTS_MIN, "Lots", {"trdBuyId": ids}, config.page_size):
                scanned += len(page)
                for row in page:
                    if not is_goszakup_platform(row):
                        continue
                    if row.get("trdBuyId") not in ids:
                        raise APIError("Фильтр Lots.trdBuyId вернул посторонний лот")
                    if row.get("isDeleted") != 1:
                        collected.append(row)
            state.add_candidates(collected, ids, scanned=scanned)
            LOG.info("Лоты: обработано объявлений %s, найдено %s", len(ids), len(collected))

        LOG.info("Проверяю КАТО и получаю подробности подходящих лотов")
        while pending := state.pending_candidates(50):
            to_detail = []
            for lot_id, minimal in pending:
                if not is_goszakup_platform(minimal):
                    state.add_result(lot_id, minimal, "exclude")
                    continue
                preliminary, _ = lot_geography(minimal)
                active_plans = [p for p in minimal.get("Plans") or []
                                if p.get("isDeleted") != 1 and p.get("isActive") != 0]
                if preliminary == "exclude" or (active_plans and all(p.get("isQvazi") == 1 for p in active_plans)):
                    state.add_result(lot_id, minimal, "exclude")
                else:
                    to_detail.append(lot_id)
            if not to_detail:
                continue
            chunks = list(batches(to_detail, 20))
            details = {}
            with ThreadPoolExecutor(max_workers=config.max_concurrent_requests) as pool:
                futures = [pool.submit(_detailed_batch, client, chunk, config.page_size) for chunk in chunks]
                for future in as_completed(futures):
                    details.update(future.result())
            for lot_id in to_detail:
                lot = details[lot_id]
                if not is_goszakup_platform(lot, with_announcement=True):
                    state.add_result(lot_id, lot, "exclude")
                    continue
                if lot.get("isDeleted") == 1:
                    state.add_result(lot_id, lot, "exclude")
                    continue
                announcement = lot.get("TrdBuy") or {}
                if announcement.get("refBuyStatusId") not in statuses:
                    state.add_result(lot_id, lot, "review", "Текущий статус объявления изменился или неизвестен")
                    continue
                if not period_matches(announcement, now=now, start_day=start_day,
                                      days=config.window_days, mode=config.date_mode):
                    state.add_result(lot_id, lot, "review", "Период приёма изменился после первичного отбора")
                    continue
                if announcement.get("Cancel") or announcement.get("Pause"):
                    state.add_result(lot_id, lot, "review", "Есть сведения об отмене или приостановлении объявления")
                    continue
                plans = [p for p in lot.get("Plans") or []
                         if p.get("isDeleted") != 1 and p.get("isActive") != 0]
                if not plans or any(p.get("isQvazi") is None for p in plans):
                    state.add_result(lot_id, lot, "review", "Нет подтверждения статуса ГЗ в пункте плана")
                    continue
                if any(p.get("isQvazi") != 0 for p in plans):
                    state.add_result(lot_id, lot, "exclude")
                    continue
                classification, reason = lot_geography(lot)
                state.add_result(lot_id, lot, classification, reason)
            LOG.info("Подробности: обработано лотов %s", len(to_detail))

        # Reconsider saved reviews after a resumed run if the API supplied only
        # an empty lot-level KATO placeholder but a precise plan delivery KATO.
        for lot, reason in state.results("review"):
            if reason == "КАТО лота противоречит местам поставки пунктов плана":
                classification, _ = lot_geography(lot)
                if classification == "match":
                    state.promote_review(lot["id"], lot)

        state.set("complete", True)
        stats = {key: state.count(key) for key in ("announcements", "candidates", "lots", "review")}
        stats["lots_after_exclusions"] = stats["lots"] + stats["review"]
        stats["excluded_candidates"] = stats["candidates"] - stats["lots_after_exclusions"]
        if stats["excluded_candidates"] < 0:
            raise APIError("Число обработанных лотов превышает число кандидатов")
        exclusion_counts = {"outside_region": 0, "quasi": 0, "other": 0}
        for candidate in state.excluded_candidates():
            geography, _ = lot_geography(candidate)
            plans = [p for p in candidate.get("Plans") or []
                     if p.get("isDeleted") != 1 and p.get("isActive") != 0]
            if geography == "exclude":
                exclusion_counts["outside_region"] += 1
            elif plans and all(p.get("isQvazi") == 1 for p in plans):
                exclusion_counts["quasi"] += 1
            else:
                exclusion_counts["other"] += 1
        if sum(exclusion_counts.values()) != stats["excluded_candidates"]:
            raise APIError("Исключённые лоты не согласуются со статистикой")
        stats.update({f"excluded_{reason}": count for reason, count in exclusion_counts.items()})
        stats["announcements_scanned"] = state.get("announcements_scanned", 0)
        stats["lots_scanned"] = state.get("lots_scanned", 0)
        stats["duplicate_ids"] = state.get("duplicate_ids", 0)
        return state.results("lots"), state.results("review"), stats
    finally:
        state.close()
