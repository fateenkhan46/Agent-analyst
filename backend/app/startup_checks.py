"""Refuse to start a production deployment that is quietly insecure.

Every check here is something that is harmless in development and a real
exposure once the app is reachable by a company. The failure mode this
prevents is the one that matters: a deployment that *appears* to work, serves
traffic, and is wide open -- a missing signing secret does not break any
request, it just means every token is forgeable after a restart.

Nothing here reaches outside the process: it reads settings only, so it runs
identically in a container build, a CI check and at boot.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings


@dataclass
class Finding:
    setting: str
    problem: str
    fix: str
    blocking: bool = True


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if not f.blocking]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def as_text(self) -> str:
        lines: list[str] = []
        if self.blocking:
            lines.append(
                f"Refusing to start: {len(self.blocking)} setting(s) are unsafe for "
                f"ENVIRONMENT=production."
            )
            for f in self.blocking:
                lines.append(f"  [{f.setting}] {f.problem}")
                lines.append(f"      fix: {f.fix}")
        for f in self.warnings:
            lines.append(f"  (warning) [{f.setting}] {f.problem}")
            lines.append(f"      fix: {f.fix}")
        return "\n".join(lines)


def check(settings: Settings) -> Report:
    """Audit the settings. Only ENVIRONMENT=production produces blocking findings."""
    r = Report()
    production = settings.environment.lower() == "production"
    add = r.findings.append

    if not settings.auth_secret:
        add(Finding(
            "AUTH_SECRET",
            "No token signing secret is set, so one is generated per boot. Every "
            "session is invalidated on restart, and a multi-replica deployment "
            "signs with a different key on each replica.",
            "Set AUTH_SECRET to a value of at least 32 random characters, from a "
            "secret manager rather than a file. Generate one with: "
            "python -c 'import secrets; print(secrets.token_urlsafe(48))'",
            blocking=production,
        ))
    elif len(settings.auth_secret) < 32:
        add(Finding(
            "AUTH_SECRET",
            f"The signing secret is only {len(settings.auth_secret)} characters.",
            "Use at least 32 random characters.",
            blocking=production,
        ))

    if production and settings.allow_dev_auth:
        add(Finding(
            "ALLOW_DEV_AUTH",
            "Development auth is still enabled. It is inert while ENVIRONMENT is "
            "production, but leaving it on means a single environment variable "
            "turns authentication off.",
            "Set ALLOW_DEV_AUTH=false.",
            blocking=True,
        ))

    if production and settings.allow_open_registration:
        add(Finding(
            "ALLOW_OPEN_REGISTRATION",
            "Anyone who can reach the API can create an account.",
            "Set ALLOW_OPEN_REGISTRATION=false and provision accounts from the "
            "directory, or put the app behind an authenticating proxy and set "
            "TRUST_PROXY_AUTH=true.",
            blocking=True,
        ))

    if production and not settings.is_postgres:
        add(Finding(
            "DATABASE_URL",
            "The deployment would use a SQLite file. SQLite serialises writers, "
            "does not survive a container restart without a volume, and cannot be "
            "shared between replicas.",
            "Set DATABASE_URL to a PostgreSQL URL, e.g. "
            "postgresql+psycopg://user:password@host:5432/agent_analyst",
            blocking=True,
        ))

    origins = settings.cors_origin_list
    if production and any("localhost" in o or "127.0.0.1" in o for o in origins):
        add(Finding(
            "CORS_ORIGINS",
            f"Localhost is still an allowed browser origin: {', '.join(origins)}",
            "Set CORS_ORIGINS to the real front-end origin only, e.g. "
            "https://agent-analyst.internal.example.com",
            blocking=True,
        ))
    if production and "*" in origins:
        add(Finding(
            "CORS_ORIGINS",
            "Any origin may call the API with credentials attached.",
            "List the exact front-end origins.",
            blocking=True,
        ))

    if settings.trust_proxy_auth and not production:
        add(Finding(
            "TRUST_PROXY_AUTH",
            "Proxy-header authentication is on outside production, where the app "
            "is usually reachable directly. Any caller can then set the header "
            "and become any user.",
            "Only enable it where the app cannot be reached except through the "
            "authenticating proxy.",
            blocking=False,
        ))

    if production and settings.hsts_seconds == 0:
        add(Finding(
            "HSTS_SECONDS",
            "Strict-Transport-Security is not sent.",
            "Once TLS is terminated in front of the app, set HSTS_SECONDS=31536000.",
            blocking=False,
        ))

    if production and settings.llm_configured:
        add(Finding(
            "ANTHROPIC_API_KEY",
            "An LLM key is configured, so uploaded business data and meeting "
            "transcripts will be sent to a third-party API.",
            "Confirm this is covered by a data processing agreement and permitted "
            "for the data classification being uploaded. Leave the key unset to "
            "keep every call local.",
            blocking=False,
        ))

    return r
