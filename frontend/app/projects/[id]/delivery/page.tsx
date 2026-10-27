"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api, loadMinutes, optional } from "@/lib/api";
import { DASH, bytes, num, when } from "@/lib/format";
import { useAction, useAsync } from "@/lib/hooks";
import {
  Badge, Button, Card, Empty, ErrorBox, Grid, Loading, Notice, PageHeader,
  SeverityBadge, Stat, Table, Td,
} from "@/components/ui";
import type { CapabilityStatus, MeetingMinutes } from "@/lib/types";

const CAP_TONE: Record<CapabilityStatus, "ok" | "warn" | "bad" | "muted"> = {
  implemented: "ok",
  not_configured: "warn",
  unavailable: "bad",
  not_implemented: "muted",
};

export default function DeliveryPage() {
  const { id } = useParams<{ id: string }>();
  const delivery = useAsync(() => api.delivery(id), [id]);
  const exports = useAsync(() => api.exports(id), [id]);
  const minutes = useAsync(() => optional(loadMinutes(id)), [id]);

  const runExport = useAction(async () => {
    const res = await api.runTask(id, "export_pbip");
    delivery.reload();
    exports.reload();
    return res;
  });

  if (delivery.loading) return <Loading what="deliverables" />;
  if (delivery.error) {
    return (
      <>
        <PageHeader title="Deliverables" />
        <ErrorBox message={delivery.error} />
      </>
    );
  }
  if (!delivery.data) return null;

  const bundle = delivery.data;
  const exportList = exports.data?.exports ?? [];
  const available = Object.values(bundle.sections).filter((s) => s.available).length;
  const presentExports = exportList.filter((e) => e.file_present);

  return (
    <>
      <PageHeader
        title="Deliverables"
        description="What this project has actually produced. A section is marked available only when the artifact exists; a download appears only when the file is verified present on disk."
        actions={
          <Button variant="primary" pending={runExport.pending} onClick={() => void runExport.run()}>
            Build PBIP export
          </Button>
        }
      />

      <Grid cols={4}>
        <Stat
          label="Sections available"
          value={`${num(available)} / ${num(Object.keys(bundle.sections).length)}`}
        />
        <Stat
          label="Downloadable files"
          value={num(presentExports.length)}
          tone={presentExports.length ? "ok" : "warn"}
          hint={
            exportList.length > presentExports.length
              ? `${exportList.length - presentExports.length} recorded but missing on disk`
              : "Verified present on disk"
          }
        />
        <Stat
          label="Open questions"
          value={num(bundle.unresolved_questions.length)}
          tone={bundle.unresolved_questions.length ? "warn" : "ok"}
        />
        <Stat
          label="Unconfirmed assumptions"
          value={num(bundle.unconfirmed_assumptions.length)}
          tone={bundle.unconfirmed_assumptions.length ? "warn" : "ok"}
        />
      </Grid>

      {runExport.error && <div className="mt-4"><ErrorBox message={runExport.error} /></div>}

      <div className="mt-4">
        <Card
          title="Exports"
          subtitle="The primary deliverable is an editable Power BI project (PBIP with TMDL and PBIR), not a screenshot or a static page. Every file is re-read and verified before a download link is offered."
          dense
        >
          {exports.loading && <div className="p-4"><Loading what="exports" /></div>}
          {exportList.length === 0 && !exports.loading && (
            <div className="p-4">
              <Empty title="Nothing has been exported yet">
                Run <span className="font-mono">export_pbip</span>. The export is only registered
                if every file it wrote can be read back successfully.
              </Empty>
            </div>
          )}
          {exportList.length > 0 && (
            <Table head={["File", "Kind", "Files", "Size", "Opens in", "Verified", "Built", ""]}>
              {exportList.map((e) => (
                <tr key={e.id}>
                  <Td>
                    <p className="font-medium text-ink">{e.filename}</p>
                    <p className="font-mono text-[10px] text-muted" title={e.sha256}>
                      {e.sha256 ? `${e.sha256.slice(0, 12)}…` : DASH}
                    </p>
                  </Td>
                  <Td><Badge tone="muted">{e.kind}</Badge></Td>
                  <Td align="right">{num(e.file_count)}</Td>
                  <Td align="right">{bytes(e.size_bytes)}</Td>
                  <Td className="text-muted">{e.opens_in || DASH}</Td>
                  <Td>
                    {e.file_present ? (
                      <Badge tone="ok" title={e.verified_at ? `Verified ${when(e.verified_at)}` : undefined}>
                        present
                      </Badge>
                    ) : (
                      <Badge tone="bad" title="The database has a record but the file is not on disk.">
                        missing
                      </Badge>
                    )}
                  </Td>
                  <Td className="whitespace-nowrap text-muted">{when(e.created_at)}</Td>
                  <Td align="right">
                    {e.file_present ? (
                      <a
                        href={api.downloadHref(id, e.id)}
                        className="inline-flex items-center rounded-md border border-brand bg-brand px-2.5 py-1.5 text-xs font-medium text-white hover:bg-brand-dark"
                      >
                        Download
                      </a>
                    ) : (
                      <span className="text-[11px] text-muted">re-run the export</span>
                    )}
                  </Td>
                </tr>
              ))}
            </Table>
          )}
          {exportList.some((e) => e.remaining_manual_steps.length > 0) && (
            <div className="border-t border-paper-line p-4">
              <Notice tone="warn" title="Steps that still have to be done by hand">
                <ul className="mt-1 list-inside list-disc">
                  {Array.from(
                    new Set(exportList.flatMap((e) => e.remaining_manual_steps)),
                  ).map((s, i) => <li key={i}>{s}</li>)}
                </ul>
              </Notice>
            </div>
          )}
        </Card>
      </div>

      <div className="mt-4">
        <Card title="Sections" subtitle="Each with the reason it is or is not available." dense>
          <Table head={["Section", "Available", "Detail"]}>
            {Object.entries(bundle.sections).map(([key, s]) => (
              <tr key={key}>
                <Td className="font-medium capitalize">{key.replace(/_/g, " ")}</Td>
                <Td>
                  {s.available ? <Badge tone="ok">yes</Badge> : <Badge tone="muted">no</Badge>}
                </Td>
                <Td className="max-w-2xl text-muted">
                  {s.available ? (
                    <span className="text-[11px]">
                      {Object.entries(s.summary ?? {})
                        .map(([k, v]) => `${k.replace(/_/g, " ")}: ${String(v)}`)
                        .join(" · ") || "Produced."}
                    </span>
                  ) : (
                    s.reason
                  )}
                </Td>
              </tr>
            ))}
          </Table>
        </Card>
      </div>

      <div className="mt-4">
        <MinutesCard projectId={id} minutes={minutes.data?.artifact ?? null} loading={minutes.loading} />
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card
          title="Unresolved questions"
          subtitle="Carried forward from the transcript and the requirements. The agents asked rather than inventing a definition."
          dense
        >
          {bundle.unresolved_questions.length === 0 ? (
            <div className="p-4"><Empty title="Nothing is outstanding" /></div>
          ) : (
            <ul className="divide-y divide-paper-line">
              {bundle.unresolved_questions.map((q) => (
                <li key={q.id} className="px-4 py-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <SeverityBadge severity={q.severity} />
                    <Badge tone="muted">{q.source}</Badge>
                  </div>
                  <p className="mt-1 text-sm text-ink">{q.question}</p>
                  {q.why_it_matters && (
                    <p className="mt-0.5 text-xs text-muted">{q.why_it_matters}</p>
                  )}
                  {q.blocks.length > 0 && (
                    <p className="mt-0.5 font-mono text-[10px] text-amber-700">
                      blocks: {q.blocks.join(", ")}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title="Unconfirmed assumptions"
          subtitle="Recorded where a definition was needed and none was agreed. Each says what breaks if the assumption is wrong."
          dense
        >
          {bundle.unconfirmed_assumptions.length === 0 ? (
            <div className="p-4"><Empty title="Every assumption has been confirmed" /></div>
          ) : (
            <ul className="divide-y divide-paper-line">
              {bundle.unconfirmed_assumptions.map((a) => (
                <li key={a.id} className="px-4 py-3">
                  <SeverityBadge severity={a.severity} />
                  <p className="mt-1 text-sm text-ink">{a.statement}</p>
                  <p className="mt-0.5 text-xs text-muted">
                    <span className="font-medium">If wrong:</span> {a.impact_if_wrong}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="mt-4">
        <Card
          title="What this installation can do"
          subtitle="Read from the backend's capability register, so this page cannot claim an integration the backend does not have."
          dense
        >
          <Table head={["Capability", "Status", "Detail"]}>
            {bundle.capabilities.map((c) => (
              <tr key={c.key}>
                <Td>
                  <p className="font-medium">{c.title}</p>
                  <p className="font-mono text-[10px] text-muted">{c.key}</p>
                </Td>
                <Td><Badge tone={CAP_TONE[c.status]}>{c.status.replace(/_/g, " ")}</Badge></Td>
                <Td className="max-w-2xl text-muted">{c.detail}</Td>
              </tr>
            ))}
          </Table>
          <div className="border-t border-paper-line px-4 py-2.5">
            <Link href="/system" className="text-xs text-brand hover:underline">
              Full capability register →
            </Link>
          </div>
        </Card>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
function MinutesCard({
  projectId, minutes, loading,
}: { projectId: string; minutes: MeetingMinutes | null; loading: boolean }) {
  const [tab, setTab] = useState<"summary" | "decisions" | "actions" | "questions">("summary");

  if (loading) return <Card title="Minutes of Meeting"><Loading what="the minutes" /></Card>;
  if (!minutes) {
    return (
      <Card title="Minutes of Meeting">
        <Empty title="No minutes have been produced">
          Attach a meeting transcript to the project and run{" "}
          <span className="font-mono">extract_requirements</span>. The minutes are generated from
          the transcript, with every decision citing the lines it came from.
        </Empty>
      </Card>
    );
  }

  return (
    <Card
      title={minutes.title || "Minutes of Meeting"}
      subtitle={
        <>
          {minutes.meeting_date && <>{minutes.meeting_date} · </>}
          {num(minutes.transcript_line_count)} transcript lines ·{" "}
          {num(minutes.attendees.length)} attendees
        </>
      }
      actions={
        <a
          href={api.minutesHref(projectId)}
          className="inline-flex items-center rounded-md border border-paper-line bg-white px-2.5 py-1.5 text-xs font-medium text-ink hover:bg-paper-soft"
        >
          Download Markdown
        </a>
      }
      dense
    >
      <div className="flex gap-1 border-b border-paper-line px-4 py-2">
        {([
          ["summary", "Summary"],
          ["decisions", `Decisions (${minutes.decisions.length})`],
          ["actions", `Actions (${minutes.action_items.length})`],
          ["questions", `Open questions (${minutes.open_questions.length})`],
        ] as const).map(([k, l]) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            className={`rounded px-2 py-1 text-[11px] ${
              tab === k ? "bg-brand-light font-medium text-brand-dark" : "text-muted hover:text-ink"
            }`}
          >
            {l}
          </button>
        ))}
      </div>

      <div className="p-4 text-xs">
        {tab === "summary" && (
          <div className="space-y-3">
            {minutes.executive_summary ? (
              <p className="whitespace-pre-wrap leading-relaxed text-ink">
                {minutes.executive_summary}
              </p>
            ) : (
              <p className="text-muted">No executive summary was produced.</p>
            )}
            <div>
              <p className="font-medium text-ink">Attendees</p>
              <ul className="mt-1 space-y-0.5">
                {minutes.attendees.map((a) => (
                  <li key={a.name} className="text-muted">
                    <span className="font-medium text-ink">{a.name}</span>
                    {a.role && ` — ${a.role}`} · {num(a.speaking_turns)} turns
                    {a.inferred && <span className="ml-1 text-[10px] text-amber-600">(role inferred)</span>}
                  </li>
                ))}
              </ul>
            </div>
            {minutes.agenda.length > 0 && (
              <div>
                <p className="font-medium text-ink">Agenda</p>
                <ul className="mt-1 list-inside list-disc text-muted">
                  {minutes.agenda.map((a, i) => <li key={i}>{a}</li>)}
                </ul>
              </div>
            )}
            {minutes.discussion_points.length > 0 && (
              <div>
                <p className="font-medium text-ink">Discussion</p>
                <ul className="mt-1 space-y-1.5">
                  {minutes.discussion_points.map((d, i) => (
                    <li key={i}>
                      <p className="font-medium text-ink">{d.topic}</p>
                      <p className="text-muted">{d.summary}</p>
                      {d.participants.length > 0 && (
                        <p className="text-[10px] text-muted">{d.participants.join(", ")}</p>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {minutes.risks.length > 0 && (
              <div>
                <p className="font-medium text-ink">Risks</p>
                <ul className="mt-1 list-inside list-disc text-muted">
                  {minutes.risks.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              </div>
            )}
          </div>
        )}

        {tab === "decisions" && (
          minutes.decisions.length === 0 ? (
            <p className="text-muted">No decision was recorded in this transcript.</p>
          ) : (
            <ul className="space-y-2.5">
              {minutes.decisions.map((d) => (
                <li key={d.id} className="rounded-md border border-paper-line p-2.5">
                  <p className="font-medium text-ink">{d.statement}</p>
                  <p className="mt-0.5 text-muted">
                    decided by {d.decided_by || "unattributed"} · confidence {d.confidence}
                    {d.affects.length > 0 && ` · affects ${d.affects.join(", ")}`}
                  </p>
                  {d.spans.map((s, i) => (
                    <p key={i} className="mt-1 border-l-2 border-paper-line pl-2 font-mono text-[10px] text-muted">
                      lines {s.start_line}–{s.end_line} · {s.speaker}: “{s.quote}”
                    </p>
                  ))}
                </li>
              ))}
            </ul>
          )
        )}

        {tab === "actions" && (
          minutes.action_items.length === 0 ? (
            <p className="text-muted">No action item was recorded.</p>
          ) : (
            <Table head={["Action", "Owner", "Due", "Status"]}>
              {minutes.action_items.map((a) => (
                <tr key={a.id}>
                  <Td>{a.description}</Td>
                  <Td className="text-muted">{a.owner || "unassigned"}</Td>
                  <Td className="text-muted">{a.due || DASH}</Td>
                  <Td><Badge tone="muted">{a.status}</Badge></Td>
                </tr>
              ))}
            </Table>
          )
        )}

        {tab === "questions" && (
          minutes.open_questions.length === 0 ? (
            <p className="text-muted">Nothing was left open.</p>
          ) : (
            <ul className="space-y-2">
              {minutes.open_questions.map((q) => (
                <li key={q.id} className="rounded-md border border-paper-line p-2.5">
                  <SeverityBadge severity={q.severity} />
                  <p className="mt-1 font-medium text-ink">{q.question}</p>
                  {q.why_it_matters && <p className="mt-0.5 text-muted">{q.why_it_matters}</p>}
                  {q.answer ? (
                    <p className="mt-0.5 text-ok">Answered: {q.answer}</p>
                  ) : (
                    <p className="mt-0.5 text-amber-700">Still open.</p>
                  )}
                </li>
              ))}
            </ul>
          )
        )}
      </div>
    </Card>
  );
}
