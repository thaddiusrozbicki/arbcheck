"""Tests for the Kalshi poller. requests.get is faked, so these never hit the network."""

import json

import requests

from poller import kalshi
from poller.kalshi import collect_kalshi

PAIR = {"id": "example-pair", "kalshi_ticker": "KXEXAMPLE-26NOV01-A", "active": True}
BOOK = {
    "orderbook_fp": {
        "yes_dollars": [["0.4000", "100.00"], ["0.4100", "80.00"]],
        "no_dollars": [["0.5800", "50.00"], ["0.5900", "120.00"]],
    }
}


class FakeResponse:
    def __init__(self, body, status_code=200, url="https://example.test/orderbook"):
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.status_code = status_code
        self.url = url

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def fake_get(monkeypatch, response, calls=None):
    def get(url, **kwargs):
        if calls is not None:
            calls.append(url)
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(kalshi.requests, "get", get)


def saved_files(tmp_path):
    return list(tmp_path.rglob("*.json"))


def test_saves_raw_and_returns_book(monkeypatch, tmp_path):
    calls = []
    fake_get(monkeypatch, FakeResponse(BOOK), calls)
    records = collect_kalshi(PAIR, data_dir=tmp_path)

    assert calls == [f"{kalshi.KALSHI_API}/markets/KXEXAMPLE-26NOV01-A/orderbook"]
    assert len(records) == 1
    assert records[0]["pair_id"] == "example-pair"
    assert records[0]["book"] == BOOK["orderbook_fp"]

    [path] = saved_files(tmp_path)
    saved = json.loads(path.read_text())
    assert saved["exchange"] == "kalshi"
    assert saved["ticker"] == "KXEXAMPLE-26NOV01-A"
    assert saved["http_status"] == 200
    assert json.loads(saved["raw_body"]) == BOOK
    assert path.parent.name == saved["fetched_at"][:10]


def test_empty_side_is_ok(monkeypatch, tmp_path):
    fake_get(monkeypatch, FakeResponse({"orderbook_fp": {"yes_dollars": [["0.4100", "80.00"]]}}))
    assert len(collect_kalshi(PAIR, data_dir=tmp_path)) == 1


def test_http_error_still_saves_raw(monkeypatch, tmp_path):
    fake_get(monkeypatch, FakeResponse({"error": "not found"}, status_code=404))
    assert collect_kalshi(PAIR, data_dir=tmp_path) == []
    [path] = saved_files(tmp_path)
    assert json.loads(path.read_text())["http_status"] == 404


def test_rate_limited_returns_nothing(monkeypatch, tmp_path):
    fake_get(monkeypatch, FakeResponse("slow down", status_code=429))
    assert collect_kalshi(PAIR, data_dir=tmp_path) == []
    assert len(saved_files(tmp_path)) == 1


def test_unexpected_body_returns_nothing(monkeypatch, tmp_path):
    for body in ("not json", [], {"orderbook": {}}, {"orderbook_fp": {"yes_dollars": "oops"}}):
        fake_get(monkeypatch, FakeResponse(body))
        assert collect_kalshi(PAIR, data_dir=tmp_path) == []


def test_network_error_returns_nothing(monkeypatch, tmp_path):
    fake_get(monkeypatch, requests.ConnectionError("down"))
    assert collect_kalshi(PAIR, data_dir=tmp_path) == []
    assert saved_files(tmp_path) == []


def test_skips_bad_or_paused_pairs(monkeypatch, tmp_path):
    calls = []
    fake_get(monkeypatch, FakeResponse(BOOK), calls)
    for pair in (
        dict(PAIR, active=False),
        dict(PAIR, kalshi_ticker=""),
        dict(PAIR, kalshi_ticker="../markets"),
        {k: v for k, v in PAIR.items() if k != "id"},
        "not a pair",
    ):
        assert collect_kalshi(pair, data_dir=tmp_path) == []
    assert calls == []
