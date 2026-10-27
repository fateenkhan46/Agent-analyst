"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { useAction } from "@/lib/hooks";
import {
  Button, Card, ErrorBox, Field, Notice, PageHeader, inputClass, monoInputClass,
} from "@/components/ui";

const SAMPLE_TRANSCRIPT = `[00:01] Priya Raman (Programme Director): Thanks everyone. The board wants one view of delivery cost against budget by month.
[00:02] Daniel Okoro (Finance): Agreed. Note that cost is reported in the source currency — we report in USD only, so exclude the EUR vendor line until Treasury confirms a rate.
[00:04] Priya Raman: Decision: the dashboard reports USD only this quarter.
[00:05] Sara Lund (Delivery): Can we see utilisation by consultant as well?
[00:06] Daniel Okoro: Utilisation needs a definition. Is it billable hours over capacity, or over logged hours?
[00:07] Priya Raman: Open question for Finance — park it, do not guess.
[00:08] Daniel Okoro: Action: I will send the FY25 cost extract by Friday.
`;

export default function NewProjectPage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [objective, setObjective] = useState("");
  const [description, setDescription] = useState("");
  const [kpis, setKpis] = useState("");
  const [questions, setQuestions] = useState("");
  const [period, setPeriod] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [units, setUnits] = useState("");
  const [transcript, setTranscript] = useState("");

  const create = useAction(async () => {
    const { project } = await api.createProject({
      name: name.trim(),
      description,
      business_objective: objective,
      desired_kpis: splitLines(kpis),
      reporting_questions: splitLines(questions),
      reporting_period: period,
      currency,
      units,
      meeting_transcript: transcript,
    });
    router.push(`/projects/${project.id}`);
    return project;
  });

  const canSubmit = name.trim().length > 0 && !create.pending;

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader
        title="New project"
        description="Only the name is required. Everything else narrows what the agents have to infer — and whatever you leave out, the agents will ask about rather than invent."
      />

      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) void create.run();
        }}
      >
        <Card title="Brief">
          <div className="space-y-4">
            <Field label="Project name" required>
              <input
                className={inputClass}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="FY25 Delivery Cost Review"
                maxLength={160}
              />
            </Field>
            <Field label="Business objective" hint="What decision should this report support?">
              <textarea
                className={inputClass}
                rows={2}
                value={objective}
                onChange={(e) => setObjective(e.target.value)}
                placeholder="Show delivery cost against budget by month and project, so the board can spot overruns early."
              />
            </Field>
            <Field label="Description">
              <textarea
                className={inputClass}
                rows={2}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </Field>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Field label="Desired KPIs" hint="One per line. Left blank, they are derived from the transcript and brief.">
                <textarea
                  className={inputClass}
                  rows={4}
                  value={kpis}
                  onChange={(e) => setKpis(e.target.value)}
                  placeholder={"Total Cost\nBudget Variance %\nCost per Project"}
                />
              </Field>
              <Field label="Reporting questions" hint="One per line.">
                <textarea
                  className={inputClass}
                  rows={4}
                  value={questions}
                  onChange={(e) => setQuestions(e.target.value)}
                  placeholder={"Which projects are over budget?\nHow has cost trended month over month?"}
                />
              </Field>
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <Field label="Reporting period">
                <input
                  className={inputClass}
                  value={period}
                  onChange={(e) => setPeriod(e.target.value)}
                  placeholder="FY25"
                />
              </Field>
              <Field label="Reporting currency" hint="Used to decide which rows a currency guard admits.">
                <input
                  className={inputClass}
                  value={currency}
                  onChange={(e) => setCurrency(e.target.value.toUpperCase().slice(0, 8))}
                  placeholder="USD"
                />
              </Field>
              <Field label="Units" hint="e.g. hours, FTE">
                <input
                  className={inputClass}
                  value={units}
                  onChange={(e) => setUnits(e.target.value)}
                />
              </Field>
            </div>
          </div>
        </Card>

        <Card
          title="Meeting transcript"
          subtitle="Stored as a source file, so the minutes can cite line numbers against something immutable."
          actions={
            <Button onClick={() => setTranscript(SAMPLE_TRANSCRIPT)} variant="ghost">
              Insert example
            </Button>
          }
        >
          <Notice tone="info">
            The requirements agent reads this to produce <strong>Minutes of Meeting</strong> —
            attendees, decisions, action items and open questions, each citing the transcript lines
            it came from — and to extract the KPI requirements. A question that is asked and then
            deferred is recorded as an <em>open question</em>, not answered. You can also attach a
            transcript later, or upload it as a file.
          </Notice>
          <div className="mt-3">
            <textarea
              className={monoInputClass}
              rows={10}
              value={transcript}
              onChange={(e) => setTranscript(e.target.value)}
              placeholder={"[00:01] Speaker (Role): …\n[00:02] Another Speaker: …"}
            />
            <p className="mt-1 text-[11px] text-muted">
              {transcript ? `${transcript.split("\n").length} lines` : "Optional"}
            </p>
          </div>
        </Card>

        {create.error && <ErrorBox title="The project was not created" message={create.error} />}

        <div className="flex items-center gap-2 pb-8">
          <Button type="submit" variant="primary" size="md" disabled={!canSubmit} pending={create.pending}>
            Create project
          </Button>
          <span className="text-xs text-muted">
            Next step: upload your data files, then run the pipeline.
          </span>
        </div>
      </form>
    </div>
  );
}

function splitLines(s: string): string[] {
  return s.split("\n").map((l) => l.trim()).filter(Boolean);
}
