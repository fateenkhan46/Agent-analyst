"""Report design and the PBIP export -- the actual deliverable."""
from __future__ import annotations

import io
import json
import zipfile

import pytest


@pytest.fixture
def designed_project(client, profiled_project) -> str:
    r = client.post(
        f"/api/v1/projects/{profiled_project}/run",
        json={"stop_after": "design_report", "auto_approve": True},
    )
    failed = [s for s in r.json()["steps"] if not s.get("succeeded")]
    assert not failed, failed
    return profiled_project


@pytest.fixture
def exported(client, designed_project):
    """Export through the service, bypassing the QA gate which is tested
    separately."""
    from app.db import session_scope
    from app.schemas.prep import TransformationExecution
    from app.schemas.report import ReportSpec
    from app.schemas.semantic import SemanticModel
    from app.services import repository as repo
    from app.services.pbip.project import export_pbip

    with session_scope() as s:
        model = repo.load_artifact(
            s, designed_project, repo.ArtifactKind.MODEL, SemanticModel
        )
        report = repo.load_artifact(
            s, designed_project, repo.ArtifactKind.REPORT, ReportSpec
        )
        execution = repo.load_artifact(
            s, designed_project, repo.ArtifactKind.EXECUTION, TransformationExecution
        )
        artifact, verification, built = export_pbip(
            s, designed_project, model=model, report=report,
            project_name="Test Report", execution=execution,
        )
    return designed_project, artifact, verification, built


def _report(client, pid) -> dict:
    return client.get(f"/api/v1/projects/{pid}/artifacts/report").json()["artifact"]


# --- report design -------------------------------------------------------
def test_every_visual_states_its_question_and_rationale(client, designed_project) -> None:
    """No visual exists to fill space."""
    report = _report(client, designed_project)
    assert report["pages"], "no pages designed"
    for page in report["pages"]:
        assert page["purpose"], f"{page['name']} has no purpose"
        for visual in page["visuals"]:
            assert visual["business_question"], f"{visual['name']} has no question"
            assert visual["design_rationale"], f"{visual['name']} has no rationale"


def test_every_binding_resolves_against_the_model(client, designed_project) -> None:
    from app.schemas.semantic import SemanticModel

    model = SemanticModel.model_validate(
        client.get(f"/api/v1/projects/{designed_project}/artifacts/model").json()["artifact"]
    )
    report = _report(client, designed_project)
    for page in report["pages"]:
        for visual in page["visuals"]:
            for binding in visual["bindings"]:
                if binding["is_measure"]:
                    assert model.measure(binding["field"]) is not None, (
                        f"{visual['name']} binds to unknown measure {binding['field']}"
                    )
                else:
                    assert model.resolve_column(
                        binding["table"], binding["field"]
                    ) is not None, (
                        f"{visual['name']} binds to unknown column "
                        f"{binding['table']}[{binding['field']}]"
                    )


def test_no_date_table_column_is_used_as_a_project_name(client, designed_project) -> None:
    """A regression guard: 'day_name' has the 'name' role, so an unguarded
    lookup put a weekday where the project name belongs."""
    report = _report(client, designed_project)
    detail = next(
        (p for p in report["pages"] if p["name"] == "ProjectDetail"), None
    )
    if detail is None:
        pytest.skip("no detail page in this fixture")
    fields = [
        b["field"] for v in detail["visuals"] for b in v["bindings"]
    ]
    assert "day_name" not in fields and "month_name" not in fields, fields


def test_non_slicer_visuals_bind_a_measure(client, designed_project) -> None:
    report = _report(client, designed_project)
    for page in report["pages"]:
        for visual in page["visuals"]:
            if visual["visual_type"] in {"slicer", "textbox"}:
                continue
            assert any(b["is_measure"] for b in visual["bindings"]), (
                f"{visual['name']} reports no measure"
            )


def test_a_drill_through_page_is_hidden_and_keyed(client, designed_project) -> None:
    report = _report(client, designed_project)
    drill = [p for p in report["pages"] if p["is_drill_through"]]
    if not drill:
        pytest.skip("no drill-through page in this fixture")
    for page in drill:
        assert page["is_hidden"], "a drill-through page must not be navigable"
        assert page["drill_through_field"], "a drill-through page needs its key field"


def test_slicers_sit_on_dimensions_not_facts(client, designed_project) -> None:
    """A slicer on a fact column filters only that table and leaves the other
    totals silently unfiltered."""
    from app.schemas.semantic import SemanticModel, TableRole

    model = SemanticModel.model_validate(
        client.get(f"/api/v1/projects/{designed_project}/artifacts/model").json()["artifact"]
    )
    report = _report(client, designed_project)
    for page in report["pages"]:
        for visual in page["visuals"]:
            if visual["visual_type"] != "slicer":
                continue
            for binding in visual["bindings"]:
                table = model.table(binding["table"])
                assert table is not None
                assert table.role in {TableRole.DIMENSION, TableRole.DATE}, (
                    f"slicer on fact table {table.name}"
                )


def test_report_has_no_blocking_issues(client, designed_project) -> None:
    report = _report(client, designed_project)
    blocking = [i for i in report["issues"] if i["severity"] in {"critical", "high"}]
    assert not blocking, blocking


def test_unmapped_kpis_are_reported_not_hidden(client, designed_project) -> None:
    """A requested KPI with no measure must be listed, not quietly dropped."""
    report = _report(client, designed_project)
    requirements = client.get(
        f"/api/v1/projects/{designed_project}/artifacts/requirements"
    ).json()["artifact"]
    bound = {
        b["field"] for p in report["pages"] for v in p["visuals"]
        for b in v["bindings"] if b["is_measure"]
    }
    for kpi in requirements["kpis"]:
        if kpi["name"] not in bound:
            assert kpi["name"] in report["unmapped_kpis"] or not kpi["definition"], (
                f"'{kpi['name']}' has no visual and is not reported as unmapped"
            )


# --- TMDL ----------------------------------------------------------------
def test_tmdl_uses_tabs_and_consistent_nesting(exported) -> None:
    _, _, verification, built = exported
    tmdl_files = [p for p in built.files if p.endswith(".tmdl")]
    assert tmdl_files, "no TMDL was written"
    for path in tmdl_files:
        text = built.files[path].decode()
        for i, line in enumerate(text.splitlines(), 1):
            if line.strip() and line.startswith(" "):
                pytest.fail(f"{path}:{i} is indented with spaces; TMDL uses tabs")


def test_multiline_dax_is_written_as_a_tmdl_block(exported) -> None:
    """A multi-line expression must be introduced by ``= ``` `` and indented;
    inlining it produces a project Desktop refuses to open."""
    _, _, _, built = exported
    tables = {
        p: built.files[p].decode() for p in built.files
        if "/definition/tables/" in p
    }
    multiline = [
        text for text in tables.values()
        if "measure " in text and "= ```" in text
    ]
    if not multiline:
        pytest.skip("no multi-line measure in this fixture")
    for text in multiline:
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not line.rstrip().endswith("= ```"):
                continue
            opening_depth = len(line) - len(line.lstrip("\t"))
            body = lines[i + 1]
            body_depth = len(body) - len(body.lstrip("\t"))
            assert body_depth == opening_depth + 1, (
                f"a block expression body must be one level deeper: {body!r}"
            )


def test_implicit_measures_are_discouraged(exported) -> None:
    """Every measure is explicit; an implicit one that sums an id or averages
    a rate is a silent source of wrong numbers."""
    _, _, _, built = exported
    model_tmdl = next(
        built.files[p].decode() for p in built.files if p.endswith("/model.tmdl")
    )
    assert "discourageImplicitMeasures" in model_tmdl
    for path, content in built.files.items():
        if "/definition/tables/" not in path:
            continue
        for line in content.decode().splitlines():
            if line.strip().startswith("summarizeBy:"):
                assert line.strip() == "summarizeBy: none", line


def test_referential_integrity_is_not_asserted_with_orphan_keys(exported) -> None:
    """Enabling it would silently drop unmatched rows from every query."""
    pid, _, _, built = exported
    relationships = [
        built.files[p].decode() for p in built.files if p.endswith("relationships.tmdl")
    ]
    if not relationships:
        pytest.skip("no relationships file")
    from app.schemas.semantic import SemanticModel
    from app.db import session_scope
    from app.services import repository as repo
    with session_scope() as s:
        model = repo.load_artifact(s, pid, repo.ArtifactKind.MODEL, SemanticModel)
    if not any(r.orphan_key_count for r in model.relationships):
        pytest.skip("no orphan keys in this fixture")
    assert "relyOnReferentialIntegrity" not in relationships[0]


def test_every_table_has_a_power_query_partition(exported) -> None:
    _, _, _, built = exported
    for path, content in built.files.items():
        if "/definition/tables/" not in path:
            continue
        text = content.decode()
        assert "partition " in text, f"{path} has no partition"
        assert "mode: import" in text, f"{path} partition has no mode"


def test_generated_power_query_is_carried_into_the_partition(exported) -> None:
    """The point of the two-layer M: the cleaning must be visible in Power BI."""
    _, _, _, built = exported
    sources = [
        built.files[p].decode() for p in built.files
        if "/definition/tables/" in p
    ]
    assert any(
        "Table.PromoteHeaders" in text and "DataFolder" in text for text in sources
    ), "no table partition reads the bundled data through the DataFolder parameter"


# --- report.json ---------------------------------------------------------
def test_report_json_is_valid_and_visual_configs_parse(exported) -> None:
    """``config`` is a JSON *string*; a malformed one opens as an empty visual."""
    _, _, _, built = exported
    path = next(p for p in built.files if p.endswith("Report/report.json"))
    document = json.loads(built.files[path].decode())
    assert document["sections"], "the report has no pages"
    containers = 0
    for section in document["sections"]:
        json.loads(section["config"]) if "config" in section else None
        for container in section["visualContainers"]:
            config = json.loads(container["config"])
            assert "singleVisual" in config
            containers += 1
    assert containers, "no visual containers written"


def test_visual_annotations_carry_the_design_reasoning(exported) -> None:
    _, _, _, built = exported
    path = next(p for p in built.files if p.endswith("Report/report.json"))
    document = json.loads(built.files[path].decode())
    annotated = [
        c for s in document["sections"] for c in s["visualContainers"]
        if any(a["name"] == "businessQuestion" for a in c.get("annotations", []))
    ]
    assert annotated, "the design reasoning was not carried into the project"


def test_report_points_at_the_semantic_model(exported) -> None:
    _, _, _, built = exported
    path = next(p for p in built.files if p.endswith("definition.pbir"))
    document = json.loads(built.files[path].decode())
    assert document["datasetReference"]["byPath"]["path"].endswith(".SemanticModel")


# --- the archive ---------------------------------------------------------
def test_export_is_verified_before_it_is_recorded(exported) -> None:
    _, artifact, verification, _ = exported
    assert verification.ok, verification.failures
    assert artifact is not None
    assert artifact.verified_at is not None
    assert artifact.sha256 and artifact.size_bytes > 0


def test_archive_contains_everything_the_manifest_claims(client, exported) -> None:
    pid, artifact, _, _ = exported
    r = client.get(f"/api/v1/projects/{pid}/exports/{artifact.id}/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        names = set(archive.namelist())
        assert set(artifact.manifest) == names, (
            f"manifest and archive differ: "
            f"{set(artifact.manifest) ^ names}"
        )
        for name in names:
            assert archive.read(name) is not None


def test_archive_has_the_required_pbip_structure(client, exported) -> None:
    pid, artifact, _, _ = exported
    r = client.get(f"/api/v1/projects/{pid}/exports/{artifact.id}/download")
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        names = archive.namelist()
    assert any(n.endswith(".pbip") for n in names)
    assert any(n.endswith(".SemanticModel/definition/model.tmdl") for n in names)
    assert any(n.endswith(".Report/report.json") for n in names)
    assert any(n.endswith(".Report/definition.pbir") for n in names)
    assert any(n.startswith("data/") and n.endswith(".csv") for n in names)
    assert "OPEN_IN_POWER_BI.md" in names


def test_manual_steps_are_specific(exported) -> None:
    """'Open it in Desktop' is not a precise remaining step; naming the date
    table and the exact menu path is."""
    _, artifact, _, built = exported
    assert artifact is not None
    steps = " ".join(artifact.remaining_manual_steps)
    assert "Mark as date table" in steps, steps
    assert ".pbip" in steps


def test_readme_never_claims_a_pbix_or_an_engine_validation(exported) -> None:
    _, _, _, built = exported
    readme = built.files["OPEN_IN_POWER_BI.md"].decode()
    assert "`.pbix` included | **No.**" in readme
    assert "Analysis Services" in readme
    lowered = readme.lower()
    assert "validated by the power bi engine | **no.**" in lowered


def test_failed_verification_registers_no_download(client, designed_project) -> None:
    """If the project does not verify, there must be no download to click."""
    from app.db import session_scope
    from app.schemas.report import ReportSpec
    from app.schemas.semantic import SemanticModel
    from app.services import repository as repo
    from app.services.pbip import project as writer

    original = writer.verify_project

    def broken(files, *, root):  # noqa: ANN001, ANN202
        result = original(files, root=root)
        result.ok = False
        result.failures.append("injected failure")
        return result

    writer.verify_project = broken  # type: ignore[assignment]
    try:
        with session_scope() as s:
            model = repo.load_artifact(
                s, designed_project, repo.ArtifactKind.MODEL, SemanticModel
            )
            report = repo.load_artifact(
                s, designed_project, repo.ArtifactKind.REPORT, ReportSpec
            )
            artifact, verification, _ = writer.export_pbip(
                s, designed_project, model=model, report=report,
                project_name="Broken",
            )
    finally:
        writer.verify_project = original  # type: ignore[assignment]

    assert artifact is None
    assert not verification.ok
    exports = client.get(f"/api/v1/projects/{designed_project}/exports").json()["exports"]
    assert not any(e["filename"].startswith("Broken") for e in exports)


def test_verifier_catches_broken_json() -> None:
    from app.services.pbip.project import verify_project

    files = {
        "X.pbip": b"{not json",
        "X.SemanticModel/.platform": b"{}",
        "X.SemanticModel/definition.pbism": b"{}",
        "X.SemanticModel/definition/database.tmdl": b"database\n\tcompatibilityLevel: 1550\n",
        "X.SemanticModel/definition/model.tmdl": b"model Model\n\tculture: en-US\n",
        "X.Report/.platform": b"{}",
        "X.Report/definition.pbir": b"{}",
        "X.Report/report.json": b'{"sections": []}',
        "OPEN_IN_POWER_BI.md": b"# x",
        "data/x.csv": b"a,b\n1,2\n",
    }
    result = verify_project(files, root="X")
    assert not result.ok
    assert any("json:X.pbip" in f for f in result.failures)


def test_verifier_catches_space_indented_tmdl() -> None:
    from app.services.pbip.project import verify_project

    files = {
        "X.SemanticModel/definition/tables/T.tmdl":
            b"table T\n    column a\n        dataType: string\n",
    }
    result = verify_project(files, root="X")
    assert not result.ok
    assert any("space indentation" in f for f in result.failures)


def test_every_query_a_partition_references_is_defined_in_the_project(exported) -> None:
    """A partition that references an undefined query will not refresh.

    The dataset queries are deliberately two-layer: a per-source query reads
    the raw extract and cleans it, and a dataset query combines the cleaned
    sources by name (``#"project_costs_fy25_costs"``). Only the dataset layer
    becomes a table partition, so the source layer has to be written into
    expressions.tmdl as a shared query. It was not, and the exported project
    verified cleanly while being unopenable -- every file was readable, and the
    M referred to names that existed nowhere.
    """
    import re

    _pid, _artifact, _verification, built = exported
    files = {name: body.decode("utf-8", "replace") for name, body in built.files.items()}
    tmdl = "\n".join(body for name, body in files.items() if name.endswith(".tmdl"))

    # Names bound as a step inside some `let` are local; names defined by an
    # `expression` or a `table` are global. Anything referenced but neither is
    # a dangling reference.
    defined = set(re.findall(r"^expression '?([^'\n=]+?)'? =", tmdl, re.M))
    defined |= {m.strip() for m in re.findall(r"^table '?([^'\n]+?)'?$", tmdl, re.M)}

    dangling: set[str] = set()
    for name, body in files.items():
        if not name.endswith(".tmdl"):
            continue
        local = set(re.findall(r'#"([^"]+)"\s*=', body))
        for referenced in re.findall(r'#"([^"]+)"', body):
            if referenced not in local and referenced not in defined:
                dangling.add(referenced)

    assert not dangling, (
        f"the generated Power Query references {sorted(dangling)}, which nothing "
        f"in the project defines; the model would fail to refresh. Defined: "
        f"{sorted(defined)}"
    )
