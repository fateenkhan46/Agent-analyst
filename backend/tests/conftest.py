"""Test fixtures.

Each test gets an isolated data directory (its own SQLite file and object
store), so tests cannot see each other's state and the suite is order
independent.
"""
from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from openpyxl import Workbook

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "sample_project"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_SECRET", "test-secret-not-a-real-key")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    from app.config import get_settings
    from app.db import reset_engine
    from app.services import storage
    from app.services.capabilities import reset_probe_cache

    get_settings.cache_clear()
    reset_engine()
    storage.reset_store()
    reset_probe_cache()
    yield tmp_path
    reset_engine()
    storage.reset_store()
    get_settings.cache_clear()


@pytest.fixture
def client(isolated_env: Path) -> Iterator:
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def session(isolated_env: Path) -> Iterator:
    from app.db import init_db, session_scope

    init_db()
    with session_scope() as s:
        yield s


#: A short transcript with the decisions the downstream agents need, so the
#: fixture exercises the MoM path as well as the data path.
FIXTURE_TRANSCRIPT = """Cost Reporting Requirements
Date: 2026-02-10
Attendees: Dana Okoro (Finance Lead), Sam Petrov (Data Engineering)

Dana Okoro: Budget Variance is Budget minus Actual Cost. Positive means under budget.
Sam Petrov: Understood.
Dana Okoro: The variance percentage is variance divided by budget, not by actual.
Sam Petrov: Agreed. And hours? Some rows are in minutes.
Dana Okoro: Everything becomes hours.
Sam Petrov: Will do.
Dana Okoro: Average cost per project should only count projects that have actual cost.
Sam Petrov: Noted, a distinct count of projects in the cost data.
"""

#: KPIs the synthetic fixture data can actually support, so the model and
#: measure stages have something real to work on.
FIXTURE_KPIS = [
    "Total Cost", "Total Budget", "Budget Variance", "Budget Variance %",
    "Total Labour Hours", "Average Cost per Project", "Year-to-Date Cost",
]


@pytest.fixture
def open_registration(monkeypatch) -> Iterator[None]:
    """Self-registration is closed by default, so a deployment cannot be left
    open by accident. Tests that exercise local sign-up turn it on explicitly,
    exactly as an operator would have to."""
    from app.config import get_settings

    monkeypatch.setenv("ALLOW_OPEN_REGISTRATION", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("ALLOW_OPEN_REGISTRATION", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def project_id(client) -> str:
    r = client.post("/api/v1/projects", json={
        "name": "Test Project",
        "business_objective": "Show which projects are over budget and why.",
        "currency": "USD", "units": "hours",
        "desired_kpis": FIXTURE_KPIS,
        "reporting_questions": ["Which projects are over budget?",
                                "What is the monthly cost trend?"],
        "reporting_period": "FY2025",
        "meeting_transcript": FIXTURE_TRANSCRIPT,
    })
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


@pytest.fixture
def bare_project_id(client) -> str:
    """A project with no KPIs and no transcript, for tests that need the
    empty case."""
    r = client.post("/api/v1/projects", json={"name": "Bare", "currency": "USD"})
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


# --------------------------------------------------------------------------
# Synthetic fixtures with specific, named defects
# --------------------------------------------------------------------------
def make_xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name[:31])
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def inconsistent_workbook() -> bytes:
    """Two sheets describing the same thing with different column names,
    a title row above the header, and mixed date/currency formats."""
    return make_xlsx({
        "FY25": [
            ["Acme Cost Ledger"],
            [],
            ["Proj ID", "Cost Date", "Cost Amount"],
            ["PRJ-1", "2025-01-15", "$1,200.00"],
            ["PRJ-1", "16/01/2025", "900"],
            ["PRJ-2", "17-Jan-2025", "1.234,56"],
            ["PRJ-2", "January 18, 2025", "(400.00)"],
            ["PRJ-3", "01/19/2025", "2,500.00 USD"],
            ["PRJ-3", "03/04/2025", "1,100.00"],
            # exact duplicate of the first data row -> double-count risk
            ["PRJ-1", "2025-01-15", "$1,200.00"],
        ],
        "FY24": [
            ["project_id", "transaction_date", "amount_usd"],
            ["PRJ-1", "2024-06-01", 500.0],
            ["PRJ-2", "2024-06-02", 750.0],
        ],
    })


@pytest.fixture
def labour_csv() -> bytes:
    """Mixed hour units, a duplicate key with different values, a missing FK,
    and an identifier written in a different style."""
    return (
        "timesheet_id,project,employee_id,hours_worked\n"
        "TS-1,PRJ-1,E-1,7.50\n"
        "TS-2,PRJ-1,E-2,8h\n"
        "TS-3,PRJ-2,E-1,480min\n"
        "TS-4,PRJ-2,E-3,07:30\n"
        "TS-5,prj 3,E-2,6.25\n"
        "TS-6,,E-3,4.00\n"
        "TS-1,PRJ-1,E-1,9.00\n"
    ).encode()


@pytest.fixture
def register_csv() -> bytes:
    """The dimension: unique keys, one member with no activity anywhere."""
    return (
        "Project Code,Project Name,Status,Approved Budget\n"
        "PRJ-1,Alpha,In Flight,\"$10,000\"\n"
        "PRJ-2,Beta,Complete,\"$8,000\"\n"
        "PRJ-3,Gamma,On Hold,\"$5,000\"\n"
        "PRJ-4,Delta,In Flight,\"$2,000\"\n"
    ).encode()


@pytest.fixture
def loaded_project(client, project_id, inconsistent_workbook, labour_csv, register_csv) -> str:
    """A project with the three synthetic sources uploaded."""
    r = client.post(
        f"/api/v1/projects/{project_id}/files",
        files=[
            ("files", ("costs.xlsx", inconsistent_workbook,
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("files", ("labour.csv", labour_csv, "text/csv")),
            ("files", ("register.csv", register_csv, "text/csv")),
        ],
    )
    assert r.status_code == 201, r.text
    assert len(r.json()["accepted"]) == 3, r.json()
    return project_id


@pytest.fixture
def profiled_project(client, loaded_project) -> str:
    r = client.post(f"/api/v1/projects/{loaded_project}/tasks/profile_sources/run", json={})
    assert r.status_code == 200 and r.json()["succeeded"], r.text
    return loaded_project
