"""T3 — Secret safety canaries.

scan_for_secrets must catch the dangerous categories and must NEVER return
the raw secret value — only the category label, which is all the audit log
is allowed to carry.
"""

from __future__ import annotations

from jarvis.security.memory_safety import scan_for_secrets


def _categories(text):
    return [hit.category for hit in scan_for_secrets(text)]


def test_openai_key_detected():
    cats = _categories("minha chave sk-abcdefghijklmnopqrstuvwx é essa")
    assert "openai_api_key" in cats


def test_github_token_detected():
    cats = _categories("token ghp_" + "a" * 36 + " aqui")
    assert "github_token" in cats


def test_aws_key_detected():
    cats = _categories("AKIAIOSFODNN7EXAMPLE no config")
    assert "aws_access_key" in cats


def test_slack_token_detected():
    cats = _categories("xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx pronto")
    assert "slack_token" in cats


def test_private_key_detected():
    cats = _categories("-----BEGIN PRIVATE KEY-----\nMIIE...")
    assert "private_key" in cats


def test_bearer_token_detected():
    token = "eyJhbGciOiJIUzI1NiJ9." + "a" * 40 + "." + "b" * 40
    cats = _categories(f"Authorization: Bearer {token}")
    assert "bearer_token" in cats


def test_credential_assignment_detected():
    for text in (
        "password = hunter2secret",
        "api_key: abcdefgh12345678",
        'secret="mysupersecretvalue"',
    ):
        assert "credential_assignment" in _categories(text), text


def test_clean_text_has_no_hits():
    assert scan_for_secrets("Lembre que gosto de café sem açúcar.") == []
    assert scan_for_secrets("A reunião é amanhã às 10h.") == []


def test_hits_never_carry_raw_values():
    secret = "sk-abcdefghijklmnopqrstuvwx"  # noqa: S105 - synthetic test vector, not a credential
    hits = scan_for_secrets(f"chave {secret} aqui")
    assert hits, "expected a hit"
    for hit in hits:
        assert secret not in hit.model_dump_json()
        assert secret not in repr(hit)
    # Only the category label is exposed.
    assert hits[0].category == "openai_api_key"


def test_multiple_categories_all_reported():
    text = "AKIAIOSFODNN7EXAMPLE e password = hunter2secret"
    cats = _categories(text)
    assert "aws_access_key" in cats
    assert "credential_assignment" in cats
