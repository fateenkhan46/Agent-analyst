"""Sign-up / sign-in."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request, status

from ..config import get_settings
from ..schemas.agent import AuditAction
from ..services import repository as repo
from ..services.auth import (
    Principal, current_principal, hash_password, issue_token, verify_password,
)
from .deps import Depends, Session, get_session

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(
    request: Request,
    payload: dict = Body(...),
    s: Session = Depends(get_session),
) -> dict[str, object]:
    """Create an account.

    Closed by default. On a corporate deployment accounts come from the
    directory -- either provisioned on first sign-in through the
    authenticating proxy, or created by an administrator here. Leaving this
    open would let anyone who can reach the API grant themselves a tenant.
    """
    settings = get_settings()
    email_attempted = str(payload.get("email", "")).strip().lower()
    if not settings.allow_open_registration:
        # An administrator may still create accounts; anyone else is refused
        # without being told whether registration merely needs a token.
        try:
            principal = current_principal(request, s)
            is_admin = principal.role in {"owner", "admin"} and not principal.is_dev
        except HTTPException:
            is_admin = False
        if not is_admin:
            repo.record_denial(
                s, actor="anonymous", action=AuditAction.AUTH_DENIED,
                summary="Registration attempted while self-registration is closed.",
                detail={"email": email_attempted},
            )
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Self-registration is disabled on this deployment. Accounts are "
                "provisioned through your organisation's identity provider, or by "
                "an administrator.",
            )

    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    if "@" not in email or len(email) < 5:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A valid email address is required.")
    if len(password) < 10:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "The password must be at least 10 characters."
        )
    if repo.get_user_by_email(s, email):
        # Do not confirm whether an address is already registered.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That account could not be created. If you already have one, sign in instead.",
        )
    row = repo.create_user(
        s, email=email, display_name=str(payload.get("display_name", "")),
        password_hash=hash_password(password),
        tenant_id=str(payload.get("tenant_id", "") or "default"),
    )
    repo.record_audit(s, actor=row.id, action=AuditAction.AUTH_LOGIN, summary="Account created.")
    return {"token": issue_token(row.id, row.tenant_id), "user": {
        "id": row.id, "email": row.email, "display_name": row.display_name,
    }}


@router.post("/login")
def login(payload: dict = Body(...), s: Session = Depends(get_session)) -> dict[str, object]:
    email = str(payload.get("email", "")).strip().lower()
    row = repo.get_user_by_email(s, email)
    if row is None or not verify_password(str(payload.get("password", "")), row.password_hash):
        repo.record_denial(
            s, actor=email or "unknown", action=AuditAction.AUTH_DENIED,
            summary="Failed sign-in attempt.",
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password.")
    repo.record_audit(s, actor=row.id, action=AuditAction.AUTH_LOGIN, summary="Signed in.")
    return {"token": issue_token(row.id, row.tenant_id), "user": {
        "id": row.id, "email": row.email, "display_name": row.display_name,
    }}


@router.get("/me")
def me(principal: Principal = Depends(current_principal)) -> dict[str, object]:
    return {
        "user_id": principal.user_id, "email": principal.email,
        "tenant_id": principal.tenant_id, "role": principal.role,
        "development_mode": principal.is_dev,
        "note": "Unauthenticated requests are attributed to a local development user. "
                "Set AUTH_SECRET and ENVIRONMENT=production to require real sign-in."
        if principal.is_dev else "",
    }
