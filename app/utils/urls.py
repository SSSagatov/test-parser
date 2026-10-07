from urllib.parse import urlsplit


def official_url(url: str | None, hosts: list[str]) -> bool:
    if not url:
        return False
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in hosts
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 80, 443}
        )
    except ValueError:
        return False
