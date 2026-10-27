"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api, loadImprovements, loadValidation, optional } from "@/lib/api";
import { DASH, num, when } from "@/lib/format";
import { useAction, useAsync } from "@/lib/hooks";
import {
  Badge, Button, Card, Empty, ErrorBox, Grid, Loading, Notice, PageHeader, Prov,
  SeverityBadge, Stat, Table, Td, TestStatusBadge,
} from "@/components/ui";
import type { ImprovementRecommendation, TestStatus, ValidationResult } from "@/lib/types";

const ORDER: TestStatus[] = ["fail", "error", "warn", "not_supported", "skipped", "pass"];

export default function QaPage() {
  const { id } = useParams<{ id: string }>();
  const validation = useAsync(() => optional(loadValidation(id)), [id]);
  const improvements = useAsync(() => optional(loadImprovements(id)), [id]);
  const [filter, setFilter] = useState<TestStatus | "all">("all");
  const [category, setCategory] = useState<string>("all");

  const runQa = useAction(async () => {
    const res = await api.runTask(id, "run_qa");
    validation.reload();
    return res;
  });

  if (validation.loading) return <Loading what="the QA report" />;

  if (!validation.data) {
    return (
      <>
        <PageHeader
          title="QA & Recommendations"
          actions={
            <Button variant="primary" pending={runQa.pending} onClick={() => void runQa.run()}>
              Run QA
            </Button>
          }
        />
        <Empty title="QA has not run for this project yet">
          The QA agent is deterministic — it makes no model calls at all. It reconciles row counts
          against the originals, re-checks every measure reference, looks for double counting by
          comparing a total against the sum of its slices, and verifies that any exported file
          really exists on disk.
        </Empty>
        {runQa.error && <div className="mt-3"><ErrorBox message={runQa.error} /></div>}
      </>
    );
  }

  const report = validation.data.artifact;
  const results = report.results;
  const counts = ORDER.reduce<Record<string, number>>((acc, s) => {
    acc[s] = results.filter((r) => r.status === s).length;
    return acc;
  }, {});
  const blocking = results.filter(
    (r) => (r.status === "fail" || r.status === "error") &&
      (r.severity === "critical" || r.severity === "high"),
  );
  const categories = Array.from(new Set(results.map((r) => r.category))).sort();

  const shown = results.filter(
    (r) => (filter === "all" || r.status === filter) && (category === "all" || r.category === category),
  );

  // "Passed" is only true when nothing blocking failed. Skipped and
  // not-supported are never counted as passes.
  const passed = blocking.length === 0;

  return (
    <>
      <PageHeader
        title="QA & Recommendations"
        description={report.validation_scope_note}
        actions={
          <>
            <span className="text-xs text-muted">{when(report.generated_at)}</span>
            <Button pending={runQa.pending} onClick={() => void runQa.run()}>Re-run QA</Button>
          </>
        }
      />

      <Grid cols={4}>
        <Stat
          label="Overall"
          value={passed ? "Passed" : "Failing"}
          tone={passed ? "ok" : "bad"}
          hint={`${num(blocking.length)} blocking failure${blocking.length === 1 ? "" : "s"}`}
        />
        <Stat label="Checks run" value={num(results.length)} hint={`${num(categories.length)} categories`} />
        <Stat
          label="Not supported"
          value={num(counts.not_supported + counts.skipped)}
          tone={counts.not_supported + counts.skipped ? "warn" : "neutral"}
          hint="Never counted as a pass"
        />
        <Stat
          label="Rework requests"
          value={num(report.rework_requests.length)}
          tone={report.rework_requests.length ? "warn" : "ok"}
          hint="Raised back to the owning agent"
        />
      </Grid>

      {!passed && (
        <div className="mt-4">
          <Notice tone="warn" title="This project has blocking QA failures">
            A blocking failure means a check of critical or high severity did not pass. Nothing
            downstream treats the model or the report as validated while these stand.
          </Notice>
        </div>
      )}

      {(counts.not_supported > 0 || counts.skipped > 0) && (
        <div className="mt-4">
          <Notice tone="info" title="Checks that could not be executed here">
            {num(counts.not_supported)} check(s) involve DAX outside the executable subset and{" "}
            {num(counts.skipped)} were skipped. They are reported as unexecuted, not as passes —
            validate those in Power BI Desktop after opening the exported project. See the{" "}
            <Link href="/system" className="font-medium underline">capability register</Link> for
            what this installation can and cannot execute.
          </Notice>
        </div>
      )}

      {report.rework_requests.length > 0 && (
        <div className="mt-4">
          <Card title="Rework requests" subtitle="QA routes a failure back to the agent that owns it, with instructions." dense>
            <ul className="divide-y divide-paper-line">
              {report.rework_requests.map((r) => (
                <li key={r.id} className="px-4 py-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <SeverityBadge severity={r.severity} />
                    <Badge tone="brand">{r.target_agent}</Badge>
                  </div>
                  <p className="mt-1 text-sm text-ink">{r.reason}</p>
                  {r.instructions.length > 0 && (
                    <ul className="mt-1 list-inside list-disc text-xs text-muted">
                      {r.instructions.map((ins, i) => <li key={i}>{ins}</li>)}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          </Card>
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-1.5">
        <FilterChip label={`All (${results.length})`} active={filter === "all"} onClick={() => setFilter("all")} />
        {ORDER.filter((s) => counts[s] > 0).map((s) => (
          <FilterChip
            key={s}
            label={`${s.replace("_", " ")} (${counts[s]})`}
            active={filter === s}
            onClick={() => setFilter(s)}
          />
        ))}
        <span className="mx-2 h-4 w-px bg-paper-line" />
        <select
          className="rounded-md border border-paper-line bg-white px-2 py-1 text-xs"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
        >
          <option value="all">All categories</option>
          {categories.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </div>

      <div className="mt-3">
        <Card
          title="Validation results"
          subtitle="Each row names the engine that produced the verdict, so a parser check is never mistaken for an executed one."
          dense
        >
          {shown.length === 0 ? (
            <div className="p-4"><Empty title="No checks match this filter" /></div>
          ) : (
            <Table head={["Check", "Target", "Status", "Expected", "Actual", "Engine", "Owner", "Fix"]}>
              {shown.map((r) => <ResultRow key={r.id} result={r} />)}
            </Table>
          )}
        </Card>
      </div>

      <div className="mt-4">
        <Prov provenance={report.provenance} />
      </div>

      <div className="mt-4">
        <Card
          title="Improvement recommendations"
          subtitle="Each recommendation states its trade-offs. Approving one records the decision — it does not silently rewrite the approved model."
          dense
        >
          {improvements.loading && <div className="p-4"><Loading what="recommendations" /></div>}
          {improvements.data === null && !improvements.loading && (
            <div className="p-4">
              <Empty title="The improvement agent has not run yet">
                Run <span className="font-mono">suggest_improvements</span> from the{" "}
                <Link href={`/projects/${id}`} className="text-brand hover:underline">workspace</Link>.
              </Empty>
            </div>
          )}
          {improvements.data && (
            <>
              {improvements.data.artifact.summary && (
                <p className="border-b border-paper-line px-4 py-2.5 text-xs text-muted">
                  {improvements.data.artifact.summary}
                </p>
              )}
              <ul className="divide-y divide-paper-line">
                {improvements.data.artifact.recommendations.map((rec) => (
                  <RecommendationRow
                    key={rec.id}
                    projectId={id}
                    rec={rec}
                    onDecided={() => improvements.reload()}
                  />
                ))}
              </ul>
            </>
          )}
        </Card>
      </div>
    </>
  );
}

function FilterChip({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`rounded-md border px-2.5 py-1 text-xs capitalize transition ${
        active
          ? "border-brand bg-brand-light font-medium text-brand-dark"
          : "border-paper-line bg-white text-ink hover:bg-paper-soft"
      }`}
    >
      {label}
    </button>
  );
}

function ResultRow({ result: r }: { result: ValidationResult }) {
  const bad = r.status === "fail" || r.status === "error";
  return (
    <tr className={bad ? "bg-red-50/40" : undefined}>
      <Td>
        <p className="font-medium text-ink">{r.test_name}</p>
        <p className="font-mono text-[10px] text-muted">{r.category}</p>
      </Td>
      <Td mono className="max-w-[12rem] truncate text-[11px]" >{r.target || DASH}</Td>
      <Td>
        <TestStatusBadge status={r.status} />
        {bad && <div className="mt-0.5"><SeverityBadge severity={r.severity} /></div>}
      </Td>
      <Td className="max-w-xs text-muted">{r.expected || DASH}</Td>
      <Td className="max-w-xs">{r.actual || DASH}</Td>
      <Td mono className="text-[10px] text-muted">{r.engine || DASH}</Td>
      <Td className="text-[11px] text-muted">{r.responsible_agent || DASH}</Td>
      <Td className="max-w-sm text-muted">
        {r.recommended_fix || r.skipped_reason || DASH}
      </Td>
    </tr>
  );
}

function RecommendationRow({
  projectId, rec, onDecided,
}: { projectId: string; rec: ImprovementRecommendation; onDecided: () => void }) {
  const decide = useAction(async (approve: boolean) => {
    const res = await api.decideImprovement(projectId, rec.id, approve);
    onDecided();
    return res;
  });
  const decided = rec.state === "approved" || rec.state === "rejected";

  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <SeverityBadge severity={rec.severity} />
        <Badge tone="muted">{rec.area.replace(/_/g, " ")}</Badge>
        <Badge tone="neutral">effort: {rec.effort}</Badge>
        {decided && <Badge tone={rec.state === "approved" ? "ok" : "bad"}>{rec.state}</Badge>}
      </div>
      <p className="mt-1.5 text-sm font-medium text-ink">{rec.title}</p>
      <p className="mt-0.5 text-xs text-muted">{rec.rationale}</p>

      <div className="mt-2 grid grid-cols-1 gap-x-6 gap-y-1.5 text-xs sm:grid-cols-2">
        <div>
          <p className="font-medium text-ink">Expected benefit</p>
          <p className="text-muted">
            {rec.expected_benefit}
            {rec.quantified_benefit && <> — <span className="font-mono">{rec.quantified_benefit}</span></>}
          </p>
        </div>
        <div>
          <p className="font-medium text-ink">Trade-offs</p>
          {rec.trade_offs.length ? (
            <ul className="list-inside list-disc text-muted">
              {rec.trade_offs.map((t, i) => <li key={i}>{t}</li>)}
            </ul>
          ) : (
            <p className="text-muted">None stated.</p>
          )}
        </div>
      </div>

      {rec.evidence.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-[11px] text-muted">Evidence</summary>
          <ul className="mt-1 list-inside list-disc text-[11px] text-muted">
            {rec.evidence.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        </details>
      )}
      {rec.affected_objects.length > 0 && (
        <p className="mt-1 font-mono text-[10px] text-muted">
          affects: {rec.affected_objects.join(", ")}
        </p>
      )}

      {!decided && (
        <div className="mt-2 flex items-center gap-2">
          <Button variant="primary" pending={decide.pending} onClick={() => void decide.run(true)}>
            Approve
          </Button>
          <Button pending={decide.pending} onClick={() => void decide.run(false)}>Reject</Button>
          <span className="text-[11px] text-muted">
            Approving records the decision; apply it by re-running the owning agent.
          </span>
          {decide.error && <span className="text-xs text-bad">{decide.error}</span>}
        </div>
      )}
    </li>
  );
}
