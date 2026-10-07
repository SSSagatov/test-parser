from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: object, timezone: str = "Asia/Almaty") -> datetime | None:
    """Normalize official ISO/local dates to UTC without guessing a missing date."""
    if value is None or value == "":
        return None
    raw = str(value).strip()
    for fmt in (None, "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = (
                datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if fmt is None
                else datetime.strptime(raw, fmt)
            )
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
            return parsed.astimezone(UTC)
        except ValueError:
            continue
    raise ValueError(f"Unsupported official date: {raw!r}")
