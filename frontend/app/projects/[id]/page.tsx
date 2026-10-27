"use client";

import { useParams } from "next/navigation";
import { useRef, useState } from "react";
import { TASK_LABELS, api } from "@/lib/api";
import { ago, bytes, duration, money, num, percent, when } from "@/lib/format";
import { useAction, useAsync } from "@/lib/hooks";
import {
  Badge, Button, Card, Empty, ErrorBox, Grid, KeyValue, LinkButton, Loading, Notice,
  PageHeader, ProgressBar, SeverityBadge, Stat, StatusBadge, Table, Td, inputClass,
  monoInputClass,
} from "@/components/ui";
import type { AgentTask, ApprovalRequest, SourceFile } from "@/lib/types";

export default function WorkspacePage() {
  const { id } = useParams<{ id: string }>();
  const project = useAsync(() => api.getProject(id), [id]);
  const tasks = useAsync(() => api.tasks(id), [id]);
  const approvals = useAsync(() => api.approvals(id), [id]);
  const executions = useAsync(() => api.executions(id, 20), [id]);

  const refreshAll = () => {
    project.reload();
    tasks.reload();
    approvals.reload();
    executions.reload();
  };

  const [lastRun, setLastRun] = useState<string | null>(null);

  const runTask = useAction(async (kind: string) => {
    const res = await api.runTask(id, kind);
    setLastRun(
      res.blocked
        ? `${TASK_LABELS[kind] ?? kind} is blocked: ${res.reason ?? "an approval is pending"}`
        : res.succeeded
          ? `${TASK_LABELS[kind] ?? kind} succeeded.`
          : `${TASK_LABELS[kind] ?? kind} failed: ${String(res.error ?? res.reason ?? "see the execution log")}`,
    );
    refreshAll();
    return res;
  });

  const runPipeline = useAction(async () => {
    const res = await api.runPipeline(id);
    const ok = res.steps.filter((s) => s.succeeded).length;
    const blocked = res.steps.find((s) => !s.succeeded);
    setLastRun(
      blocked
        ? `${ok} task(s) succeeded, then stopped at ${TASK_LABELS[blocked.kind] ?? blocked.kind}: ${blocked.reason ?? String(blocked.error ?? "see the execution log")}`
        : `All ${ok} task(s) succeeded.`,
    );
    refreshAll();
    return res;
  });

  if (project.loading) return <Loading what="the project" />;
  if (project.error) {
    return (
      <>
        <PageHeader title="Project" />
        <ErrorBox title="The project could not be loaded" message={project.error} />
      </>
    );
  }
  if (!project.data) return null;

  const p = project.data.project;
  const taskList = tasks.data?.tasks ?? project.data.tasks;
  const runnable = tasks.data?.runnable ?? [];
  const progress = tasks.data?.progress ?? project.data.progress;
  const pending = (approvals.data?.approvals ?? []).filter((a) => a.state === "pending");
  const decidedApprovals = (approvals.data?.approvals ?? []).filter((a) => a.state !== "pending");
  const dataFiles = project.data.files.filter((f) => f.kind !== "transcript" && f.kind !== "text");
  const transcripts = project.data.files.filter((f) => f.kind === "transcript" || f.kind === "text");

  return (
    <>
      <PageHeader
        title={p.name}
        description={p.business_objective || p.description || "No objective was stated for this project."}
        actions={
          <>
            <Button onClick={refreshAll} variant="ghost">Refresh</Button>
            <Button
              variant="primary"
              size="md"
              onClick={() => void runPipeline.run()}
              pending={runPipeline.pending}
              title="Runs each task in order and stops at the first failure or approval gate."
            >
              Run pipeline
            </Button>
          </>
        }
      />

      <Grid cols={4}>
        <Stat label="Pipeline progress" value={percent(progress, 0)} hint={`${taskList.filter((t) => t.status === "succeeded").length} of ${taskList.length} tasks succeeded`} />
        <Stat label="Source files" value={num(project.data.files.length)} hint={`${dataFiles.length} data, ${transcripts.length} transcript`} />
        <Stat
          label="Pending approvals"
          value={num(pending.length)}
          tone={pending.length ? "warn" : "ok"}
          hint={pending.length ? "The pipeline will stop at these" : "Nothing is waiting on you"}
        />
        <Stat
          label="Spend"
          value={money(project.data.spend_usd)}
          hint={`Budget ${money(p.budget_usd)}`}
          tone={p.budget_usd > 0 && project.data.spend_usd > p.budget_usd * 0.8 ? "warn" : "neutral"}
        />
      </Grid>

      {lastRun && (
        <div className="mt-4">
          <Notice tone={/succeeded\.$/.test(lastRun) ? "ok" : "warn"} title="Last run">
            {lastRun}
          </Notice>
        </div>
      )}
      {(runTask.error || runPipeline.error) && (
        <div className="mt-4">
          <ErrorBox
            title="The request failed"
            message={runTask.error ?? runPipeline.error ?? ""}
          />
        </div>
      )}

      <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          <Card
            title="Workflow"
            subtitle="Dependencies are enforced by the orchestrator, not by this page — a task whose inputs have not succeeded cannot be run."
            dense
          >
            <Table head={["#", "Task", "Owner", "Status", "Attempt", "Updated", ""]}>
              {taskList.map((t, i) => (
                <TaskRow
                  key={t.id}
                  index={i + 1}
                  task={t}
                  runnable={runnable.includes(t.kind)}
                  pending={runTask.pending}
                  onRun={() => void runTask.run(t.kind)}
                />
              ))}
            </Table>
            <div className="border-t border-paper-line px-3 py-2">
              <ProgressBar value={progress} />
            </div>
          </Card>

          <Card
            title="Approvals"
            subtitle="Ambiguous relationships, destructive transformations and low-confidence requirements are gated here. Approving is recorded against your user id."
            dense
          >
            {approvals.loading && <div className="p-4"><Loading what="approvals" /></div>}
            {approvals.error && <div className="p-4"><ErrorBox message={approvals.error} /></div>}
            {approvals.data && approvals.data.approvals.length === 0 && (
              <div className="p-4">
                <Empty title="No approval has been requested">
                  Agents raise an approval when a decision would otherwise be a guess — a
                  relationship whose key overlap is partial, a step that drops rows, or a KPI
                  definition the transcript left open.
                </Empty>
              </div>
            )}
            {approvals.data && approvals.data.approvals.length > 0 && (
              <>
                {pending.length > 0 ? (
                  <ul className="divide-y divide-paper-line">
                    {pending.map((a) => (
                      <ApprovalRow key={a.id} projectId={id} approval={a} onDecided={refreshAll} />
                    ))}
                  </ul>
                ) : (
                  <p className="px-4 py-3 text-xs text-muted">
                    Nothing is waiting on you.
                  </p>
                )}
                {decidedApprovals.length > 0 && (
                  <details className="border-t border-paper-line">
                    <summary className="cursor-pointer px-4 py-2.5 text-xs text-muted">
                      {decidedApprovals.length} decided approval
                      {decidedApprovals.length === 1 ? "" : "s"} — who decided what, and when
                    </summary>
                    <ul className="divide-y divide-paper-line border-t border-paper-line">
                      {decidedApprovals.map((a) => (
                        <ApprovalRow key={a.id} projectId={id} approval={a} onDecided={refreshAll} />
                      ))}
                    </ul>
                  </details>
                )}
              </>
            )}
          </Card>

          <Card title="Recent executions" subtitle="Per-agent timing, cost and tool calls." dense>
            {executions.loading && <div className="p-4"><Loading what="executions" /></div>}
            {executions.data && executions.data.executions.length === 0 && (
              <div className="p-4"><Empty title="No agent has run for this project yet" /></div>
            )}
            {executions.data && executions.data.executions.length > 0 && (
              <Table head={["Agent", "Status", "Duration", "LLM calls", "Tools", "Cost", "When"]}>
                {executions.data.executions.map((e) => (
                  <tr key={e.id} className={e.error ? "bg-red-50/40" : undefined}>
                    <Td>
                      <span className="font-medium">{e.agent}</span>
                      {e.error && <p className="mt-0.5 text-[11px] text-bad">{e.error}</p>}
                    </Td>
                    <Td><StatusBadge status={e.status} /></Td>
                    <Td align="right">{duration(e.duration_ms)}</Td>
                    <Td align="right">{num(e.llm_calls.length)}</Td>
                    <Td align="right">
                      {num(e.tool_calls.length)}
                      {e.tool_calls.some((t) => !t.succeeded) && (
                        <span className="ml-1 text-bad">
                          ({e.tool_calls.filter((t) => !t.succeeded).length} failed)
                        </span>
                      )}
                    </Td>
                    <Td align="right">{e.cost.amount_usd ? money(e.cost.amount_usd) : "—"}</Td>
                    <Td className="whitespace-nowrap text-muted">{ago(e.started_at)}</Td>
                  </tr>
                ))}
              </Table>
            )}
            <div className="border-t border-paper-line px-3 py-2">
              <LinkButton href={`/projects/${id}/audit`}>Full activity &amp; audit log</LinkButton>
            </div>
          </Card>
        </div>

        <div className="space-y-4">
          <UploadCard projectId={id} files={project.data.files} onChange={refreshAll} />

          <TranscriptCard projectId={id} transcripts={transcripts} onChange={refreshAll} />

          <Card title="Brief" dense>
            <div className="p-4">
              <KeyValue
                items={[
                  ["Status", <StatusBadge key="s" status={p.status} />],
                  ["Created", when(p.created_at)],
                  ["Reporting period", p.reporting_period || "—"],
                  ["Currency", p.currency || "—"],
                  ["Units", p.units || "—"],
                  ["Project id", <span key="i" className="font-mono text-[11px]">{p.id}</span>],
                ]}
              />
              {p.desired_kpis.length > 0 && (
                <div className="mt-3">
                  <p className="text-[11px] uppercase tracking-wide text-muted">Desired KPIs</p>
                  <ul className="mt-1 list-inside list-disc text-xs text-ink">
                    {p.desired_kpis.map((k) => <li key={k}>{k}</li>)}
                  </ul>
                </div>
              )}
              {p.reporting_questions.length > 0 && (
                <div className="mt-3">
                  <p className="text-[11px] uppercase tracking-wide text-muted">Reporting questions</p>
                  <ul className="mt-1 list-inside list-disc text-xs text-ink">
                    {p.reporting_questions.map((q) => <li key={q}>{q}</li>)}
                  </ul>
                </div>
              )}
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
function TaskRow({
  index, task, runnable, pending, onRun,
}: { index: number; task: AgentTask; runnable: boolean; pending: boolean; onRun: () => void }) {
  const blockedReason =
    task.status === "blocked"
      ? "An upstream task has not succeeded yet."
      : task.status === "awaiting_approval"
        ? "An approval is pending for this task."
        : "";
  return (
    <tr className={task.status === "failed" ? "bg-red-50/40" : undefined}>
      <Td align="right" className="w-8 text-muted">{index}</Td>
      <Td>
        <p className="font-medium text-ink">{TASK_LABELS[task.kind] ?? task.kind}</p>
        <p className="font-mono text-[11px] text-muted">{task.kind}</p>
        {task.error && <p className="mt-0.5 max-w-md text-[11px] text-bad">{task.error}</p>}
        {task.rework_reason && (
          <p className="mt-0.5 max-w-md text-[11px] text-amber-700">
            Rework: {task.rework_reason}
          </p>
        )}
      </Td>
      <Td className="text-muted">{task.owner}</Td>
      <Td>
        <StatusBadge status={task.status} />
        {blockedReason && <p className="mt-0.5 text-[11px] text-muted">{blockedReason}</p>}
      </Td>
      <Td align="right">{task.attempt || "—"}</Td>
      <Td className="whitespace-nowrap text-muted">{task.updated_at ? ago(task.updated_at) : "—"}</Td>
      <Td align="right">
        <Button
          onClick={onRun}
          pending={pending}
          disabled={!runnable && task.status !== "failed" && task.status !== "succeeded"}
          title={
            runnable
              ? "Run this task now"
              : task.status === "succeeded"
                ? "Re-run this task, replacing its artifact with a new version"
                : task.status === "failed"
                  ? "Retry this task"
                  : blockedReason || "Not runnable yet"
          }
        >
          {task.status === "succeeded" ? "Re-run" : task.status === "failed" ? "Retry" : "Run"}
        </Button>
      </Td>
    </tr>
  );
}

function ApprovalRow({
  projectId, approval, onDecided,
}: { projectId: string; approval: ApprovalRequest; onDecided: () => void }) {
  const [note, setNote] = useState("");
  const decide = useAction(async (approve: boolean) => {
    const res = await api.decideApproval(projectId, approval.id, approve, note);
    onDecided();
    return res;
  });
  const decided = approval.state !== "pending";

  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <SeverityBadge severity={approval.severity} />
        <Badge tone="muted">{approval.kind.replace(/_/g, " ")}</Badge>
        {decided && <StatusBadge status={approval.state} />}
        <span
          className="max-w-[22rem] truncate font-mono text-[11px] text-muted"
          title={approval.subject_label || approval.subject_id}
        >
          {approval.subject_label || approval.subject_id}
        </span>
        <span className="ml-auto text-[11px] text-muted">{ago(approval.created_at)}</span>
      </div>
      <p className="mt-1.5 text-sm text-ink">{approval.question}</p>
      {approval.detail && (
        <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-muted">{approval.detail}</p>
      )}
      {decided ? (
        <p className="mt-2 text-[11px] text-muted">
          {approval.state} by {approval.decided_by || "unknown"}
          {approval.decision_note ? ` — “${approval.decision_note}”` : ""}
        </p>
      ) : (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <input
            className={`${inputClass} max-w-xs`}
            placeholder="Note (recorded with your decision)"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <Button variant="primary" pending={decide.pending} onClick={() => void decide.run(true)}>
            Approve
          </Button>
          <Button variant="danger" pending={decide.pending} onClick={() => void decide.run(false)}>
            Reject
          </Button>
          {decide.error && <span className="text-xs text-bad">{decide.error}</span>}
        </div>
      )}
    </li>
  );
}

function UploadCard({
  projectId, files, onChange,
}: { projectId: string; files: SourceFile[]; onChange: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [rejected, setRejected] = useState<{ filename: string; reason: string }[]>([]);
  const upload = useAction(async (list: File[]) => {
    const res = await api.uploadFiles(projectId, list);
    setRejected(res.rejected);
    onChange();
    return res;
  });

  return (
    <Card
      title="Source files"
      subtitle="The original upload is stored byte-for-byte and never modified; transformations write new datasets."
      dense
    >
      <div className="p-4">
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".csv,.xlsx,.xlsm,.tsv,.txt,.md,.json"
          className="block w-full text-xs text-muted file:mr-3 file:rounded-md file:border file:border-paper-line file:bg-white file:px-2.5 file:py-1.5 file:text-xs file:font-medium file:text-ink hover:file:bg-paper-soft"
          onChange={(e) => {
            const list = Array.from(e.target.files ?? []);
            if (list.length) void upload.run(list);
            if (inputRef.current) inputRef.current.value = "";
          }}
        />
        {upload.pending && <p className="mt-2 text-xs text-muted">Uploading…</p>}
        {upload.error && <div className="mt-2"><ErrorBox message={upload.error} /></div>}
        {rejected.length > 0 && (
          <div className="mt-2 space-y-1">
            {rejected.map((r, i) => (
              <p key={i} className="text-xs text-bad">
                <span className="font-medium">{r.filename}</span> rejected — {r.reason}
              </p>
            ))}
          </div>
        )}
      </div>
      {files.length === 0 ? (
        <div className="px-4 pb-4"><Empty title="Nothing uploaded yet" /></div>
      ) : (
        <Table head={["File", "Kind", "Size", "Status"]}>
          {files.map((f) => (
            <tr key={f.id}>
              <Td>
                <p className="max-w-[12rem] truncate font-medium" title={f.original_filename}>
                  {f.original_filename}
                </p>
                <p className="font-mono text-[10px] text-muted" title={f.sha256}>
                  {f.sha256.slice(0, 12)}…
                </p>
              </Td>
              <Td className="text-muted">{f.kind}</Td>
              <Td align="right">{bytes(f.size_bytes)}</Td>
              <Td>
                {f.status === "rejected" ? (
                  <>
                    <Badge tone="bad">rejected</Badge>
                    <p className="mt-0.5 text-[11px] text-bad">{f.rejection_reason}</p>
                  </>
                ) : (
                  <Badge tone="ok">{f.status}</Badge>
                )}
              </Td>
            </tr>
          ))}
        </Table>
      )}
    </Card>
  );
}

function TranscriptCard({
  projectId, transcripts, onChange,
}: { projectId: string; transcripts: SourceFile[]; onChange: () => void }) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const attach = useAction(async () => {
    const res = await api.addTranscript(projectId, text);
    setText("");
    setOpen(false);
    onChange();
    return res;
  });

  return (
    <Card
      title="Meeting transcript"
      subtitle="Feeds the Minutes of Meeting and the requirements extraction."
      actions={
        <Button onClick={() => setOpen((o) => !o)}>{open ? "Cancel" : "Attach text"}</Button>
      }
      dense
    >
      <div className="p-4">
        {transcripts.length === 0 ? (
          <p className="text-xs text-muted">
            None attached. Without a transcript the requirements agent works from the brief alone,
            and no minutes are produced.
          </p>
        ) : (
          <ul className="space-y-1 text-xs">
            {transcripts.map((t) => (
              <li key={t.id} className="flex items-center justify-between gap-2">
                <span className="truncate font-medium" title={t.original_filename}>
                  {t.original_filename}
                </span>
                <span className="shrink-0 text-muted">{bytes(t.size_bytes)}</span>
              </li>
            ))}
          </ul>
        )}
        {open && (
          <div className="mt-3">
            <textarea
              className={monoInputClass}
              rows={8}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={"[00:01] Speaker (Role): …"}
            />
            <div className="mt-2 flex items-center gap-2">
              <Button
                variant="primary"
                disabled={!text.trim()}
                pending={attach.pending}
                onClick={() => void attach.run()}
              >
                Attach transcript
              </Button>
              {attach.error && <span className="text-xs text-bad">{attach.error}</span>}
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
