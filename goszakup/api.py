"""Authenticated HTTP and safe ID cursor pagination."""
import logging
import random
import time
from threading import Lock
from urllib.parse import urljoin, urlparse

import requests

LOG = logging.getLogger(__name__)


class APIError(RuntimeError):
    pass


class AuthError(APIError):
    pass


class Client:
    def __init__(self, config):
        self.config = config
        self.request_count = 0
        self._count_lock = Lock()

    def _request(self, method, url, *, payload=None):
        if urlparse(url).hostname != "ows.goszakup.gov.kz" or urlparse(url).scheme != "https":
            raise APIError("Запрос к неофициальному адресу заблокирован")
        headers = {"Authorization": f"Bearer {self.config.token}", "Accept": "application/json"}
        for attempt in range(self.config.max_retries + 1):
            try:
                with self._count_lock:
                    self.request_count += 1
                response = requests.request(method, url, json=payload, headers=headers,
                                            timeout=self.config.request_timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == self.config.max_retries:
                    raise APIError("Сеть или тайм-аут API после повторных попыток") from exc
                response = None
            if response is not None:
                if response.status_code in (401, 403):
                    raise AuthError("API отклонил токен (401/403)")
                if response.status_code not in (429, 500, 502, 503, 504):
                    try:
                        response.raise_for_status()
                        result = response.json()
                    except (requests.HTTPError, ValueError) as exc:
                        raise APIError(f"HTTP {response.status_code} или невалидный JSON API") from exc
                    if not isinstance(result, dict):
                        raise APIError("Некорректная структура ответа API")
                    return result
                if attempt == self.config.max_retries:
                    raise APIError(f"HTTP {response.status_code} после повторных попыток")
                retry_after = response.headers.get("Retry-After")
            else:
                retry_after = None
            delay = min(30, 2 ** attempt + random.random())
            if retry_after:
                try:
                    delay = min(60, max(delay, float(retry_after)))
                except ValueError:
                    pass
            LOG.warning("Временная ошибка API; повтор через %.1f с", delay)
            time.sleep(delay)
        raise AssertionError("unreachable")

    def graphql(self, query, variables):
        response = self._request("POST", self.config.graphql_url,
                                 payload={"query": query, "variables": variables})
        if response.get("errors"):
            # Deliberately do not log server error text, which could echo request headers.
            raise APIError(f"GraphQL вернул {len(response['errors'])} ошибок; частичный ответ отклонён")
        if not isinstance(response.get("data"), dict):
            raise APIError("GraphQL не вернул data")
        return response

    def reference_statuses(self):
        """Official V3 reference, fetched once per run. No guessed status IDs."""
        url = "https://ows.goszakup.gov.kz/v3/refs/ref_buy_status"
        items = []
        seen = set()
        while url:
            if url in seen:
                raise APIError("Цикл в пагинации справочника статусов")
            seen.add(url)
            page = self._request("GET", url)
            values = page.get("items")
            if not isinstance(values, list):
                raise APIError("Справочник статусов имеет неожиданный формат")
            items.extend(values)
            following = page.get("next_page")
            url = urljoin("https://ows.goszakup.gov.kz", following) if following else None
        return items

    def reference_kato_codes(self, prefix="55"):
        """Read the official descending KATO reference through the region's range."""
        url = "https://ows.goszakup.gov.kz/v3/refs/ref_kato"
        seen_urls = set()
        codes = []
        previous = None
        entered_region = False
        while url:
            if url in seen_urls:
                raise APIError("Цикл в пагинации справочника КАТО")
            seen_urls.add(url)
            page = self._request("GET", url)
            items = page.get("items")
            if not isinstance(items, list):
                raise APIError("Справочник КАТО имеет неожиданный формат")
            if not items:
                break
            below_region = False
            for item in items:
                code = item.get("code") if isinstance(item, dict) else None
                if not isinstance(code, str) or len(code) != 9 or not code.isdigit():
                    raise APIError("Некорректный код в справочнике КАТО")
                if previous is not None and code >= previous:
                    raise APIError("Нарушен порядок или повторён код в справочнике КАТО")
                previous = code
                if code.startswith(prefix):
                    if below_region:
                        raise APIError("Нарушен порядок кодов области в справочнике КАТО")
                    entered_region = True
                    codes.append(code)
                elif entered_region and code < prefix:
                    below_region = True
            if below_region:
                break
            following = page.get("next_page")
            url = urljoin("https://ows.goszakup.gov.kz", following) if following else None
        if not codes:
            raise APIError("Справочник КАТО не содержит кодов Павлодарской области")
        return codes

    def pages(self, query, root, filter_value, page_size, start_after=None):
        after = start_after
        direction = None
        seen_cursors = set()
        seen_pages = set()
        while True:
            variables = {"filter": filter_value, "limit": page_size, "after": after}
            response = self.graphql(query, variables)
            rows = response["data"].get(root)
            page_info = (response.get("extensions") or {}).get("pageInfo") or {}
            if rows is None and page_info.get("totalCount") == 0 and page_info.get("hasNextPage") is False:
                return
            if not isinstance(rows, list):
                raise APIError(f"GraphQL поле {root} не является списком")
            if not rows:
                return
            ids = tuple(row.get("id") for row in rows)
            if any(not isinstance(i, int) for i in ids):
                raise APIError("Страница содержит запись без целочисленного ID")
            if len(ids) > 1:
                if all(a < b for a, b in zip(ids, ids[1:])):
                    page_direction = 1
                elif all(a > b for a, b in zip(ids, ids[1:])):
                    page_direction = -1
                else:
                    raise APIError("ID на странице GraphQL не упорядочены")
                if direction is not None and page_direction != direction:
                    raise APIError("Направление пагинации GraphQL изменилось")
                direction = page_direction
            if ids in seen_pages:
                raise APIError("Повторяющаяся страница GraphQL")
            seen_pages.add(ids)
            last_id = page_info.get("lastId") if isinstance(page_info.get("lastId"), int) else ids[-1]
            if after is not None and (last_id == after or
                                      (direction == 1 and last_id < after) or
                                      (direction == -1 and last_id > after)):
                raise APIError("Курсор GraphQL не продвинулся")
            if last_id in seen_cursors:
                raise APIError("Циклическая пагинация GraphQL")
            seen_cursors.add(last_id)
            yield rows, last_id
            if page_info.get("hasNextPage") is False:
                return
            # A short page is not proof of completion when the API filters after indexing.
            after = last_id

