"""Robots rules with longest-match precedence (RFC 9309 sections 2.2.1–2.2.3).

Python's urllib.robotparser uses the first matching path rule, which wrongly
permits /api/ when this portal places Allow: / before Disallow: /api/.
"""

import math
import string
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_HEX = frozenset(string.hexdigits)


def _normalize(value: str, *, pattern: bool = False) -> str:
    """Normalize percent escapes without turning an encoded slash into a separator."""
    output: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if (
            character == "%"
            and index + 2 < len(value)
            and all(digit in _HEX for digit in value[index + 1 : index + 3])
        ):
            escaped = value[index + 1 : index + 3]
            decoded = chr(int(escaped, 16))
            output.append(decoded if decoded in _UNRESERVED else "%" + escaped.upper())
            index += 3
            continue
        if (
            ord(character) > 127
            or character.isspace()
            or (character == "*" and not pattern)
            or (character == "$" and (not pattern or index != len(value) - 1))
        ):
            output.append(quote(character, safe=""))
        else:
            output.append(character)
        index += 1
    return "".join(output)


@dataclass(frozen=True)
class _Rule:
    pattern: str
    allow: bool

    def matches(self, path: str) -> bool:
        """Match glob segments without regex backtracking on untrusted rule text."""
        anchored = self.pattern.endswith("$")
        value = self.pattern[:-1] if anchored else self.pattern
        pieces = value.split("*")
        if not path.startswith(pieces[0]):
            return False
        position = len(pieces[0])
        if len(pieces) == 1:
            return not anchored or position == len(path)
        for index, piece in enumerate(pieces[1:], start=1):
            if anchored and index == len(pieces) - 1:
                return path.endswith(piece) and len(path) - len(piece) >= position
            found = path.find(piece, position)
            if found < 0:
                return False
            position = found + len(piece)
        return True


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)
    delays: list[float] = field(default_factory=list)


class RobotsRules:
    """Select the most-specific user-agent groups, merge them, then match paths.

    Crawl-delay is an extension: repeated values in applicable groups use their
    maximum. Empty directives do not deny URLs. Rule order never changes access.
    """

    def __init__(self, url: str = "") -> None:
        self.url = url
        self._groups: list[_Group] = []

    def parse(self, lines: Iterable[str]) -> None:
        self._groups = []
        current = _Group()
        directives_started = False
        for original in lines:
            line = original.lstrip("\ufeff").split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.casefold()
            if key == "user-agent":
                if not value:
                    continue
                if directives_started:
                    self._groups.append(current)
                    current = _Group()
                    directives_started = False
                current.agents.append(value.casefold())
            elif current.agents and key in {"allow", "disallow"}:
                directives_started = True
                if value:
                    current.rules.append(_Rule(_normalize(value, pattern=True), key == "allow"))
            elif current.agents and key == "crawl-delay":
                directives_started = True
                try:
                    delay = float(value)
                    if math.isfinite(delay) and delay >= 0:
                        current.delays.append(delay)
                except ValueError:
                    continue
            # Unknown records (including Sitemap) never terminate a group.
        if current.agents:
            self._groups.append(current)

    def _applicable(self, user_agent: str) -> list[_Group]:
        product = user_agent.casefold().split("/", 1)[0].split(" ", 1)[0]
        candidates: list[tuple[int, _Group]] = []
        for group in self._groups:
            specificity = max(
                (len(agent) for agent in group.agents if agent != "*" and agent in product),
                default=-1,
            )
            if specificity >= 0:
                candidates.append((specificity, group))
        if candidates:
            longest = max(size for size, _ in candidates)
            return [group for size, group in candidates if size == longest]
        return [group for group in self._groups if "*" in group.agents]

    def can_fetch(self, user_agent: str, url: str) -> bool:
        parsed = urlsplit(url)
        path = _normalize((parsed.path or "/") + ("?" + parsed.query if parsed.query else ""))
        if parsed.path == "/robots.txt":
            return True
        matches = [
            rule
            for group in self._applicable(user_agent)
            for rule in group.rules
            if rule.matches(path)
        ]
        if not matches:
            return True
        # Allow wins a tie, independent of directive order.
        return max(matches, key=lambda rule: (len(rule.pattern), rule.allow)).allow

    def crawl_delay(self, user_agent: str) -> float | None:
        return max(
            (delay for group in self._applicable(user_agent) for delay in group.delays),
            default=None,
        )
