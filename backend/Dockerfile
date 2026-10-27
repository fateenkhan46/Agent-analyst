# Multi-stage so the runtime image carries no build toolchain.
FROM python:3.13-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt \
 && /opt/venv/bin/pip install "psycopg[binary]==3.2.3"

FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    DATA_DIR=/var/lib/agent-analyst
# curl is the healthcheck; nothing else is added, so the attack surface stays
# the Python runtime plus the application.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home /home/app --create-home app \
 && mkdir -p /var/lib/agent-analyst \
 && chown -R app:app /var/lib/agent-analyst
COPY --from=build /opt/venv /opt/venv
WORKDIR /srv
COPY --chown=app:app app ./app
COPY --chown=app:app pytest.ini ./
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/v1/system/health || exit 1
# The app refuses to start when ENVIRONMENT=production is configured unsafely,
# so a misconfigured deployment fails its health check instead of serving.
# uvicorn directly rather than under gunicorn: uvicorn.workers is deprecated,
# and in a container the orchestrator already supervises the process.
# --proxy-headers so the client IP the rate limiter keys on is the real one,
# and --forwarded-allow-ips restricted to the pod/VNet CIDR in your platform
# config, since anything that can reach the port could otherwise spoof it.
CMD ["uvicorn", "app.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "4", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-graceful-shutdown", "30"]
