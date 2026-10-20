"""PowerShell-friendly command line entry point."""
import argparse
from datetime import date, datetime
from decimal import Decimal
import logging
import sys
from zoneinfo import ZoneInfo

from .api import APIError, Client
from .collector import collect
from .config import Config
from .export import export_excel, export_json
from .specifications import SpecificationExtractor


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Лоты ГЗ Павлодарской области через GraphQL V3")
    parser.add_argument("--test-connection", action="store_true", help="проверить токен и endpoint")
    parser.add_argument("--date", type=date.fromisoformat, help="начало окна YYYY-MM-DD")
    parser.add_argument("--mode", choices=["active_now", "starts_in_window", "ends_in_window", "overlaps_window"])
    parser.add_argument("--resume", action="store_true", help="продолжить сохранённую выгрузку")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    try:
        config = Config.load()
        if args.test_connection:
            print("Connecting to goszakup.kz GraphQL V3...")
            Client(config).graphql("query ConnectionTest { __typename }", {})
            print("Authentication successful.")
            return 0
        today = datetime.now(ZoneInfo(config.timezone)).date()
        start_day = args.date or today
        if start_day < today:
            raise ValueError("Исторический снимок статусов API недоступен; прошедшая --date запрещена")
        if args.mode:
            from dataclasses import replace
            config = replace(config, date_mode=args.mode)
        print(f"Region: Pavlodar Region\nDate: {start_day}\n"
              f"Window: {start_day} — {date.fromordinal(start_day.toordinal()+config.window_days-1)}")
        lots, review, stats = collect(config, start_day, args.resume)
        stats["errors"] = 0
        stats["total_amount_tenge"] = str(sum((Decimal(str(lot.get("amount") or 0)) for lot in lots), Decimal(0)))
        stem = f"pavlodar_lots_{start_day}"
        if config.date_mode != "starts_in_window":
            stem += f"_{config.date_mode}"
        json_path = config.output_dir / f"{stem}.json"
        xlsx_path = config.output_dir / f"{stem}.xlsx"
        print("Exporting JSON...")
        export_json(json_path, lots, review, stats)
        print("Загружаю и читаю технические спецификации...")
        specifications = SpecificationExtractor(config).for_lots(lots)
        extracted_count = sum(bool(item.text) for item in specifications.values())
        print(f"Текст техспецификации извлечён для {extracted_count} из {len(lots)} лотов.")
        print("Exporting Excel...")
        export_excel(xlsx_path, lots, review, stats, specifications)
        print(f"Лоты объявлений: {stats['candidates']}; исключено по месту поставки: "
              f"{stats['excluded_outside_region']}, квазигосзакупки: {stats['excluded_quasi']}, "
              f"прочее: {stats['excluded_other']}; осталось: {stats['lots_after_exclusions']}.")
        print(f"Export completed: {len(lots)} confirmed lots; {len(review)} for review.")
        print(json_path.resolve())
        print(xlsx_path.resolve())
        return 0
    except (ValueError, APIError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
