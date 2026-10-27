# Deployment

Read [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md) first. It lists the gaps that
are still open, including two that may be blocking depending on your policy.

> The container images in this repository have **not been built or run** —
> Docker was unavailable in the environment where they were written. Build and
> smoke-test them before any real deployment.

## What you need

- A container platform (Azure Container Apps / App Service, AWS ECS or EKS,
  or Kubernetes on-premise)
- Managed PostgreSQL 14+
- Persistent storage: a volume mounted at `DATA_DIR`, or an S3-compatible bucket
- An identity provider and something in front of the app that authenticates
  against it (details below)
- TLS terminated in front of the app

## The shape of it

```
  Browser ──TLS──> authenticating proxy ──> web (Next.js) ──> api (FastAPI)
                   terminates SSO,              server-side        │
                   injects identity headers     /api rewrite       └──> PostgreSQL
                                                                   └──> volume or S3
```

Only the proxy is published. **The API must not be reachable any other way** —
when `TRUST_PROXY_AUTH=true` it believes the identity headers, so anything that
can reach it directly can impersonate any user. Enforce this with a private
network or an internal-only ingress.

## Local reference deployment

```bash
cd deploy
cp .env.example .env
# fill AUTH_SECRET, POSTGRES_PASSWORD, CORS_ORIGINS and the OIDC_* values
docker compose up -d --build
```

`docker-compose.yml` is the reference topology: Postgres, the API, the front
end, and oauth2-proxy for sign-in. On a managed platform, replace `postgres`
with the managed database and `auth-proxy` with the platform's own
authentication, which injects the same headers.

## Platform notes

### Azure

- **Container Apps** or **App Service** for `web` and `api`; keep `api`
  internal-only (ingress `internal`).
- **Authentication** (EasyAuth) with **Entra ID** on the `web` app. It injects
  `X-MS-CLIENT-PRINCIPAL-NAME`; set `PROXY_AUTH_EMAIL_HEADER` to match, or run
  oauth2-proxy and keep the defaults.
- **Azure Database for PostgreSQL – Flexible Server**, private endpoint.
- **Key Vault** for `AUTH_SECRET` and the database password, referenced as
  secrets rather than plain environment values.
- **Azure Files** mounted at `DATA_DIR`, or switch `STORAGE_BACKEND=s3` against
  a compatible gateway.

### AWS

- **ECS Fargate** or **EKS**. Put the API in a private subnet with no public
  ingress.
- An **ALB with an OIDC authenticate action** in front of `web`. It injects
  `x-amzn-oidc-data`; run oauth2-proxy if you prefer the standard
  `x-auth-request-*` headers this app reads by default.
- **RDS PostgreSQL**, private subnet.
- **Secrets Manager** for `AUTH_SECRET` and the database URL.
- **EFS** at `DATA_DIR`, or `STORAGE_BACKEND=s3` with an S3 bucket and an IAM
  task role (no keys in the environment).

### On-premise Kubernetes

- Two Deployments, two Services, one Ingress reaching `web` only.
- An OIDC filter at the ingress, or oauth2-proxy as a sidecar.
- A NetworkPolicy so only `web` may reach `api`.
- `AUTH_SECRET` and the database password as Secrets.
- A ReadWriteMany PVC at `DATA_DIR` if you run more than one API replica on
  local storage; otherwise use S3.

## Before the first production boot

The application audits its own configuration and **refuses to start** if any of
these is wrong under `ENVIRONMENT=production`:

| Setting | Required |
|---|---|
| `AUTH_SECRET` | 32+ random characters, identical across replicas |
| `ALLOW_DEV_AUTH` | `false` |
| `ALLOW_OPEN_REGISTRATION` | `false` |
| `DATABASE_URL` | a PostgreSQL URL |
| `CORS_ORIGINS` | the real front-end origin, no localhost, no wildcard |

Check the configuration without starting the app:

```bash
cd backend
ENVIRONMENT=production AUTH_SECRET=... DATABASE_URL=... CORS_ORIGINS=... \
  python -c "from app.config import Settings; from app.startup_checks import check; \
             r=check(Settings()); print(r.as_text() or 'configuration ok'); \
             raise SystemExit(0 if r.ok else 1)"
```

Run this in CI against the deployment's real variables and the pipeline fails
before anything ships.

## Scale and limits

- `api` is stateless apart from the volume; scale horizontally. All replicas
  must share `AUTH_SECRET` and the database.
- The container runs 4 uvicorn workers. The in-process rate limiter is **per
  worker**, so the real ceiling is `RATE_LIMIT_PER_MINUTE × 4 × replicas`. Put
  the enforceable limit at the gateway.
- Long transformations run inside the request. Allow 300s at the proxy and the
  load balancer, or large profiling jobs will be cut off.
- Memory: profiling holds a sheet in memory. 4 GB per replica suits the 64 MB
  upload cap; raise both together.

## Health and monitoring

- `GET /api/v1/system/health` — liveness, no dependencies.
- `GET /api/v1/system/capabilities` — what this deployment can actually do,
  including which LLM provider is active. Worth checking after every deploy.
- Audit events are in the `audit_events` table and at
  `GET /api/v1/projects/{id}/audit`. There is no SIEM forwarder; add one if
  policy requires central collection.
- Watch for `auth_denied` bursts — nothing alerts on them today.

## Upgrades

Tables are created on boot (`init_db`). There is **no migration framework**;
a column change needs one adding (Alembic) or a manual migration. Back up the
database before upgrading.
