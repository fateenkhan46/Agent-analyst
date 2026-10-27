"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import init_db

log = logging.getLogger("agent_analyst")


class InsecureConfiguration(RuntimeError):
    """Raised at boot when a production deployment is configured unsafely."""


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    settings = get_settings()

    # Audit the settings before serving a single request. A deployment that is
    # quietly open would otherwise look perfectly healthy.
    from .startup_checks import check

    report = check(settings)
    for warning in report.warnings:
        log.warning("startup: [%s] %s -- %s", warning.setting, warning.problem, warning.fix)
    if not report.ok:
        log.error("startup refused:\n%s", report.as_text())
        raise InsecureConfiguration(report.as_text())

    init_db()
    log.info(
        "Agent Analyst ready: db=%s storage=%s llm=%s",
        "postgresql" if settings.is_postgres else "sqlite",
        settings.storage_backend,
        "anthropic" if settings.llm_configured else "heuristic (development mode)",
    )
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        description=(
            "An agent workforce that turns inconsistent business data into a validated, "
            "editable Power BI project. GET /api/v1/system/capabilities reports exactly "
            "which integrations are live in this deployment."
        ),
        version="0.6.0",
        lifespan=lifespan,
    )
    # Middleware runs outermost-last, so these are added in reverse order of
    # how a request meets them: headers wrap everything, then the rate limit
    # rejects before CORS does any work.
    from .middleware import RateLimitMiddleware, SecurityHeadersMiddleware

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

    from .api import (
        artifacts, auth, dashboard, deliverables, orchestration, projects, system,
    )
    for module in (system, auth, dashboard, projects, orchestration, artifacts, deliverables):
        app.include_router(module.router, prefix=settings.api_prefix)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Never leak internals to a client. The detail goes to the log and the
        audit trail; the caller gets a correlation id."""
        import uuid

        ref = uuid.uuid4().hex[:12]
        log.exception("unhandled error ref=%s path=%s", ref, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "An internal error occurred. Quote this reference if you report it.",
                "reference": ref,
            },
        )

    @app.get("/")
    def root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "docs": "/docs",
            "capabilities": f"{settings.api_prefix}/system/capabilities",
        }

    return app


app = create_app()
