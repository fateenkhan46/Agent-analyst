"""API surface: projects, uploads, profiling, persistence, authorisation."""
from __future__ import annotations

import pytest


# --- system --------------------------------------------------------------
def test_capabilities_are_reported_honestly(client) -> None:
    body = client.get("/api/v1/system/capabilities").json()
    by_key = {c["key"]: c for c in body["capabilities"]}
    # Things that genuinely cannot work on this host must say so.
    assert by_key["export.pbix"]["status"] == "unavailable"
    assert by_key["model.tom"]["status"] == "unavailable"
    assert by_key["dax.analysis_services"]["status"] == "unavailable"
    # Things we built must be claimed only if usable.
    assert by_key["model.tmdl"]["status"] == "implemented"
    assert body["environment"]["powerbi_desktop_available"] is False


def test_contracts_endpoint_serves_json_schema(client) -> None:
    body = client.get("/api/v1/system/contracts").json()
    assert body["count"] >= 21
    assert "properties" in body["contracts"]["Project"]


def test_health(client) -> None:
    assert client.get("/api/v1/system/health").json()["status"] == "ok"


# --- projects ------------------------------------------------------------
def test_create_project_and_task_graph(client) -> None:
    r = client.post("/api/v1/projects", json={"name": "Cost Report", "currency": "USD"})
    assert r.status_code == 201
    pid = r.json()["project"]["id"]
    tasks = client.get(f"/api/v1/projects/{pid}/tasks").json()
    assert len(tasks["tasks"]) == 10
    assert "profile_sources" in tasks["runnable"]
    assert tasks["progress"] == 0.0


def test_project_name_is_required(client) -> None:
    assert client.post("/api/v1/projects", json={"name": ""}).status_code == 422


def test_transcript_at_creation_is_stored_as_a_source(client) -> None:
    pid = client.post("/api/v1/projects", json={
        "name": "With transcript", "meeting_transcript": "Alice: we need cost by project.",
    }).json()["project"]["id"]
    files = client.get(f"/api/v1/projects/{pid}/files").json()["files"]
    assert any(f["kind"] == "transcript" for f in files), files


# --- uploads -------------------------------------------------------------
def test_upload_accepts_data_and_records_hashes(client, project_id, labour_csv) -> None:
    r = client.post(
        f"/api/v1/projects/{project_id}/files",
        files=[("files", ("labour.csv", labour_csv, "text/csv"))],
    )
    assert r.status_code == 201
    accepted = r.json()["accepted"]
    assert len(accepted) == 1 and len(accepted[0]["sha256"]) == 64


def test_identical_reupload_is_deduplicated(client, project_id, labour_csv) -> None:
    for _ in range(2):
        client.post(
            f"/api/v1/projects/{project_id}/files",
            files=[("files", ("labour.csv", labour_csv, "text/csv"))],
        )
    files = client.get(f"/api/v1/projects/{project_id}/files").json()["files"]
    assert len(files) == 1, "the same bytes must not create a second source"


@pytest.mark.parametrize(
    "filename,content,expect_in_reason",
    [
        ("evil.exe", b"MZ\x90\x00", "not an accepted file type"),
        ("fake.csv", b"MZ\x90\x00\x03", "does not match its extension"),
        ("fake.xlsx", b"a,b\n1,2", "not a valid Excel workbook"),
        ("binary.csv", b"ok\x00bad", "binary data"),
        ("empty.csv", b"", "empty"),
    ],
)
def test_dangerous_or_mislabelled_uploads_are_rejected_with_reasons(
    client, project_id, filename, content, expect_in_reason
) -> None:
    r = client.post(
        f"/api/v1/projects/{project_id}/files",
        files=[("files", (filename, content, "application/octet-stream"))],
    )
    rejected = r.json()["rejected"]
    assert len(rejected) == 1, r.json()
    assert expect_in_reason in rejected[0]["reason"], rejected[0]["reason"]


def test_one_bad_file_does_not_abort_the_batch(client, project_id, labour_csv) -> None:
    r = client.post(
        f"/api/v1/projects/{project_id}/files",
        files=[
            ("files", ("good.csv", labour_csv, "text/csv")),
            ("files", ("bad.exe", b"MZ\x00", "application/octet-stream")),
        ],
    )
    body = r.json()
    assert len(body["accepted"]) == 1 and len(body["rejected"]) == 1


def test_upload_size_limit(client, project_id, monkeypatch) -> None:
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 32)
    r = client.post(
        f"/api/v1/projects/{project_id}/files",
        files=[("files", ("big.csv", b"a,b\n" + b"1,2\n" * 100, "text/csv"))],
    )
    assert len(r.json()["rejected"]) == 1
    assert "limit" in r.json()["rejected"][0]["reason"]


# --- profiling -----------------------------------------------------------
def test_profiling_requires_a_file(client, project_id) -> None:
    r = client.post(f"/api/v1/projects/{project_id}/profile")
    assert r.status_code == 400 and "Upload at least one" in r.json()["detail"]


def test_profile_detects_the_planted_defects(client, profiled_project) -> None:
    body = client.get(f"/api/v1/projects/{profiled_project}/profile").json()
    profile = body["profile"]
    tables = {t["table_name"]: t for t in profile["tables"]}
    assert len(tables) == 4, list(tables)  # 2 sheets + 2 csv

    all_codes = {
        i["code"]
        for t in profile["tables"]
        for i in (t["issues"] + [x for c in t["columns"] for x in c["issues"]])
    } | {i["code"] for i in profile["cross_table_issues"]}

    for expected in (
        "column.inconsistent_format",      # mixed date and currency formats
        "column.conflicting_date_orders",  # provably both dd/mm and mm/dd present
        "column.ambiguous_date_order",     # a value where the order cannot be known
        "column.duplicate_key_values",     # TS-1 twice with different hours
        "keys.unmatched_identifiers",      # orphan project keys
        "table.mixed_naming",              # register headers
    ):
        assert expected in all_codes, f"{expected} not found in {sorted(all_codes)}"


def test_header_below_title_row_is_handled_end_to_end(client, profiled_project) -> None:
    profile = client.get(f"/api/v1/projects/{profiled_project}/profile").json()["profile"]
    fy25 = next(t for t in profile["tables"] if t["table_name"].endswith("fy25"))
    assert fy25["header_row_index"] == 2
    assert [c["name"] for c in fy25["columns"]] == ["Proj ID", "Cost Date", "Cost Amount"]


def test_identifier_styles_are_matched_across_sources(client, profiled_project) -> None:
    """'prj 3' in labour must match 'PRJ-3' in the register."""
    profile = client.get(f"/api/v1/projects/{profiled_project}/profile").json()["profile"]
    pair = next(
        (o for o in profile["key_overlaps"]
         if o["left"].startswith("labour") and "register" in o["right"]
         or o["right"].startswith("labour") and "register" in o["left"]),
        None,
    )
    assert pair is not None, profile["key_overlaps"]
    assert pair["matched_distinct"] >= 3, pair


def test_source_rows_are_never_lost(client, profiled_project) -> None:
    profile = client.get(f"/api/v1/projects/{profiled_project}/profile").json()["profile"]
    labour = next(t for t in profile["tables"] if t["table_name"] == "labour")
    assert labour["row_count"] == 7, "every uploaded row must be accounted for"


def test_table_rows_endpoint_pages(client, profiled_project) -> None:
    r = client.get(
        f"/api/v1/projects/{profiled_project}/tables/labour/rows?limit=2&offset=1"
    )
    body = r.json()
    assert body["total_rows"] == 7 and len(body["rows"]) == 2 and body["offset"] == 1


def test_unknown_table_is_404(client, profiled_project) -> None:
    r = client.get(f"/api/v1/projects/{profiled_project}/tables/nope/rows")
    assert r.status_code == 404


# --- persistence and audit ----------------------------------------------
def test_profile_is_versioned(client, profiled_project) -> None:
    client.post(f"/api/v1/projects/{profiled_project}/tasks/profile_sources/run", json={})
    body = client.get(f"/api/v1/projects/{profiled_project}/profile").json()
    versions = body["versions"]
    assert len(versions) >= 2
    assert sum(1 for v in versions if v["is_current"]) == 1


def test_audit_trail_records_the_work(client, profiled_project) -> None:
    events = client.get(f"/api/v1/projects/{profiled_project}/audit").json()["events"]
    actions = {e["action"] for e in events}
    for expected in ("project_created", "file_uploaded", "file_read",
                     "profile_generated", "agent_started", "agent_finished"):
        assert expected in actions, f"{expected} missing from {sorted(actions)}"


def test_execution_is_recorded_with_tool_calls(client, profiled_project) -> None:
    ex = client.get(f"/api/v1/projects/{profiled_project}/executions").json()["executions"]
    assert ex, "no execution recorded"
    first = ex[0]
    assert first["agent"] == "data_profiling_agent"
    assert first["status"] == "succeeded"
    assert "profile_project" in [t["name"] for t in first["tool_calls"]]
    assert first["cost"]["amount_usd"] == 0.0, "a deterministic agent must cost nothing"


def test_state_survives_a_restart(client, profiled_project) -> None:
    """A new app instance against the same data dir must see the project."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as fresh:
        body = fresh.get(f"/api/v1/projects/{profiled_project}").json()
        assert body["project"]["id"] == profiled_project
        assert len(body["files"]) == 3
        assert fresh.get(f"/api/v1/projects/{profiled_project}/profile").status_code == 200


# --- authorisation -------------------------------------------------------
def test_another_tenants_project_is_not_found(client, project_id) -> None:
    """Cross-tenant access returns 404, never 403: the API must not confirm
    that a resource it will not serve exists."""
    from app.db import session_scope
    from app.services import repository as repo
    from app.services.auth import hash_password, issue_token

    with session_scope() as s:
        other = repo.create_user(
            s, email="other@example.com", password_hash=hash_password("x" * 12),
            tenant_id="tenant-b",
        )
        token = issue_token(other.id, other.tenant_id)

    r = client.get(
        f"/api/v1/projects/{project_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 404


def test_invalid_token_is_rejected(client) -> None:
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not.a.token"})
    assert r.status_code == 401


def test_register_and_login(client) -> None:
    r = client.post("/api/v1/auth/register", json={
        "email": "a@example.com", "password": "correct-horse-battery",
    })
    assert r.status_code == 201 and r.json()["token"]
    r = client.post("/api/v1/auth/login", json={
        "email": "a@example.com", "password": "correct-horse-battery",
    })
    assert r.status_code == 200
    token = r.json()["token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["email"] == "a@example.com" and me["development_mode"] is False


def test_weak_password_is_refused(client) -> None:
    r = client.post("/api/v1/auth/register", json={"email": "b@example.com", "password": "short"})
    assert r.status_code == 400


def test_login_failure_does_not_reveal_whether_the_account_exists(client) -> None:
    client.post("/api/v1/auth/register", json={
        "email": "c@example.com", "password": "correct-horse-battery",
    })
    known = client.post("/api/v1/auth/login", json={
        "email": "c@example.com", "password": "wrong-password-here",
    })
    unknown = client.post("/api/v1/auth/login", json={
        "email": "nobody@example.com", "password": "wrong-password-here",
    })
    assert known.status_code == unknown.status_code == 401
    assert known.json()["detail"] == unknown.json()["detail"]


# --- dashboard and delivery ---------------------------------------------
def test_dashboard_lists_the_project(client, profiled_project) -> None:
    body = client.get("/api/v1/dashboard").json()
    assert body["project_count"] >= 1
    assert any(p["id"] == profiled_project for p in body["projects"])
    assert body["environment"]["llm_provider_active"] in {"heuristic", "anthropic"}


def test_delivery_marks_unproduced_sections_unavailable_with_reasons(
    client, profiled_project
) -> None:
    body = client.get(f"/api/v1/projects/{profiled_project}/delivery").json()
    sections = body["sections"]
    assert sections["source_profile"]["available"] is True
    for key in ("semantic_model", "measure_catalog", "report_catalog", "validation"):
        assert sections[key]["available"] is False
        assert sections[key]["reason"], f"{key} must explain why it is missing"
    assert body["exports"] == []


def test_delivery_for_unknown_project_is_404(client) -> None:
    assert client.get("/api/v1/projects/proj_nope/delivery").status_code == 404
