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
./run.sh --sample
```

One command. It checks your Python and Node versions, installs what is
missing, generates the sample data, builds the UI, starts both services and
loads a worked project, then prints a link. `./run.sh --stop` stops it.

Everything runs on your machine: a local API, a SQLite file and an uploads
folder under `./data`. **No data leaves the machine** — see below.

<details>
<summary>Running the pieces by hand</summary>

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload            # http://127.0.0.1:8000/docs

python ../samples/generate.py            # sample data with planted defects
cd .. && python scripts/seed_sample_project.py --auto-approve

cd frontend && npm install && npm run build && npm start
```
</details>

## Your data stays on your machine

With no `ANTHROPIC_API_KEY` configured, this system makes **no network calls at
all**. Not a request, not a DNS lookup. That is not a promise in a document —
`backend/tests/test_no_egress.py` runs the entire pipeline on the sample data
with every outbound socket and DNS lookup intercepted, and fails if a single
byte would leave the machine. A second test deliberately attempts an outbound
call to prove the guard actually fires.

Setting an API key changes that: uploaded files and meeting transcripts are
then sent to a third-party API. Nothing does so by default, and a production
deployment logs a warning naming it at boot when a key is present.

### Tests
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
