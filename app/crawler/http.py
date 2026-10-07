"""Rate-limited official-only HTTP transport with robots and safe redirects."""

import asyncio
import hashlib
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import Settings
from app.crawler.robots import RobotsRules


class SourceBlocked(RuntimeError):
    """The official source does not permit this request."""


class SourceChanged(RuntimeError):
    """Source layout/schema changed; stopping avoids silently missing data."""


class OfficialPageUnavailable(RuntimeError):
    """A previously listed official detail page is permanently unavailable."""


def retryable(exc: BaseException) -> bool:
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
    )


class OfficialTransport:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=settings.request_timeout,
            limits=httpx.Limits(
                max_connections=settings.concurrency + settings.download_concurrency
            ),
            proxy=settings.proxy.get_secret_value() if settings.proxy else None,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(settings.concurrency + settings.download_concurrency)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._robots_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last: dict[str, float] = defaultdict(float)
        self._robots: dict[str, RobotsRules] = {}

    def check_url(self, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
            raise SourceBlocked(f"Unsafe source URL: {parsed.hostname}")
        if parsed.hostname not in self.settings.official_hosts or parsed.port not in {
            None,
            80,
            443,
        }:
            raise SourceBlocked(f"Host is not an approved official source: {parsed.hostname}")
        return f"{parsed.scheme}://{parsed.netloc}"

    async def _pace(self, origin: str) -> None:
        delay = 1 / self.settings.requests_per_second
        robot = self._robots.get(origin)
        if robot:
            delay = max(delay, robot.crawl_delay(self.settings.user_agent) or 0)
        async with self._locks[origin]:
            await asyncio.sleep(max(0, self._last[origin] + delay - time.monotonic()))
            self._last[origin] = time.monotonic()

    async def _load_robots(self, origin: str) -> RobotsRules:
        async with self._robots_locks[origin]:
            if origin in self._robots:
                return self._robots[origin]
            url = origin + "/robots.txt"
            robot = RobotsRules(url)
            response = await self._request_retry("GET", url, robots=False)
            if response.status_code in {404, 410}:
                robot.parse(["User-agent: *", "Disallow:"])
            elif response.status_code in {401, 403}:
                robot.parse(["User-agent: *", "Disallow: /"])
            else:
                response.raise_for_status()
                if "<html" in response.text[:1000].lower():
                    raise SourceBlocked(f"robots.txt returned HTML at {origin}; rules unverified")
                robot.parse(response.text.splitlines())
            self._robots[origin] = robot
            return robot

    async def allowed(self, url: str) -> bool:
        origin = self.check_url(url)
        if not self.settings.respect_robots:
            return True
        robot = await self._load_robots(origin)
        return robot.can_fetch(self.settings.user_agent, url)

    async def before_navigation(self, url: str) -> None:
        if not await self.allowed(url):
            raise SourceBlocked(f"robots.txt disallows {urlsplit(url).path}")
        await self._pace(self.check_url(url))

    async def _once(self, method: str, url: str, robots: bool, **kwargs) -> httpx.Response:
        for _ in range(6):
            effective_url = str(
                self.client.build_request(method, url, params=kwargs.get("params")).url
            )
            origin = self.check_url(effective_url)
            if robots and not await self.allowed(effective_url):
                raise SourceBlocked(f"robots.txt disallows {urlsplit(url).path}")
            await self._pace(origin)
            async with self._semaphore:
                # Ordinary JSON/HTML responses are bounded to protect the ingestion process.
                async with self.client.stream(
                    method, url, follow_redirects=False, **kwargs
                ) as response:
                    if response.is_redirect:
                        next_url = urljoin(str(response.url), response.headers["location"])
                        self.check_url(next_url)
                        if urlsplit(url).scheme == "https" and urlsplit(next_url).scheme != "https":
                            raise SourceBlocked("HTTPS downgrade redirect refused")
                        if urlsplit(next_url).netloc != urlsplit(url).netloc:
                            if (
                                self.client.headers.get("authorization")
                                or self.client.auth is not None
                            ):
                                raise SourceBlocked(
                                    "Cross-origin redirect with client credentials refused"
                                )
                            headers = dict(kwargs.get("headers", {}))
                            headers = {
                                k: v for k, v in headers.items() if k.lower() != "authorization"
                            }
                            kwargs["headers"] = headers
                        url = next_url
                        kwargs.pop("params", None)
                        if response.status_code == 303:
                            method = "GET"
                            kwargs.pop("json", None)
                            kwargs.pop("data", None)
                        continue
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.settings.max_response_bytes:
                            raise SourceChanged("Official response exceeds MAX_RESPONSE_BYTES")
                    headers = {
                        k: v
                        for k, v in response.headers.items()
                        if k.lower() not in {"content-encoding", "content-length"}
                    }
                    result = httpx.Response(
                        response.status_code,
                        headers=headers,
                        content=bytes(body),
                        request=response.request,
                    )
                    if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                        retry_after = response.headers.get("retry-after", "")
                        if retry_after.isdigit():
                            await asyncio.sleep(
                                min(float(retry_after), self.settings.retry_max_wait)
                            )
                        result.raise_for_status()
                    logger.info(
                        "downloaded_page method={} path={} status={} bytes={}",
                        method,
                        urlsplit(url).path,
                        result.status_code,
                        len(body),
                    )
                    return result
        raise SourceBlocked("Too many source redirects")

    async def _request_retry(
        self, method: str, url: str, robots: bool = True, **kwargs
    ) -> httpx.Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.settings.retry_attempts),
            wait=wait_exponential(min=1, max=self.settings.retry_max_wait),
            retry=retry_if_exception(retryable),
            reraise=True,
            before_sleep=lambda state: logger.warning(
                "retry path={} attempt={}", urlsplit(url).path, state.attempt_number
            ),
        ):
            with attempt:
                return await self._once(method, url, robots, **kwargs)
        raise RuntimeError("Retry loop exhausted")

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        response = await self._request_retry(method, url, **kwargs)
        response.raise_for_status()
        return response

    async def get(self, url: str, params=None) -> httpx.Response:
        return await self.request("GET", url, params=params)

    async def download(self, url: str, destination: Path, max_bytes: int) -> tuple[str, int]:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.settings.retry_attempts),
            wait=wait_exponential(min=1, max=self.settings.retry_max_wait),
            retry=retry_if_exception(retryable),
            reraise=True,
            before_sleep=lambda state: logger.warning(
                "retry_file path={} attempt={}", urlsplit(url).path, state.attempt_number
            ),
        ):
            with attempt:
                current = url
                for _ in range(6):
                    effective_url = str(self.client.build_request("GET", current).url)
                    await self.before_navigation(effective_url)
                    async with (
                        self._semaphore,
                        self.client.stream("GET", current, follow_redirects=False) as response,
                    ):
                        if response.is_redirect:
                            next_url = urljoin(str(response.url), response.headers["location"])
                            self.check_url(next_url)
                            if (
                                urlsplit(current).scheme == "https"
                                and urlsplit(next_url).scheme != "https"
                            ):
                                raise SourceBlocked("HTTPS downgrade redirect refused")
                            if urlsplit(current).netloc != urlsplit(next_url).netloc and (
                                self.client.headers.get("authorization")
                                or self.client.auth is not None
                            ):
                                raise SourceBlocked(
                                    "Cross-origin redirect with client credentials refused"
                                )
                            current = next_url
                            continue
                        response.raise_for_status()
                        content_length = response.headers.get("content-length")
                        if content_length and int(content_length) > max_bytes:
                            raise ValueError("Attachment exceeds MAX_DOWNLOAD_BYTES")
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        digest, size = hashlib.sha256(), 0
                        with destination.open("wb") as target:
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > max_bytes:
                                    raise ValueError("Attachment exceeds MAX_DOWNLOAD_BYTES")
                                digest.update(chunk)
                                target.write(chunk)
                        logger.info(
                            "downloaded_file path={} bytes={} sha256={}",
                            urlsplit(url).path,
                            size,
                            digest.hexdigest(),
                        )
                        return digest.hexdigest(), size
                raise SourceBlocked("Too many file redirects")
        raise RuntimeError("Retry loop exhausted")

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
