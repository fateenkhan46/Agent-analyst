"""The checks that matter once this is reachable by a company rather than by
one developer on a laptop.

Each test here corresponds to a way a deployment can look healthy while being
open: it serves traffic, returns 200s, and anyone can use it.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.startup_checks import check


# --- the boot guard ------------------------------------------------------
def test_production_refuses_the_development_defaults() -> None:
    report = check(Settings(environment="production"))
    assert not report.ok
    blocked = {f.setting for f in report.blocking}
    assert {"AUTH_SECRET", "DATABASE_URL", "CORS_ORIGINS", "ALLOW_DEV_AUTH"} <= blocked


def test_a_correctly_configured_production_deployment_starts() -> None:
    report = check(Settings(
        environment="production",
        auth_secret="x" * 48,
        allow_dev_auth=False,
        allow_open_registration=False,
        database_url="postgresql+psycopg://u:p@db:5432/agent_analyst",
        cors_origins="https://agent-analyst.internal.example.com",
        hsts_seconds=31_536_000,
    ))
    assert report.ok, report.as_text()
    assert not report.warnings, report.as_text()


def test_development_is_never_blocked() -> None:
    """The defaults must stay usable on a laptop with no configuration."""
    assert check(Settings(environment="development")).ok


def test_a_short_signing_secret_is_rejected() -> None:
    report = check(Settings(
        environment="production", auth_secret="tooshort", allow_dev_auth=False,
        database_url="postgresql+psycopg://u:p@db/x",
        cors_origins="https://x.example.com",
    ))
    assert "AUTH_SECRET" in {f.setting for f in report.blocking}


def test_an_llm_key_in_production_is_surfaced_not_blocked() -> None:
    """Sending business data to a third party is a policy decision, not a bug:
    it is reported so it appears in the deployment log, and it does not stop
    the app."""
    report = check(Settings(
        environment="production", auth_secret="x" * 48, allow_dev_auth=False,
        database_url="postgresql+psycopg://u:p@db/x",
        cors_origins="https://x.example.com", hsts_seconds=31_536_000,
        anthropic_api_key="sk-ant-not-a-real-key",
    ))
    assert report.ok
    assert "ANTHROPIC_API_KEY" in {f.setting for f in report.warnings}


def test_the_boot_guard_actually_stops_the_app(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import InsecureConfiguration, create_app

    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    try:
        with pytest.raises(InsecureConfiguration):
            with TestClient(create_app()):
                pass
    finally:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        get_settings.cache_clear()


# --- registration --------------------------------------------------------
def test_self_registration_is_closed_by_default(client) -> None:
    r = client.post("/api/v1/auth/register",
                    json={"email": "outsider@example.com", "password": "a-long-password"})
    assert r.status_code == 403
    assert "identity provider" in r.json()["detail"]


def test_a_closed_registration_attempt_is_audited(client, session) -> None:
    client.post("/api/v1/auth/register",
                json={"email": "outsider@example.com", "password": "a-long-password"})
    from app.services import repository as repo

    actions = [e.action.value for e in repo.list_audit(session, limit=50)]
    assert "auth_denied" in actions


def test_registration_works_when_deliberately_opened(client, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("ALLOW_OPEN_REGISTRATION", "true")
    get_settings.cache_clear()
    try:
        r = client.post("/api/v1/auth/register",
                        json={"email": "new@example.com", "password": "a-long-password"})
        assert r.status_code == 201, r.text
        assert r.json()["token"]
    finally:
        monkeypatch.delenv("ALLOW_OPEN_REGISTRATION", raising=False)
        get_settings.cache_clear()


# --- security headers ----------------------------------------------------
def test_every_response_carries_the_security_headers(client) -> None:
    h = client.get("/api/v1/system/health").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert "camera=()" in h["Permissions-Policy"]


def test_hsts_is_absent_until_tls_is_configured(client) -> None:
    """Sending HSTS before TLS is terminated locks browsers out of the app."""
    assert "Strict-Transport-Security" not in client.get("/api/v1/system/health").headers


# --- proxy SSO -----------------------------------------------------------
def test_proxy_headers_are_ignored_unless_explicitly_trusted(client) -> None:
    """The default must be to ignore them: otherwise anyone reaching the app
    directly can claim to be anyone."""
    r = client.get("/api/v1/auth/me",
                   headers={"x-auth-request-email": "ceo@example.com"})
    assert r.status_code == 200
    assert r.json()["email"] != "ceo@example.com"


def test_a_trusted_proxy_provisions_and_identifies_the_user(client, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("TRUST_PROXY_AUTH", "true")
    monkeypatch.setenv("PROXY_ADMIN_GROUPS", "bi-platform-admins")
    get_settings.cache_clear()
    try:
        r = client.get("/api/v1/auth/me", headers={
            "x-auth-request-email": "Dana.Okoro@example.com",
            "x-auth-request-user": "Dana Okoro",
            "x-auth-request-groups": "all-staff,bi-platform-admins",
        })
        assert r.status_code == 200, r.text
        assert r.json()["email"] == "dana.okoro@example.com"
        assert r.json()["role"] == "admin", "directory group should map to the admin role"

        plain = client.get("/api/v1/auth/me", headers={
            "x-auth-request-email": "sam@example.com",
            "x-auth-request-groups": "all-staff",
        })
        assert plain.json()["role"] == "member"
    finally:
        monkeypatch.delenv("TRUST_PROXY_AUTH", raising=False)
        monkeypatch.delenv("PROXY_ADMIN_GROUPS", raising=False)
        get_settings.cache_clear()


def test_a_proxy_provisioned_account_cannot_sign_in_with_a_password(client, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("TRUST_PROXY_AUTH", "true")
    get_settings.cache_clear()
    try:
        client.get("/api/v1/auth/me", headers={"x-auth-request-email": "sso@example.com"})
    finally:
        monkeypatch.delenv("TRUST_PROXY_AUTH", raising=False)
        get_settings.cache_clear()

    for attempt in ("", "!", "password"):
        r = client.post("/api/v1/auth/login",
                        json={"email": "sso@example.com", "password": attempt})
        assert r.status_code == 401, f"{attempt!r} must not authenticate an SSO account"


# --- rate limiting -------------------------------------------------------
def test_sign_in_attempts_are_rate_limited(client, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("RATE_LIMIT_AUTH_PER_MINUTE", "5")
    get_settings.cache_clear()
    try:
        codes = [
            client.post("/api/v1/auth/login",
                        json={"email": "a@b.com", "password": f"guess-{i}"}).status_code
            for i in range(9)
        ]
        assert 429 in codes, f"no request was throttled: {codes}"
        assert codes.index(429) >= 5, "throttled before the configured limit"
    finally:
        monkeypatch.delenv("RATE_LIMIT_AUTH_PER_MINUTE", raising=False)
        get_settings.cache_clear()


def test_a_throttled_response_says_when_to_retry(client, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("RATE_LIMIT_AUTH_PER_MINUTE", "2")
    get_settings.cache_clear()
    try:
        last = None
        for i in range(5):
            last = client.post("/api/v1/auth/login",
                               json={"email": "a@b.com", "password": f"g{i}"})
        assert last is not None and last.status_code == 429
        assert int(last.headers["Retry-After"]) >= 1
    finally:
        monkeypatch.delenv("RATE_LIMIT_AUTH_PER_MINUTE", raising=False)
        get_settings.cache_clear()


# --- the audit trail must survive the rollback ---------------------------
def test_a_failed_sign_in_is_recorded(client, session) -> None:
    """A denial rolls back the request transaction, so an audit row written
    inside it is discarded. Every refused access used to vanish this way."""
    from app.services import repository as repo

    client.post("/api/v1/auth/login",
                json={"email": "nobody@example.com", "password": "wrong"})
    events = repo.list_audit(session, limit=50)
    denied = [e for e in events if e.action.value == "auth_denied"]
    assert denied, "a failed sign-in left no audit trail"
    assert any("nobody@example.com" in e.actor for e in denied)


def test_a_cross_tenant_project_access_is_recorded(client, session, project_id) -> None:
    from app.config import get_settings
    from app.services import repository as repo
    from app.services.auth import hash_password

    other = repo.create_user(
        session, email="other@example.com", display_name="Other",
        password_hash=hash_password("a-long-password-here"), tenant_id="default",
    )
    session.commit()
    token = client.post("/api/v1/auth/login",
                        json={"email": "other@example.com",
                              "password": "a-long-password-here"}).json()["token"]

    r = client.get(f"/api/v1/projects/{project_id}",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404, "another user's project must look absent, not forbidden"

    denied = [e for e in repo.list_audit(session, limit=80)
              if e.action.value == "auth_denied" and e.actor == other.id]
    assert denied, "a cross-tenant access attempt left no audit trail"
