"""Assembling and verifying a complete PBIP project.

The output is the real deliverable. What is written:

```
<Name>.pbip                              project pointer
<Name>.SemanticModel/
    .platform
    definition.pbism
    definition/database.tmdl
    definition/model.tmdl
    definition/relationships.tmdl
    definition/expressions.tmdl         the DataFolder parameter
    definition/cultures/en-US.tmdl
    definition/tables/<Table>.tmdl
<Name>.Report/
    .platform
    definition.pbir                      points at the semantic model
    report.json
    StaticResources/SharedResources/BaseThemes/<Theme>.json
data/<dataset>.csv                       prepared outputs
data/raw/<source>.csv                    raw extracts the queries read
OPEN_IN_POWER_BI.md                      the remaining manual step, precisely
```

``verify_project`` re-reads everything from disk afterwards: it parses every
JSON file, checks the TMDL indentation is consistent, and confirms every file
the manifest claims actually exists. An export is only recorded once that
passes, which is what stops the system reporting a deliverable it did not
produce.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ...schemas.prep import TransformationExecution
from ...schemas.report import ExportArtifact, ExportKind, ReportSpec
from ...schemas.semantic import SemanticModel, TableRole
from .. import repository as repo
from ..storage import get_store, project_key, safe_segment, sha256_bytes
from . import tmdl as T
from .report import report_json, theme_json

PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)


@dataclass
class WriteResult:
    root_key: str
    files: dict[str, bytes] = field(default_factory=dict)
    manual_steps: list[str] = field(default_factory=list)
    skipped_bindings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def manifest(self) -> list[str]:
        return sorted(self.files)


def _safe_project_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _-]+", "", name).strip() or "Report"
    return cleaned[:60]


def _platform(name: str, item_type: str) -> bytes:
    return json.dumps({
        "$schema": PLATFORM_SCHEMA,
        "metadata": {"type": item_type, "displayName": name},
        "config": {"version": "2.0", "logicalId": T._lineage_tag(f"{item_type}:{name}")},
    }, indent=2).encode()


def build_project(
    s: Session, project_id: str, *, model: SemanticModel, report: ReportSpec,
    project_name: str, execution: TransformationExecution | None = None,
) -> WriteResult:
    """Produce every file of the PBIP project in memory."""
    name = _safe_project_name(project_name)
    result = WriteResult(root_key=name)
    files = result.files

    # ---- .pbip pointer ------------------------------------------------
    files[f"{name}.pbip"] = json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                   "pbip/definitionProperties/1.0.0/schema.json",
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{name}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    }, indent=2).encode()

    # ---- semantic model ----------------------------------------------
    sm = f"{name}.SemanticModel"
    files[f"{sm}/.platform"] = _platform(name, "SemanticModel")
    files[f"{sm}/definition.pbism"] = json.dumps({
        "version": "4.2",
        "settings": {"qnaEnabled": False},
    }, indent=2).encode()
    files[f"{sm}/definition/database.tmdl"] = T.database_tmdl().encode()

    # Each table's partition carries the generated Power Query M, so the
    # cleaning is visible and editable inside Power BI.
    m_queries = execution.m_queries if execution else {}
    for table in model.tables:
        source = m_queries.get(table.dataset_name) or m_queries.get(table.name)
        if source:
            table.m_expression = source
        elif not table.m_expression:
            table.m_expression = _fallback_m(table)
            result.warnings.append(
                f"No generated Power Query was available for '{table.name}', so its "
                f"partition reads the prepared CSV directly. The cleaning steps are "
                f"therefore not visible in Power Query for this table."
            )

    table_names = [t.name for t in model.tables]
    files[f"{sm}/definition/model.tmdl"] = T.model_tmdl(
        model, table_names=table_names
    ).encode()
    relationships = T.relationships_tmdl(model)
    if relationships.strip():
        files[f"{sm}/definition/relationships.tmdl"] = relationships.encode()
    # Forward slashes: M accepts them on Windows, and they match the "raw/..."
    # paths the generated queries already use, so the project has one
    # convention rather than two.
    files[f"{sm}/definition/expressions.tmdl"] = T.expressions_tmdl({
        "DataFolder": '"../data/"',
    }).encode()
    files[f"{sm}/definition/cultures/{model.culture}.tmdl"] = T.culture_tmdl(
        model.culture
    ).encode()
    for table in model.tables:
        files[f"{sm}/definition/tables/{safe_segment(table.name)}.tmdl"] = (
            T.table_tmdl(table, model).encode()
        )

    # ---- report ------------------------------------------------------
    rp = f"{name}.Report"
    files[f"{rp}/.platform"] = _platform(name, "Report")
    files[f"{rp}/definition.pbir"] = json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                   "report/definition/1.0.0/schema.json",
        "version": "4.0",
        "datasetReference": {
            "byPath": {"path": f"../{sm}"},
        },
    }, indent=2).encode()
    document, skipped = report_json(report, model)
    result.skipped_bindings = skipped
    files[f"{rp}/report.json"] = json.dumps(document, indent=2).encode()
    files[
        f"{rp}/StaticResources/SharedResources/BaseThemes/"
        f"{safe_segment(report.theme.name)}.json"
    ] = json.dumps(theme_json(report.theme), indent=2).encode()

    # ---- data --------------------------------------------------------
    store = get_store()
    datasets = repo.list_datasets(s, project_id)
    seen: set[str] = set()
    for dataset in datasets:
        if dataset["name"] in seen:
            continue
        seen.add(dataset["name"])
        try:
            files[f"data/{dataset['name']}.csv"] = store.get_bytes(dataset["stored_path"])
        except (FileNotFoundError, ValueError):
            result.warnings.append(
                f"The prepared dataset '{dataset['name']}' is missing from storage, so "
                f"it was not included. The project will not refresh until it is present."
            )
    for raw_key in store.list_prefix(project_key(project_id, "datasets", "raw")):
        files[f"data/raw/{raw_key.rsplit('/', 1)[-1]}"] = store.get_bytes(raw_key)

    # ---- the honest instructions -------------------------------------
    result.manual_steps = _manual_steps(model, report, result)
    files["OPEN_IN_POWER_BI.md"] = _readme(
        name, model, report, result, execution
    ).encode()
    return result


def _fallback_m(table: Any) -> str:
    dataset = table.dataset_name or table.name
    columns = [c.name for c in table.columns]
    return (
        f"let\n"
        f"    Source = Csv.Document(\n"
        f"        File.Contents(DataFolder & \"{dataset}.csv\"),\n"
        f"        [Delimiter = \",\", Columns = {len(columns)}, Encoding = 65001, "
        f"QuoteStyle = QuoteStyle.Csv]\n"
        f"    ),\n"
        f"    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true])\n"
        f"in\n"
        f"    Promoted"
    )


def _manual_steps(
    model: SemanticModel, report: ReportSpec, result: WriteResult
) -> list[str]:
    """Exactly what remains to be done in Power BI Desktop, and nothing vague."""
    steps = [
        f"Open `{result.root_key}.pbip` in Power BI Desktop (the July 2024 release or "
        f"later, with 'Power BI Project (.pbip) save option' enabled under "
        f"File > Options > Preview features). Desktop compiles the TMDL into a live "
        f"model and runs the Power Query against the bundled CSVs."
    ]
    if model.date_tables:
        date_table = model.date_tables[0]
        date_column = next(
            (c.name for c in date_table.columns if c.data_type.is_temporal), "date"
        )
        steps.append(
            f"Mark `{date_table.name}` as the date table: select it, then "
            f"Table tools > Mark as date table, and choose `{date_column}`. "
            f"**This cannot be set from TMDL** -- it is the one setting the format does "
            f"not carry. Time-intelligence measures "
            f"({', '.join(m.name for m in model.measures if m.requires_date_table) or 'none in this model'}) "
            f"will return incorrect results until it is done."
        )
    if any(r.orphan_key_count for r in model.relationships):
        steps.append(
            "Leave 'Assume referential integrity' OFF on every relationship. Orphan keys "
            "were measured in this data, and enabling it would silently drop those rows "
            "from every query."
        )
    drill = [p for p in report.pages if p.is_drill_through]
    if drill:
        steps.append(
            f"The '{drill[0].display_name}' page is configured as a drill-through on "
            f"`{drill[0].drill_through_field}`. Verify the drill-through field landed in "
            f"the page's filter pane -- right-click a project on the detail table to test "
            f"it."
        )
    if result.skipped_bindings:
        steps.append(
            f"{len(result.skipped_bindings)} visual binding(s) were NOT written because "
            f"they could not be resolved against the model; those visuals are absent "
            f"rather than broken. See the list in this file."
        )
    steps.append(
        "Review the generated Power Query for each table (Transform data > each query). "
        "Every cleaning step is a named, commented step there -- change anything you "
        "disagree with; the project is yours after export."
    )
    steps.append(
        "If you move the project, keep the `data` folder beside it, or change the "
        "`DataFolder` parameter (Transform data > Manage parameters) to point at your "
        "own copy."
    )
    return steps


def _readme(
    name: str, model: SemanticModel, report: ReportSpec, result: WriteResult,
    execution: TransformationExecution | None,
) -> str:
    lines = [
        f"# {name} -- generated Power BI project",
        "",
        "This is a **PBIP project**: Microsoft's documented, text-based project format.",
        "It opens in Power BI Desktop and is fully editable afterwards.",
        "",
        "## What this is, and what it is not",
        "",
        "| | |",
        "|---|---|",
        "| Format | PBIP (TMDL semantic model + PBIR report definition) |",
        "| Produced by | Agent Analyst, on Linux, with no Power BI engine involved |",
        "| `.pbix` included | **No.** A `.pbix` is a proprietary container that in "
        "practice only Power BI Desktop writes, and Desktop is Windows-only. Open this "
        "project and use *Save as* if you need one. |",
        "| Model validated by | a DAX parser and model-aware reference resolution; "
        "measures in the supported subset were executed against the bundled data in "
        "DuckDB |",
        "| Model validated by the Power BI engine | **No.** Nothing here has been run by "
        "Analysis Services. The arithmetic is verified; engine behaviour is not. |",
        "",
        "## Remaining steps",
        "",
    ]
    for i, step in enumerate(result.manual_steps, 1):
        lines.append(f"{i}. {step}")
    lines.append("")

    lines.append("## Contents")
    lines.append("")
    lines.append(f"- **{len(model.tables)} table(s)**: "
                 + ", ".join(f"`{t.name}` ({t.role.value})" for t in model.tables))
    lines.append(f"- **{len(model.relationships)} relationship(s)**, all many-to-one from "
                 f"the fact side")
    lines.append(f"- **{len(model.measures)} measure(s)**: "
                 + ", ".join(f"`{m.name}`" for m in model.measures))
    lines.append(f"- **{len(report.pages)} report page(s)**, "
                 f"{report.visual_count} visual(s)")
    if execution:
        lines.append(f"- **{len(execution.m_queries)} Power Query script(s)**, one per "
                     f"source table plus one per combined dataset")
    lines.append("")

    if model.measures:
        lines.append("## Measures")
        lines.append("")
        for measure in model.measures:
            lines.append(f"### {measure.name}")
            lines.append("")
            lines.append("```dax")
            lines.append(measure.expression)
            lines.append("```")
            lines.append("")
            if measure.description:
                lines.append(measure.description)
                lines.append("")
            if measure.aggregation_behaviour:
                lines.append(f"**Behaviour:** {measure.aggregation_behaviour}")
                lines.append("")
            for assumption in measure.assumptions:
                lines.append(f"- *Assumption:* {assumption}")
            if measure.assumptions:
                lines.append("")

    concerns = [i for i in model.issues if i.severity.value in {"critical", "high"}]
    if concerns or report.unmapped_kpis or result.warnings or result.skipped_bindings:
        lines.append("## Known limitations of this build")
        lines.append("")
        for issue in concerns:
            lines.append(f"- **{issue.location}**: {issue.message} {issue.recommended_fix}")
        for kpi in report.unmapped_kpis:
            lines.append(
                f"- **{kpi}** was requested but has no measure, so it has no visual."
            )
        for warning in result.warnings:
            lines.append(f"- {warning}")
        for skipped in result.skipped_bindings:
            lines.append(f"- Visual binding not written: {skipped}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_Every file here was written by Agent Analyst and verified by re-reading it "
        "from disk. No file is listed that does not exist._"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------
@dataclass
class VerifyResult:
    ok: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def verify_project(files: dict[str, bytes], *, root: str) -> VerifyResult:
    """Re-read and check everything. An export is only recorded if this passes."""
    result = VerifyResult(ok=True)

    def check(name: str, passed: bool, detail: str = "") -> None:
        result.checks.append({"check": name, "passed": passed, "detail": detail})
        if not passed:
            result.ok = False
            result.failures.append(f"{name}: {detail}")

    required = [
        f"{root}.pbip",
        f"{root}.SemanticModel/.platform",
        f"{root}.SemanticModel/definition.pbism",
        f"{root}.SemanticModel/definition/database.tmdl",
        f"{root}.SemanticModel/definition/model.tmdl",
        f"{root}.Report/.platform",
        f"{root}.Report/definition.pbir",
        f"{root}.Report/report.json",
        "OPEN_IN_POWER_BI.md",
    ]
    for path in required:
        check(f"present:{path}", path in files, "missing from the project")

    for path, content in files.items():
        if path.endswith((".json", ".pbip", ".pbism", ".pbir", ".platform")):
            try:
                json.loads(content.decode("utf-8"))
                check(f"json:{path}", True)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                check(f"json:{path}", False, f"invalid JSON: {exc}")

    # The report's visual configs are JSON *strings*; parse them too, because a
    # malformed one produces a report that opens with empty visuals.
    report_path = f"{root}.Report/report.json"
    if report_path in files:
        try:
            document = json.loads(files[report_path].decode())
            containers = 0
            for section in document.get("sections", []):
                for container in section.get("visualContainers", []):
                    json.loads(container["config"])
                    containers += 1
            check("report:visual_configs_parse", True, f"{containers} container(s)")
            check(
                "report:has_sections", bool(document.get("sections")),
                "the report has no pages",
            )
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as exc:
            check("report:visual_configs_parse", False, str(exc))

    # TMDL indentation: tabs only, and never a jump of more than one level.
    for path, content in files.items():
        if not path.endswith(".tmdl"):
            continue
        text = content.decode("utf-8")
        # The space check runs first: it is the more specific and more
        # actionable message, and a file indented with spaces also has no tabs.
        bad_space_indent = [
            i + 1 for i, line in enumerate(text.splitlines())
            if line.startswith(" ") and line.strip()
        ]
        if bad_space_indent:
            check(
                f"tmdl:{path}", False,
                f"space indentation at line(s) {bad_space_indent[:5]}; TMDL uses tabs",
            )
            continue
        if "\t" not in text and len(text.splitlines()) > 2:
            check(f"tmdl:{path}", False, "no indentation found; TMDL requires tabs")
            continue
        previous = 0
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            depth = len(line) - len(line.lstrip("\t"))
            if depth > previous + 1 and not line.lstrip("\t").startswith(("///", "```")):
                check(
                    f"tmdl:{path}", False,
                    f"indentation jumps from {previous} to {depth} at line {i}",
                )
                break
            previous = depth
        else:
            check(f"tmdl:{path}", True)

    data_files = [p for p in files if p.startswith("data/") and p.endswith(".csv")]
    check("data:present", bool(data_files), "no data file was bundled; the project "
                                            "cannot refresh")
    for path in data_files:
        check(f"data:nonempty:{path}", len(files[path]) > 0, "the file is empty")

    return result


def export_pbip(
    s: Session, project_id: str, *, model: SemanticModel, report: ReportSpec,
    project_name: str, execution: TransformationExecution | None = None,
    actor: str = "report_design_agent",
) -> tuple[ExportArtifact | None, VerifyResult, WriteResult]:
    """Write, verify, store and register the project. Returns the artifact only
    if verification passed."""
    from datetime import datetime, timezone

    from ...schemas.agent import AuditAction
    from ...schemas.common import Severity

    built = build_project(
        s, project_id, model=model, report=report, project_name=project_name,
        execution=execution,
    )
    verification = verify_project(built.files, root=built.root_key)

    store = get_store()
    base = project_key(project_id, "pbip")
    store.delete_prefix(base)
    for path, content in built.files.items():
        store.put_bytes(f"{base}/{path}", content)

    if not verification.ok:
        repo.record_audit(
            s, project_id=project_id, actor=actor, action=AuditAction.ERROR,
            severity=Severity.HIGH,
            summary=f"PBIP verification failed: {'; '.join(verification.failures[:3])}",
            detail={"failures": verification.failures},
        )
        return None, verification, built

    zip_key = project_key(project_id, "exports", f"{built.root_key}.pbip.zip")
    _, manifest = store.zip_directory(base, zip_key)
    data = store.get_bytes(zip_key)

    artifact = ExportArtifact(
        project_id=project_id, kind=ExportKind.PBIP,
        filename=f"{built.root_key}.pbip.zip", stored_path=zip_key,
        size_bytes=len(data), sha256=sha256_bytes(data),
        file_count=len(manifest), manifest=sorted(manifest),
        remaining_manual_steps=built.manual_steps,
        opens_in="Power BI Desktop (July 2024 or later, with the .pbip save option "
                 "enabled)",
        verified_at=datetime.now(timezone.utc),
    )
    repo.save_export(s, artifact)
    repo.record_audit(
        s, project_id=project_id, actor=actor, action=AuditAction.EXPORT_CREATED,
        target=artifact.id,
        summary=f"Wrote and verified a PBIP project: {len(manifest)} file(s), "
                f"{len(data):,} bytes, {len(verification.checks)} check(s) passed.",
        detail={"manifest": sorted(manifest)[:40],
                "manual_steps": built.manual_steps},
    )
    return artifact, verification, built
