"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api, loadExecution, loadPlan, optional } from "@/lib/api";
import { DASH, decimal, num, percent, when } from "@/lib/format";
import { useAction, useAsync } from "@/lib/hooks";
import {
  Badge, Button, Card, ConfidenceBadge, Empty, ErrorBox, Grid, IssueList, Loading,
  Notice, PageHeader, Prov, SeverityBadge, Stat, Table, Td,
} from "@/components/ui";
import type { ColumnProfile, SourceTable } from "@/lib/types";

export default function DataExplorerPage() {
  const { id } = useParams<{ id: string }>();
  const profile = useAsync(() => api.getProfile(id), [id]);
  const plan = useAsync(() => optional(loadPlan(id)), [id]);
  const execution = useAsync(() => optional(loadExecution(id)), [id]);

  const [selected, setSelected] = useState<string>("");
  const tables = profile.data?.profile.tables ?? [];

  useEffect(() => {
    if (!selected && tables.length) setSelected(tables[0].table_name);
  }, [tables, selected]);

  const runProfile = useAction(async () => {
    const res = await api.runProfile(id);
    profile.reload();
    return res;
  });

  if (profile.loading) return <Loading what="the data profile" />;

  if (profile.missing || !profile.data) {
    return (
      <>
        <PageHeader
          title="Data Explorer"
          actions={
            <Button variant="primary" pending={runProfile.pending} onClick={() => void runProfile.run()}>
              Profile sources now
            </Button>
          }
        />
        {profile.error && !profile.missing && <ErrorBox message={profile.error} />}
        <Empty title="This project has not been profiled yet">
          Upload at least one <code className="font-mono">.csv</code> or{" "}
          <code className="font-mono">.xlsx</code> file, then profile it. Profiling reads every
          sheet, finds the real header row, classifies each column by the pattern its values
          match, measures key uniqueness and overlap between tables, and records every
          inconsistency it finds — it changes nothing.
        </Empty>
        {runProfile.error && <div className="mt-3"><ErrorBox message={runProfile.error} /></div>}
      </>
    );
  }

  const prof = profile.data.profile;
  const table = tables.find((t) => t.table_name === selected) ?? tables[0];
  const totalRows = tables.reduce((a, t) => a + t.row_count, 0);
  const totalIssues =
    tables.reduce((a, t) => a + t.issues.length + t.columns.reduce((b, c) => b + c.issues.length, 0), 0) +
    prof.cross_table_issues.length;

  return (
    <>
      <PageHeader
        title="Data Explorer"
        description="Measured facts about the uploaded sources. Nothing here is inferred from column names alone — a key is a key because its values are unique, and an overlap is an overlap because the values matched."
        actions={
          <>
            <span className="text-xs text-muted">
              v{profile.data.versions[0]?.version ?? 1} · {when(prof.generated_at)}
            </span>
            <Button pending={runProfile.pending} onClick={() => void runProfile.run()}>
              Re-profile
            </Button>
          </>
        }
      />

      <Grid cols={4}>
        <Stat label="Tables" value={num(tables.length)} hint={`${num(tables.reduce((a, t) => a + t.column_count, 0))} columns`} />
        <Stat label="Rows read" value={num(totalRows)} hint="Across every sheet" />
        <Stat
          label="Issues recorded"
          value={num(totalIssues)}
          tone={totalIssues ? "warn" : "ok"}
          hint="Reported, not silently fixed"
        />
        <Stat
          label="Key overlaps"
          value={num(prof.key_overlaps.length)}
          hint="Candidate joins, measured by value"
        />
      </Grid>

      <div className="mt-4">
        <Prov provenance={prof.provenance} />
      </div>

      {prof.cross_table_issues.length > 0 && (
        <div className="mt-4">
          <Card title="Cross-table findings" subtitle="Problems that only show up when the sources are compared.">
            <IssueList issues={prof.cross_table_issues} />
          </Card>
        </div>
      )}

      <div className="mt-4 flex flex-wrap gap-1.5">
        {tables.map((t) => (
          <button
            key={t.table_name}
            onClick={() => setSelected(t.table_name)}
            className={`rounded-md border px-2.5 py-1.5 text-xs transition ${
              t.table_name === selected
                ? "border-brand bg-brand-light font-medium text-brand-dark"
                : "border-paper-line bg-white text-ink hover:bg-paper-soft"
            }`}
          >
            {t.display_name || t.table_name}
            <span className="ml-1.5 text-[11px] text-muted">{num(t.row_count)}</span>
          </button>
        ))}
      </div>

      {table && (
        <div className="mt-4 space-y-4">
          <TableDetail projectId={id} table={table} />
        </div>
      )}

      {prof.key_overlaps.length > 0 && (
        <div className="mt-4">
          <Card
            title="Measured key overlaps"
            subtitle="Match shares are computed on normalised values, so PRJ-1001, prj 1001 and 0001001 are compared as the same key. A high name similarity alone never makes a relationship."
            dense
          >
            <Table head={["Left", "Right", "Left distinct", "Right distinct", "Matched", "Left match", "Right match", "Unique side", "Name similarity"]}>
              {prof.key_overlaps.map((o, i) => (
                <tr key={i}>
                  <Td mono className="text-[11px]">{o.left}</Td>
                  <Td mono className="text-[11px]">{o.right}</Td>
                  <Td align="right">{num(o.left_distinct)}</Td>
                  <Td align="right">{num(o.right_distinct)}</Td>
                  <Td align="right">{num(o.matched_distinct)}</Td>
                  <Td align="right">{percent(o.left_match_share)}</Td>
                  <Td align="right">{percent(o.right_match_share)}</Td>
                  <Td>
                    {o.left_is_unique && o.right_is_unique ? (
                      <Badge tone="warn" title="Both sides unique: this is 1:1, not a dimension join.">both</Badge>
                    ) : o.right_is_unique ? (
                      <Badge tone="ok">right</Badge>
                    ) : o.left_is_unique ? (
                      <Badge tone="ok">left</Badge>
                    ) : (
                      <Badge tone="bad" title="Neither side is unique: a direct join would fan out.">neither</Badge>
                    )}
                  </Td>
                  <Td align="right">{percent(o.name_similarity, 0)}</Td>
                </tr>
              ))}
            </Table>
          </Card>
        </div>
      )}

      <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card
          title="Transformation plan"
          subtitle="What the preparation agent proposes to do, and why. Nothing runs until the plan is executed."
          dense
        >
          {plan.loading && <div className="p-4"><Loading what="the plan" /></div>}
          {plan.data === null && !plan.loading && (
            <div className="p-4">
              <Empty title="No plan has been proposed yet">
                Run <span className="font-mono">plan_transformations</span> from the workspace.
              </Empty>
            </div>
          )}
          {plan.data && (
            <>
              <div className="border-b border-paper-line px-4 py-3 text-xs text-muted">
                {plan.data.artifact.summary}
              </div>
              <Table head={["#", "Step", "Target", "Risk", "Confidence", "Approval"]}>
                {plan.data.artifact.steps.map((s) => (
                  <tr key={s.id}>
                    <Td align="right" className="text-muted">{s.ordinal}</Td>
                    <Td>
                      <p className="font-medium text-ink">{s.title}</p>
                      <p className="font-mono text-[10px] text-muted">{s.op}</p>
                      <p className="mt-0.5 max-w-md text-[11px] text-muted">{s.reason}</p>
                      {s.expected_effect && (
                        <p className="mt-0.5 max-w-md text-[11px] text-muted">
                          Expected: {s.expected_effect}
                        </p>
                      )}
                    </Td>
                    <Td mono className="text-[11px]">{s.target_table}</Td>
                    <Td>
                      <Badge tone={s.risk === "destructive" ? "bad" : s.risk === "review" ? "warn" : "ok"}>
                        {s.risk}
                      </Badge>
                    </Td>
                    <Td><ConfidenceBadge confidence={s.confidence} /></Td>
                    <Td>
                      {!s.requires_approval ? (
                        <span className="text-[11px] text-muted">not required</span>
                      ) : s.approved === true ? (
                        <Badge tone="ok">approved</Badge>
                      ) : s.approved === false ? (
                        <Badge tone="bad">rejected</Badge>
                      ) : (
                        <Badge tone="warn">pending</Badge>
                      )}
                    </Td>
                  </tr>
                ))}
              </Table>
            </>
          )}
        </Card>

        <Card
          title="Execution result"
          subtitle="Row counts before and after every step, so a dropped row cannot pass unnoticed."
          dense
        >
          {execution.loading && <div className="p-4"><Loading what="the execution" /></div>}
          {execution.data === null && !execution.loading && (
            <div className="p-4">
              <Empty title="The plan has not been executed yet" />
            </div>
          )}
          {execution.data && (
            <ExecutionDetail exec={execution.data.artifact} />
          )}
        </Card>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
function TableDetail({ projectId, table }: { projectId: string; table: SourceTable }) {
  const rows = useAsync(
    () => api.tableRows(projectId, table.table_name, 50, 0),
    [projectId, table.table_name],
  );
  const colIssues = table.columns.flatMap((c) => c.issues);

  return (
    <>
      <Card
        title={table.display_name || table.table_name}
        subtitle={
          <>
            {table.sheet_name && <>sheet <span className="font-mono">{table.sheet_name}</span> · </>}
            {num(table.row_count)} rows · {num(table.column_count)} columns · header on row{" "}
            {table.header_row_index + 1} · naming convention{" "}
            <span className="font-mono">{table.naming_convention || "mixed"}</span>
          </>
        }
        dense
      >
        {table.duplicate_row_count > 0 && (
          <div className="px-4 pt-4">
            <Notice tone="warn">
              {num(table.duplicate_row_count)} fully duplicated row
              {table.duplicate_row_count === 1 ? "" : "s"} detected. They are reported, not
              removed — a repeated row may be a legitimate repeated record.
            </Notice>
          </div>
        )}
        {table.candidate_primary_keys.length > 0 && (
          <div className="px-4 pt-3 text-xs text-muted">
            Candidate primary key{table.candidate_primary_keys.length === 1 ? "" : "s"}:{" "}
            {table.candidate_primary_keys.map((k) => (
              <span key={k.join(",")} className="mr-2 font-mono text-ink">{k.join(" + ")}</span>
            ))}
          </div>
        )}
        <div className="mt-3">
          <Table
            head={["Column", "Type", "Nulls", "Blank", "Unique", "Dupes", "PK score", "Range / top values", "Format variants"]}
          >
            {table.columns.map((c) => <ColumnRow key={c.id} column={c} rowCount={table.row_count} />)}
          </Table>
        </div>
      </Card>

      {(table.issues.length > 0 || colIssues.length > 0) && (
        <Card title="Findings in this table" dense>
          <div className="p-4">
            <IssueList issues={[...table.issues, ...colIssues]} />
          </div>
        </Card>
      )}

      <Card
        title="Preview"
        subtitle="Read from the stored original upload, not from a transformed copy."
        dense
      >
        {rows.loading && <div className="p-4"><Loading what="rows" /></div>}
        {rows.error && <div className="p-4"><ErrorBox message={rows.error} /></div>}
        {rows.data && (
          <>
            <div className="scroll-thin max-h-96 overflow-auto">
              <Table head={rows.data.columns}>
                {rows.data.rows.map((r, i) => (
                  <tr key={i} className="hover:bg-paper-soft">
                    {rows.data!.columns.map((c) => (
                      <Td key={c} mono className="max-w-[16rem] truncate text-[11px]">
                        {r[c] === "" || r[c] === null ? (
                          <span className="text-muted">{DASH}</span>
                        ) : (
                          String(r[c])
                        )}
                      </Td>
                    ))}
                  </tr>
                ))}
              </Table>
            </div>
            <div className="border-t border-paper-line px-3 py-2 text-[11px] text-muted">
              Showing {num(rows.data.rows.length)} of {num(rows.data.total_rows)} rows.
            </div>
          </>
        )}
      </Card>
    </>
  );
}

function ColumnRow({ column: c, rowCount }: { column: ColumnProfile; rowCount: number }) {
  const nullShare = rowCount ? c.null_count / rowCount : 0;
  return (
    <tr>
      <Td>
        <p className="font-medium text-ink">{c.name}</p>
        {c.detected_currency_symbols.length > 0 && (
          <p className="text-[10px] text-muted">
            currency symbols: {c.detected_currency_symbols.join(" ")}
          </p>
        )}
      </Td>
      <Td>
        <Badge tone="muted">{c.inferred_type}</Badge>
        <span className="ml-1"><ConfidenceBadge confidence={c.type_confidence} /></span>
      </Td>
      <Td align="right" className={nullShare > 0.2 ? "text-amber-600" : undefined}>
        {num(c.null_count)}
        {rowCount > 0 && <span className="ml-1 text-[10px] text-muted">{percent(nullShare, 0)}</span>}
      </Td>
      <Td align="right">{num(c.blank_count)}</Td>
      <Td align="right">
        {num(c.unique_count)}
        {c.is_unique && <span className="ml-1"><Badge tone="ok">unique</Badge></span>}
      </Td>
      <Td align="right" className={c.duplicate_count ? "text-amber-600" : undefined}>
        {num(c.duplicate_count)}
      </Td>
      <Td align="right">{c.primary_key_score ? decimal(c.primary_key_score, 2) : DASH}</Td>
      <Td className="max-w-xs">
        {c.numeric ? (
          <span className="font-mono text-[11px]">
            {decimal(c.numeric.min)} … {decimal(c.numeric.max)}
            {c.numeric.mean !== null && <> · mean {decimal(c.numeric.mean)}</>}
          </span>
        ) : c.temporal ? (
          <span className="font-mono text-[11px]">
            {c.temporal.min_date ?? DASH} … {c.temporal.max_date ?? DASH}
            {c.temporal.granularity && <> · {c.temporal.granularity}</>}
          </span>
        ) : c.top_values.length ? (
          <span className="text-[11px] text-muted">
            {c.top_values.slice(0, 4).map((t) => `${t.value} (${percent(t.share, 0)})`).join(", ")}
          </span>
        ) : (
          <span className="text-muted">{DASH}</span>
        )}
      </Td>
      <Td className="max-w-xs">
        {c.format_variants.length > 1 ? (
          <div className="space-y-0.5">
            {c.format_variants.map((v, i) => (
              <p key={i} className="text-[11px]">
                <span className="font-mono text-ink">{v.pattern}</span>{" "}
                <span className="text-muted">
                  ×{num(v.count)}{v.examples.length ? ` e.g. ${v.examples[0]}` : ""}
                </span>
              </p>
            ))}
          </div>
        ) : (
          <span className="text-[11px] text-muted">
            {c.format_variants[0]?.pattern ?? DASH}
          </span>
        )}
      </Td>
    </tr>
  );
}

function ExecutionDetail({ exec }: { exec: import("@/lib/types").TransformationExecution }) {
  const [tab, setTab] = useState<"rows" | "lineage" | "quality" | "m">("rows");
  const regressions = (exec.quality_after?.issues ?? []).filter(
    (i) => i.severity === "critical" || i.severity === "high",
  );

  return (
    <>
      <div className="flex items-center gap-2 border-b border-paper-line px-4 py-2.5">
        {exec.succeeded ? <Badge tone="ok">succeeded</Badge> : <Badge tone="bad">failed</Badge>}
        {exec.skipped_steps.length > 0 && (
          <Badge tone="warn" title={exec.skipped_steps.join(", ")}>
            {exec.skipped_steps.length} step(s) skipped
          </Badge>
        )}
        <div className="ml-auto flex gap-1">
          {(["rows", "lineage", "quality", "m"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`rounded px-2 py-1 text-[11px] ${
                tab === t ? "bg-brand-light font-medium text-brand-dark" : "text-muted hover:text-ink"
              }`}
            >
              {t === "m" ? "Power Query M" : t}
            </button>
          ))}
        </div>
      </div>

      {exec.error && <div className="p-4"><ErrorBox message={exec.error} /></div>}

      {tab === "rows" && (
        <Table head={["Step", "Table", "Before", "After", "Δ"]}>
          {exec.row_count_deltas.map((d, i) => {
            const delta = d.rows_after - d.rows_before;
            return (
              <tr key={i} className={delta < 0 ? "bg-amber-50/50" : undefined}>
                <Td mono className="text-[11px]">{d.op}</Td>
                <Td mono className="text-[11px]">{d.table}</Td>
                <Td align="right">{num(d.rows_before)}</Td>
                <Td align="right">{num(d.rows_after)}</Td>
                <Td align="right" className={delta < 0 ? "font-semibold text-bad" : delta > 0 ? "text-ok" : "text-muted"}>
                  {delta === 0 ? "0" : delta > 0 ? `+${num(delta)}` : num(delta)}
                </Td>
              </tr>
            );
          })}
        </Table>
      )}

      {tab === "lineage" && (
        <Table head={["Target", "Sources", "How"]}>
          {exec.lineage.map((l, i) => (
            <tr key={i}>
              <Td mono className="text-[11px]">{l.target}</Td>
              <Td mono className="text-[11px]">{l.sources.join(", ")}</Td>
              <Td className="max-w-md text-[11px] text-muted">{l.transform_summary}</Td>
            </tr>
          ))}
        </Table>
      )}

      {tab === "quality" && (
        <div className="space-y-3 p-4">
          {regressions.length > 0 && (
            <Notice tone="warn" title="Quality regressions after preparation">
              These were measured by comparing the before and after snapshots.
            </Notice>
          )}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <QualityPane label="Before" report={exec.quality_before} />
            <QualityPane label="After" report={exec.quality_after} />
          </div>
          <IssueList issues={exec.quality_after?.issues ?? []} emptyText="No quality issues after preparation." />
        </div>
      )}

      {tab === "m" && (
        <div className="space-y-3 p-4">
          <p className="text-[11px] text-muted">
            Generated Power Query M, in two layers: a query per source that reads the raw extract
            and cleans it, then a dataset query that combines the cleaned sources. Each step
            therefore applies exactly once and the result stays editable in Power BI.
          </p>
          {Object.entries(exec.m_queries).map(([name, code]) => (
            <details key={name} className="rounded-md border border-paper-line">
              <summary className="cursor-pointer px-3 py-2 font-mono text-[11px] font-medium text-ink">
                {name}
              </summary>
              <pre className="scroll-thin overflow-x-auto border-t border-paper-line bg-paper-soft p-3 font-mono text-[11px] leading-relaxed">
                {code}
              </pre>
            </details>
          ))}
        </div>
      )}
    </>
  );
}

function QualityPane({
  label, report,
}: { label: string; report: import("@/lib/types").QualityReport | null }) {
  if (!report) {
    return (
      <div className="rounded-md border border-paper-line p-3">
        <p className="text-xs font-semibold">{label}</p>
        <p className="mt-1 text-[11px] text-muted">Not captured.</p>
      </div>
    );
  }
  const rows = Object.entries(report.table_row_counts);
  return (
    <div className="rounded-md border border-paper-line p-3">
      <p className="text-xs font-semibold">{label}</p>
      <dl className="mt-1.5 space-y-0.5 text-[11px]">
        {rows.map(([t, n]) => (
          <div key={t} className="flex justify-between gap-2">
            <dt className="truncate font-mono text-muted">{t}</dt>
            <dd className="tabular-nums">{num(n)}</dd>
          </div>
        ))}
      </dl>
      {Object.keys(report.orphan_key_counts).length > 0 && (
        <p className="mt-1.5 text-[11px] text-amber-700">
          orphan keys:{" "}
          {Object.entries(report.orphan_key_counts).map(([k, v]) => `${k}=${v}`).join(", ")}
        </p>
      )}
    </div>
  );
}
