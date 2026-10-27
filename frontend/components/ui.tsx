"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { DASH, percent } from "@/lib/format";
import type { Issue, Severity, TestStatus } from "@/lib/types";

// ---------------------------------------------------------------------------
// Layout primitives
// ---------------------------------------------------------------------------
export function Card({
  title, subtitle, actions, children, className = "", dense = false,
}: {
  title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode;
  children: ReactNode; className?: string; dense?: boolean;
}) {
  return (
    <section className={`rounded-lg border border-paper-line bg-white shadow-sm ${className}`}>
      {(title || actions) && (
        <header className="flex items-start justify-between gap-4 border-b border-paper-line px-4 py-3">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-semibold text-ink">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={dense ? "" : "p-4"}>{children}</div>
    </section>
  );
}

export function PageHeader({
  title, description, actions,
}: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
        {description && <p className="mt-1 max-w-3xl text-sm text-muted">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Grid({ cols = 2, children }: { cols?: 2 | 3 | 4; children: ReactNode }) {
  const map = { 2: "md:grid-cols-2", 3: "md:grid-cols-3", 4: "md:grid-cols-2 xl:grid-cols-4" };
  return <div className={`grid grid-cols-1 gap-4 ${map[cols]}`}>{children}</div>;
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------
type ButtonProps = {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  disabled?: boolean;
  pending?: boolean;
  type?: "button" | "submit";
  title?: string;
};

export function Button({
  children, onClick, variant = "secondary", size = "sm",
  disabled, pending, type = "button", title,
}: ButtonProps) {
  const base =
    "inline-flex items-center justify-center gap-1.5 rounded-md border font-medium transition " +
    "disabled:cursor-not-allowed disabled:opacity-50";
  const sizes = { sm: "px-2.5 py-1.5 text-xs", md: "px-3.5 py-2 text-sm" };
  const variants = {
    primary: "border-brand bg-brand text-white hover:bg-brand-dark",
    secondary: "border-paper-line bg-white text-ink hover:bg-paper-soft",
    ghost: "border-transparent bg-transparent text-muted hover:text-ink hover:bg-paper-soft",
    danger: "border-bad bg-white text-bad hover:bg-red-50",
  };
  return (
    <button
      type={type} title={title} onClick={onClick} disabled={disabled || pending}
      className={`${base} ${sizes[size]} ${variants[variant]}`}
    >
      {pending && <Spinner />}
      {children}
    </button>
  );
}

export function LinkButton({
  href, children, variant = "secondary", external = false,
}: { href: string; children: ReactNode; variant?: "primary" | "secondary"; external?: boolean }) {
  const cls =
    "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-medium transition " +
    (variant === "primary"
      ? "border-brand bg-brand text-white hover:bg-brand-dark"
      : "border-paper-line bg-white text-ink hover:bg-paper-soft");
  if (external) {
    return <a href={href} className={cls}>{children}</a>;
  }
  return <Link href={href} className={cls}>{children}</Link>;
}

export function Spinner() {
  return (
    <span
      aria-hidden
      className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-r-transparent"
    />
  );
}

export function Field({
  label, hint, children, required,
}: { label: string; hint?: string; children: ReactNode; required?: boolean }) {
  return (
    <label className="block">
      <span className="block text-xs font-medium text-ink">
        {label} {required && <span className="text-bad">*</span>}
      </span>
      {hint && <span className="mt-0.5 block text-xs text-muted">{hint}</span>}
      <span className="mt-1.5 block">{children}</span>
    </label>
  );
}

export const inputClass =
  "w-full rounded-md border border-paper-line bg-white px-2.5 py-1.5 text-sm text-ink " +
  "placeholder:text-muted/70 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand";

export const monoInputClass = `${inputClass} font-mono text-xs leading-relaxed`;

// ---------------------------------------------------------------------------
// Status display
// ---------------------------------------------------------------------------
const SEVERITY_CLASS: Record<Severity, string> = {
  critical: "bg-red-50 text-bad border-bad/30",
  high: "bg-red-50 text-bad border-bad/30",
  medium: "bg-amber-50 text-amber-700 border-amber-300",
  low: "bg-slate-100 text-muted border-paper-line",
  info: "bg-slate-100 text-muted border-paper-line",
};

export function Badge({
  children, tone = "neutral", title,
}: {
  children: ReactNode;
  tone?: "neutral" | "ok" | "warn" | "bad" | "brand" | "muted";
  title?: string;
}) {
  const tones = {
    neutral: "bg-slate-100 text-ink border-paper-line",
    ok: "bg-emerald-50 text-ok border-ok/30",
    warn: "bg-amber-50 text-amber-700 border-amber-300",
    bad: "bg-red-50 text-bad border-bad/30",
    brand: "bg-brand-light text-brand-dark border-brand/30",
    muted: "bg-slate-50 text-muted border-paper-line",
  };
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-0.5 text-[11px] font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span
      className={`inline-flex rounded border px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide ${SEVERITY_CLASS[severity] ?? SEVERITY_CLASS.info}`}
    >
      {severity}
    </span>
  );
}

const TEST_TONE: Record<TestStatus, "ok" | "bad" | "warn" | "muted" | "neutral"> = {
  pass: "ok",
  fail: "bad",
  error: "bad",
  warn: "warn",
  skipped: "muted",
  not_supported: "muted",
};

export function TestStatusBadge({ status }: { status: TestStatus }) {
  return <Badge tone={TEST_TONE[status] ?? "neutral"}>{status.replace("_", " ")}</Badge>;
}

const TASK_TONE: Record<string, "ok" | "bad" | "warn" | "brand" | "muted" | "neutral"> = {
  succeeded: "ok",
  completed: "ok",
  failed: "bad",
  error: "bad",
  running: "brand",
  blocked: "warn",
  awaiting_approval: "warn",
  pending: "muted",
  skipped: "muted",
  ready: "neutral",
  needs_rework: "warn",
  timed_out: "bad",
};

export function StatusBadge({ status }: { status: string }) {
  return <Badge tone={TASK_TONE[status] ?? "neutral"}>{status.replace(/_/g, " ")}</Badge>;
}

export function ConfidenceBadge({ confidence }: { confidence: string }) {
  const tone = confidence === "high" ? "ok" : confidence === "medium" ? "warn" : "bad";
  return <Badge tone={tone} title="Agent confidence">{confidence}</Badge>;
}

export function ProgressBar({ value, label: showLabel = true }: { value: number; label?: boolean }) {
  const pct = Math.max(0, Math.min(1, value || 0));
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-paper-soft">
        <div
          className="h-full rounded-full bg-brand transition-all"
          style={{ width: `${pct * 100}%` }}
        />
      </div>
      {showLabel && (
        <span className="w-10 shrink-0 text-right text-xs tabular-nums text-muted">
          {percent(pct, 0)}
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------
export function ErrorBox({ title = "Something failed", message }: { title?: string; message: string }) {
  return (
    <div className="rounded-md border border-bad/30 bg-red-50 px-3 py-2.5 text-sm text-bad">
      <p className="font-semibold">{title}</p>
      <p className="mt-0.5 whitespace-pre-wrap text-xs leading-relaxed">{message}</p>
    </div>
  );
}

export function Notice({
  tone = "info", title, children,
}: { tone?: "info" | "warn" | "ok"; title?: string; children: ReactNode }) {
  const tones = {
    info: "border-brand/25 bg-brand-light/50 text-ink",
    warn: "border-amber-300 bg-amber-50 text-amber-900",
    ok: "border-ok/30 bg-emerald-50 text-emerald-900",
  };
  return (
    <div className={`rounded-md border px-3 py-2.5 text-xs leading-relaxed ${tones[tone]}`}>
      {title && <p className="mb-0.5 text-sm font-semibold">{title}</p>}
      {children}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-paper-line bg-paper-soft px-4 py-8 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      {children && <div className="mx-auto mt-1 max-w-xl text-xs text-muted">{children}</div>}
    </div>
  );
}

export function Loading({ what = "data" }: { what?: string }) {
  return (
    <div className="flex items-center gap-2 px-1 py-6 text-sm text-muted">
      <Spinner /> Loading {what}…
    </div>
  );
}

// ---------------------------------------------------------------------------
// Data display
// ---------------------------------------------------------------------------
export function Table({ head, children }: { head: ReactNode[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="border-b border-paper-line bg-paper-soft text-left">
            {head.map((h, i) => (
              <th key={i} className="whitespace-nowrap px-3 py-2 font-semibold text-muted">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-paper-line">{children}</tbody>
      </table>
    </div>
  );
}

export function Td({
  children, mono = false, align = "left", className = "", colSpan,
}: {
  children: ReactNode; mono?: boolean;
  align?: "left" | "right"; className?: string; colSpan?: number;
}) {
  return (
    <td
      colSpan={colSpan}
      className={`px-3 py-1.5 align-top ${mono ? "font-mono" : ""} ${align === "right" ? "text-right tabular-nums" : ""} ${className}`}
    >
      {children}
    </td>
  );
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
      {items.map(([k, v]) => (
        <div key={k} className="min-w-0">
          <dt className="text-[11px] uppercase tracking-wide text-muted">{k}</dt>
          <dd className="mt-0.5 break-words text-sm text-ink">{v ?? DASH}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Stat({
  label, value, hint, tone = "neutral",
}: { label: string; value: ReactNode; hint?: ReactNode; tone?: "neutral" | "ok" | "warn" | "bad" }) {
  const tones = { neutral: "text-ink", ok: "text-ok", warn: "text-amber-600", bad: "text-bad" };
  return (
    <div className="rounded-lg border border-paper-line bg-white p-4 shadow-sm">
      <p className="text-[11px] uppercase tracking-wide text-muted">{label}</p>
      <p className={`mt-1 text-2xl font-semibold tabular-nums ${tones[tone]}`}>{value}</p>
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
    </div>
  );
}

export function Code({ children, wrap = true }: { children: string; wrap?: boolean }) {
  return (
    <pre
      className={`rounded-md border border-paper-line bg-paper-soft p-3 font-mono text-[11px] leading-relaxed text-ink ${wrap ? "whitespace-pre-wrap break-words" : "overflow-x-auto"}`}
    >
      {children}
    </pre>
  );
}

export function IssueList({ issues, emptyText }: { issues: Issue[]; emptyText?: string }) {
  if (!issues.length) {
    return <p className="text-xs text-muted">{emptyText ?? "No issues recorded."}</p>;
  }
  return (
    <ul className="space-y-2">
      {issues.map((issue, i) => (
        <li key={`${issue.code}-${i}`} className="rounded-md border border-paper-line p-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <SeverityBadge severity={issue.severity} />
            <span className="font-mono text-[11px] text-muted">{issue.code}</span>
            {issue.location && (
              <span className="font-mono text-[11px] text-muted">{issue.location}</span>
            )}
          </div>
          <p className="mt-1 text-xs text-ink">{issue.message}</p>
          {issue.recommended_fix && (
            <p className="mt-1 text-xs text-muted">
              <span className="font-medium">Fix:</span> {issue.recommended_fix}
            </p>
          )}
        </li>
      ))}
    </ul>
  );
}

export function Prov({ provenance }: { provenance: { origin: string; produced_by: string; confidence: string; rationale?: string } | null }) {
  if (!provenance) return null;
  return (
    <p className="text-[11px] text-muted">
      {provenance.origin} · {provenance.produced_by} · confidence {provenance.confidence}
      {provenance.rationale ? ` · ${provenance.rationale}` : ""}
    </p>
  );
}
