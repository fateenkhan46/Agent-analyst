"""Data preparation: planning, execution, lineage, M generation, approvals."""
from __future__ import annotations

import pandas as pd
import pytest

from app.schemas.prep import Risk, TransformOp, TransformationStep
from app.services.profiling.inference import is_nullish


def strip_m_strings(source: str) -> str:
    """Remove M string literals so brackets inside them are not counted.

    Needed because the generated M legitimately contains things like
    ``Text.StartsWith(Txt0, "(")`` -- a naive bracket count reads that as an
    unmatched parenthesis. M escapes a quote by doubling it.
    """
    out: list[str] = []
    i, in_string = 0, False
    while i < len(source):
        ch = source[i]
        if in_string:
            if ch == '"':
                if i + 1 < len(source) and source[i + 1] == '"':
                    i += 2
                    continue
                in_string = False
        elif ch == '"':
            in_string = True
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# --- the op allowlist is complete and closed -----------------------------
def test_every_op_has_handler_and_m_emitter() -> None:
    """The brief's requirement that transformations be reproducible in Power
    BI only holds if every executable op also emits M."""
    from app.services.prep.executor import HANDLERS
    from app.services.prep.mquery import EMITTERS

    missing_handlers = [o.value for o in TransformOp if o not in HANDLERS]
    missing_emitters = [o.value for o in TransformOp if o not in EMITTERS]
    assert not missing_handlers, f"ops with no handler: {missing_handlers}"
    assert not missing_emitters, f"ops with no M emitter: {missing_emitters}"


def test_computed_column_rejects_an_unknown_computation() -> None:
    """No expression evaluation: an unknown 'kind' must raise, not eval."""
    from app.services.prep.executor import ExecContext, h_add_computed

    frame = pd.DataFrame({"a": [1, 2]}, dtype=object)
    with pytest.raises(ValueError, match="unsupported computation"):
        h_add_computed(
            frame, {"column": "x", "kind": "__import__('os').system('id')"},
            ExecContext({}),
        )


def test_filter_rejects_an_unknown_predicate() -> None:
    from app.services.prep.executor import ExecContext, h_filter_rows

    frame = pd.DataFrame({"a": [1, 2]}, dtype=object)
    with pytest.raises(ValueError, match="unsupported filter"):
        h_filter_rows(frame, {"column": "a", "op": "eval"}, ExecContext({}))


def test_aggregate_rejects_an_unknown_function() -> None:
    from app.services.prep.executor import ExecContext, h_aggregate

    frame = pd.DataFrame({"k": ["a"], "v": [1]}, dtype=object)
    with pytest.raises(ValueError, match="unsupported aggregation"):
        h_aggregate(
            frame, {"group_by": ["k"], "aggregations": {"v": "exec"}}, ExecContext({})
        )


# --- individual handlers -------------------------------------------------
def _ctx(frames: dict[str, pd.DataFrame] | None = None):
    from app.services.prep.executor import ExecContext
    return ExecContext(frames or {})


def test_hour_units_are_normalised_and_audited() -> None:
    from app.services.prep.executor import h_parse_number

    frame = pd.DataFrame(
        {"labour_hours": ["7.50", "8h", "480min", "07:30", "bad"]}, dtype=object
    )
    out, notes = h_parse_number(
        frame,
        {"column": "labour_hours", "unit_normalisation": "hours",
         "audit_column": "labour_hours_source_format",
         "quarantine_column": "labour_hours_unparsed"},
        _ctx(),
    )
    assert out["labour_hours"].tolist()[:4] == [7.5, 8.0, 8.0, 7.5]
    assert is_nullish(out["labour_hours"].tolist()[4]), "the unparseable value must be null"
    assert "bad" in out["labour_hours_unparsed"].tolist(), "unparseable kept, not dropped"
    assert any("unit converted" in n for n in notes)


def test_mixed_currency_is_flagged_not_summed() -> None:
    from app.services.prep.executor import h_parse_currency

    frame = pd.DataFrame(
        {"cost_amount": ["$100.00", "EUR 200.00", "(50.00)", "1.234,56"]}, dtype=object
    )
    out, notes = h_parse_currency(
        frame,
        {"column": "cost_amount", "expected_currency": "USD",
         "currency_column": "cost_amount_currency"},
        _ctx(),
    )
    assert out["cost_amount"].tolist() == [100.0, 200.0, -50.0, 1234.56]
    assert out["cost_amount_currency"].tolist()[:2] == ["USD", "EUR"]
    assert out["cost_amount_is_reporting_currency"].tolist()[1] is False
    assert any("not in the reporting currency" in n for n in notes)


def test_dates_in_several_formats_are_parsed_and_failures_quarantined() -> None:
    from app.services.prep.executor import h_parse_date

    frame = pd.DataFrame(
        {"d": ["2025-01-15", "16/01/2025", "17-Jan-2025", "January 18, 2025", "oops"]},
        dtype=object,
    )
    out, notes = h_parse_date(frame, {"column": "d"}, _ctx())
    parsed = [v for v in out["d"].tolist() if not is_nullish(v)]
    assert len(parsed) == 4, parsed
    assert "oops" in out["d_unparsed"].tolist()
    assert any("could not be parsed" in n for n in notes)


def test_identifier_normalisation_keeps_the_original() -> None:
    from app.services.prep.executor import h_normalise_identifier

    frame = pd.DataFrame({"project_id": ["PRJ-1", "prj 1", "0001"]}, dtype=object)
    out, notes = h_normalise_identifier(
        frame, {"column": "project_id"}, _ctx()
    )
    assert out["project_id"].tolist() == ["PRJ1", "PRJ1", "1"]
    assert out["project_id_original"].tolist() == ["PRJ-1", "prj 1", "0001"]
    assert any("collapsed" in n for n in notes)


def test_missing_key_is_filled_with_an_explicit_member_and_flagged() -> None:
    from app.services.prep.executor import h_fill_missing

    frame = pd.DataFrame({"project_id": ["PRJ-1", "", None]}, dtype=object)
    out, notes = h_fill_missing(
        frame,
        {"column": "project_id", "value": "UNASSIGNED",
         "flag_column": "project_id_is_missing"},
        _ctx(),
    )
    assert out["project_id"].tolist() == ["PRJ-1", "UNASSIGNED", "UNASSIGNED"]
    assert out["project_id_is_missing"].tolist() == [False, True, True]
    assert len(out) == 3, "no row may be dropped"


def test_missing_measure_is_flagged_never_imputed() -> None:
    from app.services.prep.executor import h_flag_missing

    frame = pd.DataFrame({"cost_amount": [100, None]}, dtype=object)
    out, notes = h_flag_missing(frame, {"column": "cost_amount"}, _ctx())
    assert out["cost_amount"].tolist() == [100, None], "a missing measure is not a zero"
    assert out["cost_amount_is_missing"].tolist() == [False, True]


def test_unconfirmed_category_mapping_changes_nothing() -> None:
    from app.services.prep.executor import h_map_category

    frame = pd.DataFrame({"status": ["Active", "Closed"]}, dtype=object)
    out, notes = h_map_category(frame, {"column": "status", "mapping": {}}, _ctx())
    assert out["status"].tolist() == ["Active", "Closed"]
    assert any("no value was changed" in n for n in notes)


def test_join_reports_row_fan_out() -> None:
    """The silent killer of additive measures must be reported loudly."""
    from app.services.prep.executor import h_join

    left = pd.DataFrame({"k": ["a", "b"], "v": [1, 2]}, dtype=object)
    right = pd.DataFrame({"k": ["a", "a", "b"], "attr": ["x", "y", "z"]}, dtype=object)
    out, notes = h_join(
        left, {"right_table": "r", "left_keys": ["k"], "right_keys": ["k"]},
        _ctx({"r": right}),
    )
    assert len(out) == 3
    assert any("FAN-OUT" in n for n in notes)


def test_append_reports_columns_absent_from_one_side() -> None:
    from app.services.prep.executor import h_append

    a = pd.DataFrame({"x": [1], "y": [2]}, dtype=object)
    b = pd.DataFrame({"x": [3]}, dtype=object)
    out, notes = h_append(
        pd.DataFrame(), {"tables": ["a", "b"]}, _ctx({"a": a, "b": b})
    )
    assert len(out) == 2 and "source_table" in out.columns
    assert any("null, not zero" in n for n in notes)


def test_date_table_is_contiguous() -> None:
    from app.services.prep.executor import h_build_date_table

    out, notes = h_build_date_table(
        pd.DataFrame(), {"start": "2025-01-01", "end": "2025-12-31"}, _ctx()
    )
    assert len(out) == 365
    assert out["date"].is_monotonic_increasing
    assert set(["year", "quarter", "month_number", "month_name", "date_key"]) <= set(out.columns)


def test_unknown_member_is_added_once() -> None:
    from app.services.prep.executor import h_build_dimension

    dim = pd.DataFrame({"project_id": ["PRJ-1"], "name": ["Alpha"]}, dtype=object)
    out, _ = h_build_dimension(
        dim, {"add_unknown_member": True, "key": "project_id"}, _ctx()
    )
    assert "UNASSIGNED" in out["project_id"].tolist() and len(out) == 2
    # Idempotent: running it again must not add a second UNASSIGNED row, which
    # would break the dimension's key uniqueness.
    out2, _ = h_build_dimension(
        out, {"add_unknown_member": True, "key": "project_id"}, _ctx()
    )
    assert len(out2) == 2
    assert out2["project_id"].tolist().count("UNASSIGNED") == 1


def test_unmatched_fact_keys_get_their_own_dimension_row() -> None:
    """An orphan key keeps its real code rather than being lumped into
    UNASSIGNED, so a reader can see which code is unrecognised."""
    from app.services.prep.executor import h_build_dimension

    dim = pd.DataFrame(
        {"project_id": ["PRJ-1"], "name": ["Alpha"], "status": ["Active"]}, dtype=object
    )
    fact = pd.DataFrame(
        {"project_id": ["PRJ-1", "PRJ-99", "PRJ-99", "UNASSIGNED"]}, dtype=object
    )
    out, notes = h_build_dimension(
        dim,
        {"add_unknown_member": True, "key": "project_id",
         "adopt_unmatched_from": ["facts.project_id"],
         "unmatched_label": "Not in the register"},
        _ctx({"facts": fact}),
    )
    keys = out["project_id"].tolist()
    assert "PRJ-99" in keys, "the unrecognised code must stay visible"
    assert keys.count("UNASSIGNED") == 1, (
        "a fact table already using the UNASSIGNED literal must not cause a "
        "second row, which would break key uniqueness"
    )
    assert len(keys) == len(set(keys)), f"dimension key is not unique: {keys}"
    orphan = out[out["project_id"] == "PRJ-99"].iloc[0]
    assert orphan["status"] == "Not in the register"


def test_added_dimension_rows_do_not_corrupt_typed_columns() -> None:
    """A date or numeric column must not receive a text label, and an audit
    column has no 'original' value to record."""
    import datetime as dt

    from app.services.prep.executor import h_build_dimension

    dim = pd.DataFrame({
        "project_id": ["PRJ-1"],
        "start_date": [dt.date(2025, 1, 1)],
        "budget": [1000.0],
        "project_id_original": ["PRJ-1"],
        "status": ["Active"],
    }, dtype=object)
    fact = pd.DataFrame({"project_id": ["PRJ-1", "PRJ-99"]}, dtype=object)
    out, _ = h_build_dimension(
        dim,
        {"add_unknown_member": True, "key": "project_id",
         "adopt_unmatched_from": ["facts.project_id"],
         "unmatched_label": "Not in the register"},
        _ctx({"facts": fact}),
    )
    added = out[out["project_id"] == "PRJ-99"].iloc[0]
    assert added["status"] == "Not in the register"
    assert is_nullish(added["start_date"]), "a date column must not get a text label"
    assert is_nullish(added["budget"]), "a numeric column must not get a text label"
    assert is_nullish(added["project_id_original"]), (
        "an added row has no pre-transformation value to record"
    )


def test_textual_detection_survives_a_pandas_map() -> None:
    """pandas 3 gives a column the 'str' dtype after an elementwise map, so a
    dtype == object test silently reports False for every cleaned column."""
    from app.services.prep.executor import _is_textual

    frame = pd.DataFrame({"manager": ["Tom  Okafor", "Ann Lee"]}, dtype=object)
    frame["manager"] = frame["manager"].map(lambda v: " ".join(str(v).split()))
    assert frame["manager"].dtype != object, "fixture no longer exercises the trap"
    assert _is_textual(frame["manager"])


# --- planning ------------------------------------------------------------
def test_plan_maps_inconsistent_headers_to_one_schema(client, profiled_project) -> None:
    client.post(
        f"/api/v1/projects/{profiled_project}/tasks/extract_requirements/run", json={}
    )
    r = client.post(
        f"/api/v1/projects/{profiled_project}/tasks/plan_transformations/run", json={}
    )
    assert r.json()["succeeded"], r.text
    plan = client.get(f"/api/v1/projects/{profiled_project}/artifacts/plan").json()["artifact"]

    by_table: dict[str, dict[str, str]] = {}
    for m in plan["mappings"]:
        by_table.setdefault(m["source_table"], {})[m["source_column"]] = m["canonical_name"]
    fy25 = next(k for k in by_table if k.endswith("fy25"))
    fy24 = next(k for k in by_table if k.endswith("fy24"))
    assert by_table[fy25]["Proj ID"] == by_table[fy24]["project_id"]
    assert by_table[fy25]["Cost Amount"] == by_table[fy24]["amount_usd"]


def test_no_two_columns_in_a_table_share_a_canonical_name(client, profiled_project) -> None:
    """A collision would make the rename step lose one column's data."""
    client.post(f"/api/v1/projects/{profiled_project}/tasks/extract_requirements/run", json={})
    client.post(f"/api/v1/projects/{profiled_project}/tasks/plan_transformations/run", json={})
    plan = client.get(f"/api/v1/projects/{profiled_project}/artifacts/plan").json()["artifact"]
    seen: dict[str, set[str]] = {}
    for m in plan["mappings"]:
        bucket = seen.setdefault(m["source_table"], set())
        assert m["canonical_name"] not in bucket, (
            f"{m['source_table']}: two columns both map to {m['canonical_name']}"
        )
        bucket.add(m["canonical_name"])


def test_deduplication_requires_approval(client, profiled_project) -> None:
    client.post(f"/api/v1/projects/{profiled_project}/tasks/extract_requirements/run", json={})
    client.post(f"/api/v1/projects/{profiled_project}/tasks/plan_transformations/run", json={})
    plan = client.get(f"/api/v1/projects/{profiled_project}/artifacts/plan").json()["artifact"]
    dedupes = [s for s in plan["steps"] if s["op"] == "deduplicate"]
    assert dedupes, "the duplicated row should produce a dedupe proposal"
    for step in dedupes:
        assert step["requires_approval"] and step["risk"] == "destructive"


def test_joins_are_proposed_from_measured_overlap(client, profiled_project) -> None:
    client.post(f"/api/v1/projects/{profiled_project}/tasks/extract_requirements/run", json={})
    client.post(f"/api/v1/projects/{profiled_project}/tasks/plan_transformations/run", json={})
    plan = client.get(f"/api/v1/projects/{profiled_project}/artifacts/plan").json()["artifact"]
    assert plan["join_proposals"], "a join to the register should be proposed"
    for j in plan["join_proposals"]:
        assert 0.0 < j["match_share"] <= 1.0
        assert j["evidence"], "a join must carry its measured evidence"
        assert any("matched" in e for e in j["evidence"])


def test_every_step_states_a_reason_and_expected_effect(client, profiled_project) -> None:
    client.post(f"/api/v1/projects/{profiled_project}/tasks/extract_requirements/run", json={})
    client.post(f"/api/v1/projects/{profiled_project}/tasks/plan_transformations/run", json={})
    plan = client.get(f"/api/v1/projects/{profiled_project}/artifacts/plan").json()["artifact"]
    for step in plan["steps"]:
        assert step["reason"], f"{step['op']} has no reason"
        assert step["expected_effect"], f"{step['op']} has no expected effect"


# --- execution -----------------------------------------------------------
@pytest.fixture
def executed_project(client, profiled_project) -> str:
    client.post(f"/api/v1/projects/{profiled_project}/tasks/extract_requirements/run", json={})
    client.post(f"/api/v1/projects/{profiled_project}/tasks/plan_transformations/run", json={})
    for a in client.get(f"/api/v1/projects/{profiled_project}/approvals").json()["approvals"]:
        if a["state"] == "pending":
            client.post(
                f"/api/v1/projects/{profiled_project}/approvals/{a['id']}",
                json={"approve": True, "note": "approved in test"},
            )
    r = client.post(
        f"/api/v1/projects/{profiled_project}/tasks/execute_transformations/run", json={}
    )
    assert r.json()["succeeded"], r.text
    return profiled_project


def test_execution_produces_datasets_with_roles(client, executed_project) -> None:
    ex = client.get(f"/api/v1/projects/{executed_project}/artifacts/execution").json()["artifact"]
    assert ex["succeeded"]
    by_name = {d["name"]: d for d in ex["datasets"]}
    assert "dim_date" in by_name and by_name["dim_date"]["role_hint"] == "date"
    register = next(d for n, d in by_name.items() if "register" in n)
    assert register["role_hint"] == "dimension", \
        "a table with one row per project is a dimension even though it carries a budget"


def test_unapproved_steps_are_skipped_not_applied(client, profiled_project) -> None:
    client.post(f"/api/v1/projects/{profiled_project}/tasks/extract_requirements/run", json={})
    client.post(f"/api/v1/projects/{profiled_project}/tasks/plan_transformations/run", json={})
    # Reject every gate, then execute.
    approvals = client.get(f"/api/v1/projects/{profiled_project}/approvals").json()["approvals"]
    for a in approvals:
        if a["state"] == "pending":
            client.post(
                f"/api/v1/projects/{profiled_project}/approvals/{a['id']}",
                json={"approve": False, "note": "rejected in test"},
            )
    r = client.post(
        f"/api/v1/projects/{profiled_project}/tasks/execute_transformations/run", json={}
    )
    assert r.json()["succeeded"], r.text
    ex = client.get(
        f"/api/v1/projects/{profiled_project}/artifacts/execution"
    ).json()["artifact"]
    assert ex["skipped_steps"], "rejected steps must be skipped"
    assert any("SKIPPED" in line for line in ex["log"])


def test_lineage_is_recorded_for_renamed_columns(client, executed_project) -> None:
    ex = client.get(f"/api/v1/projects/{executed_project}/artifacts/execution").json()["artifact"]
    edges = {e["target"]: e for e in ex["lineage"]}
    renamed = [
        e for e in edges.values()
        if "renamed from" in (e["transform_summary"] or "")
    ]
    assert renamed, "a rename must leave a lineage edge back to the source column"


def test_row_count_deltas_reconcile(client, executed_project) -> None:
    ex = client.get(f"/api/v1/projects/{executed_project}/artifacts/execution").json()["artifact"]
    for delta in ex["row_count_deltas"]:
        op = delta["op"]
        change = delta["rows_after"] - delta["rows_before"]
        if op in {"rename_column", "parse_date", "parse_currency", "parse_number",
                  "parse_percentage", "trim_whitespace", "normalise_identifier",
                  "fill_missing", "flag_missing"}:
            assert change == 0, f"{op} must not change the row count (changed by {change})"


def test_quality_reports_are_produced_before_and_after(client, executed_project) -> None:
    ex = client.get(f"/api/v1/projects/{executed_project}/artifacts/execution").json()["artifact"]
    assert ex["quality_before"]["phase"] == "before"
    assert ex["quality_after"]["phase"] == "after"
    assert ex["quality_before"]["table_row_counts"]
    assert ex["quality_after"]["type_conformance"]


def test_m_is_generated_in_two_layers(client, executed_project) -> None:
    """Source queries read the raw extract; dataset queries combine them, so
    no transformation is applied twice."""
    ex = client.get(f"/api/v1/projects/{executed_project}/artifacts/execution").json()["artifact"]
    queries = ex["m_queries"]
    source = next(q for name, q in queries.items() if "Source query" in q)
    assert "raw/" in source, "a source query must read the RAW extract"
    assert "Table.PromoteHeaders" in source
    dataset = [q for q in queries.values() if "Dataset query" in q]
    for q in dataset:
        assert "Csv.Document" not in q, "a dataset query must not re-read a file"


def test_generated_m_is_balanced(client, executed_project) -> None:
    ex = client.get(f"/api/v1/projects/{executed_project}/artifacts/execution").json()["artifact"]
    assert ex["m_queries"], "no M was generated"
    for name, query in ex["m_queries"].items():
        code = strip_m_strings(
            "\n".join(l for l in query.splitlines() if not l.strip().startswith("//"))
        )
        assert code.count("(") == code.count(")"), f"{name}: unbalanced parentheses"
        assert code.count("{") == code.count("}"), f"{name}: unbalanced braces"
        assert code.count("[") == code.count("]"), f"{name}: unbalanced square brackets"
        assert query.count('"') % 2 == 0, f"{name}: unbalanced quotes"
        import re as _re
        lets = len(_re.findall(r"\blet\b", code))
        ins = len(_re.findall(r"\bin\b", code))
        assert lets == ins, f"{name}: {lets} 'let' vs {ins} 'in'"
