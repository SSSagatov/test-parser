"""Official robots snapshot plus protocol boundary tests; never fetch disallowed paths."""

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.crawler.http import OfficialTransport, SourceBlocked
from app.crawler.robots import RobotsRules

FIXTURE = Path(__file__).parent / "fixtures" / "official" / "robots.txt"


def rules(body: str) -> RobotsRules:
    result = RobotsRules()
    result.parse(body.splitlines())
    return result


def test_actual_portal_rules_disallow_api_despite_allow_root_coming_first() -> None:
    robot = rules(FIXTURE.read_text(encoding="utf-8"))
    agent = Settings(_env_file=None).user_agent
    assert robot.can_fetch(agent, "https://zakup.gov.kz/")
    assert robot.can_fetch(agent, "https://zakup.gov.kz/announcement/40745436")
    assert not robot.can_fetch(agent, "https://zakup.gov.kz/api/core/api/public/lots/")
    assert not robot.can_fetch(agent, "https://zakup.gov.kz/login")


@pytest.mark.asyncio
async def test_actual_portal_rules_block_http_api_before_any_api_request() -> None:
    requested = []
    body = FIXTURE.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        assert request.url.path == "/robots.txt"
        return httpx.Response(200, text=body)

    settings = Settings(_env_file=None, requests_per_second=100000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = OfficialTransport(settings, client)
        url = "https://zakup.gov.kz/api/core/api/public/lots/"
        assert not await transport.allowed(url)
        with pytest.raises(SourceBlocked, match="robots.txt disallows"):
            await transport.get(url)
    assert requested == ["/robots.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("client_defaults", [False, True])
async def test_http_checks_robots_against_effective_query_parameters(client_defaults: bool) -> None:
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        assert request.url.path == "/robots.txt"
        return httpx.Response(200, text="User-agent: *\nDisallow: /*?download=1$")

    settings = Settings(_env_file=None, requests_per_second=100000)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        params={"download": "1"} if client_defaults else None,
    ) as client:
        transport = OfficialTransport(settings, client)
        with pytest.raises(SourceBlocked):
            await transport.get(
                "https://zakup.gov.kz/document",
                params=None if client_defaults else {"download": "1"},
            )
    assert requested == ["/robots.txt"]


@pytest.mark.parametrize(
    "directives",
    [
        "Allow: /\nDisallow: /private/\nAllow: /private/public/",
        "Allow: /private/public/\nDisallow: /private/\nAllow: /",
    ],
)
def test_longest_path_wins_regardless_of_order(directives: str) -> None:
    robot = rules("User-agent: *\n" + directives)
    assert robot.can_fetch("Bot", "https://example.com/anything")
    assert not robot.can_fetch("Bot", "https://example.com/private/secret")
    assert robot.can_fetch("Bot", "https://example.com/private/public/document")


@pytest.mark.parametrize(
    "directives",
    [
        "Allow: /same\nDisallow: /same",
        "Disallow: /same\nAllow: /same",
    ],
)
def test_allow_wins_equal_specificity_ties(directives: str) -> None:
    assert rules("User-agent: *\n" + directives).can_fetch("Bot", "/same/document")


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("/folder/file.pdf", False),
        ("/folder/nested/file.pdf", False),
        ("/folder/file.pdf?download=true", True),
        ("/folder/file.pdfx", True),
        ("/folder/file.PDF", True),
        ("/folder/file.txt", True),
    ],
)
def test_wildcard_and_terminal_anchor(url: str, allowed: bool) -> None:
    robot = rules("User-agent: *\nDisallow: /*.pdf$")
    assert robot.can_fetch("Bot", url) is allowed


@pytest.mark.parametrize("url", ["/start/end", "/start/middle/end", "/start/end/end"])
def test_zero_or_more_wildcard_matches_with_terminal_end(url: str) -> None:
    assert not rules("User-agent: *\nDisallow: /start/*end$").can_fetch("Bot", url)


def test_all_equally_specific_useragent_groups_merge() -> None:
    robot = rules("""User-agent: ExampleBot
Disallow: /one
User-agent: examplebot
Disallow: /two
Allow: /one/open
User-agent: *
Disallow: /
""")
    assert not robot.can_fetch("EXAMPLEBOT/1.2", "/one")
    assert not robot.can_fetch("EXAMPLEBOT/1.2", "/two")
    assert robot.can_fetch("EXAMPLEBOT/1.2", "/one/open")
    assert robot.can_fetch("EXAMPLEBOT/1.2", "/elsewhere")
    assert not robot.can_fetch("OtherBot", "/elsewhere")


def test_most_specific_agent_group_wins_and_does_not_inherit_generic_delay() -> None:
    robot = rules("""User-agent: *
Disallow: /
Crawl-delay: 5
User-agent: Example
Disallow: /generic
Crawl-delay: 4
User-agent: ExampleBot
Disallow: /specific
Crawl-delay: 2
User-agent: ExampleBot
Disallow: /also-specific
Crawl-delay: 3
""")
    assert robot.can_fetch("ExampleBot/1.2", "/generic")
    assert not robot.can_fetch("ExampleBot/1.2", "/specific")
    assert not robot.can_fetch("ExampleBot/1.2", "/also-specific")
    assert robot.crawl_delay("ExampleBot/1.2") == 3
    assert robot.crawl_delay("UnrelatedBot") == 5


def test_multiple_agents_in_one_group_and_unknown_records_do_not_break_group() -> None:
    robot = rules("""User-agent: FirstBot
User-agent: SecondBot
Sitemap: https://example.com/sitemap.xml
Disallow: /hidden
Unknown: ignored
Allow: /hidden/open
""")
    for agent in ("FirstBot", "SecondBot"):
        assert not robot.can_fetch(agent, "/hidden")
        assert robot.can_fetch(agent, "/hidden/open")


def test_empty_disallow_bom_comments_case_and_rules_outside_groups() -> None:
    robot = rules("""Disallow: /outside
\ufeffUsEr-AgEnT: * # applies to all
Disallow:
Disallow: /Private # inline comment
""")
    assert robot.can_fetch("Bot", "/outside")
    assert robot.can_fetch("Bot", "/public")
    assert robot.can_fetch("Bot", "/private")
    assert not robot.can_fetch("Bot", "/Private")


@pytest.mark.parametrize(
    "pattern,url",
    [
        ("/caf%C3%A9", "/café"),
        ("/café", "/caf%c3%a9"),
        ("/api/", "/%61pi/document"),
        ("/api/", "/a%70i/document"),
        ("/file%2A.txt", "/file*.txt"),
        ("/price%24", "/price$"),
        ("/part%23two", "/part%23two"),
    ],
)
def test_percent_encoded_paths_match_consistently(pattern: str, url: str) -> None:
    assert not rules("User-agent: *\nDisallow: " + pattern).can_fetch("Bot", url)


def test_encoded_slash_is_not_decoded_into_path_separator() -> None:
    robot = rules("User-agent: *\nDisallow: /private/")
    assert robot.can_fetch("Bot", "/private%2Fdocument")
    assert not robot.can_fetch("Bot", "/private/document")


def test_query_is_matched_but_fragment_is_not() -> None:
    robot = rules("User-agent: *\nDisallow: /*?download=1$")
    assert not robot.can_fetch("Bot", "/document?download=1#fragment")
    assert robot.can_fetch("Bot", "/document?download=2")


def test_robots_itself_is_always_allowed() -> None:
    assert rules("User-agent: *\nDisallow: /").can_fetch("Bot", "/robots.txt")


def test_empty_specific_group_is_not_overridden_by_wildcard_group() -> None:
    robot = rules("User-agent: *\nDisallow: /\nUser-agent: ExampleBot\nDisallow:")
    assert robot.can_fetch("ExampleBot", "/anything")


def test_finite_decimal_crawl_delay_and_old_portal_delay_five() -> None:
    robot = rules("User-agent: *\nCrawl-delay: NaN\nCrawl-delay: -1\nCrawl-delay: 5")
    assert robot.crawl_delay("KazakhstanProcurementParser/0.1") == 5
    fractional = rules("User-agent: *\nCrawl-delay: 0.5")
    assert fractional.crawl_delay("Bot") == 0.5
