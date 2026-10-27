"use client";

import { useParams } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { DASH, ago, duration, money, num, when } from "@/lib/format";
import { useAsync } from "@/lib/hooks";
import {
  Badge, Button, Card, Code, Empty, ErrorBox, Grid, Loading, PageHeader,
  SeverityBadge, Stat, StatusBadge, Table, Td,
} from "@/components/ui";
import type { AgentExecution, AuditEvent } from "@/lib/types";

const PAGE_SIZE = 100;

export default function AuditPage() {
  const { id } = useParams<{ id: string }>();
  const [offset, setOffset] = useState(0);
  const [actionFilter, setActionFilter] = useState("all");
  const audit = useAsync(() => api.audit(id, PAGE_SIZE, offset), [id, offset]);
  const executions = useAsync(() => api.executions(id, 200), [id]);

  const events = audit.data?.events ?? [];
  const actions = Array.from(new Set(events.map((e) => e.action))).sort();
  const shown = actionFilter === "all" ? events : events.filter((e) => e.action === actionFilter);

  const execs = executions.data?.executions ?? [];
  const totalCost = execs.reduce((a, e) => a + e.cost.amount_usd, 0);
  const totalTokens = execs.reduce(
    (a, e) => a + e.cost.input_tokens + e.cost.output_tokens, 0,
  );
  const failed = execs.filter((e) => e.status !== "succeeded");

  return (
    <>
      <PageHeader
        title="Activity & Audit Log"
        description="Every agent run, tool call, approval and download, in the order it happened. The log is append-only and is written by the backend, not by this page."
        actions={
          <Button onClick={() => { audit.reload(); executions.reload(); }}>Refresh</Button>
        }
      />

      <Grid cols={4}>
        <Stat label="Audit events" value={num(audit.data?.total ?? 0)} />
        <Stat
          label="Agent executions"
          value={num(execs.length)}
          hint={failed.length ? `${failed.length} did not succeed` : "all succeeded"}
          tone={failed.length ? "warn" : "ok"}
        />
        <Stat label="LLM spend" value={money(totalCost)} hint={`${num(totalTokens)} tokens`} />
        <Stat
          label="Total agent time"
          value={duration(execs.reduce((a, e) => a + e.duration_ms, 0))}
        />
      </Grid>

      <div className="mt-4">
        <Card
          title="Agent executions"
          subtitle="Each run records its tool calls, its model calls and its cost. Expand a row for the agent's own log."
          dense
        >
          {executions.loading && <div className="p-4"><Loading what="executions" /></div>}
          {executions.error && <div className="p-4"><ErrorBox message={executions.error} /></div>}
          {execs.length === 0 && !executions.loading && (
            <div className="p-4"><Empty title="No agent has run yet" /></div>
          )}
          {execs.length > 0 && (
            <ul className="divide-y divide-paper-line">
              {execs.map((e) => <ExecutionRow key={e.id} execution={e} />)}
            </ul>
          )}
        </Card>
      </div>

      <div className="mt-4">
        <Card
          title="Audit trail"
          subtitle="Who did what, and when."
          actions={
            <select
              className="rounded-md border border-paper-line bg-white px-2 py-1 text-xs"
              value={actionFilter}
              onChange={(e) => setActionFilter(e.target.value)}
            >
              <option value="all">All actions</option>
              {actions.map((a) => <option key={a} value={a}>{a.replace(/_/g, " ")}</option>)}
            </select>
          }
          dense
        >
          {audit.loading && <div className="p-4"><Loading what="the audit log" /></div>}
          {audit.error && <div className="p-4"><ErrorBox message={audit.error} /></div>}
          {shown.length === 0 && !audit.loading && (
            <div className="p-4"><Empty title="Nothing has been recorded yet" /></div>
          )}
          {shown.length > 0 && (
            <Table head={["When", "Actor", "Action", "Summary", "Target", "Severity", ""]}>
              {shown.map((e) => <AuditRow key={e.id} event={e} />)}
            </Table>
          )}
          <div className="flex items-center justify-between gap-2 border-t border-paper-line px-4 py-2.5 text-xs text-muted">
            <span>
              Showing {num(shown.length)} of {num(audit.data?.total ?? 0)} events
              {offset > 0 && ` (from ${num(offset + 1)})`}
            </span>
            <span className="flex gap-2">
              <Button
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Newer
              </Button>
              <Button
                disabled={offset + PAGE_SIZE >= (audit.data?.total ?? 0)}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Older
              </Button>
            </span>
          </div>
        </Card>
      </div>
    </>
  );
}

function ExecutionRow({ execution: e }: { execution: AgentExecution }) {
  const [open, setOpen] = useState(false);
  const failedTools = e.tool_calls.filter((t) => !t.succeeded);

  return (
    <li className={e.error ? "bg-red-50/30" : undefined}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full flex-wrap items-center gap-3 px-4 py-2.5 text-left hover:bg-paper-soft"
      >
        <span className="text-xs font-medium text-ink">{e.agent}</span>
        <StatusBadge status={e.status} />
        <span className="text-[11px] text-muted">{duration(e.duration_ms)}</span>
        <span className="text-[11px] text-muted">
          {num(e.tool_calls.length)} tool call{e.tool_calls.length === 1 ? "" : "s"}
          {failedTools.length > 0 && <span className="text-bad"> · {failedTools.length} failed</span>}
        </span>
        <span className="text-[11px] text-muted">
          {e.llm_calls.length ? `${num(e.llm_calls.length)} LLM call(s)` : "no LLM calls"}
        </span>
        <span className="text-[11px] text-muted">
          {e.cost.amount_usd ? money(e.cost.amount_usd) : DASH}
        </span>
        <span className="ml-auto text-[11px] text-muted">{ago(e.started_at)}</span>
        <span className="text-[11px] text-muted">{open ? "▴" : "▾"}</span>
      </button>

      {open && (
        <div className="space-y-3 border-t border-paper-line bg-paper-soft/50 px-4 py-3">
          {e.error && <ErrorBox title="Execution error" message={e.error} />}

          {e.tool_calls.length > 0 && (
            <div>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
                Tool calls
              </p>
              <div className="flex flex-wrap gap-1.5">
                {e.tool_calls.map((t, i) => (
                  <Badge key={i} tone={t.succeeded ? "ok" : "bad"} title={duration(t.duration_ms)}>
                    {t.name}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          {e.llm_calls.length > 0 && (
            <div>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
                Model calls
              </p>
              <Table head={["Provider", "Model", "Purpose", "Cost"]}>
                {e.llm_calls.map((c, i) => (
                  <tr key={i}>
                    <Td>{c.provider}</Td>
                    <Td mono className="text-[11px]">{c.model}</Td>
                    <Td className="text-muted">{c.purpose}</Td>
                    <Td align="right">{c.cost.amount_usd ? money(c.cost.amount_usd) : DASH}</Td>
                  </tr>
                ))}
              </Table>
            </div>
          )}

          {e.log.length > 0 && (
            <div>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
                Agent log
              </p>
              <Code>{e.log.join("\n")}</Code>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

function AuditRow({ event: e }: { event: AuditEvent }) {
  const [open, setOpen] = useState(false);
  const hasDetail = Object.keys(e.detail ?? {}).length > 0;
  return (
    <>
      <tr className={e.severity === "critical" || e.severity === "high" ? "bg-red-50/30" : undefined}>
        <Td className="whitespace-nowrap text-muted" >{when(e.at)}</Td>
        <Td mono className="max-w-[10rem] truncate text-[11px]">{e.actor}</Td>
        <Td><Badge tone="muted">{e.action.replace(/_/g, " ")}</Badge></Td>
        <Td className="max-w-xl">{e.summary}</Td>
        <Td mono className="max-w-[10rem] truncate text-[11px] text-muted">{e.target || DASH}</Td>
        <Td><SeverityBadge severity={e.severity} /></Td>
        <Td align="right">
          {hasDetail && (
            <button onClick={() => setOpen((o) => !o)} className="text-[11px] text-brand hover:underline">
              {open ? "hide" : "detail"}
            </button>
          )}
        </Td>
      </tr>
      {open && hasDetail && (
        <tr>
          <Td colSpan={7} className="bg-paper-soft/60">
            <Code>{JSON.stringify(e.detail, null, 2)}</Code>
          </Td>
        </tr>
      )}
    </>
  );
}
