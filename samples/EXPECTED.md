# The sample project: planted defects and expected results

`generate.py` writes a reproducible sample (seeded, so the output is identical
every run). Every defect below is **deliberate**, and this file is the ground
truth the pipeline's findings are checked against.

```bash
python samples/generate.py
```

## The sources

| File | Shape | Role |
|---|---|---|
| `project_costs.xlsx` | 2 visible sheets + 1 hidden, title row above the header | cost ledger (fact) |
| `labour_hours.csv` | 138 rows | timesheets (fact) |
| `project_register.csv` | 10 rows | project register (dimension) |
| `reporting_brief.md` | — | the written brief |
| `requirements_workshop_transcript.txt` | 131 lines, 4 speakers | the stakeholder meeting |

## Planted defects, and what should happen

| # | Defect | Where | Expected handling |
|---|---|---|---|
| 1 | Five different date formats in one column, including `dd/mm` **and** `mm/dd` | `FY25 Costs.Cost Date` | `column.conflicting_date_orders` (**critical**): the column provably contains both orders. Each detected format is parsed separately; unparseable values are quarantined, never nulled. |
| 2 | Six currency/number formats: `$1,234.56`, `1,234.56`, `(1,234.56)`, `1.234,56`, `1,234.56 USD` | `FY25 Costs.Cost Amount` | `column.inconsistent_format`; all parse to the same numeric value. |
| 3 | `PRJ-1099` appears in costs but not in the register | cost ledger | `keys.unmatched_identifiers` (**critical**). Gets its **own** dimension row labelled "Not in the register" — the code stays visible, and totals still reconcile. |
| 4 | An exact duplicate cost row | `FY25 Costs` | `table.duplicate_rows`. De-duplication is **proposed**, never applied without approval. |
| 5 | A negative cost `(4,250.00)` and a blank amount | `FY25 Costs` | Parsed to `-4250.0`; the blank is **flagged, not imputed** — a missing measure is not a zero. |
| 6 | One row in EUR among USD | `PRJ-1007` subcontract | `column.multiple_currencies` (**critical**). `Total Cost` filters to the reporting currency, so the EUR 31,500 is **excluded** — which is what the stakeholder demands in the transcript. |
| 7 | The FY24 sheet uses entirely different headers | `FY24 Costs` | Mapped to the same canonical names, then **appended** (not joined) into one fact table. |
| 8 | Hours in three units: `7.5`, `8h`, `450min`, `07:30` | `labour_hours.hours_worked` | `column.mixed_types`; all normalised to decimal hours, with a `_source_format` audit column recording what changed. |
| 9 | `TS-5000` appears twice with different hours | `labour_hours` | `column.duplicate_key_values` (**high**) — a *conflicting record*, reported for adjudication, never auto-resolved. |
| 10 | A timesheet with no project | `labour_hours` | Filled with an explicit `UNASSIGNED` member and flagged, so the hours stay in the total. |
| 11 | `prj 1003` written in a different style | `labour_hours` | Normalised to match `PRJ-1003`. **All** keys in the join's connected component are normalised together. |
| 12 | `PRJ-1009` in the register with no cost and no labour | register | `Average Cost per Project` divides by a `DISTINCTCOUNT` of projects **in the cost table**, so an inactive project does not drag the average down. |
| 13 | `"Tom  Okafor"` with a double space | register | Whitespace normalised; the distinct manager count falls from 5 to 4. |
| 14 | Status vocabularies disagree (`Active/Closed` vs `In Flight/Complete/On Hold/Cancelled`) | costs vs register | `keys.conflicting_category_values`. **No mapping is applied** until the correspondence is confirmed. The transcript settles it: the register is the system of record. |

## Expected pipeline output

### Profiling
4 tables (2 workbook sheets + 2 CSVs; the hidden sheet is skipped), 350 source
rows, header found on **row 3** of the FY25 sheet.

### Minutes of meeting
4 attendees with roles and measured speaking turns; ~15 decisions with
transcript line citations; ~12 action items (owners and due dates where
stated, unassigned ones flagged); open questions; 5 risks.

The decisions that must be captured, because the downstream measures depend on
them:

- *"Budget Variance is Budget minus Actual Cost. Positive means under budget."*
- *"The variance percentage is variance divided by budget, not divided by actual."*
- *"Show it as blank rather than infinity."*
- *"Everything becomes hours."*
- *"The register is the system of record for status."*
- *"Only projects that have actual cost."*
- *"We report in USD only... don't just add it to the dollars."*

### Semantic model
A **galaxy schema**: two fact tables at different grains sharing conformed
dimensions, never related to each other.

| Table | Role | Rows |
|---|---|---|
| `FactProjectCosts` | fact | 202 |
| `FactLabourHours` | fact | 138 |
| `DimProjectRegister` | dimension | 11 |
| `DimCostCategory` | dimension | 6 |
| `DimDate` | date | 1,461 |

5 relationships, all many-to-one from the fact side, all keys 100% matched
after normalisation.

### Measures (evaluated against the real data)

| Measure | Value | Check |
|---|---|---|
| Total Cost | 4,668,426.51 | USD only; the EUR row is excluded |
| Total Budget | 5,088,000.00 | sum of the 9 register budgets |
| Budget Variance | 419,573.49 | = Budget − Cost |
| Budget Variance % | 0.0825 | = Variance ÷ Budget, via `DIVIDE` |
| Total Labour Hours | 3,505.12 | all units normalised |
| Average Cost per Project | 518,714.06 | = Cost ÷ 9 projects **with cost** |
| Project Completion % | 0.57 | average of the register |
| Year-to-Date Cost | 1,281,350.62 | cumulative within the year in context |

Every figure is cross-checked by QA against a sum computed directly from the
prepared CSV, independently of the DAX.

### QA
**72 checks, 0 failures.** Two checks report `not_supported` (a cost-category
filter cannot reach the budget or labour facts — there is no relationship
path, which is correct), and four are skipped with stated reasons.

### Export
A 27-file PBIP project passing all 38 verification checks.

## Reproducing

```bash
cd backend
python -m pytest tests/test_qa_orchestration.py -q   # drives the whole pipeline
```

The assertions in `tests/` encode the table above, so a regression in any of
it fails a named test rather than being noticed later.
