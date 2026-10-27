"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api, loadModel, optional } from "@/lib/api";
import { DASH, decimal, duration, num } from "@/lib/format";
import { useAction, useAsync } from "@/lib/hooks";
import {
  Badge, Button, Card, Code, ConfidenceBadge, Empty, ErrorBox, Grid, Loading, Notice,
  PageHeader, Stat, Table, Td, TestStatusBadge, inputClass, monoInputClass,
} from "@/components/ui";
import type { EvaluationOutcome, MeasureDefinition, SemanticModel, ValidationResult } from "@/lib/types";

export default function DaxStudioPage() {
  const { id } = useParams<{ id: string }>();
  const model = useAsync(() => optional(loadModel(id)), [id]);
  const [selected, setSelected] = useState<string>("");

  const measures = model.data?.artifact.measures ?? [];
  useEffect(() => {
    if (!selected && measures.length) setSelected(measures[0].name);
  }, [measures, selected]);

  if (model.loading) return <Loading what="the model" />;
  if (model.error && !model.missing) {
    return (
      <>
        <PageHeader title="DAX Studio" />
        <ErrorBox message={model.error} />
      </>
    );
  }
  if (!model.data) {
    return (
      <>
        <PageHeader title="DAX Studio" />
        <Empty title="No measures exist yet">
          Propose a semantic model and run{" "}
          <span className="font-mono">generate_measures</span> from the{" "}
          <Link href={`/projects/${id}`} className="text-brand hover:underline">workspace</Link>.
        </Empty>
      </>
    );
  }

  const m: SemanticModel = model.data.artifact;
  const measure = measures.find((x) => x.name === selected);

  return (
    <>
      <PageHeader
        title="DAX Studio"
        description="Edit a measure, validate it against the model, and evaluate it against the prepared data. Validation and execution are separate: an expression is only stored once it parses and every column it names exists."
        actions={<Badge tone="muted">model v{m.version}</Badge>}
      />

      <Grid cols={4}>
        <Stat label="Measures" value={num(measures.length)} />
        <Stat
          label="Needing a date table"
          value={num(measures.filter((x) => x.requires_date_table).length)}
          hint="Time intelligence will not fold without one"
        />
        <Stat
          label="With validation cases"
          value={num(measures.filter((x) => x.validation_cases.length).length)}
          hint="Checked against a figure from the source"
        />
        <Stat
          label="Low confidence"
          value={num(measures.filter((x) => x.confidence === "low").length)}
          tone={measures.some((x) => x.confidence === "low") ? "warn" : "ok"}
        />
      </Grid>

      <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-4">
        <Card title="Measures" dense className="xl:col-span-1">
          <ul className="max-h-[32rem] divide-y divide-paper-line overflow-y-auto">
            {measures.map((x) => (
              <li key={x.id}>
                <button
                  onClick={() => setSelected(x.name)}
                  className={`block w-full px-3 py-2 text-left transition hover:bg-paper-soft ${
                    x.name === selected ? "bg-brand-light/50" : ""
                  }`}
                >
                  <p className="text-xs font-medium text-ink">{x.name}</p>
                  <p className="font-mono text-[10px] text-muted">
                    {x.home_table}
                    {x.display_folder ? ` · ${x.display_folder}` : ""}
                  </p>
                </button>
              </li>
            ))}
          </ul>
        </Card>

        <div className="xl:col-span-3">
          {measure ? (
            <MeasureEditor key={measure.id} projectId={id} measure={measure} model={m} onSaved={() => model.reload()} />
          ) : (
            <Card title="No measure selected"><Empty title="Pick a measure on the left" /></Card>
          )}
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
function MeasureEditor({
  projectId, measure, model, onSaved,
}: { projectId: string; measure: MeasureDefinition; model: SemanticModel; onSaved: () => void }) {
  const [expression, setExpression] = useState(measure.expression);
  const [formatString, setFormatString] = useState(measure.format_string);
  const [description, setDescription] = useState(measure.description);
  const [validation, setValidation] = useState<ValidationResult[] | null>(null);
  const [saveNote, setSaveNote] = useState<string>("");
  const [outcome, setOutcome] = useState<EvaluationOutcome | null>(null);
  const [filterText, setFilterText] = useState("");

  useEffect(() => {
    setExpression(measure.expression);
    setFormatString(measure.format_string);
    setDescription(measure.description);
    setValidation(null);
    setOutcome(null);
    setSaveNote("");
  }, [measure.id, measure.expression, measure.format_string, measure.description]);

  const dirty =
    expression !== measure.expression ||
    formatString !== measure.format_string ||
    description !== measure.description;

  const save = useAction(async (force: boolean) => {
    const res = await api.editMeasure(projectId, measure.name, {
      expression, format_string: formatString, description, force,
    });
    setValidation(res.validation);
    setSaveNote(
      res.saved
        ? "Saved. The model artifact was written as a new version; the previous one is still readable."
        : res.reason ?? "Not saved.",
    );
    if (res.saved) onSaved();
    return res;
  });

  const evaluate = useAction(async () => {
    const filters = parseFilters(filterText);
    const res = await api.evaluateMeasure(projectId, measure.name, filters);
    setOutcome(res);
    return res;
  });

  const blocking = (validation ?? []).filter(
    (r) => r.status === "fail" || r.status === "error",
  );

  return (
    <div className="space-y-4">
      <Card
        title={measure.name}
        subtitle={
          <>
            home table <span className="font-mono">{measure.home_table}</span>
            {measure.kpi_id && <> · KPI <span className="font-mono">{measure.kpi_id}</span></>}
            {measure.unit && <> · unit {measure.unit}</>}
          </>
        }
        actions={
          <>
            <ConfidenceBadge confidence={measure.confidence} />
            {measure.requires_date_table && <Badge tone="warn">needs date table</Badge>}
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-ink">Expression</label>
            <textarea
              className={monoInputClass}
              rows={Math.min(18, Math.max(6, expression.split("\n").length + 2))}
              value={expression}
              spellCheck={false}
              onChange={(e) => setExpression(e.target.value)}
            />
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs font-medium text-ink">Format string</label>
              <input
                className={`${inputClass} font-mono text-xs`}
                value={formatString}
                onChange={(e) => setFormatString(e.target.value)}
                placeholder="#,##0.00"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-ink">Description</label>
              <input
                className={inputClass}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="primary"
              disabled={!dirty}
              pending={save.pending}
              onClick={() => void save.run(false)}
              title="Validates the expression, then stores it only if nothing blocking failed."
            >
              Validate &amp; save
            </Button>
            {validation && blocking.length > 0 && (
              <Button
                variant="danger"
                pending={save.pending}
                onClick={() => void save.run(true)}
                title="Stores the expression despite the failures. It is marked unapproved, so QA will keep reporting it."
              >
                Save anyway ({blocking.length} failing)
              </Button>
            )}
            <Button
              disabled={dirty}
              pending={evaluate.pending}
              onClick={() => void evaluate.run()}
              title={dirty ? "Save first — evaluation runs the stored expression." : "Execute against the prepared data via DuckDB"}
            >
              Evaluate
            </Button>
            {dirty && <span className="text-[11px] text-muted">Unsaved changes.</span>}
          </div>

          {save.error && <ErrorBox message={save.error} />}
          {saveNote && (
            <Notice tone={blocking.length ? "warn" : "ok"}>{saveNote}</Notice>
          )}
        </div>
      </Card>

      {validation && (
        <Card
          title="Validation"
          subtitle="Three gates, each reported separately: the expression parses; every table, column and measure it names exists in the model; and it executes against the prepared data. A construct outside the supported subset is reported as not supported — never as a pass."
          dense
        >
          <Table head={["Gate", "Status", "Expected", "Actual", "Engine", "Fix"]}>
            {validation.map((r) => (
              <tr key={r.id} className={r.status === "fail" || r.status === "error" ? "bg-red-50/40" : undefined}>
                <Td>
                  <p className="font-medium">{r.test_name}</p>
                  <p className="font-mono text-[10px] text-muted">{r.category}</p>
                </Td>
                <Td><TestStatusBadge status={r.status} /></Td>
                <Td className="max-w-xs text-muted">{r.expected || DASH}</Td>
                <Td className="max-w-xs">{r.actual || DASH}</Td>
                <Td mono className="text-[11px] text-muted">{r.engine || DASH}</Td>
                <Td className="max-w-xs text-muted">
                  {r.recommended_fix || r.skipped_reason || DASH}
                </Td>
              </tr>
            ))}
          </Table>
        </Card>
      )}

      <Card
        title="Evaluate"
        subtitle="Executed by translating the supported subset into SQL over DuckDB. Each aggregation is its own query and the arithmetic is combined afterwards, so a measure that mixes a dimension column with a fact column cannot double-count."
        dense
      >
        <div className="space-y-3 p-4">
          <div>
            <label className="mb-1 block text-xs font-medium text-ink">
              Filters <span className="font-normal text-muted">(one per line, Table[Column]=Value)</span>
            </label>
            <textarea
              className={monoInputClass}
              rows={3}
              value={filterText}
              spellCheck={false}
              onChange={(e) => setFilterText(e.target.value)}
              placeholder={"DimProject[project_name]=Atlas\nDimDate[year]=2025"}
            />
          </div>
          <Button pending={evaluate.pending} disabled={dirty} onClick={() => void evaluate.run()}>
            Run
          </Button>
          {evaluate.error && <ErrorBox message={evaluate.error} />}

          {outcome && (
            <div className="rounded-md border border-paper-line">
              <div className="flex flex-wrap items-center gap-2 border-b border-paper-line px-3 py-2">
                {outcome.supported ? (
                  <Badge tone="ok">executed</Badge>
                ) : (
                  <Badge tone="warn">not supported</Badge>
                )}
                <span className="font-mono text-[11px] text-muted">{outcome.engine}</span>
                <span className="ml-auto text-[11px] text-muted">{duration(outcome.duration_ms)}</span>
              </div>
              <div className="px-3 py-3">
                {outcome.supported ? (
                  <p className="text-2xl font-semibold tabular-nums text-ink">
                    {outcome.is_blank ? (
                      <span className="text-base font-normal text-muted">
                        BLANK — the filter context selected no rows
                      </span>
                    ) : (
                      decimal(outcome.value, 2)
                    )}
                  </p>
                ) : (
                  <>
                    <p className="text-sm text-amber-700">
                      {outcome.reason || "This expression is outside the executable subset."}
                    </p>
                    {outcome.unsupported_construct && (
                      <p className="mt-1 font-mono text-[11px] text-muted">
                        construct: {outcome.unsupported_construct}
                      </p>
                    )}
                    <p className="mt-1.5 text-[11px] text-muted">
                      It is reported as unsupported rather than assumed correct. Validate it in
                      Power BI Desktop after opening the exported project.
                    </p>
                  </>
                )}
                {outcome.notes.length > 0 && (
                  <ul className="mt-2 list-inside list-disc text-[11px] text-muted">
                    {outcome.notes.map((n, i) => <li key={i}>{n}</li>)}
                  </ul>
                )}
              </div>
              {outcome.sql_executed.length > 0 && (
                <details className="border-t border-paper-line">
                  <summary className="cursor-pointer px-3 py-2 text-[11px] text-muted">
                    SQL executed ({outcome.sql_executed.length} statement
                    {outcome.sql_executed.length === 1 ? "" : "s"})
                  </summary>
                  <div className="space-y-2 px-3 pb-3">
                    {outcome.sql_executed.map((sql, i) => <Code key={i}>{sql}</Code>)}
                  </div>
                </details>
              )}
            </div>
          )}
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Dependencies & assumptions" dense>
          <div className="space-y-2.5 p-4 text-xs">
            <Dep label="Columns" items={measure.depends_on_columns} />
            <Dep label="Measures" items={measure.depends_on_measures} />
            {measure.aggregation_behaviour && (
              <p className="text-muted">
                <span className="font-medium text-ink">Aggregation behaviour:</span>{" "}
                {measure.aggregation_behaviour}
              </p>
            )}
            {measure.assumptions.length > 0 ? (
              <div>
                <p className="font-medium text-ink">Assumptions</p>
                <ul className="mt-0.5 list-inside list-disc text-muted">
                  {measure.assumptions.map((a, i) => <li key={i}>{a}</li>)}
                </ul>
              </div>
            ) : (
              <p className="text-muted">No assumptions were recorded for this measure.</p>
            )}
            {measure.provenance && (
              <p className="text-[11px] text-muted">
                {measure.provenance.origin} · {measure.provenance.produced_by} ·{" "}
                {measure.provenance.rationale}
              </p>
            )}
          </div>
        </Card>

        <Card
          title="Validation cases"
          subtitle="Figures the measure is expected to reproduce, taken from the source data."
          dense
        >
          {measure.validation_cases.length === 0 ? (
            <div className="p-4">
              <Empty title="No validation case was defined">
                QA will still check the expression and its references, but it cannot confirm the
                number against an independent figure.
              </Empty>
            </div>
          ) : (
            <Table head={["Case", "Filters", "Expected from", "Comparison"]}>
              {measure.validation_cases.map((c) => (
                <tr key={c.id}>
                  <Td className="font-medium">{c.name}</Td>
                  <Td mono className="text-[11px]">
                    {Object.entries(c.filters).map(([k, v]) => `${k}=${v}`).join(", ") || "none"}
                  </Td>
                  <Td className="text-muted">{c.expected_source}</Td>
                  <Td mono className="text-[11px]">{c.comparison}</Td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      </div>

      <Card title="Model reference" subtitle="What the expression may name." dense>
        <Table head={["Table", "Role", "Columns"]}>
          {model.tables.map((t) => (
            <tr key={t.name}>
              <Td mono className="text-[11px] font-medium">{t.name}</Td>
              <Td><Badge tone="muted">{t.role}</Badge></Td>
              <Td className="font-mono text-[10px] leading-relaxed text-muted">
                {t.columns.map((c) => c.name).join(", ")}
              </Td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}

function Dep({ label, items }: { label: string; items: string[] }) {
  return (
    <p className="text-muted">
      <span className="font-medium text-ink">{label}:</span>{" "}
      {items.length ? <span className="font-mono text-[11px]">{items.join(", ")}</span> : DASH}
    </p>
  );
}

/** `Table[Column]=Value` per line. Anything else is ignored rather than
 *  guessed at, so a typo cannot silently change the filter context. */
function parseFilters(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const m = /^\s*([^=]+?)\s*=\s*(.+?)\s*$/.exec(line);
    if (m) out[m[1]] = m[2];
  }
  return out;
}
