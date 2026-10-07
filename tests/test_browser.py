"""Browser orchestration tests; network data is the saved official HTML."""

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import Settings
from app.crawler.api.official import SourceSchemaError, UnsupportedFilter
from app.crawler.browser import LegacyBrowserSource
from app.crawler.browser.legacy import playwright_proxy
from app.crawler.http import OfficialPageUnavailable
from app.models.domain import SearchFilters

FIXTURES = Path(__file__).parent / "fixtures" / "official"


class AuthorizedTransport:
    def check_url(self, url):
        assert urlsplit(url).hostname == "old.goszakup.gov.kz"


class CapturedBrowser(LegacyBrowserSource):
    """Replace rendering only; run the real source, selectors, cursors and parsers."""

    def __init__(self, settings):
        super().__init__(settings, AuthorizedTransport())
        self.visits = []

    async def _render(self, url, selector="body"):
        self.visits.append(url)
        return (FIXTURES / "legacy_lots.html").read_text(encoding="utf-8")


async def test_browser_forwards_official_pagination_without_page_one_restart():
    source = CapturedBrowser(Settings(_env_file=None))
    first = await source.fetch_page(SearchFilters(keyword="medical"))
    second = await source.fetch_page(SearchFilters(keyword="medical"), first.next_cursor)
    assert parse_qs(urlsplit(source.visits[0]).query)["filter[name]"] == ["medical"]
    assert source.visits[-1] == first.next_cursor
    assert second.next_cursor.endswith("page=3")


async def test_status_filter_resolves_actual_dropdown_option():
    source = CapturedBrowser(Settings(_env_file=None))
    url = await source._search_url(SearchFilters(status="Опубликован (прием заявок)"))
    query = parse_qs(urlsplit(url).query)
    assert query["filter[status][]"] == ["220"]
    assert source._context is None


async def test_publication_date_is_not_replaced_with_submission_date():
    from datetime import UTC, datetime

    source = CapturedBrowser(Settings(_env_file=None))
    url = await source._search_url(SearchFilters(date_from=datetime(2026, 10, 1, tzinfo=UTC)))
    assert "filter[start_date_from]" not in parse_qs(urlsplit(url).query)


async def test_browser_rejects_unverifiable_cpv_and_external_cursor():
    source = CapturedBrowser(Settings(_env_file=None))
    with pytest.raises(UnsupportedFilter, match="ENSTRU"):
        await source.fetch_page(SearchFilters(cpv="30200000"))
    with pytest.raises(SourceSchemaError, match="outside"):
        await source.fetch_page(SearchFilters(), "https://example.com/ru/search/lots?page=2")
    assert not source.visits
    await source.close()


@pytest.mark.parametrize("status", [404, 410])
async def test_permanently_unavailable_lot_is_distinct_from_schema_failure(status):
    class Transport(AuthorizedTransport):
        async def before_navigation(self, url):
            self.check_url(url)

    class MissingPage:
        url = "https://old.goszakup.gov.kz/ru/subpriceoffer/index/17746286/43519574"

        async def goto(self, url, **kwargs):
            return SimpleNamespace(status=status)

    source = LegacyBrowserSource(Settings(_env_file=None), Transport())
    with pytest.raises(OfficialPageUnavailable, match=str(status)):
        await source._goto(MissingPage(), MissingPage.url)


def test_playwright_proxy_separates_credentials_and_preserves_ipv6():
    assert playwright_proxy("http://first%40name:p%3Ass%2Fword@[::1]:8080/") == {
        "server": "http://[::1]:8080",
        "username": "first@name",
        "password": "p:ss/word",
    }
    assert playwright_proxy("socks5://proxy.example:1080") == {
        "server": "socks5://proxy.example:1080"
    }


@pytest.mark.parametrize(
    "url", ["proxy:8080", "http://proxy.example/path", "http://proxy.example:99999"]
)
def test_malformed_proxy_url_is_rejected(url):
    with pytest.raises(ValueError, match="PROXY"):
        playwright_proxy(url)


@pytest.mark.parametrize("timeout", [False, True])
async def test_discovery_waits_for_lots_response_and_keeps_observed_urls(monkeypatch, timeout):
    import asyncio

    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    class Transport:
        def check_url(self, url):
            assert urlsplit(url).hostname == "zakup.gov.kz"

    class ObservedPage:
        def __init__(self):
            self.callback = None
            self.predicate = None
            self.response_ready = asyncio.Event()
            self.closed = False
            self.timeout = None

        def on(self, event, callback):
            assert event == "response"
            self.callback = callback

        def emit(self, path):
            response = SimpleNamespace(
                url="https://zakup.gov.kz" + path,
                status=200,
                headers={"content-type": "application/json"},
                request=SimpleNamespace(method="GET"),
            )
            self.callback(response)
            if self.predicate(response):
                self.response_ready.set()

        def expect_response(self, predicate, *, timeout):
            self.predicate, self.timeout = predicate, timeout
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if timeout:
                raise PlaywrightTimeoutError("Controlled response timeout")
            await self.response_ready.wait()

        async def close(self):
            self.closed = True

    page = ObservedPage()
    source = LegacyBrowserSource(Settings(_env_file=None, request_timeout=7), Transport())

    async def new_page():
        return page

    async def goto(actual_page, url):
        assert actual_page.predicate is not None  # Listener must precede navigation.
        actual_page.emit("/api/core/api/public/stats/counts/")
        if not timeout:
            asyncio.get_running_loop().call_soon(
                actual_page.emit, "/api/core/api/public/lots/?limit=10&offset=0"
            )

    monkeypatch.setattr(source, "_page", new_page)
    monkeypatch.setattr(source, "_goto", goto)
    endpoints = await source.discover()
    assert page.closed and page.timeout == 7000
    assert "https://zakup.gov.kz/api/core/api/public/stats/counts/" in endpoints
    assert any("/lots/" in endpoint for endpoint in endpoints) is not timeout
