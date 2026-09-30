"""Integration tests: F3.1 web shell.

GET / serves the static JARVIS page; the page queries GET /health live
(same origin). The page must carry no secrets, no keys, and no business
logic. Driven over httpx ASGITransport.
"""

from __future__ import annotations

import re

import httpx
import pytest

from jarvis.api.main import create_app
from jarvis.config import Settings

# NOTE: the `db_url` and `stub_nexus` fixtures are provided by tests/conftest.py.


@pytest.fixture()
def api_settings(db_url, stub_nexus):
    return Settings(
        database_url=db_url,
        nexus_base_url=stub_nexus.base_url,
        llm_provider="fake",
        log_level="WARNING",
        log_format="text",
    )


@pytest.fixture()
def client(api_settings):
    transport = httpx.ASGITransport(app=create_app(api_settings))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_root_returns_html_200(client):
    # T1 — GET / returns a valid HTML document.
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text.lower()
    assert "<!doctype html>" in body and "</html>" in body


async def test_root_identifies_jarvis(client):
    # T2 — the page identifies JARVIS.
    r = await client.get("/")
    assert r.status_code == 200
    assert "JARVIS" in r.text
    assert "Local Intelligence" in r.text


async def test_page_queries_health_live(client):
    # T3a — the page performs a real fetch of /health (relative, same-origin),
    # it does not hardcode "Online".
    r = await client.get("/")
    assert r.status_code == 200
    assert 'fetch("/health"' in r.text
    # F3.2: the health indicator moved into the header; it still starts in a
    # "checking" state before the live fetch resolves.
    assert "checking" in r.text.lower()
    # "Online" is only ever rendered after the live fetch returns
    # {"status": "ok"} — never hardcoded by the server.
    assert 'data.status === "ok"' in r.text


async def test_health_contract_unchanged(client):
    # T3b — GET /health keeps its real contract.
    r = await client.get("/health")
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "ok" and "version" in payload


async def test_single_file_no_external_assets(client):
    # T4 — the page is self-contained: no external stylesheets, scripts or
    # resources that would need separate serving.
    r = await client.get("/")
    body = r.text
    assert not re.search(r"<script\s+[^>]*src=", body)
    assert not re.search(r'<link\s+[^>]*rel=["\']stylesheet["\']', body)
    assert "http://" not in body and "https://" not in body


async def test_no_secrets_or_keys_in_ui(client):
    # T5 — the UI exposes no configuration secrets: no API keys, no tokens,
    # no provider/bank references at all.
    r = await client.get("/")
    body = r.text.lower()
    for token in ("api_key", "apikey", "sk-", "bearer", "password", "secret"):
        assert token not in body
