from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    graphql_url: str
    timezone: str
    date_mode: str
    window_days: int
    page_size: int
    request_timeout: int
    max_retries: int
    max_concurrent_requests: int
    output_dir: Path

    @classmethod
    def load(cls) -> "Config":
        load_dotenv()
        token = os.getenv("GOSZAKUP_API_TOKEN", "").strip()
        if not token or token == "YOUR_API_TOKEN":
            raise ValueError("GOSZAKUP_API_TOKEN отсутствует в локальном .env")
        url = os.getenv("GOSZAKUP_GRAPHQL_URL", "https://ows.goszakup.gov.kz/v3/graphql")
        if url != "https://ows.goszakup.gov.kz/v3/graphql":
            raise ValueError("Разрешён только официальный GraphQL V3 endpoint")
        if os.getenv("TARGET_REGION", "Pavlodar").casefold() != "pavlodar":
            raise ValueError("Эта версия поддерживает только TARGET_REGION=Pavlodar")
        timezone = os.getenv("TIMEZONE", "Asia/Almaty")
        ZoneInfo(timezone)
        mode = os.getenv("DATE_MODE", "starts_in_window")
        if mode not in {"active_now", "starts_in_window", "ends_in_window", "overlaps_window"}:
            raise ValueError("Недопустимый DATE_MODE")
        days = int(os.getenv("WINDOW_DAYS", "2"))
        size = int(os.getenv("PAGE_SIZE", "100"))
        timeout = int(os.getenv("REQUEST_TIMEOUT", "30"))
        retries = int(os.getenv("MAX_RETRIES", "5"))
        concurrency = int(os.getenv("MAX_CONCURRENT_REQUESTS", "3"))
        if not (1 <= days <= 31 and 1 <= size <= 200 and timeout > 0 and 0 <= retries <= 10 and 1 <= concurrency <= 10):
            raise ValueError("Некорректные числовые настройки")
        return cls(token, url, timezone, mode, days, size, timeout, retries, concurrency,
                   Path(os.getenv("OUTPUT_DIR", "output")))

