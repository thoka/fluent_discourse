from conf_tests import BASE_URL, client, USERNAME, API_KEY
from fluent_discourse import *
import pytest
import json
import requests
import time


def test_accumulate_strings(client):
    endpoint = client.test.a.path
    assert endpoint._cache == ["test", "a", "path"]


def test_accumulate_integers(client):
    endpoint = client.test.a.path[5]
    assert endpoint._cache == ["test", "a", "path", "5"]


def test_make_url(client):
    client._cache = ["this", "is", "a", "test", ".json"]
    assert client._make_url() == f"{BASE_URL}/this/is/a/test.json"


def test_format_base_url():
    # add trailing slash to BASE URL
    url = BASE_URL + "/"
    client = Discourse(url, USERNAME, API_KEY)
    assert client._base_url == BASE_URL


def test_from_env():
    client = Discourse.from_env()


def test_set_raise_for_rate_limit():
    client = Discourse.from_env(raise_for_rate_limit=False)
    assert client._raise_for_rate_limit == False


class MockResponse:
    status_code = 404
    url = BASE_URL
    text = '{"test":"Hello World", "extras": {"wait_seconds": "3"}}'

    @classmethod
    def json(cls):
        return json.loads(cls.text)


def test_page_not_found_error(client):
    with pytest.raises(PageNotFoundError):
        client._handle_error(MockResponse, "GET", None, None, None)


def test_unauthorized_error(client):
    MockResponse.status_code = 403
    with pytest.raises(UnauthorizedError):
        client._handle_error(MockResponse, "GET", None, None, None)


def test_rate_limit_error(client):
    MockResponse.status_code = 429
    with pytest.raises(RateLimitError):
        client._handle_error(MockResponse, "GET", None, None, None)


def test_unhandled_error(client):
    MockResponse.status_code = 500
    with pytest.raises(DiscourseError):
        client._handle_error(MockResponse, "GET", None, None, None)


def test_wait_for_rate_limit():
    MockResponse.status_code = 429
    client = Discourse.from_env(raise_for_rate_limit=False)
    client._wait_for_rate_limit(MockResponse, "GET", None, None, None)


def test_retryable_error_retries_then_succeeds(client, monkeypatch):
    MockResponse.status_code = 503
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    class SuccessResponse:
        status_code = 200
        text = '{"ok": true}'

        @classmethod
        def json(cls):
            return {"ok": True}

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            return MockResponse
        return SuccessResponse

    monkeypatch.setattr(requests, "request", fake_request)

    result = client._request("GET", BASE_URL)

    assert result == {"ok": True}
    assert calls["n"] == 3
    assert sleeps == [1, 2]


def test_retryable_error_exhausts_retries(client, monkeypatch):
    MockResponse.status_code = 503
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(
        requests, "request", lambda *a, **kw: MockResponse
    )

    with pytest.raises(DiscourseError):
        client._request("GET", BASE_URL)
