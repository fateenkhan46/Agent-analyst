"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { api, loadModel, optional } from "@/lib/api";
import { DASH, num, percent } from "@/lib/format";
import { useAsync } from "@/lib/hooks";
import {
  Badge, Card, ConfidenceBadge, Code, Empty, ErrorBox, Grid, IssueList, LinkButton,
  Loading, Notice, PageHeader, Prov, Stat, Table, Td,
} from "@/components/ui";
import type { DiagramEdge, DiagramNode, ModelDiagram, SemanticModel, TableDefinition } from "@/lib/types";

const ROLE_TONE: Record<TableDefinition["role"], "brand" | "ok" | "warn" | "muted" | "neutral"> = {
  fact: "brand",
  dimension: "ok",
  date: "neutral",
  bridge: "warn",
  aggregate: "muted",
  helper: "muted",
};

export default function DataModelPage() {
  const { id } = useParams<{ id: string }>();
  const model = useAsync(() => optional(loadModel(id)), [id]);
  const diagram = useAsync(() => optional(api.modelDiagram(id)), [id]);
  const [selectedTable, setSelectedTable] = useState<string>("");

  if (model.loading) return <Loading what="the semantic model" />;
  if (model.error && !model.missing) {
    return (
      <>
        <PageHeader title="Data Model" />
        <ErrorBox message={model.error} />
      </>
    );
  }
  if (!model.data) {
    return (
      <>
        <PageHeader title="Data Model" />
        <Empty title="No semantic model has been proposed yet">
          Prepare the data first, then run{" "}
          <span className="font-mono">propose_semantic_model</span> from the{" "}
          <Link href={`/projects/${id}`} className="text-brand hover:underline">workspace</Link>.
          Table roles are decided from measured key uniqueness, not from table names, and a
          relationship is only proposed where the key values actually overlap.
        </Empty>
      </>
    );
  }

  const m: SemanticModel = model.data.artifact;
  const table = m.tables.find((t) => t.name === selectedTable);
  const inactive = m.relationships.filter((r) => !r.is_active);
  const unapproved = m.relationships.filter((r) => r.requires_approval && r.approved !== true);

  return (
    <>
      <PageHeader
        title="Data Model"
        description={m.design_rationale || "The proposed semantic model."}
        actions={
          <>
            <Badge tone="muted">v{m.version}</Badge>
            <Badge tone="brand">{m.schema_shape}</Badge>
            <LinkButton href={`/projects/${id}/dax`}>DAX Studio</LinkButton>
          </>
        }
      />

      <Grid cols={4}>
        <Stat
          label="Tables"
          value={num(m.tables.length)}
          hint={`${m.tables.filter((t) => t.role === "fact").length} fact · ${m.tables.filter((t) => t.role === "dimension").length} dimension · ${m.tables.filter((t) => t.role === "date").length} date`}
        />
        <Stat
          label="Relationships"
          value={num(m.relationships.length)}
          hint={inactive.length ? `${inactive.length} inactive` : "all active"}
        />
        <Stat label="Measures" value={num(m.measures.length)} />
        <Stat
          label="Model issues"
          value={num(m.issues.length)}
          tone={m.issues.length ? "warn" : "ok"}
        />
      </Grid>

      {unapproved.length > 0 && (
        <div className="mt-4">
          <Notice tone="warn" title={`${unapproved.length} relationship(s) need a decision`}>
            These were proposed with partial key overlap or a non-unique one-side, so they are not
            accepted automatically. Approve or reject them on the{" "}
            <Link href={`/projects/${id}`} className="font-medium underline">workspace</Link>.
          </Notice>
        </div>
      )}

      <div className="mt-4">
        <Card
          title="Diagram"
          subtitle="Positions are computed by the backend, so the picture matches the model the designer reasoned about. Arrows point from the many side to the one side — the direction a filter is looked up."
          dense
        >
          {diagram.loading && <div className="p-4"><Loading what="the diagram" /></div>}
          {diagram.data && (
            <ModelDiagramView
              diagram={diagram.data.diagram}
              onSelect={(n) => setSelectedTable(n === selectedTable ? "" : n)}
              selected={selectedTable}
            />
          )}
        </Card>
      </div>

      {table && (
        <div className="mt-4">
          <Card
            title={table.name}
            subtitle={
              <>
                <Badge tone={ROLE_TONE[table.role]}>{table.role}</Badge>{" "}
                {num(table.row_count)} rows · grain:{" "}
                <span className="font-mono">{table.grain || "not stated"}</span> · from dataset{" "}
                <span className="font-mono">{table.dataset_name}</span>
              </>
            }
            dense
          >
            <div className="border-b border-paper-line px-4 py-2.5 text-xs text-muted">
              {table.rationale || table.description}
            </div>
            <Table head={["Column", "Type", "Key", "Hidden", "Format", "Description"]}>
              {table.columns.map((c) => (
                <tr key={c.name}>
                  <Td className="font-medium">{c.name}</Td>
                  <Td><Badge tone="muted">{c.data_type}</Badge></Td>
                  <Td>{c.is_key ? <Badge tone="ok">key</Badge> : DASH}</Td>
                  <Td>{c.is_hidden ? <Badge tone="muted">hidden</Badge> : DASH}</Td>
                  <Td mono className="text-[11px]">{c.format_string || DASH}</Td>
                  <Td className="max-w-md text-muted">{c.description || DASH}</Td>
                </tr>
              ))}
            </Table>
          </Card>
        </div>
      )}

      <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card title="Tables" subtitle="Click a table to see its columns." dense>
          <Table head={["Table", "Role", "Rows", "Primary key", "Grain"]}>
            {m.tables.map((t) => (
              <tr
                key={t.name}
                onClick={() => setSelectedTable(t.name === selectedTable ? "" : t.name)}
                className={`cursor-pointer hover:bg-paper-soft ${t.name === selectedTable ? "bg-brand-light/40" : ""}`}
              >
                <Td className="font-medium">{t.name}</Td>
                <Td><Badge tone={ROLE_TONE[t.role]}>{t.role}</Badge></Td>
                <Td align="right">{num(t.row_count)}</Td>
                <Td mono className="text-[11px]">{t.primary_key.join(" + ") || DASH}</Td>
                <Td className="max-w-xs text-muted">{t.grain || DASH}</Td>
              </tr>
            ))}
          </Table>
        </Card>

        <Card
          title="Relationships"
          subtitle="Match share and orphan counts are measured against the prepared data, not assumed."
          dense
        >
          {m.relationships.length === 0 ? (
            <div className="p-4"><Empty title="No relationships were proposed" /></div>
          ) : (
            <Table head={["From (many)", "To (one)", "Cardinality", "Match", "Orphans", "One side unique", "Active", "Approval"]}>
              {m.relationships.map((r) => (
                <tr key={r.id} className={!r.is_active ? "bg-paper-soft/60" : undefined}>
                  <Td mono className="text-[11px]">{r.from_table}[{r.from_column}]</Td>
                  <Td mono className="text-[11px]">{r.to_table}[{r.to_column}]</Td>
                  <Td><Badge tone="muted">{r.cardinality}</Badge></Td>
                  <Td align="right" className={r.match_share < 0.95 ? "text-amber-600" : undefined}>
                    {percent(r.match_share)}
                  </Td>
                  <Td align="right" className={r.orphan_key_count ? "text-amber-600" : undefined}>
                    {num(r.orphan_key_count)}
                  </Td>
                  <Td>
                    {r.to_side_is_unique
                      ? <Badge tone="ok">yes</Badge>
                      : <Badge tone="bad" title="Power BI requires the one side to be unique.">no</Badge>}
                  </Td>
                  <Td>{r.is_active ? <Badge tone="ok">active</Badge> : <Badge tone="muted">inactive</Badge>}</Td>
                  <Td>
                    {!r.requires_approval ? (
                      <span className="text-[11px] text-muted">not required</span>
                    ) : r.approved === true ? (
                      <Badge tone="ok">approved</Badge>
                    ) : r.approved === false ? (
                      <Badge tone="bad">rejected</Badge>
                    ) : (
                      <Badge tone="warn">pending</Badge>
                    )}
                  </Td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card title="Relationship rationale" subtitle="Why each relationship was proposed, with the evidence it rests on.">
          <ul className="space-y-2.5">
            {m.relationships.map((r) => (
              <li key={r.id} className="rounded-md border border-paper-line p-2.5">
                <p className="font-mono text-[11px] text-ink">
                  {r.from_table}[{r.from_column}] → {r.to_table}[{r.to_column}]
                  <span className="ml-2"><ConfidenceBadge confidence={r.confidence} /></span>
                </p>
                <p className="mt-1 text-xs text-muted">{r.rationale}</p>
                {r.evidence.length > 0 && (
                  <ul className="mt-1 list-inside list-disc text-[11px] text-muted">
                    {r.evidence.map((e, i) => <li key={i}>{e}</li>)}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </Card>

        <Card title="Model validation findings" subtitle="Reported by the designer against the proposed model.">
          <IssueList issues={m.issues} emptyText="The designer found no structural problems." />
          <div className="mt-3">
            <Prov provenance={m.provenance} />
          </div>
        </Card>
      </div>

      {diagram.data?.diagram.mermaid && (
        <div className="mt-4">
          <Card
            title="Mermaid export"
            subtitle="The same diagram as text, for pasting into documentation."
          >
            <Code wrap={false}>{diagram.data.diagram.mermaid}</Code>
          </Card>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Inline SVG diagram. No layout engine in the browser: x/y come from the
// backend, which is what keeps the drawing and the reasoning in step.
// ---------------------------------------------------------------------------
const NODE_W = 168;
const HEADER_H = 26;
const ROW_H = 13;
const MAX_COLS = 7;

function nodeHeight(n: DiagramNode): number {
  return HEADER_H + Math.min(n.columns.length, MAX_COLS) * ROW_H + 8;
}

const ROLE_FILL: Record<string, { bg: string; bar: string; text: string }> = {
  fact: { bg: "#E8F0FE", bar: "#2E6BE6", text: "#1F4FB8" },
  dimension: { bg: "#E7F7F5", bar: "#17A398", text: "#0F6E67" },
  date: { bg: "#F3F4F6", bar: "#64748B", text: "#334155" },
  bridge: { bg: "#FEF3E2", bar: "#F2A541", text: "#92650F" },
  aggregate: { bg: "#F6F8FB", bar: "#94A3B8", text: "#475569" },
  helper: { bg: "#F6F8FB", bar: "#94A3B8", text: "#475569" },
};

function ModelDiagramView({
  diagram, onSelect, selected,
}: { diagram: ModelDiagram; onSelect: (name: string) => void; selected: string }) {
  if (!diagram.nodes.length) {
    return <div className="p-4"><Empty title="The diagram is empty" /></div>;
  }

  const pad = 40;
  const minX = Math.min(...diagram.nodes.map((n) => n.x));
  const minY = Math.min(...diagram.nodes.map((n) => n.y));
  const maxX = Math.max(...diagram.nodes.map((n) => n.x + NODE_W));
  const maxY = Math.max(...diagram.nodes.map((n) => n.y + nodeHeight(n)));
  const width = maxX - minX + pad * 2;
  const height = maxY - minY + pad * 2;
  const pos = new Map(diagram.nodes.map((n) => [n.id, n]));

  const anchor = (n: DiagramNode) => ({
    cx: n.x - minX + pad + NODE_W / 2,
    cy: n.y - minY + pad + nodeHeight(n) / 2,
  });

  return (
    <div className="scroll-thin overflow-auto bg-paper-soft p-2">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label="Semantic model diagram"
        className="min-w-full"
      >
        <defs>
          <marker id="one" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#64748B" />
          </marker>
        </defs>

        {diagram.edges.map((e) => {
          const from = pos.get(e.source);
          const to = pos.get(e.target);
          if (!from || !to) return null;
          const a = anchor(from);
          const b = anchor(to);
          // Several edges can share a midpoint on a star layout, which stacks
          // their labels on top of each other. Sliding each label along its
          // own line, by an amount derived from the edge id, separates them
          // without needing a collision pass.
          const t = 0.34 + ((e.id.charCodeAt(0) + e.id.length) % 5) * 0.06;
          const mx = a.cx + (b.cx - a.cx) * t;
          const my = a.cy + (b.cy - a.cy) * t;
          const warned = Boolean(e.warning);
          return (
            <g key={e.id}>
              <line
                x1={a.cx} y1={a.cy} x2={b.cx} y2={b.cy}
                stroke={warned ? "#F2A541" : e.is_active ? "#64748B" : "#CBD5E1"}
                strokeWidth={warned ? 2 : 1.5}
                strokeDasharray={e.is_active ? undefined : "5 4"}
                markerEnd="url(#one)"
              />
              <title>
                {`${e.source} → ${e.target} · ${e.cardinality} · cross-filter ${e.cross_filter}` +
                  (e.is_active ? "" : " · inactive") +
                  (e.warning ? ` · ${e.warning}` : "")}
              </title>
              <text
                x={mx} y={my - 4} textAnchor="middle" fontSize="9" fill="#475569"
                className="font-mono"
                stroke="#F6F8FB" strokeWidth="3" paintOrder="stroke"
              >
                {e.label}
              </text>
              {warned && (
                <text x={mx} y={my + 8} textAnchor="middle" fontSize="9" fill="#B06E12">
                  ⚠
                </text>
              )}
            </g>
          );
        })}

        {diagram.nodes.map((n) => {
          const h = nodeHeight(n);
          const x = n.x - minX + pad;
          const y = n.y - minY + pad;
          const colour = ROLE_FILL[n.role] ?? ROLE_FILL.helper;
          const isSel = n.id === selected;
          return (
            <g
              key={n.id}
              onClick={() => onSelect(n.id)}
              style={{ cursor: "pointer" }}
            >
              <rect
                x={x} y={y} width={NODE_W} height={h} rx={6}
                fill="#FFFFFF"
                stroke={isSel ? colour.bar : "#E3E8F0"}
                strokeWidth={isSel ? 2 : 1}
              />
              <rect x={x} y={y} width={NODE_W} height={HEADER_H} rx={6} fill={colour.bg} />
              <rect x={x} y={y + HEADER_H - 2} width={NODE_W} height={2} fill={colour.bar} />
              <text x={x + 8} y={y + 17} fontSize="11" fontWeight="600" fill={colour.text}>
                {n.label.length > 22 ? `${n.label.slice(0, 21)}…` : n.label}
              </text>
              <text x={x + NODE_W - 8} y={y + 17} fontSize="9" fill={colour.text} textAnchor="end">
                {n.row_count ? n.row_count.toLocaleString("en-US") : ""}
              </text>
              {n.columns.slice(0, MAX_COLS).map((c, i) => (
                <text
                  key={c}
                  x={x + 8}
                  y={y + HEADER_H + 11 + i * ROW_H}
                  fontSize="9.5"
                  fill="#475569"
                  className="font-mono"
                >
                  {c.length > 24 ? `${c.slice(0, 23)}…` : c}
                </text>
              ))}
              {n.columns.length > MAX_COLS && (
                <text
                  x={x + 8}
                  y={y + HEADER_H + 11 + MAX_COLS * ROW_H}
                  fontSize="9"
                  fill="#94A3B8"
                >
                  +{n.columns.length - MAX_COLS} more
                </text>
              )}
              <title>{`${n.label} · ${n.role} · ${n.row_count.toLocaleString("en-US")} rows`}</title>
            </g>
          );
        })}
      </svg>
      <div className="flex flex-wrap items-center gap-3 px-2 pt-2 text-[11px] text-muted">
        {Object.entries(ROLE_FILL).slice(0, 4).map(([role, c]) => (
          <span key={role} className="inline-flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: c.bar }} />
            {role}
          </span>
        ))}
        <span className="inline-flex items-center gap-1">
          <svg width="22" height="6"><line x1="0" y1="3" x2="22" y2="3" stroke="#CBD5E1" strokeWidth="1.5" strokeDasharray="5 4" /></svg>
          inactive
        </span>
        <span className="text-amber-700">⚠ relationship with a warning</span>
      </div>
    </div>
  );
}
