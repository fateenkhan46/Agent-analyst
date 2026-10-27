# Agent Analyst — an AI Power BI development workforce

A multi-agent system that takes inconsistent Excel/CSV exports, a reporting
brief and a stakeholder **meeting transcript**, and produces a validated,
**editable Power BI project** (PBIP/TMDL) — along with minutes of meeting, a
requirements specification, a transformation plan with lineage, a semantic
model, a DAX measure catalogue, a report definition, a QA report and
improvement recommendations.

It does the work. It is not a chatbot that explains how to use Power BI.

## What it actually does, and what it cannot

This deployment reports its own capabilities at
`GET /api/v1/system/capabilities`, and the UI reads that endpoint rather than
making claims of its own. In short, on a Linux host:

| | |
|---|---|
| ✅ **Produced** | PBIP project (TMDL semantic model + PBIR report), Power Query M, DAX measures, MoM, profiles, lineage, QA report |
| ✅ **Validated** | by a real DAX parser, by model-aware reference resolution, and by **executing the supported DAX subset against the real data in DuckDB** |
| ❌ **Not possible here** | `.pbix` generation, TOM/AMO model editing, Analysis Services execution, Power BI service publish (needs credentials) |

No result is ever labelled engine-validated, and no file is reported as
generated unless it exists on disk. See
[`docs/INTEGRATION_LIMITATIONS.md`](docs/INTEGRATION_LIMITATIONS.md) for the
probed evidence behind every row above.

## Quick start

```bash
# backend
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload            # http://127.0.0.1:8000/docs

# sample data with documented, deliberate defects
python ../samples/generate.py

# tests
python -m pytest
```

No API key is required. Without `ANTHROPIC_API_KEY` the system runs its
deterministic **heuristic planner**, and every artifact it produces is stamped
`origin: heuristic` so you always know what produced what. Setting the key
switches the reasoning steps to Claude; the deterministic steps (parsing,
profiling, transformation, validation) never change.

## Layout

```
backend/app/
  schemas/      typed contracts for every artifact and inter-agent message
  services/     deterministic engines: profiling, prep, semantic, dax, pbip, qa
  agents/       the nine agents, each with a declared tool allowlist and policy
  orchestrator/ task dependency graph, approval gates, retries
  api/          FastAPI routers
samples/        generator for the sample project + expected results
docs/           architecture, integration limits, DAX subset
```

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design and the reasoning behind it
- [`docs/INTEGRATION_LIMITATIONS.md`](docs/INTEGRATION_LIMITATIONS.md) — what was probed and what follows
- [`docs/SETUP.md`](docs/SETUP.md) — configuration and deployment
