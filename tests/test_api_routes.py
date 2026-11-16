"""Tests for api/main.py through a real HTTP client. Needs fastapi and httpx installed."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from api import main, service  # noqa: E402
from poller import db  # noqa: E402

PAIR = {"id": "fed", "name": "Fed hikes 25 bps in December", "category": "economics",
        "resolution_date": "2026-12-09", "active": True}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "site.db")
    monkeypatch.setattr(service, "load_pairs", lambda active_only=True: [PAIR])
    return TestClient(main.app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["last_book_at"] is None


def test_pairs_and_unknown_pair(client):
    assert client.get("/api/pairs").json()[0]["status"] == "no_data"
    assert client.get("/api/pairs/fed").status_code == 200
    assert client.get("/api/pairs/nope").status_code == 404


def test_log_and_results(client):
    assert client.get("/api/opportunities").json() == []
    assert client.get("/api/results").json()["gaps_before_fees"] == 0


def test_website_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "app.js" in r.text
