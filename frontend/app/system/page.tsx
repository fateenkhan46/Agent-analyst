"use client";

import { api } from "@/lib/api";
import { money, num } from "@/lib/format";
import { useAsync } from "@/lib/hooks";
import {
  Badge, Card, ErrorBox, Loading, Notice, PageHeader, Table, Td,
} from "@/components/ui";
import type { CapabilityStatus } from "@/lib/types";

const STATUS_TONE: Record<CapabilityStatus, "ok" | "warn" | "bad" | "muted"> = {
  implemented: "ok",
  not_configured: "warn",
  unavailable: "bad",
  not_implemented: "muted",
};

const STATUS_MEANING: Record<CapabilityStatus, string> = {
  implemented: "Built and exercised by the test suite.",
  not_configured: "Built, but needs a credential or setting before it will run here.",
  unavailable: "Cannot work in this environment — the dependency is absent or the platform is wrong.",
  not_implemented: "Not built.",
};

export default function SystemPage() {
  const caps = useAsync(() => api.capabilities(), []);
  const agents = useAsync(() => api.agents(), []);
  const contracts = useAsync(() => api.contracts(), []);

  return (
    <>
      <PageHeader
        title="Capabilities & agents"
        description="The single source of truth for what this installation can do. The UI reads this endpoint rather than asserting support of its own, so no page can advertise an integration the backend does not have."
      />

      {caps.error && <ErrorBox title="Capabilities could not be read" message={caps.error} />}
      {caps.loading && <Loading what="capabilities" />}

      {caps.data && (
        <>
          <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-4">
            {(Object.keys(STATUS_TONE) as CapabilityStatus[]).map((s) => {
              const n = caps.data!.capabilities.filter((c) => c.status === s).length;
              return (
                <div key={s} className="rounded-lg border border-paper-line bg-white p-3">
                  <div className="flex items-center justify-between">
                    <Badge tone={STATUS_TONE[s]}>{s.replace(/_/g, " ")}</Badge>
                    <span className="text-lg font-semibold tabular-nums">{num(n)}</span>
                  </div>
                  <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
                    {STATUS_MEANING[s]}
                  </p>
                </div>
              );
            })}
          </div>

          <Card
            title="Capability register"
            subtitle="“Tested” means a test in the suite exercises this capability, not merely that code exists."
            dense
            className="mb-4"
          >
            <Table head={["Capability", "Status", "Tested", "Detail", "Requires"]}>
              {caps.data.capabilities.map((c) => (
                <tr key={c.key}>
                  <Td>
                    <p className="font-medium text-ink">{c.title}</p>
                    <p className="font-mono text-[11px] text-muted">{c.key}</p>
                  </Td>
                  <Td><Badge tone={STATUS_TONE[c.status]}>{c.status.replace(/_/g, " ")}</Badge></Td>
                  <Td>
                    {c.tested ? <Badge tone="ok">yes</Badge> : <Badge tone="muted">no</Badge>}
                  </Td>
                  <Td className="max-w-xl text-muted">{c.detail}</Td>
                  <Td className="text-[11px] text-muted">
                    {c.requires.length ? c.requires.join(", ") : "—"}
                  </Td>
                </tr>
              ))}
            </Table>
          </Card>

          <Notice tone="warn" title="Power BI environment">
            {caps.data.environment.powerbi_desktop_note}
          </Notice>
        </>
      )}

      <div className="mt-4">
        <Card
          title="Agents"
          subtitle="Each agent declares the service functions it may call. Anything outside its allowlist is denied at the tool belt, so an agent cannot reach a capability it was not granted."
          dense
        >
          {agents.loading && <div className="p-4"><Loading what="agents" /></div>}
          {agents.error && <div className="p-4"><ErrorBox message={agents.error} /></div>}
          {agents.data && (
            <Table head={["Agent", "Purpose", "In → Out", "LLM", "Timeout", "Attempts", "Budget", "Allowed tools"]}>
              {agents.data.agents.map((a) => (
                <tr key={a.name}>
                  <Td><span className="font-medium">{a.name}</span></Td>
                  <Td className="max-w-sm text-muted">{a.purpose}</Td>
                  <Td mono className="text-[11px]">
                    {a.input_schema} → {a.output_schema}
                  </Td>
                  <Td>
                    {a.uses_llm
                      ? <Badge tone="brand">yes</Badge>
                      : <Badge tone="muted" title="Deterministic: this agent makes no model calls at all.">no</Badge>}
                  </Td>
                  <Td align="right">{a.policy.timeout_seconds}s</Td>
                  <Td align="right">{a.policy.max_attempts}</Td>
                  <Td align="right">{money(a.policy.max_cost_usd)}</Td>
                  <Td className="max-w-xs font-mono text-[10px] leading-relaxed text-muted">
                    {a.policy.retry_on.length > 0 && (
                      <p className="mb-1 font-sans text-[11px]">
                        retries on: {a.policy.retry_on.join(", ")}
                      </p>
                    )}
                    {a.allowed_tools.join(", ") || "—"}
                  </Td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      </div>

      <div className="mt-4">
        <Card
          title="Inter-agent contracts"
          subtitle="JSON Schema for every typed contract the agents pass between each other. The test suite snapshots this payload so a breaking change cannot slip through."
        >
          {contracts.loading && <Loading what="contracts" />}
          {contracts.error && <ErrorBox message={contracts.error} />}
          {contracts.data && (
            <>
              <p className="mb-3 text-xs text-muted">
                {num(contracts.data.count)} contracts published.
              </p>
              <div className="flex flex-wrap gap-1.5">
                {Object.keys(contracts.data.contracts).sort().map((name) => (
                  <span
                    key={name}
                    className="rounded border border-paper-line bg-paper-soft px-1.5 py-0.5 font-mono text-[11px] text-ink"
                  >
                    {name}
                  </span>
                ))}
              </div>
              <p className="mt-3 text-[11px] text-muted">
                The full schemas are served at{" "}
                <code className="font-mono">GET /api/v1/system/contracts</code>.
              </p>
            </>
          )}
        </Card>
      </div>
    </>
  );
}
