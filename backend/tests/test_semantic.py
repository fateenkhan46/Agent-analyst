"""The semantic model and measure generation, end to end on real data."""
from __future__ import annotations

import pytest


@pytest.fixture
def modelled_project(client, profiled_project) -> str:
    """A project driven through to generated measures."""
    r = client.post(
        f"/api/v1/projects/{profiled_project}/run",
        json={"stop_after": "generate_measures", "auto_approve": True},
    )
    body = r.json()
    failed = [s for s in body["steps"] if not s.get("succeeded")]
    assert not failed, failed
    return profiled_project


def _model(client, pid) -> dict:
    return client.get(f"/api/v1/projects/{pid}/artifacts/model").json()["artifact"]


# --- roles and shape -----------------------------------------------------
def test_register_is_a_dimension_despite_carrying_a_budget(client, modelled_project) -> None:
    """Shape decides the role, not the presence of numbers. Getting this wrong
    double-counts budget across every cost row."""
    model = _model(client, modelled_project)
    register = next(
        t for t in model["tables"] if "register" in t["dataset_name"].lower()
    )
    assert register["role"] == "dimension"
    assert register["primary_key"], "a dimension must have an identified key"


def test_fact_tables_are_not_given_a_key_they_do_not_have(client, modelled_project) -> None:
    model = _model(client, modelled_project)
    for table in model["tables"]:
        if table["role"] != "fact":
            continue
        for key in table["primary_key"]:
            assert key, "a blank key must not be recorded"


def test_date_dimension_is_created_and_related(client, modelled_project) -> None:
    model = _model(client, modelled_project)
    dates = [t for t in model["tables"] if t["role"] == "date"]
    assert len(dates) == 1
    date_name = dates[0]["name"]
    related = [r for r in model["relationships"] if r["to_table"] == date_name]
    assert related, "every fact table needs a date relationship for time intelligence"


def test_relationships_are_many_to_one_from_the_fact_side(client, modelled_project) -> None:
    model = _model(client, modelled_project)
    roles = {t["name"]: t["role"] for t in model["tables"]}
    for r in model["relationships"]:
        assert r["cardinality"] in {"many_to_one", "one_to_one"}, r
        if roles.get(r["from_table"]) == "fact":
            assert roles.get(r["to_table"]) in {"dimension", "date"}, (
                f"{r['from_table']} -> {r['to_table']}: a fact must relate to a dimension"
            )


def test_no_fact_to_fact_relationship(client, modelled_project) -> None:
    """Two facts at different grains must share a dimension, never relate
    directly -- a direct join fans out and double-counts."""
    model = _model(client, modelled_project)
    roles = {t["name"]: t["role"] for t in model["tables"]}
    for r in model["relationships"]:
        both_facts = (
            roles.get(r["from_table"]) == "fact" and roles.get(r["to_table"]) == "fact"
        )
        assert not both_facts, f"fact-to-fact relationship: {r['from_table']} -> {r['to_table']}"


def test_every_relationship_carries_measured_evidence(client, modelled_project) -> None:
    model = _model(client, modelled_project)
    for r in model["relationships"]:
        assert r["rationale"], f"{r['from_table']}->{r['to_table']} has no rationale"
        assert r["evidence"], f"{r['from_table']}->{r['to_table']} has no evidence"
        assert 0.0 <= r["match_share"] <= 1.0


def test_no_isolated_tables(client, modelled_project) -> None:
    model = _model(client, modelled_project)
    if len(model["tables"]) < 2:
        pytest.skip("single-table model")
    connected = set()
    for r in model["relationships"]:
        connected.add(r["from_table"])
        connected.add(r["to_table"])
    isolated = [
        t["name"] for t in model["tables"]
        if t["name"] not in connected and t["role"] != "date"
    ]
    assert not isolated, f"isolated tables: {isolated}"


def test_double_count_risk_on_a_dimension_measure_is_reported(
    client, modelled_project
) -> None:
    model = _model(client, modelled_project)
    codes = {i["code"] for i in model["issues"]}
    assert "model.dimension_measure_double_count_risk" in codes, codes


def test_diagram_has_a_node_per_table_and_mermaid(client, modelled_project) -> None:
    r = client.post(
        f"/api/v1/projects/{modelled_project}/tasks/propose_semantic_model/run", json={}
    )
    diagram = r.json()["result"]["diagram"]
    model = _model(client, modelled_project)
    assert len(diagram["nodes"]) == len(model["tables"])
    assert diagram["mermaid"].startswith("erDiagram")
    for node in diagram["nodes"]:
        assert node["x"] or node["y"], "nodes need a computed position"


# --- measures ------------------------------------------------------------
def test_measures_are_generated_for_requested_kpis_only(client, modelled_project) -> None:
    model = _model(client, modelled_project)
    requirements = client.get(
        f"/api/v1/projects/{modelled_project}/artifacts/requirements"
    ).json()["artifact"]
    requested = {k["name"].lower() for k in requirements["kpis"]}
    for measure in model["measures"]:
        assert measure["name"].lower() in requested, (
            f"'{measure['name']}' was not requested -- the agent must not invent KPIs"
        )


def test_every_measure_has_an_explanation_and_behaviour(client, modelled_project) -> None:
    model = _model(client, modelled_project)
    assert model["measures"], "no measures generated"
    for measure in model["measures"]:
        assert measure["description"], f"{measure['name']} has no explanation"
        assert measure["aggregation_behaviour"], f"{measure['name']} has no behaviour note"
        assert measure["validation_cases"], f"{measure['name']} has no validation cases"
        assert measure["provenance"], f"{measure['name']} has no provenance"


def test_ratios_use_divide(client, modelled_project) -> None:
    """A bare '/' errors on a zero denominator; the sample meeting asks for
    blank instead."""
    model = _model(client, modelled_project)
    ratios = [
        m for m in model["measures"]
        if m["format_string"].endswith("%") or "per " in m["name"].lower()
    ]
    assert ratios, "expected at least one ratio measure"
    for measure in ratios:
        assert "DIVIDE(" in measure["expression"], measure["expression"]
        assert " / " not in measure["expression"], measure["expression"]


def test_measure_dependencies_are_recorded(client, modelled_project) -> None:
    model = _model(client, modelled_project)
    variance = next(
        (m for m in model["measures"] if m["name"].lower() == "budget variance"), None
    )
    if variance is None:
        pytest.skip("budget variance not in this fixture's KPIs")
    assert set(variance["depends_on_measures"]) >= {"Total Budget", "Total Cost"}


def test_all_measures_pass_their_own_validation(client, modelled_project) -> None:
    """Anything returned must have passed the parser and the reference check;
    failures belong in 'rejected', not in the model."""
    from app.schemas.semantic import SemanticModel
    from app.services.dax.validator import validate_measure

    model = SemanticModel.model_validate(_model(client, modelled_project))
    for measure in model.measures:
        results = validate_measure(measure, model)
        blocking = [r for r in results if r.status.is_blocking]
        assert not blocking, (
            f"{measure.name} is in the model but fails validation: "
            f"{[r.actual for r in blocking]}"
        )


def test_undefined_kpi_produces_no_measure(client, project_id, labour_csv) -> None:
    """A KPI with no agreed definition must yield nothing, not a guess."""
    client.post("/api/v1/projects", json={"name": "x"})
    r = client.post(
        f"/api/v1/projects/{project_id}/files",
        files=[("files", ("labour.csv", labour_csv, "text/csv"))],
    )
    assert r.status_code == 201
    # a KPI the catalogue cannot match
    from app.db import session_scope
    from app.services import repository as repo
    with session_scope() as s:
        project = repo.get_project(s, project_id)
        project.desired_kpis = ["Widget Throughput Index"]
        repo.save_project(s, project)

    client.post(f"/api/v1/projects/{project_id}/run",
                json={"stop_after": "generate_measures", "auto_approve": True})
    r = client.get(f"/api/v1/projects/{project_id}/artifacts/model")
    if r.status_code != 200:
        pytest.skip("the model was not produced for this minimal fixture")
    model = r.json()["artifact"]
    assert not any(
        m["name"] == "Widget Throughput Index" for m in model["measures"]
    ), "an undefined KPI must not receive a generated formula"


# --- evaluation against real data ---------------------------------------
def test_measures_evaluate_to_real_numbers(client, modelled_project) -> None:
    model = _model(client, modelled_project)
    evaluated = 0
    for measure in model["measures"]:
        r = client.post(
            f"/api/v1/projects/{modelled_project}/measures/{measure['name']}/evaluate",
            json={},
        )
        assert r.status_code == 200, r.text
        outcome = r.json()
        if outcome["supported"]:
            evaluated += 1
            assert outcome["sql_executed"], "a supported evaluation must have run SQL"
            assert outcome["engine"] == "duckdb"
    assert evaluated, "no measure could be evaluated against the data"


def test_total_matches_an_independent_sum(client, modelled_project) -> None:
    """The point of the evaluator: the DAX result is checked against a number
    computed a different way."""
    import duckdb

    from app.db import session_scope
    from app.services import repository as repo
    from app.services.storage import get_store

    model = _model(client, modelled_project)
    total = next((m for m in model["measures"] if m["name"] == "Total Cost"), None)
    if total is None:
        pytest.skip("Total Cost not generated for this fixture")

    r = client.post(
        f"/api/v1/projects/{modelled_project}/measures/Total Cost/evaluate", json={}
    )
    outcome = r.json()
    assert outcome["supported"] and outcome["value"] is not None

    fact = next(t for t in model["tables"] if t["name"] == total["home_table"])
    with session_scope() as s:
        dataset = repo.get_dataset(s, modelled_project, fact["dataset_name"])
    assert dataset is not None
    path = str(get_store().local_path(dataset["stored_path"])).replace("'", "''")
    con = duckdb.connect()
    column = total["depends_on_columns"][0].split("[")[1].rstrip("]")
    flag = f"{column}_is_reporting_currency"
    has_flag = any(c["name"] == flag for c in fact["columns"])
    where = f" WHERE {flag} = 'True'" if has_flag else ""
    expected = con.execute(
        f"SELECT SUM(TRY_CAST({column} AS DOUBLE)) FROM read_csv_auto('{path}'){where}"
    ).fetchone()[0]
    con.close()
    assert expected is not None
    assert abs(outcome["value"] - float(expected)) < 0.01, (
        f"DAX returned {outcome['value']} but the direct sum is {expected}"
    )


def test_mixed_currency_is_excluded_from_the_total(client, modelled_project) -> None:
    """The sample meeting is explicit: do not add unlike currencies together."""
    model = _model(client, modelled_project)
    total = next((m for m in model["measures"] if m["name"] == "Total Cost"), None)
    if total is None:
        pytest.skip("Total Cost not generated")
    fact = next(t for t in model["tables"] if t["name"] == total["home_table"])
    flags = [c["name"] for c in fact["columns"] if c["name"].endswith("_is_reporting_currency")]
    if not flags:
        pytest.skip("this fixture has a single currency")
    assert "CALCULATE" in total["expression"] and flags[0] in total["expression"], (
        f"a multi-currency amount must be filtered to the reporting currency: "
        f"{total['expression']}"
    )


def test_unsupported_construct_is_reported_not_passed(client, modelled_project) -> None:
    """Honesty check: a measure outside the subset must say so."""
    r = client.put(
        f"/api/v1/projects/{modelled_project}/measures/Total Cost",
        json={"expression": "RANKX(ALL(DimProjectRegister), [Total Cost])", "force": True},
    )
    if r.status_code != 200 or not r.json().get("saved"):
        pytest.skip("could not stage an unsupported expression")
    outcome = client.post(
        f"/api/v1/projects/{modelled_project}/measures/Total Cost/evaluate", json={}
    ).json()
    assert outcome["supported"] is False
    assert outcome["unsupported_construct"]
    assert outcome["value"] is None


def test_hand_edited_invalid_dax_is_refused(client, modelled_project) -> None:
    r = client.put(
        f"/api/v1/projects/{modelled_project}/measures/Total Cost",
        json={"expression": "SUM(NoSuchTable[nope])"},
    )
    body = r.json()
    assert body["saved"] is False
    assert any(v["status"] == "fail" for v in body["validation"])


def test_hand_edit_is_revalidated_and_stored(client, modelled_project) -> None:
    model = _model(client, modelled_project)
    total = next(m for m in model["measures"] if m["name"] == "Total Cost")
    r = client.put(
        f"/api/v1/projects/{modelled_project}/measures/Total Cost",
        json={"expression": total["expression"], "description": "Edited by hand."},
    )
    assert r.json()["saved"] is True
    refreshed = _model(client, modelled_project)
    edited = next(m for m in refreshed["measures"] if m["name"] == "Total Cost")
    assert edited["description"] == "Edited by hand."
    assert edited["provenance"]["origin"] == "user"


def test_a_measure_returning_text_is_reported_unsupported_not_a_crash(
    client, modelled_project
) -> None:
    """A measure may legitimately evaluate to a label rather than a number.

    The evaluation contract carries a numeric value, so the runtime used to
    call ``float()`` on whatever came back and raise ValueError, surfacing as a
    500. Valid DAX that this engine cannot report must come back as
    ``not_supported`` -- the same honest answer every other out-of-subset
    construct gets.
    """
    model = _model(client, modelled_project)
    target = next((m for m in model["measures"] if m["name"] == "Total Cost"), None)
    if target is None:
        pytest.skip("Total Cost not generated for this fixture")

    # Build the expression from the measure's own dependency so the test does
    # not assume a table name the fixture may not produce.
    column = next(iter(target["depends_on_columns"]), "")
    if not column:
        pytest.skip("Total Cost has no column dependency in this fixture")

    saved = client.put(
        f"/api/v1/projects/{modelled_project}/measures/Total Cost",
        json={"expression": f'IF(SUM({column}) > 0, "high", "low")'},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["saved"], (
        f"the expression is valid DAX and should store: "
        f"{saved.json().get('validation')}"
    )

    r = client.post(
        f"/api/v1/projects/{modelled_project}/measures/Total Cost/evaluate", json={}
    )
    assert r.status_code == 200, r.text
    outcome = r.json()
    assert outcome["supported"] is False
    assert outcome["value"] is None
    assert outcome["unsupported_construct"] == "non-numeric result"
    assert "not a number" in outcome["reason"]
