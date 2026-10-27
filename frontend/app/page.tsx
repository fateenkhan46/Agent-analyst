"use client";

import Link from "next/link";
import { api } from "@/lib/api";
import { ago, duration, money, num, percent } from "@/lib/format";
import { useAsync } from "@/lib/hooks";
import {
  Badge, Card, Empty, ErrorBox, Grid, LinkButton, Loading, Notice, PageHeader,
  ProgressBar, SeverityBadge, Stat, StatusBadge, Table, Td,
} from "@/components/ui";

export default function DashboardPage() {
  const { data, error, loading, reload } = useAsync(() => api.dashboard(), []);

  if (loading) return <Loading what="the dashboard" />;
  if (error) {
    return (
      <>
        <PageHeader title="Dashboard" />
        <ErrorBox title="The dashboard could not be loaded" message={error} />
      </>
    );
  }
  if (!data) return null;

  const { projects, recent_executions, validation_failures, spend, environment } = data;
  const activeProjects = projects.length;
  const qaFailing = projects.filter((p) => p.qa_passed === false).length;

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Every number here is read from the backend. A project shows QA as passed only once a validation report exists in which no blocking check failed."
        actions={
          <>
            <LinkButton href="/projects/new" variant="primary">New project</LinkButton>
            <LinkButton href="/system">Capabilities</LinkButton>
          </>
        }
      />

      <Grid cols={4}>
        <Stat label="Projects" value={num(data.project_count)} hint={`${activeProjects} shown`} />
        <Stat
          label="QA failing"
          value={num(qaFailing)}
          tone={qaFailing ? "bad" : "ok"}
          hint={`${validation_failures.length} blocking check${validation_failures.length === 1 ? "" : "s"}`}
        />
        <Stat
          label="Failed agent runs"
          value={num(data.failed_executions)}
          tone={data.failed_executions ? "warn" : "ok"}
          hint="Across all projects"
        />
        <Stat
          label="LLM spend"
          value={money(spend.total_usd)}
          hint={spend.note || "Metered per agent execution"}
        />
      </Grid>

      {environment.llm_is_development_mode && (
        <div className="mt-4">
          <Notice tone="warn" title="Running without a live model">
            No <code className="font-mono">ANTHROPIC_API_KEY</code> is configured, so the{" "}
            <strong>{environment.llm_provider_active}</strong> provider is active. It derives what
            it can deterministically from the profiled data and refuses the rest rather than
            inventing it — so some agents will report work as unavailable instead of producing a
            plausible-looking guess. Spend therefore reads {money(spend.total_usd)}.
          </Notice>
        </div>
      )}

      <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <Card
            title="Projects"
            subtitle="Pipeline progress is the share of the ten workflow tasks that have succeeded."
            actions={<button onClick={reload} className="text-xs text-brand hover:underline">Refresh</button>}
            dense
          >
            {projects.length === 0 ? (
              <div className="p-4">
                <Empty title="No projects yet">
                  Create a project, upload your data files and a meeting transcript, then run the
                  pipeline. The agents will profile the sources, write the minutes, prepare the
                  data, design a model, generate DAX and export an editable Power BI project.
                </Empty>
              </div>
            ) : (
              <Table head={["Project", "Status", "Progress", "Files", "QA", "Spend", "Updated"]}>
                {projects.map((p) => (
                  <tr key={p.id} className="hover:bg-paper-soft">
                    <Td>
                      <Link href={`/projects/${p.id}`} className="font-medium text-brand hover:underline">
                        {p.name}
                      </Link>
                    </Td>
                    <Td><StatusBadge status={p.status} /></Td>
                    <Td className="w-40"><ProgressBar value={p.progress} /></Td>
                    <Td align="right">{num(p.file_count)}</Td>
                    <Td>
                      {p.qa_passed === null ? (
                        <Badge tone="muted" title="QA has not run for this project yet">not run</Badge>
                      ) : p.qa_passed ? (
                        <Badge tone="ok">passed</Badge>
                      ) : (
                        <Badge tone="bad">failing</Badge>
                      )}
                    </Td>
                    <Td align="right">{money(p.spend_usd)}</Td>
                    <Td className="whitespace-nowrap text-muted">{ago(p.updated_at)}</Td>
                  </tr>
                ))}
              </Table>
            )}
          </Card>
        </div>

        <Card title="Environment" subtitle="What this installation can actually do" dense>
          <dl className="divide-y divide-paper-line text-xs">
            <Row k="Database" v={environment.database} />
            <Row k="Storage" v={environment.storage_backend} />
            <Row k="LLM provider" v={environment.llm_provider_active} />
            <Row
              k="Power BI Desktop"
              v={
                <span className="text-muted">
                  {environment.powerbi_desktop_available ? "available" : "not available"}
                </span>
              }
            />
            {Object.entries(environment.probes).map(([k, ok]) => (
              <Row
                key={k}
                k={k.replace(/_/g, " ")}
                v={ok ? <Badge tone="ok">present</Badge> : <Badge tone="muted">absent</Badge>}
              />
            ))}
          </dl>
          <div className="border-t border-paper-line px-3 py-2.5 text-[11px] leading-relaxed text-muted">
            {environment.powerbi_desktop_note}
          </div>
        </Card>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card
          title="Blocking QA failures"
          subtitle="Checks that failed at critical or high severity, with the agent-recommended fix."
          dense
        >
          {validation_failures.length === 0 ? (
            <div className="p-4">
              <Empty title="No blocking failures">
                Either QA has not run yet, or every blocking check passed. Open a project&apos;s QA
                page to see the full result set, including checks that were skipped as
                unsupported.
              </Empty>
            </div>
          ) : (
            <Table head={["Project", "Check", "Target", "Severity", "Recommended fix"]}>
              {validation_failures.slice(0, 15).map((f, i) => (
                <tr key={i}>
                  <Td>
                    <Link href={`/projects/${f.project_id}/qa`} className="text-brand hover:underline">
                      {f.project_name}
                    </Link>
                  </Td>
                  <Td mono className="text-[11px]">{f.test}</Td>
                  <Td mono className="text-[11px]">{f.target}</Td>
                  <Td><SeverityBadge severity={f.severity} /></Td>
                  <Td className="max-w-sm text-muted">{f.fix}</Td>
                </tr>
              ))}
            </Table>
          )}
        </Card>

        <Card title="Recent agent executions" subtitle="Newest first, across all projects." dense>
          {recent_executions.length === 0 ? (
            <div className="p-4">
              <Empty title="No agent has run yet" />
            </div>
          ) : (
            <Table head={["Agent", "Status", "Duration", "Cost", "When"]}>
              {recent_executions.slice(0, 15).map((e) => (
                <tr key={e.id} className={e.error ? "bg-red-50/40" : undefined}>
                  <Td>
                    <Link href={`/projects/${e.project_id}/audit`} className="text-brand hover:underline">
                      {e.agent}
                    </Link>
                    {e.error && (
                      <p className="mt-0.5 max-w-xs truncate text-[11px] text-bad" title={e.error}>
                        {e.error}
                      </p>
                    )}
                  </Td>
                  <Td><StatusBadge status={e.status} /></Td>
                  <Td align="right">{duration(e.duration_ms)}</Td>
                  <Td align="right">{e.cost_usd ? money(e.cost_usd) : "—"}</Td>
                  <Td className="whitespace-nowrap text-muted">{ago(e.started_at)}</Td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      </div>

      {Object.keys(environment.counts).length > 0 && (
        <div className="mt-4">
          <Card title="Stored record counts" dense>
            <div className="flex flex-wrap gap-x-6 gap-y-2 p-4 text-xs">
              {Object.entries(environment.counts).map(([k, v]) => (
                <span key={k} className="text-muted">
                  {k.replace(/_/g, " ")}: <span className="font-semibold text-ink">{num(v)}</span>
                </span>
              ))}
            </div>
          </Card>
        </div>
      )}
    </>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 px-3 py-1.5">
      <dt className="text-muted">{k}</dt>
      <dd className="truncate text-right font-medium text-ink">{v}</dd>
    </div>
  );
}
