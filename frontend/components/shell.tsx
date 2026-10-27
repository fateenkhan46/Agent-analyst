"use client";

import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Sidebar } from "./nav";
import { Badge } from "./ui";

/** The project id is taken from the URL rather than from React context so a
 *  deep link into any page works on a cold load. */
function projectIdFromPath(pathname: string): string | undefined {
  const m = /^\/projects\/(proj_[A-Za-z0-9_-]+)/.exec(pathname);
  return m ? m[1] : undefined;
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? "/";
  const projectId = projectIdFromPath(pathname);
  const [projectName, setProjectName] = useState<string>("");
  const [env, setEnv] = useState<{ provider: string; dev: boolean; db: string } | null>(null);
  const [backendDown, setBackendDown] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .capabilities()
      .then(({ environment }) => {
        if (cancelled) return;
        setBackendDown(false);
        setEnv({
          provider: environment.llm_provider_active,
          dev: environment.llm_is_development_mode,
          db: environment.database,
        });
      })
      .catch(() => {
        if (!cancelled) setBackendDown(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!projectId) {
      setProjectName("");
      return;
    }
    let cancelled = false;
    api
      .getProject(projectId)
      .then(({ project }) => {
        if (!cancelled) setProjectName(project.name);
      })
      .catch(() => {
        if (!cancelled) setProjectName("");
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  return (
    <div className="flex min-h-screen">
      <Sidebar projectId={projectId} projectName={projectName} />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-end gap-2 border-b border-paper-line bg-white px-6 py-2.5">
          {backendDown && (
            <Badge tone="bad" title="The frontend proxies /api to the backend; start it with uvicorn.">
              backend unreachable
            </Badge>
          )}
          {env && (
            <>
              <Badge tone="muted" title="Database the backend is persisting to">
                {env.db}
              </Badge>
              <Badge
                tone={env.dev ? "warn" : "ok"}
                title={
                  env.dev
                    ? "No ANTHROPIC_API_KEY is configured, so deterministic heuristics stand in " +
                      "for the model. Anything they cannot derive is reported as unavailable " +
                      "rather than guessed."
                    : "Live LLM provider"
                }
              >
                LLM: {env.provider}
                {env.dev ? " (development heuristics)" : ""}
              </Badge>
            </>
          )}
        </header>
        <main className="min-w-0 flex-1 px-6 py-6">{children}</main>
      </div>
    </div>
  );
}
