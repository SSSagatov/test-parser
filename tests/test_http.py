"""HTTP protocol simulations exercise transport policy; they are not official source data."""

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.crawler.http import OfficialTransport, SourceBlocked, SourceChanged
from app.crawler.robots import RobotsRules

ORIGIN = "https://zakup.gov.kz"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None, requests_per_second=100000, retry_attempts=3, retry_max_wait=0.001
    )


class ConstructedChunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
async def test_robots_denial_never_requests_denied_document(settings: Settings) -> None:
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == "/robots.txt"
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = OfficialTransport(settings, client)
        with pytest.raises(SourceBlocked, match="robots.txt disallows"):
            await transport.get(ORIGIN + "/private/document")
    assert paths == ["/robots.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,allowed", [(404, True), (410, True), (401, False), (403, False)])
async def test_robots_http_status_semantics(settings: Settings, status: int, allowed: bool) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = OfficialTransport(settings, client)
        assert await transport.allowed(ORIGIN + "/constructed") is allowed
        assert await transport.allowed(ORIGIN + "/another-constructed") is allowed
    assert requests == ["/robots.txt"]


@pytest.mark.asyncio
async def test_robots_html_challenge_is_not_treated_as_permission(settings: Settings) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="<html>Constructed browser challenge</html>")
        )
    ) as client:
        with pytest.raises(SourceBlocked, match="rules unverified"):
            await OfficialTransport(settings, client).allowed(ORIGIN + "/constructed")


@pytest.mark.asyncio
async def test_crawl_delay_takes_precedence_over_rate_limit(
    settings: Settings, monkeypatch
) -> None:
    delays = []

    async def observed_sleep(delay: float) -> None:
        delays.append(delay)

    robot = RobotsRules()
    robot.parse(["User-agent: *", "Crawl-delay: 7", "Disallow:"])
    async with httpx.AsyncClient() as client:
        transport = OfficialTransport(settings, client)
        transport._robots[ORIGIN] = robot
        transport._last[ORIGIN] = 100
        monkeypatch.setattr("app.crawler.http.time.monotonic", lambda: 102)
        monkeypatch.setattr("app.crawler.http.asyncio.sleep", observed_sleep)
        await transport._pace(ORIGIN)
    assert delays == [5]


@pytest.mark.asyncio
@pytest.mark.parametrize("following_client", [False, True])
async def test_official_redirect_never_contacts_external_host(
    settings: Settings, following_client: bool
) -> None:
    settings.respect_robots = False
    hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "zakup.gov.kz":
            return httpx.Response(302, headers={"Location": "https://example.com/constructed"})
        return httpx.Response(200, text="Constructed external response must never be contacted")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=following_client
    ) as client:
        with pytest.raises(SourceBlocked):
            await OfficialTransport(settings, client).get(ORIGIN + "/constructed")
    assert hosts == ["zakup.gov.kz"]


@pytest.mark.asyncio
async def test_download_redirect_never_contacts_external_host(
    settings: Settings, tmp_path: Path
) -> None:
    settings.respect_robots = False
    hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "zakup.gov.kz":
            return httpx.Response(302, headers={"Location": "https://example.com/constructed"})
        return httpx.Response(200, content=b"must never be contacted")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(SourceBlocked):
            await OfficialTransport(settings, client).download(
                ORIGIN + "/constructed", tmp_path / "document.part", 1024
            )
    assert hosts == ["zakup.gov.kz"]


@pytest.mark.asyncio
async def test_authorization_removed_on_approved_cross_host_redirect(settings: Settings) -> None:
    settings.respect_robots = False
    authorization = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization.append(request.headers.get("authorization"))
        if request.url.host == "zakup.gov.kz":
            return httpx.Response(
                302, headers={"Location": "https://old.goszakup.gov.kz/constructed"}
            )
        return httpx.Response(200, content=b"Constructed response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await OfficialTransport(settings, client).request(
            "GET",
            ORIGIN + "/constructed",
            headers={"Authorization": "Bearer constructed-test-token"},
        )
    assert response.status_code == 200
    assert authorization == ["Bearer constructed-test-token", None]


@pytest.mark.asyncio
@pytest.mark.parametrize("download", [False, True])
@pytest.mark.parametrize("default_auth", ["header", "basic"])
async def test_client_default_credentials_never_follow_cross_origin_redirect(
    settings: Settings,
    tmp_path: Path,
    default_auth: str,
    download: bool,
) -> None:
    settings.respect_robots = False
    hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "zakup.gov.kz":
            return httpx.Response(
                302, headers={"Location": "https://old.goszakup.gov.kz/constructed"}
            )
        return httpx.Response(200, content=b"must never receive client default credentials")

    options = (
        {"headers": {"Authorization": "Bearer constructed-test-token"}}
        if default_auth == "header"
        else {"auth": ("constructed-user", "constructed-password")}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), **options) as client:
        transport = OfficialTransport(settings, client)
        with pytest.raises(SourceBlocked):
            if download:
                await transport.download(
                    ORIGIN + "/constructed", tmp_path / "constructed.part", 1024
                )
            else:
                await transport.get(ORIGIN + "/constructed")
    assert hosts == ["zakup.gov.kz"]


@pytest.mark.asyncio
@pytest.mark.parametrize("download", [False, True])
async def test_https_downgrade_redirect_never_sends_plaintext_request(
    settings: Settings,
    tmp_path: Path,
    download: bool,
) -> None:
    settings.respect_robots = False
    schemes = []

    def handler(request: httpx.Request) -> httpx.Response:
        schemes.append(request.url.scheme)
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"Location": "http://zakup.gov.kz/constructed"})
        return httpx.Response(200, content=b"must never receive a downgraded request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = OfficialTransport(settings, client)
        with pytest.raises(SourceBlocked):
            if download:
                await transport.download(
                    ORIGIN + "/constructed", tmp_path / "constructed.part", 1024
                )
            else:
                await transport.request(
                    "GET",
                    ORIGIN + "/constructed",
                    headers={"Authorization": "Bearer constructed-test-token"},
                )
    assert schemes == ["https"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
async def test_transient_http_statuses_retry(settings: Settings, status: int) -> None:
    settings.respect_robots = False
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(status if count < 3 else 200, content=b"Constructed response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OfficialTransport(settings, client).get(ORIGIN + "/constructed")
    assert result.status_code == 200
    assert count == 3


@pytest.mark.asyncio
async def test_permanent_status_is_not_retried(settings: Settings) -> None:
    settings.respect_robots = False
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await OfficialTransport(settings, client).get(ORIGIN + "/constructed")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_retry_exhaustion_is_visible(settings: Settings) -> None:
    settings.respect_robots = False
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await OfficialTransport(settings, client).get(ORIGIN + "/constructed")
    assert len(requests) == settings.retry_attempts


@pytest.mark.asyncio
async def test_response_stream_limit_without_content_length(settings: Settings) -> None:
    settings.respect_robots = False
    settings.max_response_bytes = 5
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=ConstructedChunks(b"abc", b"def"))
        )
    ) as client:
        with pytest.raises(SourceChanged, match="MAX_RESPONSE_BYTES"):
            await OfficialTransport(settings, client).get(ORIGIN + "/constructed")


@pytest.mark.asyncio
async def test_download_stream_limit_without_content_length(
    settings: Settings, tmp_path: Path
) -> None:
    settings.respect_robots = False
    destination = tmp_path / "constructed.part"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=ConstructedChunks(b"abc", b"def"))
        )
    ) as client:
        with pytest.raises(ValueError, match="MAX_DOWNLOAD_BYTES"):
            await OfficialTransport(settings, client).download(
                ORIGIN + "/constructed", destination, 5
            )
    assert destination.stat().st_size <= 5


@pytest.mark.asyncio
async def test_download_exact_limit_checksum_and_size(settings: Settings, tmp_path: Path) -> None:
    settings.respect_robots = False
    destination = tmp_path / "constructed.part"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=ConstructedChunks(b"abc", b"def"))
        )
    ) as client:
        checksum, size = await OfficialTransport(settings, client).download(
            ORIGIN + "/constructed", destination, 6
        )
    assert size == 6
    assert checksum == hashlib.sha256(b"abcdef").hexdigest()
    assert destination.read_bytes() == b"abcdef"
