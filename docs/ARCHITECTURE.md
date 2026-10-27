# Architecture

## The shape of the thing

Nine agent roles, coordinated by an orchestrator over a task dependency graph,
sitting on top of deterministic services that do the actual computation.

```
                        ┌─────────────────┐
                        │  Orchestrator   │  task DAG, approval gates,
                        │                 │  retries, budgets, rework
                        └────────┬────────┘
            ┌────────────────────┼────────────────────┐
            ▼                    ▼                    ▼
   ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
   │ Profiling       │  │ Requirements    │  │ Preparation     │
   │ (deterministic) │  │ + MoM           │  │ plan / execute  │
   └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
            └────────────────────┼────────────────────┘
                                 ▼
   ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
   │ Semantic model  │─▶│ DAX             │─▶│ Report design   │
   └─────────────────┘  └─────────────────┘  └────────┬────────┘
                                 │                    ▼
                        ┌────────▼────────┐  ┌─────────────────┐
                        │ QA (no LLM)     │─▶│ PBIP export     │
                        └────────┬────────┘  └─────────────────┘
                                 ▼
                        ┌─────────────────┐
                        │ Improvement     │
                        └─────────────────┘
```

## The division of labour that matters

**The LLM reasons. Deterministic code computes.** This is not a stylistic
preference; it is what makes the output checkable.

| Done by code, always | Done by the LLM, when configured |
|---|---|
| Parsing files, inferring types, measuring key overlap | Naming things, writing explanations |
| Executing transformations | Interpreting an unusual KPI from the brief |
| Building the model from measured uniqueness | Improving a step title or a table description |
| Generating and validating DAX | Proposing extra improvement ideas |
| Every QA verdict | — |

An agent's LLM call can only alter *prose and naming*. It cannot change a
transformation parameter, a relationship, a measure's expression or a QA
result. The `TransformOp` allowlist is the hard boundary: a plan is a list of
typed operations, and the executor dispatches on the enum. There is no path by
which generated text becomes executed code.

Without `ANTHROPIC_API_KEY` the whole pipeline still runs, on a deterministic
heuristic planner. It raises `NoHeuristicAvailable` rather than fabricating
when it has no rule, and every artifact it produces is stamped
`origin: heuristic`.

## Contracts

Every artifact and every inter-agent message is a Pydantic model under
`app/schemas`, with `extra="forbid"`. The orchestrator validates both ends of
every agent call, so a drifting output fails at the boundary instead of
corrupting downstream state. JSON Schema for all of them is served at
`GET /api/v1/system/contracts`.

Some invariants live in the contracts themselves rather than in the code that
builds them, which means they cannot be forgotten:

- a `TransformationStep` with a destructive op cannot be constructed without
  `requires_approval`;
- a `RelationshipDefinition` that is not high-confidence, or whose "one" side
  is not unique, is automatically gated;
- `ValidationReport.passed` counts a skipped or unsupported check as *not* a
  pass.

## Artifact versioning

Nothing is updated in place. `save_artifact` writes version N+1 and moves
`is_current`, so the model and report have real history and the audit trail
cannot be rewritten. (The `artifacts` table therefore needs a row id distinct
from the artifact's own id — sharing them collides on the second save.)

## Orchestration

`TASK_DEPENDENCIES` is a static DAG over ten task kinds. The orchestrator:

- refuses a task whose dependencies have not succeeded;
- blocks on pending approval gates (`APPROVAL_GATES`);
- assembles each agent's input from the persisted artifacts — agents never read
  each other's internals, which is what makes this a coordinated workflow
  rather than nine chatbots sharing a database;
- classifies failures and retries only the retryable ones (a logic bug is not
  retried: rerunning it burns budget to reproduce the same bug);
- enforces per-agent and per-project spend budgets;
- turns QA's `ReworkRequest` records into real tasks and re-blocks everything
  downstream, so stale results are never treated as current.

## QA is deliberately independent

It uses **no language model at all**, and recomputes every verdict from the
real data rather than trusting any agent's self-report. A reviewer whose
verdict could be talked out of a failure is not a reviewer.

`ValidationResult.engine` always records what actually ran, and
`validation_scope_note` states plainly what could *not* be checked. A green
report that quietly omitted half its checks would be worse than a red one.

The check that earns its keep is **double counting**: for each additive
measure, the sum over every value of a dimension must equal the unsliced
total. Slices adding to *more* means a relationship is fanning out; adding to
*less* means rows are being dropped by keys that do not match. Both are
invisible in any single number. During development this check caught four real
defects that every other test passed through:

1. identifier normalisation applied to one side of a join but not the other
   (the key-overlap *measurement* normalises both sides internally, so it
   still reported a perfect match while the executed data no longer joined);
2. normalisation decided per join rather than per connected component, so
   fixing one pair broke another;
3. a `fill_missing` sentinel written into a join key, creating an orphan the
   planner had not anticipated — the step meant to keep those rows visible was
   what dropped them;
4. a pandas 3 dtype subtlety that left the label off exactly the dimension
   rows that exist to carry it.

## What the environment forces

No .NET, no Power BI Desktop, no Analysis Services — see
[`INTEGRATION_LIMITATIONS.md`](INTEGRATION_LIMITATIONS.md). Three consequences
shaped the design:

- **PBIP, not PBIX.** TMDL and PBIR are text, so they can be written and
  verified. `.pbix` is not produced and never claimed.
- **DAX validated in three gates**, the third being execution against the real
  data via a DAX→DuckDB interpreter over a documented subset
  ([`DAX_SUBSET.md`](DAX_SUBSET.md)). Anything outside it is reported
  `not_supported`, never passed.
- **Capabilities are probed at runtime** (`services/capabilities.py`) and the
  UI reads that endpoint, so it cannot advertise something the backend cannot
  do.

## Why the DAX evaluator is an interpreter, not a transpiler

`DIVIDE([Total Budget] - [Total Cost], [Total Budget])` has budget on the
project dimension and cost on the fact table. Transpiling the whole expression
into one SQL statement over a join would multiply each project's budget by its
number of cost rows — the exact double-counting this system exists to catch.

DAX does not work that way: each measure is evaluated independently in the
current filter context and the arithmetic then combines scalars. So the
evaluator is a tree-walking interpreter whose *leaves* are SQL aggregates,
each against its own base table, joined only where a filter requires it.

## Layout

```
backend/app/
  schemas/      typed contracts (the source of truth for every artifact shape)
  services/
    profiling/  readers, type/format inference, key discovery
    prep/       planner, executor (closed op allowlist), M generator, quality
    semantic/   star-schema designer and model validation
    dax/        lexer, parser, function catalogue, references, evaluator, runtime
    report/     report designer
    pbip/       TMDL writer, PBIR writer, project assembler and verifier
    qa/         deterministic checks, runner, improvement analyser
  agents/       the agents, each with a tool allowlist and an execution policy
  orchestrator/ task graph and engine
  api/          FastAPI routers
frontend/
  app/          one route per page; every page is a client component that
                reads the API directly, so what is on screen is backend state
  lib/          api.ts (typed client), types.ts (contract mirrors), hooks.ts
  components/   ui.tsx (primitives), shell.tsx, nav.tsx
  e2e/          browser smoke test
```

## The frontend

### It reads state, it does not keep its own

Every page is a client component that fetches from the API on mount and after
any mutation. There is no client-side store holding a parallel copy of the
project, because a second copy is a second source of truth and the whole point
of the backend's artifact versioning is that there is exactly one. The cost is
a round trip after each action; the benefit is that the screen cannot drift
from what was persisted.

The same reasoning governs what is displayed. The capability register, the QA
verdict, the export's presence on disk, the diagram's node positions — all are
computed by the backend and rendered as received. A page never derives
"validated" or "available" for itself, so it cannot claim something the
backend would not.

### 404 is a state, not an error

`useAsync` separates `missing` from `error`: a 404 from an artifact endpoint
means the agent that produces it has not run, and the page says so and offers
the button that runs it. Any other failure is shown as a failure. Conflating
the two would let a broken backend look like an empty project.

### No credential reaches the browser

The client only ever requests relative `/api/*` URLs. `next.config.mjs`
rewrites those to the backend **server-side**, so the backend's address is not
in the bundle and no `NEXT_PUBLIC_*` variable carries anything. The auth token,
where the deployment requires one, lives in `sessionStorage` and is attached
per request.

### Mutations report what happened

Every action button is wired to `useAction`, which carries its own pending and
error state, and the page renders the backend's actual response — including
its refusals. Saving an invalid DAX expression shows the failing gate and the
reason nothing was stored, rather than a success toast. (`useAction` keeps the
action in a ref rather than a `useCallback` dependency list: a frozen closure
there silently re-submitted the form's mount-time state, which is how an
edited measure became a no-op save of the original.)
