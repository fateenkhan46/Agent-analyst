"""Writing the PBIR report definition.

``report.json`` describes pages and ``visualContainer`` entries. Each
container's ``config`` is a **JSON string** (not a nested object) containing the
``singleVisual`` definition -- a quirk of the format that is easy to get wrong
and produces a report Desktop opens with empty visuals.

Field bindings use the ``Measure`` / ``Column`` projection shape with a
``queryRef``. The writer refuses to emit a binding it cannot resolve against
the model, because a broken binding is worse than a missing visual: the report
opens and looks finished while showing nothing.
"""
from __future__ import annotations

import json
from typing import Any

from ...schemas.report import (
    FieldBinding, FieldRole, ReportPage, ReportSpec, Theme, VisualDefinition, VisualType,
)
from ...schemas.semantic import SemanticModel

#: Which projection bucket each role maps to, per visual type. Power BI names
#: these differently per visual, and using the wrong bucket silently drops the
#: field.
ROLE_BUCKETS: dict[VisualType, dict[FieldRole, str]] = {
    VisualType.CARD: {FieldRole.VALUES: "Values"},
    VisualType.KPI: {FieldRole.VALUES: "Indicator", FieldRole.TARGET: "TrendLine"},
    VisualType.MULTI_ROW_CARD: {FieldRole.VALUES: "Values"},
    VisualType.CLUSTERED_COLUMN: {
        FieldRole.AXIS: "Category", FieldRole.CATEGORY: "Category",
        FieldRole.VALUES: "Y", FieldRole.SERIES: "Series",
    },
    VisualType.STACKED_COLUMN: {
        FieldRole.AXIS: "Category", FieldRole.CATEGORY: "Category",
        FieldRole.VALUES: "Y", FieldRole.SERIES: "Series",
    },
    VisualType.CLUSTERED_BAR: {
        FieldRole.CATEGORY: "Category", FieldRole.AXIS: "Category",
        FieldRole.VALUES: "Y", FieldRole.SERIES: "Series",
    },
    VisualType.LINE: {
        FieldRole.AXIS: "Category", FieldRole.VALUES: "Y", FieldRole.SERIES: "Series",
    },
    VisualType.LINE_CLUSTERED_COLUMN: {
        FieldRole.AXIS: "Category", FieldRole.VALUES: "Y", FieldRole.TREND: "Y2",
        FieldRole.SERIES: "Series",
    },
    VisualType.AREA: {FieldRole.AXIS: "Category", FieldRole.VALUES: "Y"},
    VisualType.DONUT: {FieldRole.CATEGORY: "Category", FieldRole.VALUES: "Y"},
    VisualType.TABLE: {
        FieldRole.ROWS: "Values", FieldRole.VALUES: "Values",
        FieldRole.CATEGORY: "Values",
    },
    VisualType.MATRIX: {
        FieldRole.ROWS: "Rows", FieldRole.COLUMNS: "Columns", FieldRole.VALUES: "Values",
    },
    VisualType.SLICER: {FieldRole.CATEGORY: "Values", FieldRole.VALUES: "Values"},
    VisualType.GAUGE: {FieldRole.VALUES: "Y", FieldRole.TARGET: "TargetValue"},
    VisualType.SCATTER: {
        FieldRole.CATEGORY: "Category", FieldRole.VALUES: "X", FieldRole.SIZE: "Size",
    },
    VisualType.WATERFALL: {FieldRole.CATEGORY: "Category", FieldRole.VALUES: "Y"},
    VisualType.TEXT_BOX: {},
}


class UnresolvedBinding(Exception):
    """A binding that does not exist in the model. Never emitted."""


def _entity_alias(table: str) -> str:
    """The short alias Power BI uses in a query ref, e.g. 'f' for 'FactCost'."""
    letters = [c for c in table if c.isupper()] or [table[0]]
    return "".join(letters[:2]).lower() or "t"


def _projection(binding: FieldBinding, *, alias: str) -> dict[str, Any]:
    reference = f"{binding.table}.{binding.field}"
    if binding.is_measure:
        expression = {
            "Measure": {
                "Expression": {"SourceRef": {"Entity": binding.table}},
                "Property": binding.field,
            }
        }
    else:
        expression = {
            "Column": {
                "Expression": {"SourceRef": {"Entity": binding.table}},
                "Property": binding.field,
            }
        }
    projection: dict[str, Any] = {
        "queryRef": reference,
        "field": expression,
    }
    if binding.display_name and binding.display_name != binding.field:
        projection["nativeQueryRef"] = binding.display_name
    else:
        projection["nativeQueryRef"] = binding.field
    return projection


def _visual_config(
    visual: VisualDefinition, model: SemanticModel, *, page_name: str
) -> dict[str, Any]:
    buckets = ROLE_BUCKETS.get(visual.visual_type, {})
    projections: dict[str, list[dict[str, Any]]] = {}
    select: list[dict[str, Any]] = []
    sort_by: list[dict[str, Any]] = []

    for binding in visual.bindings:
        if binding.is_measure:
            if model.measure(binding.field) is None:
                raise UnresolvedBinding(
                    f"{page_name}.{visual.name}: measure '{binding.field}' is not in "
                    f"the model"
                )
        else:
            if model.resolve_column(binding.table, binding.field) is None:
                raise UnresolvedBinding(
                    f"{page_name}.{visual.name}: column "
                    f"{binding.table}[{binding.field}] is not in the model"
                )
        bucket = buckets.get(binding.role)
        if bucket is None:
            # A role this visual type has no bucket for would be dropped
            # silently by Power BI; skip it and let QA report the gap.
            continue
        alias = _entity_alias(binding.table)
        projection = _projection(binding, alias=alias)
        projections.setdefault(bucket, []).append(projection)
        select.append(projection)
        if binding.sort_direction:
            sort_by.append({
                "field": projection["field"],
                "direction": "Descending" if binding.sort_direction == "desc"
                else "Ascending",
            })

    single: dict[str, Any] = {
        "visualType": visual.visual_type.value,
        "projections": projections,
        "prototypeQuery": {
            "Version": 2,
            "From": [
                {"Name": _entity_alias(t), "Entity": t, "Type": 0}
                for t in sorted({b.table for b in visual.bindings if b.table})
            ],
            "Select": select,
            **({"OrderBy": [
                {
                    "Direction": 2 if s["direction"] == "Descending" else 1,
                    "Expression": s["field"],
                }
                for s in sort_by
            ]} if sort_by else {}),
        },
        "drillFilterOtherVisuals": True,
    }

    objects: dict[str, Any] = {}
    if visual.title:
        objects["title"] = [{
            "properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "text": {"expr": {"Literal": {"Value": f"'{_escape(visual.title)}'"}}},
                "fontSize": {"expr": {"Literal": {"Value": "12D"}}},
            }
        }]
    if visual.visual_type is VisualType.TEXT_BOX:
        # A text box carries its content in general.paragraphs, not a query.
        single["objects"] = {
            "general": [{
                "properties": {
                    "paragraphs": [
                        {
                            "textRuns": [
                                {"value": line or " "}
                            ]
                        }
                        for line in (visual.subtitle or visual.title).split("\n")
                    ]
                }
            }]
        }
        single["projections"] = {}
        single["prototypeQuery"] = {"Version": 2, "From": [], "Select": []}
    else:
        if visual.show_data_labels:
            objects["labels"] = [{
                "properties": {"show": {"expr": {"Literal": {"Value": "true"}}}}
            }]
        if not visual.show_legend:
            objects["legend"] = [{
                "properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}
            }]
        if objects:
            single["objects"] = objects

    config: dict[str, Any] = {
        "name": visual.id.replace("vis_", "")[:20],
        "layouts": [{
            "id": 0,
            "position": {
                "x": visual.layout.x, "y": visual.layout.y,
                "z": visual.layout.z, "width": visual.layout.width,
                "height": visual.layout.height, "tabOrder": visual.layout.z,
            },
        }],
        "singleVisual": single,
    }
    return config


def _escape(text: str) -> str:
    return text.replace("'", "''")


def visual_container(
    visual: VisualDefinition, model: SemanticModel, *, page_name: str
) -> dict[str, Any]:
    config = _visual_config(visual, model, page_name=page_name)
    container: dict[str, Any] = {
        "x": visual.layout.x, "y": visual.layout.y, "z": visual.layout.z,
        "width": visual.layout.width, "height": visual.layout.height,
        "tabOrder": visual.layout.z,
        # The quirk: config is a JSON *string*, not an object.
        "config": json.dumps(config, separators=(",", ":")),
    }
    annotations = {
        "businessQuestion": visual.business_question,
        "designRationale": visual.design_rationale,
        "generatedBy": "Agent Analyst",
    }
    container["annotations"] = [
        {"name": name, "value": value}
        for name, value in annotations.items() if value
    ]
    if visual.tooltip_fields:
        container["tooltipConfig"] = json.dumps(
            {"fields": [f"{b.table}.{b.field}" for b in visual.tooltip_fields]},
            separators=(",", ":"),
        )
    return container


def page_json(
    page: ReportPage, model: SemanticModel, *, skipped: list[str]
) -> dict[str, Any]:
    containers: list[dict[str, Any]] = []
    for visual in page.visuals:
        try:
            containers.append(visual_container(visual, model, page_name=page.name))
        except UnresolvedBinding as exc:
            # Refuse rather than emit a visual that renders empty.
            skipped.append(str(exc))
    out: dict[str, Any] = {
        "name": page.name,
        "displayName": page.display_name,
        "displayOption": 1,
        "width": page.width,
        "height": page.height,
        "ordinal": page.ordinal,
        "visualContainers": containers,
    }
    config: dict[str, Any] = {}
    if page.is_drill_through and page.drill_through_field:
        table, _, column = page.drill_through_field.partition("[")
        column = column.rstrip("]")
        config["relationships"] = {}
        out["filters"] = json.dumps([
            {
                "name": "drillThrough",
                "expression": {
                    "Column": {
                        "Expression": {"SourceRef": {"Entity": table}},
                        "Property": column,
                    }
                },
                "type": "Passthrough",
                "howCreated": 2,
            }
        ], separators=(",", ":"))
        config["type"] = "DrillThrough"
    if page.is_hidden:
        config["visibility"] = 1
    if page.page_filters:
        out.setdefault("filters", json.dumps([
            {
                "name": f"filter_{i}",
                "expression": {
                    "Column": {
                        "Expression": {"SourceRef": {"Entity": f.table}},
                        "Property": f.field,
                    }
                },
                "filter": {"values": f.values},
                "type": "Categorical",
            }
            for i, f in enumerate(page.page_filters)
        ], separators=(",", ":")))
    if config:
        out["config"] = json.dumps(config, separators=(",", ":"))
    return out


def report_json(report: ReportSpec, model: SemanticModel) -> tuple[dict[str, Any], list[str]]:
    """The whole report definition, plus any binding we refused to emit."""
    skipped: list[str] = []
    sections = [
        page_json(page, model, skipped=skipped)
        for page in sorted(report.pages, key=lambda p: p.ordinal)
    ]
    document: dict[str, Any] = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                   "report/definition/report/1.0.0/schema.json",
        "config": json.dumps({
            "version": "5.55",
            "themeCollection": {
                "customTheme": {
                    "name": report.theme.name,
                    "reportVersionAtImport": "5.55",
                    "type": 2,
                }
            },
            "activeSectionIndex": 0,
            "defaultDrillFilterOtherVisuals": True,
            "settings": {
                "useStylableVisualContainerHeader": True,
                "useNewFilterPaneExperience": True,
            },
        }, separators=(",", ":")),
        "layoutOptimization": 0,
        "resourcePackages": [{
            "resourcePackage": {
                "disabled": False,
                "items": [{
                    "name": report.theme.name,
                    "path": f"BaseThemes/{report.theme.name}.json",
                    "type": 202,
                }],
                "name": "SharedResources",
                "type": 2,
            }
        }],
        "sections": sections,
    }
    return document, skipped


def theme_json(theme: Theme) -> dict[str, Any]:
    """A Power BI theme file."""
    return {
        "name": theme.name,
        "dataColors": theme.data_colors,
        "foreground": theme.foreground,
        "background": theme.background,
        "backgroundLight": theme.background_light,
        "tableAccent": theme.table_accent,
        "good": theme.positive_color,
        "bad": theme.negative_color,
        "neutral": theme.neutral_color,
        "textClasses": {
            "title": {"fontFace": theme.font_family, "fontSize": 14,
                      "color": theme.foreground},
            "header": {"fontFace": theme.font_family, "fontSize": 12,
                       "color": theme.foreground},
            "label": {"fontFace": theme.font_family, "fontSize": 10,
                      "color": theme.foreground},
            "callout": {"fontFace": theme.font_family, "fontSize": 28,
                        "color": theme.table_accent},
        },
        "visualStyles": {
            "*": {
                "*": {
                    "background": [{"show": True, "transparency": 0}],
                    "border": [{"show": True, "radius": 6}],
                    "title": [{"show": True, "fontSize": 12,
                               "fontColor": {"solid": {"color": theme.foreground}}}],
                }
            },
            "card": {
                "*": {
                    "labels": [{"fontSize": 24, "color":
                                {"solid": {"color": theme.table_accent}}}],
                    "categoryLabels": [{"show": True, "fontSize": 10}],
                }
            },
        },
    }
