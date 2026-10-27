/**
 * Browser smoke test for the eight UI pages.
 *
 * It drives a real Chromium against a running backend and frontend, so a page
 * that renders an object as a React child, loses a chunk, or leaves a button
 * wired to a stale closure fails here rather than in front of a user. Both of
 * those were real defects this script caught.
 *
 *   # terminal 1
 *   cd backend && python -m uvicorn app.main:app --port 8000
 *   # terminal 2
 *   python scripts/seed_sample_project.py --auto-approve
 *   # terminal 3
 *   cd frontend && npm run build && npm start
 *   # then
 *   node e2e/smoke.mjs                      # discovers the sample project
 *   node e2e/smoke.mjs proj_xxxxxxxx        # or name one
 *
 * Requires `playwright` to be resolvable and a Chromium available to it.
 */
import { createRequire } from "node:module";

// `playwright` is usually installed outside this package (globally, or in a
// tools directory on NODE_PATH), so resolve it the way CommonJS would rather
// than requiring it as a dependency of the app itself.
const require_ = createRequire(import.meta.url);
let chromium;
try {
  ({ chromium } = require_("playwright"));
} catch {
  console.error(
    "playwright is not resolvable. Install it (npm i -D playwright && npx playwright install chromium)\n" +
    "or run this script with NODE_PATH pointing at an installation that has Chromium.",
  );
  process.exit(2);
}

const BASE = process.env.FRONTEND_URL ?? "http://127.0.0.1:3000";
const SHOTS = process.env.SHOT_DIR ?? "";

let failures = 0;
function check(name, ok, detail = "") {
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures += 1;
}

async function resolveProjectId(page, argv) {
  if (argv[0]) return argv[0];
  const res = await page.request.get(`${BASE}/api/v1/projects`);
  if (!res.ok()) throw new Error(`Cannot list projects: ${res.status()}`);
  const { projects } = await res.json();
  if (!projects.length) {
    throw new Error(
      "No project exists. Run scripts/seed_sample_project.py --auto-approve first.",
    );
  }
  // The most complete project makes the strongest smoke test.
  projects.sort((a, b) => b.progress - a.progress);
  return projects[0].id;
}

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1480, height: 1000 } });
const page = await ctx.newPage();

const pageErrors = [];
const badResponses = [];
page.on("pageerror", (e) => pageErrors.push(e.message));
page.on("console", (m) => {
  if (m.type() === "error") pageErrors.push(`console: ${m.text()}`);
});
page.on("response", (r) => {
  // Next prefetches route payloads on idle and aborts them on navigation;
  // those cancellations are not failures.
  if (r.status() >= 400 && !r.url().includes("_rsc=")) {
    badResponses.push(`${r.status()} ${r.url()}`);
  }
});

const P = await resolveProjectId(page, process.argv.slice(2));
console.log(`project under test: ${P}\n`);

async function visit(name, path, expected) {
  pageErrors.length = 0;
  badResponses.length = 0;
  await page.goto(`${BASE}${path}`, { waitUntil: "networkidle", timeout: 60_000 });
  await page.waitForTimeout(1200);
  const body = await page.innerText("body");
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
  check(`${name} renders`, body.includes(expected) && !/Loading /.test(body),
    body.includes(expected) ? "" : `missing “${expected}”`);
  check(`${name} has no page errors`, pageErrors.length === 0, pageErrors.slice(0, 2).join(" || "));
  check(`${name} has no failed requests`, badResponses.length === 0, badResponses.slice(0, 2).join(" || "));
  return body;
}

// -- the eight pages the brief names, plus the system and create pages -------
await visit("dashboard", "/", "Projects");
await visit("system", "/system", "Capability register");
await visit("new-project", "/projects/new", "Meeting transcript");
await visit("workspace", `/projects/${P}`, "Workflow");
await visit("data-explorer", `/projects/${P}/data`, "Measured key overlaps");
await visit("data-model", `/projects/${P}/model`, "Relationships");
await visit("dax-studio", `/projects/${P}/dax`, "Validation cases");
await visit("report-designer", `/projects/${P}/report`, "All visuals");
await visit("qa", `/projects/${P}/qa`, "Validation results");
await visit("delivery", `/projects/${P}/delivery`, "Exports");
await visit("audit", `/projects/${P}/audit`, "Audit trail");

console.log("");

// -- the controls have to do the work, not report it -------------------------
await page.goto(`${BASE}/projects/${P}/dax`, { waitUntil: "networkidle" });
await page.waitForTimeout(800);
const evaluateBtn = page.getByRole("button", { name: "Run", exact: true });
await evaluateBtn.click();
await page.waitForTimeout(2500);
let body = await page.innerText("body");
check("DAX evaluate returns a number from DuckDB",
  /executed[\s\S]*duckdb/i.test(body) && /SQL executed/.test(body));

const expression = page.locator("textarea").first();
await expression.fill("SUM(FactProjectCosts[no_such_column])");
await page.getByRole("button", { name: /Validate & save/ }).click();
await page.waitForTimeout(2500);
body = await page.innerText("body");
check("an expression naming a missing column is refused",
  /nothing was saved/i.test(body) && /no column named/.test(body));
check("a refused expression can still be force-saved deliberately",
  /Save anyway/.test(body));

await page.goto(`${BASE}/projects/${P}/qa`, { waitUntil: "networkidle" });
await page.waitForTimeout(900);
const everything = (await page.innerText("body")).length;
const notSupported = page.getByRole("button", { name: /^not supported \(\d+\)$/ });
if (await notSupported.count()) {
  await notSupported.click();
  await page.waitForTimeout(500);
  check("the QA status filter narrows the result table",
    (await page.innerText("body")).length < everything);
}
check("QA never counts an unexecuted check as a pass",
  /Never counted as a pass/.test(await page.innerText("body")));

await page.goto(`${BASE}/projects/${P}/model`, { waitUntil: "networkidle" });
await page.waitForTimeout(1200);
check("the model diagram is drawn from server-side positions",
  (await page.locator('svg[aria-label="Semantic model diagram"]').count()) === 1);

await page.goto(`${BASE}/projects/${P}/report`, { waitUntil: "networkidle" });
await page.waitForTimeout(1200);
const visuals = page.locator('div[style*="aspect-ratio"] button');
const visualCount = await visuals.count();
check("the report canvas places real visuals", visualCount > 0, `${visualCount} visuals`);
if (visualCount) {
  await visuals.first().click();
  await page.waitForTimeout(400);
  check("a visual exposes its field bindings", /Field bindings/.test(await page.innerText("body")));
}

await page.goto(`${BASE}/projects/${P}/delivery`, { waitUntil: "networkidle" });
await page.waitForTimeout(1200);
const href = await page.locator("a", { hasText: "Download" }).first().getAttribute("href");
check("a download link is offered for the verified export", Boolean(href), href ?? "");
if (href) {
  const res = await page.request.get(`${BASE}${href}`);
  const buf = await res.body();
  check("the download is a real zip archive",
    res.status() === 200 && buf.subarray(0, 2).toString() === "PK",
    `${res.status()}, ${buf.length} bytes`);
}
const minutes = await page.request.get(`${BASE}/api/v1/projects/${P}/minutes.md`);
const markdown = await minutes.text();
check("the minutes of meeting download as Markdown",
  minutes.status() === 200 && markdown.includes("#"), `${markdown.length} chars`);

await page.goto(`${BASE}/projects/${P}/audit`, { waitUntil: "networkidle" });
await page.waitForTimeout(1200);
const execution = page.locator("li button", { hasText: /_agent/ }).first();
await execution.click();
await page.waitForTimeout(500);
const audit = await page.innerText("body");
check("an execution expands to its tool calls and log", /tool calls|agent log/i.test(audit));
check("the QA agent is shown making no model calls", /no LLM calls/.test(audit));

await browser.close();
console.log(failures === 0 ? "\nall checks passed" : `\n${failures} check(s) failed`);
process.exit(failures ? 1 : 0);
