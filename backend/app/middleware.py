"""Response headers and request rate limiting.

The rate limiter is in-process, which is the honest limit of it: with several
replicas each holds its own counters, so the effective ceiling is the
configured rate multiplied by the replica count. It is a guard against a
runaway client and accidental hammering, not a defence against a distributed
attack -- that belongs at the gateway or WAF in front of the app, and the
deployment notes say so rather than implying this covers it.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_settings

#: Sign-in and registration get their own, much tighter bucket: these are the
#: endpoints worth guessing against.
_AUTH_PATHS = ("/auth/login", "/auth/register")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers that cost nothing and close off whole classes of attack."""

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        response = await call_next(request)
        settings = get_settings()
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        h.setdefault("Cross-Origin-Resource-Policy", "same-site")
        h.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        # The API serves JSON and file downloads, never HTML that executes, so
        # the strictest possible policy is also the correct one here.
        h.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        # A download must not be rendered inline by a browser that guesses.
        if "content-disposition" in h:
            h.setdefault("X-Download-Options", "noopen")
        if settings.hsts_seconds > 0:
            h.setdefault(
                "Strict-Transport-Security",
                f"max-age={settings.hsts_seconds}; includeSubDomains",
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window-free sliding counter per client, per bucket."""

    def __init__(self, app) -> None:  # noqa: ANN001
        super().__init__(app)
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def _client(self, request: Request) -> str:
        # Behind a proxy the socket address is the proxy's. Prefer the first
        # hop in X-Forwarded-For, which the proxy controls.
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        settings = get_settings()
        path = request.url.path
        is_auth = any(path.endswith(p) for p in _AUTH_PATHS)
        limit = settings.rate_limit_auth_per_minute if is_auth else settings.rate_limit_per_minute
        if limit <= 0:
            return await call_next(request)

        key = (self._client(request), "auth" if is_auth else "api")
        now = time.monotonic()
        window = self._hits[key]
        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= limit:
            retry = max(1, int(60 - (now - window[0])))
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(retry)},
                content={
                    "detail": f"Too many requests. Try again in {retry} second(s).",
                },
            )
        window.append(now)

        # Keep the table from growing without bound on a long-lived process.
        if len(self._hits) > 20_000:
            for k in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
                self._hits.pop(k, None)

        return await call_next(request)
