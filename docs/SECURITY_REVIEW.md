# Security review pack

For an internal security, privacy and architecture review ahead of deploying
Agent Analyst to a corporate population.

**Status: not yet approved for deployment.** This document states what the
system does, what it stores, what it sends where, and the gaps that remain —
including the ones that are a policy decision rather than an engineering task.
Nothing here should be read as a claim of compliance with any particular
policy; it is the evidence a reviewer needs to make that judgement.

Every statement below was checked against the code, not recalled. Where
something is untested, it says so.

---

## 1. What the system is

An internal web application that takes spreadsheet and CSV exports plus a
meeting transcript, and produces an editable Power BI project (PBIP/TMDL),
minutes of meeting, a documented data model and a QA report.

It is a **data processing and authoring tool**. It does not connect to source
systems, does not write to any system of record, and does not publish to the
Power BI service. Users upload files and download a project archive.

### Trust boundaries

```
  Browser ──TLS──> Auth proxy ──> Next.js (server) ──> FastAPI ──> PostgreSQL
                     (SSO)         rewrites /api            │
                                                            ├──> Object store
                                                            │    (volume or S3)
                                                            └──> DuckDB
                                                                 (in-process,
                                                                  read-only)
```

Only the auth proxy is exposed. The API and front end sit on an internal
network. No component calls out to the internet **unless an LLM key is
configured** — see §5.

---

## 2. Authentication and authorisation

| Control | Implementation | Evidence |
|---|---|---|
| Password storage | PBKDF2-HMAC-SHA256, 310,000 rounds, 16-byte per-user salt | `services/auth.py` |
| Session tokens | HMAC-SHA256 over a compact payload, 12h default TTL, constant-time compare | `services/auth.py` |
| Corporate SSO | Identity taken from headers set by an authenticating proxy (Entra ID / Okta / Ping via EasyAuth, ALB OIDC or oauth2-proxy). Accounts provision on first sign-in; role follows directory group membership | `principal_from_proxy` |
| Self-registration | **Closed by default.** Returns 403 and audits the attempt | `api/auth.py` |
| Tenant isolation | Cross-tenant access returns **404, not 403**, so the API does not confirm that another tenant's resources exist | `authorise_project` |
| Project authorisation | Owner, or `admin`/`owner` role. Every denial audited | `authorise_project` |
| Enumeration resistance | Registration and sign-in return the same message whether or not the account exists | tested |

### The SSO assumption, stated plainly

`TRUST_PROXY_AUTH=true` makes the application trust `x-auth-request-email`.
**If anything can reach the API container directly, that caller can set the
header and become any user, including an administrator.** The control is
network isolation, and it must be enforced by a private network, an internal
ingress or a mesh policy — not by convention. The setting is off by default and
the deployment notes repeat this.

Direct OIDC/SAML in the application is **not implemented**. The proxy pattern
was chosen because it works with any identity provider and keeps token
handling out of this codebase.

---

## 3. Input handling

| Control | Implementation |
|---|---|
| Upload validation | Magic-byte check, extension/content agreement, NUL-byte rejection in text types, size cap (64 MB default), per-project file cap |
| Original preservation | Source files are stored byte-for-byte and never modified; transformations write new datasets |
| Code execution | **No generated code is ever executed.** Transformations are a closed 26-value enum; anything outside it is refused at the executor |
| DAX execution | Parsed by a hand-written lexer/parser, resolved against the model, then executed as SQL aggregates over DuckDB. Constructs outside the supported subset are reported `not_supported`, never run |
| Agent capability | Each agent has a hard tool allowlist; a call outside it is denied at the tool belt |
| Error handling | Unhandled exceptions return a correlation ID only; the detail goes to the log |
| Spreadsheet formulas | openpyxl reads values, not formulas; no macro-enabled content is executed |

### Not implemented

- **Antivirus / malware scanning on upload.** Files are type-checked but not
  scanned. If policy requires scanning, put a scanner in front of the upload
  endpoint or scan the object store on write.
- **Content Disarm and Reconstruction** for Office documents.

---

## 4. Data at rest and in transit

| Item | Where it lives | Notes |
|---|---|---|
| Uploaded source files | Object store (volume or S3) under a per-project prefix | Retained until the project is deleted |
| Prepared datasets | Same store, `datasets/` prefix | Derived; regenerable |
| Exported PBIP archives | Same store, `exports/` prefix | Contains the business data as CSV |
| Projects, users, tasks, artifacts | PostgreSQL | Password hashes only, never plaintext |
| Audit events | PostgreSQL, append-only | See §6 |

**Encryption at rest is the platform's responsibility** — the application does
not encrypt what it writes. Use encrypted volumes, S3 SSE or Azure Storage
encryption, and an encrypted managed database. TLS is terminated at the proxy;
the application does not serve TLS itself.

**Deletion**: deleting a project removes its database rows and its entire
object-store prefix. There is no soft delete and no retention schedule — if
policy requires one, it is not implemented.

---

## 5. The third-party processing decision

**This is the item most likely to need a policy decision.**

Today the system makes **zero external calls**. With no `ANTHROPIC_API_KEY`
set, a deterministic planner runs in place of the model and refuses what it
cannot derive rather than guessing. The end-to-end sample run cost $0.00 and
sent nothing outside the container.

Setting `ANTHROPIC_API_KEY` sends the following to Anthropic's API:

- Column names and sample values from the profiled sources
- The meeting transcript text, in full
- Generated artifacts (requirements, model design, measures) for refinement

That is a third-party processor relationship requiring, at minimum: a data
processing agreement, a data-residency determination, and a classification
check on what users will upload. Production logs a warning naming this at boot
when a key is present, so the choice appears in the deployment record.

**The cost of leaving it off**: requirements extraction cannot infer KPI
definitions from prose. KPIs must be stated explicitly on the project rather
than derived from the brief and transcript. Everything else — profiling,
transformation, modelling, DAX generation, validation, export — is
deterministic and unaffected.

If an internally-approved LLM gateway exists, pointing at it instead is a small
change to the provider abstraction and avoids the question entirely.

---

## 6. Audit and logging

Every significant action is recorded: project creation, file upload and
rejection, file reads, profiling, agent start/finish/failure, every LLM call,
every tool call, code generation, approvals and their decider, exports and
downloads, sign-ins and **every denial**.

A denial is written through `record_denial`, which commits on the request's own
connection before the handler aborts. This matters: a denial raises, the
request transaction rolls back, and an audit row written normally inside it
would be **discarded**. Until this review every refused access — failed
sign-ins, cross-tenant attempts — left no trace. It is now tested.

### Not implemented

- **Log shipping.** Audit events live in the application database. There is no
  export to a SIEM. If policy requires central collection, add a forwarder or
  query the `/audit` endpoint on a schedule.
- **Retention and immutability.** Rows are append-only by convention; nothing
  prevents a database administrator altering them.
- **Alerting.** Nothing raises an alert on a burst of denials.

---

## 7. Availability and resource limits

| Control | State |
|---|---|
| Request rate limiting | In-process sliding window, per client, tighter on sign-in. **Per worker**: 4 uvicorn workers means 4× the configured limit per replica. Real throttling belongs at the gateway |
| Upload size and count caps | Enforced |
| Row cap on profiling | 1,000,000 per sheet, reported rather than silently truncated |
| Per-agent timeout, retries, cost budget | Enforced per agent and per project |
| Container hardening | Non-root user, read-only root filesystem, `no-new-privileges`, all capabilities dropped, CPU/memory limits |
| **Isolated execution worker** | **NOT IMPLEMENTED — see §8** |

---

## 8. Open gaps, with severity

| # | Gap | Severity | Notes |
|---|---|---|---|
| 1 | **No isolated execution worker.** `WORKER_TIMEOUT_SECONDS`, `WORKER_MAX_MEMORY_MB` and `WORKER_CPU_SECONDS` exist in config and are referenced nowhere. Data transformation runs in-process in the API | **High** | Mitigated in practice: the operation allowlist means no generated code runs, so this is a resource-exhaustion risk (a pathological file consuming API memory), not a code-execution one. Still an explicit requirement that was never built |
| 2 | No antivirus scanning of uploads | **High** if users may upload arbitrary files | Type validation only |
| 3 | No log shipping to a SIEM | **Medium** | Audit data exists; collection does not |
| 4 | No automated dependency / container vulnerability scanning in CI | **Medium** | No CI pipeline exists at all |
| 5 | Rate limiting is per worker and per replica | **Medium** | Put the enforceable limit at the gateway |
| 6 | No data retention or deletion schedule | **Medium** | Projects persist until deleted manually |
| 7 | Encryption at rest delegated to the platform | **Low** if the platform provides it | Must be confirmed, not assumed |
| 8 | No penetration test | **Unknown** | The tenant-isolation and authorisation paths are unit-tested but have never been adversarially tested |
| 9 | Direct OIDC/SAML not implemented | **Low** | The proxy pattern covers it; noted so nobody expects otherwise |
| 10 | Dockerfiles and compose topology are **unbuilt** | **Medium** | Docker was unavailable in the environment where they were written. They are structurally correct and reference files that exist, but they have never been built or run. Build and smoke-test before any deployment |

---

## 9. What the boot guard enforces

With `ENVIRONMENT=production`, the application audits its own configuration and
**refuses to start** rather than serving traffic while open. Blocking:

- `AUTH_SECRET` missing or shorter than 32 characters
- `ALLOW_DEV_AUTH` still true
- `ALLOW_OPEN_REGISTRATION` still true
- `DATABASE_URL` not PostgreSQL
- `CORS_ORIGINS` containing localhost or a wildcard

Warning (starts, but logged): HSTS not configured; an LLM key present.

A misconfigured deployment therefore fails its container health check instead
of coming up wide open. This is tested.

---

## 10. Recommended review sequence

1. **Decide the LLM question (§5)** — it changes the data-flow diagram and the
   processor inventory, so everything else depends on it.
2. **Confirm the network isolation the SSO assumption rests on (§2).**
3. **Classify what users will upload.** Project cost and resourcing data was
   the design case; if more sensitive classes are in scope, gaps 2, 3 and 6
   become blocking.
4. **Close gap 1** (isolated worker) or accept the resource-exhaustion risk in
   writing.
5. **Build and smoke-test the containers** (gap 10), then penetration-test the
   authorisation paths (gap 8).

---

*Prepared from the code at commit time. Re-check §8 against the repository
before relying on it — gaps close, and this document does not update itself.*
