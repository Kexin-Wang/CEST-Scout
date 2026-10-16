"""NCBI answers 429 when a run's own request bursts exceed 3/s without an API key; the skill
must wait and retry rather than hand the model an error to work around."""
from __future__ import annotations

from types import SimpleNamespace

from cest_scout.skills import pubmed


def test_esearch_retries_after_a_429(monkeypatch):
    calls = []
    statuses = iter([429, 200])

    def fake_get(url, params=None, timeout=None):
        status = next(statuses)
        calls.append(status)
        body = b"<eSearchResult><IdList><Id>123</Id></IdList></eSearchResult>"
        return SimpleNamespace(status_code=status, content=body, raise_for_status=lambda: None)

    sleeps = []
    monkeypatch.setattr(pubmed.requests, "get", fake_get)
    monkeypatch.setattr(pubmed.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(pubmed, "load_settings", lambda: SimpleNamespace(ncbi_api_key=None, ncbi_email="x@y.z"))

    assert pubmed._esearch("liver glycogen", 5) == ["123"]
    assert calls == [429, 200]
    assert sleeps[0] == 1.0


def test_get_gives_up_after_four_attempts(monkeypatch):
    import pytest
    import requests

    def fake_get(url, params=None, timeout=None):
        def boom():
            raise requests.HTTPError("429")
        return SimpleNamespace(status_code=429, content=b"", raise_for_status=boom)

    monkeypatch.setattr(pubmed.requests, "get", fake_get)
    monkeypatch.setattr(pubmed.time, "sleep", lambda s: None)
    with pytest.raises(requests.HTTPError):
        pubmed._get("http://x", {}, timeout=1)
