import copy
import json
from types import SimpleNamespace

import pytest
import requests

from pairs import load_pairs
from poller.rule_checker import (
    OpenAIRuleReviewer, collect_rule_sources, review_pair, validate_review,
)


@pytest.fixture
def pair():
    return copy.deepcopy(load_pairs(active_only=False)[0])


@pytest.fixture
def sources():
    return [
        {"id": "kalshi_market", "exchange": "kalshi", "url": "https://example.test/market",
         "text": "Cancellation pays half. Delays may extend two years."},
        {"id": "kalshi_terms", "exchange": "kalshi", "url": "https://example.test/terms",
         "text": "Cancellation pays half."},
        {"id": "polymarket_market", "exchange": "polymarket", "url": "https://example.test/poly",
         "text": "Cancellation resolves No."},
    ]


def different():
    return {"verdict": "different", "confidence": 0.94, "summary": "Cancellation payouts differ.",
            "differences": [{"category": "cancellation", "description": "Half payout versus No.",
                             "scenario": "The event is canceled.",
                             "kalshi": {"source_id": "kalshi_terms", "quote": "Cancellation pays half."},
                             "polymarket": {"source_id": "polymarket_market", "quote": "Cancellation resolves No."}}],
            "uncertainties": []}


def response(body):
    return SimpleNamespace(status_code=200, json=lambda: body, raise_for_status=lambda: None)


def test_openai_payload_and_citations(pair, sources):
    def post(url, **kwargs):
        assert url == "https://api.openai.com/v1/responses"
        payload = kwargs["json"]
        assert payload["store"] is False
        assert payload["text"]["format"]["strict"] is True
        assert "rule_notes" not in json.loads(payload["input"])["pair"]
        return response({"status": "completed", "output": [{"content": [
            {"type": "output_text", "text": json.dumps(different())}]}]})
    reviewer = OpenAIRuleReviewer("test-key", "test-model", post)
    before = copy.deepcopy(pair)
    result = review_pair(pair, reviewer, lambda p: (sources, []))
    assert result["verdict"] == "different"
    assert result["status"] == "completed"
    assert result["admin_status"] == "pending"
    assert pair == before
    assert "rules_verified" not in result


@pytest.mark.parametrize("change", [
    lambda r: r.update(confidence=1.1),
    lambda r: r.update(confidence=float("nan")),
    lambda r: r.update(confidence=True),
    lambda r: r.update(verdict="equivalent"),
    lambda r: r.update(differences=[]),
    lambda r: r["differences"][0]["kalshi"].update(quote="Invented rule"),
    lambda r: r["differences"][0]["kalshi"].update(source_id="polymarket_market"),
    lambda r: r.update(rules_verified=True),
])
def test_rejects_unsupported_findings(sources, change):
    result = different()
    change(result)
    with pytest.raises(ValueError):
        validate_review(result, sources)


@pytest.mark.parametrize("body", [
    {"status": "incomplete", "output": []},
    {"status": "completed", "output": [{"content": [{"type": "refusal"}]}]},
    {"status": "completed", "output": [{"content": [{"type": "output_text", "text": "invalid"}]}]},
])
def test_model_failures_return_uncertain(pair, sources, body):
    reviewer = OpenAIRuleReviewer("test-key", "test-model", lambda *a, **kw: response(body))
    result = review_pair(pair, reviewer, lambda p: (sources, []))
    assert result["verdict"] == "uncertain"
    assert result["confidence"] == 0
    assert result["status"] == "failed"
    assert result["admin_status"] == "pending"


def test_missing_sources_skip_model(pair):
    def forbidden(*args):
        pytest.fail("Model called without rules")
    forbidden.model = "test-model"
    result = review_pair(pair, forbidden, lambda p: ([], ["Missing settlement rules"]))
    assert result["uncertainties"] == ["Missing settlement rules"]


def test_collection_extracts_terms_and_checks_identity(pair, monkeypatch):
    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", lambda stream: SimpleNamespace(
        pages=[SimpleNamespace(extract_text=lambda: "Full cancellation terms")]))
    def get(url, **kwargs):
        if "/markets/" in url:
            return response({"market": {"ticker": pair["kalshi_ticker"], "rules_primary": "Primary rules"}})
        if "/series/" in url:
            return response({"series": {"ticker": pair["kalshi_ticker"].split("-")[0],
                                        "contract_terms_url": "https://assets.kalshi.com/terms.pdf"}})
        if url.endswith(".pdf"):
            assert kwargs["allow_redirects"] is False
            result = response(None)
            result.content = b"mock-pdf"
            return result
        return response([{"slug": pair["poly_slug"], "description": "Polymarket settlement rules",
                          "clobTokenIds": json.dumps([pair["poly_yes_token"], pair["poly_no_token"]])}])
    sources, errors = collect_rule_sources(pair, get)
    assert not errors
    assert len(sources) == 3
    assert all(len(s["sha256"]) == 64 for s in sources)
    wrong = dict(pair, poly_no_token="wrong")
    _, errors = collect_rule_sources(wrong, get)
    assert errors


def test_network_failure_does_not_leak_credentials(pair):
    def get(*args, **kwargs):
        raise requests.Timeout("sensitive error body")
    sources, errors = collect_rule_sources(pair, get)
    assert not sources and len(errors) == 2
    assert "sensitive" not in str(errors)


def test_cli_covers_inactive_pairs_and_continues_after_failure(tmp_path, monkeypatch):
    from scripts import check_rules
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    tracked = load_pairs(active_only=False)
    assert any(not p["active"] for p in tracked)
    calls = []
    def review(pair, reviewer):
        calls.append(pair["id"])
        return {"pair_id": pair["id"], "verdict": "uncertain", "confidence": 0,
                "status": "failed" if len(calls) == 1 else "completed"}
    monkeypatch.setattr(check_rules, "review_pair", review)
    out = tmp_path / "reviews.jsonl"
    assert check_rules.main(["--out", str(out)]) == 1
    assert calls == [p["id"] for p in tracked]
    assert len(out.read_text().splitlines()) == len(tracked)


@pytest.mark.parametrize("approval", [None, False, "true", True])
def test_poller_requires_explicit_admin_approval(monkeypatch, approval):
    from poller import run
    pair = {"id": "test", "rules_verified": approval}
    collected = []
    monkeypatch.setattr(run, "load_pairs", lambda: [pair])
    monkeypatch.setattr(run, "COLLECTORS", {"kalshi": lambda p: collected.append(p) or [{}]})
    monkeypatch.setattr(run, "NORMALIZERS", {"kalshi": lambda r: ["normalized"]})
    assert run.poll_once() == (["normalized"] if approval is True else [])
    assert collected == [pair]
