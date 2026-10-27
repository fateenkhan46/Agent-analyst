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
# 1. backend
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload            # http://127.0.0.1:8000/docs

# 2. sample data with documented, deliberate defects
python ../samples/generate.py

# 3. a worked sample project, driven through the real HTTP API
cd ..
python scripts/seed_sample_project.py --auto-approve

# 4. the UI
cd frontend
npm install
npm run build && npm start               # http://127.0.0.1:3000
```

`--auto-approve` answers the approval gates so the seed runs unattended;
without it the pipeline stops at the first gate, which is the normal
behaviour. On the sample data the ten workflow tasks all succeed and the
figures reproduce exactly — see [`samples/EXPECTED.md`](samples/EXPECTED.md).

The browser talks only to `/api/*` on its own origin; Next proxies that to
FastAPI through a **server-side rewrite**, so neither the backend host nor any
credential is compiled into the client bundle.

### Tests

```bash
cd backend  && python -m pytest          # 276 passed, 1 skipped
cd frontend && npm run typecheck
cd frontend && npm run e2e               # drives a real browser over all 8 pages
```

The end-to-end script needs the backend, a seeded project and the built
frontend all running; it fails on a page error, a failed request, or a control
that does not do what it says.

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
frontend/
  app/          the eight pages, plus project creation and the capability register
  lib/          typed API client over the 36 endpoints, contract types, hooks
  components/   shared UI primitives and the application shell
  e2e/          browser smoke test for every page and every control
scripts/        seed the sample project through the HTTP API
samples/        generator for the sample project + expected results
docs/           architecture, integration limits, DAX subset
```

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design and the reasoning behind it
- [`docs/INTEGRATION_LIMITATIONS.md`](docs/INTEGRATION_LIMITATIONS.md) — what was probed and what follows
- [`docs/DAX_SUBSET.md`](docs/DAX_SUBSET.md) — exactly which DAX can be executed here, and what happens to the rest
- [`samples/EXPECTED.md`](samples/EXPECTED.md) — the sample project's planted defects and the figures they must reproduce
