"""Lazy, bounded Playwright sessions with official-only navigation and robots."""

import asyncio
import re
from collections import OrderedDict
from urllib.parse import unquote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from loguru import logger
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings
from app.crawler.api.official import SourceSchemaError, UnsupportedFilter
from app.crawler.http import OfficialPageUnavailable, OfficialTransport
from app.models.domain import ListingPage, LotData, SearchFilters
from app.parser.html.legacy import (
    add_announcement,
    document_modals,
    parse_document_links,
    parse_legacy_listing,
    parse_lot_detail,
    text,
)


class BrowserTransientError(RuntimeError):
    """A navigation failed temporarily and can be retried safely."""


def playwright_proxy(url: str) -> dict[str, str]:
    """Convert a configured proxy URL to Playwright's separate credential fields."""
    parsed = urlsplit(url)
    if (
        not parsed.scheme
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PROXY must be an absolute proxy URL without a path, query or fragment")
    # Accessing port validates malformed/out-of-range port numbers without
    # including the URL, which can contain credentials, in our error message.
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("PROXY contains an invalid port") from exc
    result = {"server": f"{parsed.scheme}://{parsed.netloc.rsplit('@', 1)[-1]}"}
    if parsed.username is not None:
        result["username"] = unquote(parsed.username)
        result["password"] = unquote(parsed.password or "")
    return result


class LegacyBrowserSource:
    """Render real public HTML and expand only verified read-only lot controls."""

    name = "browser"

    def __init__(self, settings: Settings, transport: OfficialTransport):
        self.settings, self.transport = settings, transport
        self._playwright = self._browser = self._context = None
        self._start_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(settings.concurrency)
        self._controls: dict[str, list[tuple[str, str]]] | None = None
        self._announcement_cache: OrderedDict[str, tuple[str, str, list[tuple[str, str]]]] = (
            OrderedDict()
        )
        self._inflight: dict[str, asyncio.Task] = {}

    async def _start(self):
        async with self._start_lock:
            if self._context is not None:
                return
            self._playwright = await async_playwright().start()
            try:
                browser_type = getattr(self._playwright, self.settings.browser)
                options = {"headless": self.settings.browser_headless}
                if self.settings.proxy:
                    options["proxy"] = playwright_proxy(self.settings.proxy.get_secret_value())
                self._browser = await browser_type.launch(**options)
                self._context = await self._browser.new_context(
                    user_agent=self.settings.user_agent,
                    locale="ru-RU",
                    timezone_id=self.settings.timezone,
                    accept_downloads=False,
                )
                self._context.set_default_timeout(self.settings.request_timeout * 1000)
            except BaseException:
                # Startup can run inside an announcement task; do not cancel
                # that task recursively through the public close() method.
                if self._browser:
                    await self._browser.close()
                    self._browser = None
                await self._playwright.stop()
                self._playwright = None
                raise

    async def _page(self):
        await self._start()
        page = await self._context.new_page()

        async def guard(route):
            request = route.request
            if request.is_navigation_request() and request.frame == page.main_frame:
                try:
                    self.transport.check_url(request.url)
                    if request.redirected_from:
                        await self.transport.before_navigation(request.url)
                except Exception:
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()

        await page.route("**/*", guard)
        return page

    async def _goto(self, page, url: str):
        await self.transport.before_navigation(url)
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=self.settings.request_timeout * 1000
            )
        except PlaywrightError as exc:
            raise BrowserTransientError(
                f"Official browser navigation failed: {type(exc).__name__}"
            ) from exc
        self.transport.check_url(page.url)
        if response is None:
            raise SourceSchemaError("Official navigation returned no HTTP response")
        if response.status in {408, 425, 429, 500, 502, 503, 504}:
            raise BrowserTransientError(
                f"Official page temporarily returned HTTP {response.status}"
            )
        if response.status in {404, 410}:
            raise OfficialPageUnavailable(
                f"Official page returned HTTP {response.status}: {urlsplit(url).path}"
            )
        if response.status >= 400:
            raise SourceSchemaError(
                f"Official page returned HTTP {response.status}: {urlsplit(url).path}"
            )
        return response

    def _retry(self):
        return AsyncRetrying(
            stop=stop_after_attempt(self.settings.retry_attempts),
            wait=wait_exponential(min=1, max=self.settings.retry_max_wait),
            retry=retry_if_exception_type((BrowserTransientError, PlaywrightTimeoutError)),
            before_sleep=lambda state: logger.warning(
                "browser_retry attempt={}", state.attempt_number
            ),
            reraise=True,
        )

    async def _render(self, url: str, selector: str = "body") -> str:
        async for attempt in self._retry():
            with attempt:
                async with self._slots:
                    page = await self._page()
                    try:
                        await self._goto(page, url)
                        await page.locator(selector).first.wait_for(state="attached")
                        if self.settings.browser_wait_ms:
                            await page.wait_for_timeout(self.settings.browser_wait_ms)
                        html = await page.content()
                        if len(html.encode("utf-8")) > self.settings.max_response_bytes:
                            raise SourceSchemaError(
                                "Rendered official page exceeds MAX_RESPONSE_BYTES"
                            )
                        logger.info("browser_page path={} bytes={}", urlsplit(url).path, len(html))
                        return html
                    finally:
                        await page.close()
        raise RuntimeError("Browser retry loop exhausted")

    async def discover(self) -> list[str]:
        """Observe official JSON response URLs without replaying restricted APIs."""
        endpoints: set[str] = set()
        lots_url = (
            urljoin(
                self.settings.portal_url.rstrip("/") + "/", self.settings.public_api_path
            ).rstrip("/")
            + "/lots/"
        )
        expected = urlsplit(lots_url)
        async with self._slots:
            page = await self._page()

            def observe(response):
                if "json" not in response.headers.get("content-type", "").casefold():
                    return
                try:
                    self.transport.check_url(response.url)
                except Exception:
                    return
                if response.request.method in {"GET", "POST"}:
                    endpoints.add(response.url)

            def lots_response(response):
                parsed = urlsplit(response.url)
                return (
                    (parsed.scheme, parsed.netloc, parsed.path)
                    == (expected.scheme, expected.netloc, expected.path)
                    and response.request.method == "GET"
                    and 200 <= response.status < 300
                    and "json" in response.headers.get("content-type", "").casefold()
                )

            page.on("response", observe)
            try:
                try:
                    async with page.expect_response(
                        lots_response, timeout=self.settings.request_timeout * 1000
                    ):
                        await self._goto(page, urljoin(self.settings.portal_url, "/home/lots"))
                except PlaywrightTimeoutError:
                    # A slow/changed frontend must not make discovery report a
                    # false certainty. Preserve observations and let the source
                    # selector choose its permitted browser fallback.
                    logger.warning(
                        "api_discovery_lots_timeout observed_endpoints={}", len(endpoints)
                    )
                logger.info("api_discovery official_json_endpoints={}", len(endpoints))
            finally:
                await page.close()
        return sorted(endpoints)

    async def _search_url(self, filters: SearchFilters) -> str:
        if filters.cpv:
            raise UnsupportedFilter(
                "The official portal uses ENSTRU; CPV is not verifiable and is not substituted"
            )
        base = (
            self.settings.search_url
            or self.settings.legacy_portal_url.rstrip("/") + "/ru/search/lots"
        )
        self.transport.check_url(base)
        params: list[tuple[str, str]] = [("count_record", str(self.settings.page_size))]
        mapping = {
            "name": filters.keyword,
            "number": filters.lot_number,
            "number_anno": filters.announcement_number,
            "customer": filters.customer,
            "amount_from": filters.budget_min,
            "amount_to": filters.budget_max,
        }
        params.extend(
            (f"filter[{key}]", str(value)) for key, value in mapping.items() if value is not None
        )
        if filters.status or filters.method:
            if self._controls is None:
                html = await self._render(base, 'input[name="filter[name]"]')
                soup = BeautifulSoup(html, "lxml")
                self._controls = {
                    name: [
                        (str(option.get("value", "")), text(option))
                        for option in soup.select(f'select[name="filter[{name}][]"] option')
                    ]
                    for name in ("status", "method")
                }
            for key, requested in (("status", filters.status), ("method", filters.method)):
                if not requested:
                    continue
                if key == "status" and requested.casefold() == "active":
                    names = {name.casefold() for name in self.settings.active_statuses}
                else:
                    names = {requested.casefold()}
                matches = [
                    (value, name) for value, name in self._controls[key] if name.casefold() in names
                ]
                # A missing dictionary match is not translated to an invented
                # code. The engine still applies the exact local predicate.
                for value, _ in matches:
                    params.append((f"filter[{key}][]", value))
        return base + ("&" if "?" in base else "?") + urlencode(params)

    async def fetch_page(self, filters: SearchFilters, cursor: str | None = None) -> ListingPage:
        if cursor:
            expected = urlsplit(
                self.settings.search_url or self.settings.legacy_portal_url + "/ru/search/lots"
            )
            actual = urlsplit(cursor)
            if (expected.scheme, expected.netloc, expected.path) != (
                actual.scheme,
                actual.netloc,
                actual.path,
            ):
                raise SourceSchemaError(
                    "Browser pagination cursor points outside the official search page"
                )
            url = cursor
        else:
            url = await self._search_url(filters)
        html = await self._render(url, "table.dataTable")
        return parse_legacy_listing(html, url, self.settings)

    async def _lot_modal(self, lot: LotData) -> str:
        url = str(lot.announcement_url) + "?tab=lots"
        ajax = (
            self.settings.legacy_portal_url.rstrip("/")
            + f"/ru/announce/ajax_load_lot/{lot.announcement_id}/"
        )
        async for attempt in self._retry():
            with attempt:
                async with self._slots:
                    page = await self._page()
                    try:
                        await self._goto(page, url)
                        selector = page.locator(".btn-select-lot").filter(
                            has_text=re.compile(r"^\s*" + re.escape(str(lot.lot_number)) + r"\s*$")
                        )
                        if await selector.count() != 1:
                            raise SourceSchemaError(
                                "Official announcement has no unique matching lot control"
                            )
                        await self.transport.before_navigation(ajax)
                        async with page.expect_response(
                            lambda response: f"/ajax_load_lot/{lot.announcement_id}" in response.url
                        ) as result:
                            await selector.click()
                        response = await result.value
                        self.transport.check_url(response.url)
                        if response.status != 200:
                            raise BrowserTransientError(
                                f"Official lot modal returned HTTP {response.status}"
                            )
                        body = page.locator(".modal-body-lot")
                        await body.locator("table").first.wait_for(state="attached")
                        return await body.inner_html()
                    finally:
                        await page.close()
        raise RuntimeError("Lot modal retry loop exhausted")

    async def _announcement(self, lot: LotData) -> tuple[str, str, list[tuple[str, str]]]:
        identifier = str(lot.announcement_id)
        if identifier in self._announcement_cache:
            self._announcement_cache.move_to_end(identifier)
            return self._announcement_cache[identifier]

        async def fetch():
            general_url = str(lot.announcement_url) + "?tab=general"
            documents_url = str(lot.announcement_url) + "?tab=documents"
            general, documents = await asyncio.gather(
                self._render(general_url, "input[readonly]"),
                self._render(documents_url, "input[readonly]"),
            )
            modals = []
            for group, name in document_modals(documents, identifier):
                modal_url = (
                    self.settings.legacy_portal_url.rstrip("/")
                    + f"/ru/announce/actionAjaxModalShowFiles/{identifier}/{group}"
                )
                modals.append((name, await self._render(modal_url)))
            result = (general, documents, modals)
            self._announcement_cache[identifier] = result
            while len(self._announcement_cache) > self.settings.api_announcement_cache_size:
                self._announcement_cache.popitem(last=False)
            return result

        task = self._inflight.get(identifier)
        if task is None:
            task = asyncio.create_task(fetch())
            self._inflight[identifier] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._inflight.pop(identifier, None)

    async def enrich(self, lot: LotData) -> LotData:
        used_modal = False
        if "/subpriceoffer/index/" in lot.source_url:
            detail = await self._render(lot.source_url, "table.table")
            result = parse_lot_detail(detail, lot, self.settings)
            if not result.delivery_place and not result.work_place and not result.service_location:
                # Some official direct pages omit delivery while the plan modal
                # contains it. Read that explicit source rather than infer it.
                modal = await self._lot_modal(lot)
                result = parse_lot_detail(modal, result, self.settings)
                used_modal = True
        else:
            detail = await self._lot_modal(lot)
            result = parse_lot_detail(detail, lot, self.settings)
            used_modal = True
        if used_modal and (result.delivery_place or result.work_place or result.service_location):
            result.location_source_url = str(lot.announcement_url) + "?tab=lots"
            result.links = list(dict.fromkeys([*result.links, result.location_source_url]))
        general, documents_html, modals = await self._announcement(result)
        result = add_announcement(general, result, self.settings)
        documents = {
            document.url: document
            for document in parse_document_links(
                documents_html, str(lot.announcement_url), str(lot.lot_number)
            )
        }
        for name, html in modals:
            for document in parse_document_links(
                html, str(lot.announcement_url), str(lot.lot_number), name
            ):
                documents[document.url] = document
        values = result.model_dump()
        values.update(
            documents=list(documents.values()),
            links=list(dict.fromkeys([*result.links, *documents])),
        )
        return LotData.model_validate(values)

    async def close(self) -> None:
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
