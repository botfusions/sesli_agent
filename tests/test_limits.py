"""Origin kuralları ve günlük limit testleri."""

import asyncio

import pytest

from server.limits import check_daily, evaluate_daily, origin_allowed, session_budget

from tests.conftest import FakeStore, make_agent


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("https://musteri.example.com", True),
        ("https://musteri.example.com/", True),
        ("HTTPS://MUSTERI.EXAMPLE.COM", True),
        ("https://musteri.example.com:443", True),
        ("http://musteri.example.com", False),
        ("https://musteri.example.com:8443", False),
        ("https://kotu.example.com", False),
        ("https://musteri.example.com.kotu.com", False),
        ("http://localhost:3000", False),
        (None, False),
        ("", False),
        ("null", False),
    ],
)
def test_origin_with_list(origin, expected):
    agent = make_agent()
    assert origin_allowed(origin, agent) is expected


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("http://localhost", True),
        ("http://localhost:8090", True),
        ("http://127.0.0.1:5500", True),
        ("https://localhost:8443", True),
        ("http://[::1]:8000", True),
        ("https://example.com", False),
        ("http://localhost.example.com", False),
        (None, False),
    ],
)
def test_origin_empty_list_localhost_only(origin, expected):
    agent = make_agent(allowed_origins=[])
    assert origin_allowed(origin, agent) is expected


def test_evaluate_daily():
    agent = make_agent(limits={"max_daily_sessions": 3, "max_daily_minutes": 10})
    assert evaluate_daily({"sessions": 0, "seconds": 0}, agent) is None
    assert evaluate_daily({"sessions": 2, "seconds": 599}, agent) is None
    assert evaluate_daily({"sessions": 3, "seconds": 0}, agent) == "daily_sessions"
    assert evaluate_daily({"sessions": 1, "seconds": 600}, agent) == "daily_minutes"


def test_session_budget():
    agent = make_agent(limits={"max_session_seconds": 300, "max_daily_minutes": 10})
    assert session_budget({"sessions": 0, "seconds": 0}, agent) == (300.0, "session_time")
    assert session_budget({"sessions": 0, "seconds": 500}, agent) == (100.0, "daily_minutes")


def test_check_daily_uses_store():
    agent = make_agent(limits={"max_daily_sessions": 2})
    store = FakeStore({"sessions": 2, "seconds": 0})
    assert asyncio.run(check_daily(store, agent)) == "daily_sessions"
    store.usage = {"sessions": 0, "seconds": 0}
    assert asyncio.run(check_daily(store, agent)) is None
