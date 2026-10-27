"""Authentication and project-level authorisation.

Tokens are HMAC-signed compact payloads -- no external dependency, no JWT
library, and nothing to misconfigure. Passwords use PBKDF2-HMAC-SHA256 with a
per-user salt.

Development mode: when ``AUTH_SECRET`` is unset we generate a random secret at
boot and auto-provision a single ``dev@localhost`` user, so the app is usable
immediately. The secret being random per boot means dev tokens do not survive
a restart, which is the correct trade-off -- a stable default secret in source
would be a real vulnerability.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..schemas.agent import AuditAction
from ..schemas.common import Severity
from . import repository as repo

PBKDF2_ROUNDS = 310_000
DEV_EMAIL = "dev@localhost"

_runtime_secret: str | None = None


def _secret() -> str:
    global _runtime_secret
    configured = get_settings().auth_secret or os.getenv("AUTH_SECRET", "")
    if configured:
        return configured
    if _runtime_secret is None:
        _runtime_secret = secrets.token_urlsafe(48)
    return _runtime_secret


def hash_password(password: str, *, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, rounds, salt, digest = encoded.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds))
    return hmac.compare_digest(dk.hex(), digest)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(user_id: str, tenant_id: str = "default") -> str:
    settings = get_settings()
    body = json.dumps(
        {"sub": user_id, "tid": tenant_id, "exp": int(time.time()) + settings.auth_token_ttl_seconds},
        separators=(",", ":"),
    ).encode()
    sig = hmac.new(_secret().encode(), body, hashlib.sha256).digest()
    return f"{_b64(body)}.{_b64(sig)}"


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    email: str = ""
    role: str = "member"
    is_dev: bool = False


def decode_token(token: str) -> dict[str, object]:
    try:
        body_b64, sig_b64 = token.split(".", 1)
        body, sig = _unb64(body_b64), _unb64(sig_b64)
    except (ValueError, Exception):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid authentication token.")
    expected = hmac.new(_secret().encode(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid authentication token.")
    claims = json.loads(body)
    if int(claims.get("exp", 0)) < time.time():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired; sign in again.")
    return claims


def ensure_dev_user(s: Session) -> repo.UserRow:
    row = repo.get_user_by_email(s, DEV_EMAIL)
    if row is None:
        row = repo.create_user(
            s, email=DEV_EMAIL, display_name="Developer",
            password_hash=hash_password("dev"), role="owner", user_id="usr_dev",
        )
    return row


def current_principal(
    request: Request, s: Session = Depends(get_session)
) -> Principal:
    """Resolve the caller. In development mode, an unauthenticated request is
    attributed to the dev user rather than refused, so the app is usable out
    of the box; in any other environment a token is mandatory."""
    settings = get_settings()
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        token = request.cookies.get("aa_token", "")

    if not token:
        if settings.allow_dev_auth and settings.environment == "development":
            row = ensure_dev_user(s)
            return Principal(row.id, row.tenant_id, row.email, row.role, is_dev=True)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required.")

    claims = decode_token(token)
    user_id = str(claims.get("sub", ""))
    row = repo.get_user(s, user_id)
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown account.")
    return Principal(row.id, row.tenant_id, row.email, row.role)


def authorise_project(s: Session, principal: Principal, project_id: str):
    """Fetch a project the principal is allowed to see, or raise.

    A project belonging to another tenant returns **404, not 403**, so the API
    does not confirm the existence of resources outside the caller's tenant.
    """
    project = repo.get_project(s, project_id)
    if project is None or project.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
    if project.owner_id != principal.user_id and principal.role not in {"owner", "admin"}:
        repo.record_audit(
            s, project_id=project_id, actor=principal.user_id,
            action=AuditAction.AUTH_DENIED, severity=Severity.MEDIUM,
            summary="Denied access to a project owned by another user.",
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
    return project
