from dataclasses import replace

import pytest

from goszakup.api import APIError, AuthError, Client
from goszakup.config import Config


@pytest.fixture
def client(tmp_path):
    config = Config("secret", "https://ows.goszakup.gov.kz/v3/graphql", "Asia/Almaty",
                    "active_now", 2, 2, 1, 0, 1, tmp_path)
    return Client(config)


class Response:
    def __init__(self, status, value):
        self.status_code = status
        self.value = value
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError
            raise HTTPError()

    def json(self):
        return self.value


def test_auth_and_no_token_in_error(monkeypatch, client):
    monkeypatch.setattr("requests.request", lambda *args, **kwargs: Response(401, {}))
    with pytest.raises(AuthError) as exc:
        client.graphql("query { __typename }", {})
    assert "secret" not in str(exc.value)


def test_partial_graphql_error_rejected(monkeypatch, client):
    monkeypatch.setattr("requests.request", lambda *args, **kwargs:
                        Response(200, {"data": {"Lots": [{"id": 1}]}, "errors": [{"message": "bad"}]}))
    with pytest.raises(APIError, match="частичный ответ"):
        client.graphql("query { __typename }", {})


def test_429_retried_with_bearer(monkeypatch, client):
    client.config = replace(client.config, max_retries=1)
    statuses = iter([Response(429, {}), Response(200, {"data": {"__typename": "Query"}})])
    headers = []

    def request(*args, **kwargs):
        headers.append(kwargs["headers"])
        return next(statuses)

    monkeypatch.setattr("requests.request", request)
    monkeypatch.setattr("goszakup.api.time.sleep", lambda _: None)
    assert client.graphql("query { __typename }", {})["data"]["__typename"] == "Query"
    assert client.request_count == 2
    assert all(value["Authorization"] == "Bearer secret" for value in headers)


def test_pagination_and_cycle(client):
    pages = iter([{"data": {"Lots": [{"id": 1}, {"id": 2}]}},
                  {"data": {"Lots": [{"id": 3}]}},
                  {"data": {"Lots": []}}])
    client.graphql = lambda *_: next(pages)
    assert [cursor for _, cursor in client.pages("query", "Lots", {}, 2)] == [2, 3]
    client.graphql = lambda *_: {"data": {"Lots": [{"id": 1}]}}
    with pytest.raises(APIError, match="страница|курсор"):
        list(client.pages("query", "Lots", {}, 2))


def test_pagination_uses_server_cursor(client):
    seen = []

    def graphql(_, variables):
        seen.append(variables["after"])
        if variables["after"] is None:
            return {"data": {"Lots": [{"id": 7}]},
                    "extensions": {"pageInfo": {"lastId": 9, "hasNextPage": True}}}
        return {"data": {"Lots": [{"id": 10}]},
                "extensions": {"pageInfo": {"lastId": 10, "hasNextPage": False}}}

    client.graphql = graphql
    assert [cursor for _, cursor in client.pages("query", "Lots", {}, 2)] == [9, 10]
    assert seen == [None, 9]


def test_null_with_zero_count_is_empty_but_unexpected_null_fails(client):
    client.graphql = lambda *_: {"data": {"TrdBuy": None},
                                 "extensions": {"pageInfo": {"totalCount": 0, "hasNextPage": False}}}
    assert list(client.pages("query", "TrdBuy", {}, 2)) == []
    client.graphql = lambda *_: {"data": {"TrdBuy": None},
                                 "extensions": {"pageInfo": {"totalCount": 1, "hasNextPage": False}}}
    with pytest.raises(APIError, match="не является списком"):
        list(client.pages("query", "TrdBuy", {}, 2))


def test_descending_id_cursor_can_resume(client):
    seen = []

    def graphql(_, variables):
        seen.append(variables["after"])
        if variables["after"] == 9:
            return {"data": {"TrdBuy": [{"id": 8}, {"id": 7}]},
                    "extensions": {"pageInfo": {"lastId": 7, "hasNextPage": True}}}
        return {"data": {"TrdBuy": [{"id": 6}]},
                "extensions": {"pageInfo": {"lastId": 6, "hasNextPage": False}}}

    client.graphql = graphql
    assert [cursor for _, cursor in client.pages("query", "TrdBuy", {}, 2, start_after=9)] == [7, 6]
    assert seen == [9, 7]

