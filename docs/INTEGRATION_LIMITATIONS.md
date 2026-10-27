# Integration Limitations — verified environment inspection

Recorded: 2026-10-27. Host: Linux 6.18.44 x86_64, 4 vCPU, 16 GB RAM.
Everything below was **probed, not assumed**. Re-run `python backend/scripts/probe_env.py`
to regenerate this on a different machine.

## Probe results

| Capability | Probe | Result | Consequence for this project |
|---|---|---|---|
| Python | `python3 --version` | 3.13.17 | Backend language. |
| Node / npm | `node --version` | v22.22.0 / npm 10.9.4 | Next.js frontend is viable. |
| pandas / numpy / openpyxl | import | 3.0.5 / 2.5.3 / 3.1.5 | Deterministic profiling + `.xlsx` read/write available. |
| `xlrd` | import | **absent** | Legacy `.xls` cannot be parsed unless `xlrd` is installed. The uploader therefore *rejects* `.xls` with an explicit message rather than silently failing. |
| DuckDB | import | installed on demand (see `requirements.txt`) | Used as the **DAX evaluation engine** for the supported subset (see below). |
| PostgreSQL server | `pg_isready` | binary present (`psql`), **server not running** | Default persistence is SQLite (`DATABASE_URL` unset). Postgres is fully supported by setting `DATABASE_URL=postgresql+psycopg://…`; the schema is written to work on both. |
| .NET SDK / runtime | `which dotnet` | **absent** | **No TOM / AMO / `Microsoft.AnalysisServices.Tabular`.** Programmatic model editing through the Tabular Object Model is impossible here. |
| `pbi-tools` | `which pbi-tools` | **absent** | No PBIX compile/decompile. |
| Power BI Desktop | n/a (Linux) | **impossible** | Power BI Desktop is Windows-only. No `.pbix` can be authored, opened, refreshed or rendered on this host. |
| Power BI REST API credentials | env scan for `POWERBI_*`, `AZURE_*` | **absent** | No workspace publish, no XMLA endpoint, no dataset refresh. The REST adapter is implemented but reports `not_configured` until credentials are supplied. |
| LLM API key | env scan for `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | **absent** | The LLM layer runs the `heuristic` provider by default — a deterministic, rule-based planner, **explicitly labelled as such in every API response and in the UI**. Set `ANTHROPIC_API_KEY` to switch to the real `anthropic` provider. |

## What this means for the deliverable

### Power BI artifacts: what is actually produced

`.pbix` is a proprietary, partly binary container that in practice only Power BI Desktop
writes. **This system does not and cannot produce a `.pbix` on Linux**, and never claims to.

What it *does* produce is a **PBIP project** — Microsoft's documented, text-based,
source-controllable project format. A PBIP project is exactly what Power BI Desktop opens
when you double-click a `.pbip` file, and it is fully editable afterwards. The generated
tree is:

```
<Project>.pbip                        # project pointer file
<Project>.SemanticModel/
  .platform
  definition.pbism
  definition/
    database.tmdl
    model.tmdl                        # culture, annotations, relationships
    tables/<Table>.tmdl               # columns, measures, partitions (Power Query M)
    relationships.tmdl
    expressions.tmdl                  # shared M expressions / parameters
<Project>.Report/
  .platform
  definition.pbir                     # points at the semantic model
  report.json                         # pages, visual containers, field bindings
  StaticResources/SharedResources/BaseThemes/<theme>.json
```

TMDL and the PBIR/report JSON are **text formats we can write correctly and validate
structurally**. That is why PBIP is the primary deliverable rather than a screenshot or an
HTML dashboard.

**The precise remaining manual step** is documented per-export in
`OPEN_IN_POWER_BI.md`, which is written into every generated project:
open the `.pbip` in Power BI Desktop, which compiles the TMDL into a live model, runs the
Power Query M against the data sources, and renders the report. Nothing in the generated
project requires hand-editing to work; Desktop's own load step is the only thing we cannot
perform here.

### DAX validation: what "validated" means

There is no Analysis Services engine on Linux, so **no measure is ever reported as
"engine-validated"**. Instead, DAX goes through three independent, honestly-labelled gates:

1. **`syntax`** — a real hand-written DAX lexer + recursive-descent parser
   (`backend/app/services/dax/`). Rejects malformed expressions, unbalanced constructs and
   bad argument arity.
2. **`references`** — model-aware name resolution against the approved semantic model.
   Catches references to tables/columns/measures that do not exist, columns used without
   aggregation in a measure, and unknown or fabricated DAX functions (checked against a
   function catalogue with arity and category metadata).
3. **`evaluated`** — for the **supported DAX subset only**, the measure is transpiled to
   SQL and executed against the real transformed data in DuckDB, producing an actual
   number that QA compares against an independently-computed expected value.

The supported subset is enumerated in `docs/DAX_SUBSET.md`. A measure outside it is marked
`evaluation: unsupported_construct` with the reason — never silently "passed".
`ValidationResult.engine` always records which gate produced the verdict.

## Adapter status summary (surfaced at `GET /api/v1/system/capabilities`)

| Adapter | Status here | Becomes available when |
|---|---|---|
| `PBIPWriter` | **implemented, tested** | — |
| `PowerQueryMGenerator` | **implemented, tested** | — |
| `DaxParser` / `DaxReferenceValidator` | **implemented, tested** | — |
| `DuckDbDaxEvaluator` | **implemented, tested** (subset) | — |
| `LlmProvider=heuristic` | **implemented, dev mode — labelled** | — |
| `LlmProvider=anthropic` | implemented, inactive | `ANTHROPIC_API_KEY` set |
| `PowerBiRestAdapter` | implemented, `not_configured` | `POWERBI_*` credentials set |
| `TomModelAdapter` | **not implemented — unavailable** | .NET + Windows/AS client libs |
| `PbixCompiler` | **not implemented — unavailable** | Power BI Desktop or `pbi-tools` on Windows |

No adapter reporting `not_configured` or `unavailable` is ever counted as a success by the
QA agent, and the delivery page prints this table verbatim.
