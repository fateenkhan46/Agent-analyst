"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { loadModel, loadReport, loadRequirements, optional } from "@/lib/api";
import { DASH, num } from "@/lib/format";
import { useAsync } from "@/lib/hooks";
import {
  Badge, Card, Empty, ErrorBox, Grid, IssueList, Loading, Notice, PageHeader, Prov,
  Stat, Table, Td,
} from "@/components/ui";
import type { ReportPage, ReportSpec, VisualDefinition } from "@/lib/types";

export default function ReportDesignerPage() {
  const { id } = useParams<{ id: string }>();
  const report = useAsync(() => optional(loadReport(id)), [id]);
  const model = useAsync(() => optional(loadModel(id)), [id]);
  const requirements = useAsync(() => optional(loadRequirements(id)), [id]);

  const [pageName, setPageName] = useState("");
  const [selectedVisual, setSelectedVisual] = useState<string>("");

  const pages = report.data?.artifact.pages ?? [];
  useEffect(() => {
    if (!pageName && pages.length) setPageName(pages[0].name);
  }, [pages, pageName]);

  if (report.loading) return <Loading what="the report" />;
  if (report.error && !report.missing) {
    return (
      <>
        <PageHeader title="Report Designer" />
        <ErrorBox message={report.error} />
      </>
    );
  }
  if (!report.data) {
    return (
      <>
        <PageHeader title="Report Designer" />
        <Empty title="No report has been designed yet">
          Generate the measures first, then run <span className="font-mono">design_report</span>{" "}
          from the{" "}
          <Link href={`/projects/${id}`} className="text-brand hover:underline">workspace</Link>.
          Every visual is bound to a field that exists in the model — a visual whose binding
          cannot be resolved is refused rather than emitted broken.
        </Empty>
      </>
    );
  }

  const spec: ReportSpec = report.data.artifact;
  const page = pages.find((p) => p.name === pageName) ?? pages[0];
  const visual = page?.visuals.find((v) => v.id === selectedVisual);
  const totalVisuals = pages.reduce((a, p) => a + p.visuals.length, 0);
  const kpiNames = new Map(
    (requirements.data?.artifact.kpis ?? []).map((k) => [k.id, k.name] as const),
  );

  return (
    <>
      <PageHeader
        title="Report Designer"
        description={spec.design_notes || "The proposed report layout."}
        actions={
          <>
            <Badge tone="muted">{num(pages.length)} pages</Badge>
            <Badge tone="muted">{num(totalVisuals)} visuals</Badge>
          </>
        }
      />

      <Grid cols={4}>
        <Stat label="Pages" value={num(pages.length)} hint={`${pages.filter((p) => p.is_drill_through).length} drill-through`} />
        <Stat label="Visuals" value={num(totalVisuals)} />
        <Stat
          label="Unmapped KPIs"
          value={num(spec.unmapped_kpis.length)}
          tone={spec.unmapped_kpis.length ? "warn" : "ok"}
          hint="Requirements with no visual"
        />
        <Stat
          label="Design issues"
          value={num(spec.issues.length)}
          tone={spec.issues.length ? "warn" : "ok"}
        />
      </Grid>

      {spec.unmapped_kpis.length > 0 && (
        <div className="mt-4">
          <Notice tone="warn" title="KPIs with no visual on any page">
            <ul className="mt-1 list-inside list-disc">
              {spec.unmapped_kpis.map((k) => (
                <li key={k}>{kpiNames.get(k) ?? k}</li>
              ))}
            </ul>
            <p className="mt-1.5">
              These are reported rather than filled with an arbitrary chart. Either add the
              measure they need, or confirm the KPI is out of scope.
            </p>
          </Notice>
        </div>
      )}

      <div className="mt-4 flex flex-wrap gap-1.5">
        {pages.map((p) => (
          <button
            key={p.name}
            onClick={() => { setPageName(p.name); setSelectedVisual(""); }}
            className={`rounded-md border px-2.5 py-1.5 text-xs transition ${
              p.name === pageName
                ? "border-brand bg-brand-light font-medium text-brand-dark"
                : "border-paper-line bg-white text-ink hover:bg-paper-soft"
            }`}
          >
            {p.display_name || p.name}
            <span className="ml-1.5 text-[11px] text-muted">{p.visuals.length}</span>
            {p.is_drill_through && <span className="ml-1 text-[10px] text-amber-600">drill</span>}
          </button>
        ))}
      </div>

      {page && (
        <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-3">
          <div className="xl:col-span-2">
            <Card
              title={page.display_name || page.name}
              subtitle={
                <>
                  {page.purpose}
                  {page.audience && <> · audience: {page.audience}</>}
                  {page.is_drill_through && (
                    <> · drill-through on <span className="font-mono">{page.drill_through_field}</span></>
                  )}
                </>
              }
              dense
            >
              <PageCanvas
                page={page}
                theme={spec.theme}
                selected={selectedVisual}
                onSelect={(vid) => setSelectedVisual(vid === selectedVisual ? "" : vid)}
              />
            </Card>
          </div>

          <div className="space-y-4">
            <Card title={visual ? visual.title || visual.name : "Visual detail"} dense>
              {visual ? (
                <VisualDetail visual={visual} />
              ) : (
                <div className="p-4">
                  <Empty title="Select a visual">
                    Click a block in the layout to see its field bindings and the business
                    question it answers.
                  </Empty>
                </div>
              )}
            </Card>

            <Card title="Theme" dense>
              <div className="p-4">
                <p className="text-xs font-medium text-ink">{spec.theme.name}</p>
                <div className="mt-2 flex flex-wrap gap-1">
                  {spec.theme.data_colors.map((c) => (
                    <span
                      key={c}
                      title={c}
                      className="h-6 w-6 rounded border border-paper-line"
                      style={{ background: c }}
                    />
                  ))}
                </div>
              </div>
            </Card>
          </div>
        </div>
      )}

      <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card
          title="All visuals"
          subtitle="Each visual states the business question it exists to answer; none were added to fill space."
          dense
        >
          <Table head={["Page", "Visual", "Type", "Bindings", "Answers"]}>
            {pages.flatMap((p) =>
              p.visuals.map((v) => (
                <tr
                  key={v.id}
                  className="cursor-pointer hover:bg-paper-soft"
                  onClick={() => { setPageName(p.name); setSelectedVisual(v.id); }}
                >
                  <Td className="text-muted">{p.display_name || p.name}</Td>
                  <Td className="font-medium">{v.title || v.name}</Td>
                  <Td><Badge tone="muted">{v.visual_type}</Badge></Td>
                  <Td className="font-mono text-[10px] text-muted">
                    {v.bindings.map((b) => `${b.role}: ${b.table}[${b.field}]`).join(" · ")}
                  </Td>
                  <Td className="max-w-xs text-muted">{v.business_question || DASH}</Td>
                </tr>
              )),
            )}
          </Table>
        </Card>

        <Card title="Design findings" subtitle="Reported by the report agent.">
          <IssueList issues={spec.issues} emptyText="No design problems were reported." />
          <div className="mt-3">
            <Prov provenance={spec.provenance} />
          </div>
          {model.data && (
            <p className="mt-2 text-[11px] text-muted">
              Bound against semantic model v{model.data.artifact.version} with{" "}
              {num(model.data.artifact.measures.length)} measures.
            </p>
          )}
        </Card>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
const VISUAL_ICON: Record<string, string> = {
  card: "▦",
  multiRowCard: "▤",
  clusteredColumnChart: "▮▮",
  clusteredBarChart: "▬",
  columnChart: "▮",
  barChart: "▬",
  lineChart: "📈",
  areaChart: "◢",
  pieChart: "◕",
  donutChart: "◍",
  tableEx: "▦",
  table: "▦",
  matrix: "▥",
  slicer: "⫼",
  gauge: "◐",
  kpi: "▲",
  scatterChart: "⁘",
  map: "🗺",
  textbox: "T",
};

function PageCanvas({
  page, theme, selected, onSelect,
}: {
  page: ReportPage;
  theme: ReportSpec["theme"];
  selected: string;
  onSelect: (id: string) => void;
}) {
  const w = page.width || 1280;
  const h = page.height || 720;
  return (
    <div className="p-4">
      <div
        className="relative w-full overflow-hidden rounded border border-paper-line bg-paper-soft"
        style={{ aspectRatio: `${w} / ${h}` }}
      >
        {page.visuals.map((v, i) => {
          const isSel = v.id === selected;
          const colour = theme.data_colors[i % Math.max(1, theme.data_colors.length)] ?? "#2E6BE6";
          return (
            <button
              key={v.id}
              onClick={() => onSelect(v.id)}
              title={v.business_question || v.title || v.name}
              className={`absolute overflow-hidden rounded border bg-white p-1.5 text-left transition hover:shadow-md ${
                isSel ? "border-brand ring-2 ring-brand/40" : "border-paper-line"
              }`}
              style={{
                left: `${(v.layout.x / w) * 100}%`,
                top: `${(v.layout.y / h) * 100}%`,
                width: `${(v.layout.width / w) * 100}%`,
                height: `${(v.layout.height / h) * 100}%`,
              }}
            >
              <span
                className="mb-0.5 block h-0.5 w-6 rounded"
                style={{ background: colour }}
              />
              <span className="block truncate text-[10px] font-semibold leading-tight text-ink">
                {v.title || v.name}
              </span>
              <span className="mt-0.5 block text-[9px] text-muted">
                {VISUAL_ICON[v.visual_type] ?? "▭"} {v.visual_type}
              </span>
              {v.bindings.slice(0, 3).map((b) => (
                <span key={`${b.role}-${b.field}`} className="block truncate font-mono text-[8px] text-muted">
                  {b.role}: {b.field}
                </span>
              ))}
            </button>
          );
        })}
      </div>
      <p className="mt-2 text-[11px] text-muted">
        Layout drawn from the exported coordinates ({num(w)}×{num(h)}). This is a map of the
        report definition, not a rendering of the data — the report renders in Power BI, from the
        exported project.
      </p>
    </div>
  );
}

function VisualDetail({ visual }: { visual: VisualDefinition }) {
  return (
    <div className="space-y-3 p-4 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="brand">{visual.visual_type}</Badge>
        <span className="font-mono text-[10px] text-muted">{visual.id}</span>
      </div>
      {visual.subtitle && <p className="text-muted">{visual.subtitle}</p>}
      {visual.business_question && (
        <div>
          <p className="font-medium text-ink">Answers</p>
          <p className="text-muted">{visual.business_question}</p>
        </div>
      )}
      {visual.design_rationale && (
        <div>
          <p className="font-medium text-ink">Why this visual</p>
          <p className="text-muted">{visual.design_rationale}</p>
        </div>
      )}
      <div>
        <p className="font-medium text-ink">Field bindings</p>
        <ul className="mt-1 space-y-1">
          {visual.bindings.map((b) => (
            <li key={`${b.role}-${b.table}-${b.field}`} className="flex items-center gap-2">
              <Badge tone="muted">{b.role}</Badge>
              <span className="font-mono text-[11px]">
                {b.table}[{b.field}]
              </span>
              {b.is_measure && <Badge tone="brand">measure</Badge>}
            </li>
          ))}
        </ul>
      </div>
      <p className="text-[11px] text-muted">
        Position {visual.layout.x},{visual.layout.y} · size {visual.layout.width}×
        {visual.layout.height}
      </p>
    </div>
  );
}
