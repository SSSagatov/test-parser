"""Select a verified JSON interface first, then rendered official pages."""

from loguru import logger

from app.config import Settings
from app.crawler.api.official import OfficialPublicSource, OwsSource
from app.crawler.browser import LegacyBrowserSource
from app.crawler.engine import Source
from app.crawler.http import OfficialTransport, SourceBlocked


async def select_source(settings: Settings, transport: OfficialTransport) -> Source:
    if settings.source_mode == "ows" or (settings.source_mode == "auto" and settings.ows_token):
        return OwsSource(settings, transport)
    browser = LegacyBrowserSource(settings, transport)
    if settings.source_mode == "browser":
        return browser
    public = OfficialPublicSource(settings, transport)
    permitted = await transport.allowed(public.base + "lots/")
    if settings.source_mode == "public":
        if not permitted:
            raise SourceBlocked(
                "Public API is disallowed by robots.txt; use auto/browser or official OWS access"
            )
        return public
    try:
        endpoints = await browser.discover()
        logger.info("api_discovery official_json_endpoints={}", endpoints)
        if permitted and any("/public/lots/" in url for url in endpoints):
            await browser.close()
            return public
        logger.info("source_selected browser; public_api_permitted={}", permitted)
        return browser
    except Exception as exc:
        logger.warning(
            "api_discovery_failed {}; using official browser fallback", type(exc).__name__
        )
        return browser
