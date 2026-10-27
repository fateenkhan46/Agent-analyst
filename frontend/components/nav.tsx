"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const PROJECT_NAV = [
  { slug: "", label: "Workspace", hint: "Run the agents, approve their proposals" },
  { slug: "data", label: "Data Explorer", hint: "Profiles, previews, quality issues" },
  { slug: "model", label: "Data Model", hint: "Tables, relationships, diagram" },
  { slug: "dax", label: "DAX Studio", hint: "Edit, validate and evaluate measures" },
  { slug: "report", label: "Report Designer", hint: "Pages, visuals, field bindings" },
  { slug: "qa", label: "QA & Recommendations", hint: "Validation results, improvements" },
  { slug: "delivery", label: "Deliverables", hint: "MoM, PBIP export, downloads" },
  { slug: "audit", label: "Activity & Audit Log", hint: "Executions and audit trail" },
];

export function Sidebar({ projectId, projectName }: { projectId?: string; projectName?: string }) {
  const pathname = usePathname() ?? "";

  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-paper-line bg-white">
      <div className="border-b border-paper-line px-4 py-4">
        <Link href="/" className="block">
          <p className="text-sm font-semibold leading-tight text-ink">Power BI Agent</p>
          <p className="text-[11px] text-muted">Autonomous BI workforce</p>
        </Link>
      </div>

      <nav className="flex-1 overflow-y-auto px-2 py-3">
        <NavItem href="/" label="Dashboard" active={pathname === "/"} />
        <NavItem href="/projects/new" label="New project" active={pathname === "/projects/new"} />

        {projectId && (
          <>
            <p className="mt-5 truncate px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
              {projectName || "Project"}
            </p>
            {PROJECT_NAV.map((item) => {
              const href = item.slug
                ? `/projects/${projectId}/${item.slug}`
                : `/projects/${projectId}`;
              return (
                <NavItem
                  key={item.slug || "workspace"}
                  href={href}
                  label={item.label}
                  title={item.hint}
                  active={pathname === href}
                />
              );
            })}
          </>
        )}

        <p className="mt-5 px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
          System
        </p>
        <NavItem href="/system" label="Capabilities & agents" active={pathname === "/system"} />
      </nav>
    </aside>
  );
}

function NavItem({
  href, label, active, title,
}: { href: string; label: string; active: boolean; title?: string }) {
  return (
    <Link
      href={href}
      title={title}
      className={`block rounded-md px-2 py-1.5 text-[13px] transition ${
        active
          ? "bg-brand-light font-medium text-brand-dark"
          : "text-ink hover:bg-paper-soft"
      }`}
    >
      {label}
    </Link>
  );
}
