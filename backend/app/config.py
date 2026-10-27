"""Configuration. Every secret comes from the environment -- nothing is
defaulted to a real credential and nothing is read from the frontend bundle.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings  # type: ignore[import-not-found]


class Settings(BaseSettings):
    app_name: str = "Agent Analyst"
    api_prefix: str = "/api/v1"
    environment: str = Field(default="development")

    # --- persistence ---------------------------------------------------
    database_url: str = Field(
        default="",
        description="Empty => SQLite at <data_dir>/agent_analyst.db. "
        "Set e.g. postgresql+psycopg://user:pw@host/db for Postgres.",
    )
    data_dir: str = Field(default="")

    # --- object storage ------------------------------------------------
    storage_backend: str = Field(default="local", description="local | s3")
    s3_bucket: str = ""
    s3_endpoint_url: str = ""

    # --- upload limits -------------------------------------------------
    max_upload_bytes: int = Field(default=64 * 1024 * 1024)
    max_files_per_project: int = 40
    max_rows_profiled: int = Field(
        default=1_000_000,
        description="Hard cap on rows read per sheet; exceeding it is reported, "
        "never silently truncated without a notice.",
    )
    preview_row_limit: int = 50

    # --- LLM -----------------------------------------------------------
    llm_provider: str = Field(
        default="auto", description="auto | anthropic | heuristic"
    )
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"
    llm_max_output_tokens: int = 4096
    llm_timeout_seconds: int = 90
    default_project_budget_usd: float = 5.0

    # --- auth ----------------------------------------------------------
    auth_secret: str = Field(
        default="",
        description="Empty in development => a per-boot random secret is generated "
        "and single-user dev mode is enabled.",
    )
    auth_token_ttl_seconds: int = 60 * 60 * 12
    allow_dev_auth: bool = Field(default=True)

    # --- execution sandbox ---------------------------------------------
    worker_timeout_seconds: int = 180
    worker_max_memory_mb: int = 2048
    worker_cpu_seconds: int = 120

    # --- Power BI ------------------------------------------------------
    powerbi_tenant_id: str = ""
    powerbi_client_id: str = ""
    powerbi_client_secret: str = ""
    powerbi_workspace_id: str = ""

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    model_config = {"env_file": ".env", "env_prefix": "", "extra": "ignore"}

    # --- derived -------------------------------------------------------
    @property
    def resolved_data_dir(self) -> Path:
        p = Path(self.data_dir) if self.data_dir else Path(__file__).resolve().parents[2] / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+pysqlite:///{self.resolved_data_dir / 'agent_analyst.db'}"

    @property
    def is_postgres(self) -> bool:
        return self.resolved_database_url.startswith("postgres")

    @property
    def storage_root(self) -> Path:
        p = self.resolved_data_dir / "objects"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def llm_configured(self) -> bool:
        return bool(self.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY"))

    @property
    def powerbi_configured(self) -> bool:
        return all(
            [self.powerbi_tenant_id, self.powerbi_client_id, self.powerbi_client_secret]
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
