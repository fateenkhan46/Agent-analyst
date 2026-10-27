// Typed client over the FastAPI backend.
//
// Every request goes to a relative `/api/v1/...` URL. Next's `rewrites()`
// proxies that to the backend server-side, so the backend host never reaches
// the client bundle and there is no API key in the browser at all. The auth
// token, when the backend requires one, is held in sessionStorage rather than
// baked in at build time.

import type {
  AgentExecution, AgentTask, ApprovalRequest, ArtifactVersion, AuditEvent, Capability,
  DashboardData, DeliveryBundle, EnvironmentSummary, EvaluationOutcome,
  ExportArtifact, ImprovementReport, MeasureDefinition, MeetingMinutes,
  ModelDiagram, Project, ProjectSummary, ReportSpec, RequirementsSpec, SemanticModel,
  SourceDataProfile, SourceFile, TransformationExecution, TransformationPlan,
  ValidationReport, ValidationResult,
} from "./types";

const BASE = "/api/v1";
const TOKEN_KEY = "pbi-agent-token";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
  /** 404 from an artifact endpoint means "not produced yet", not a failure. */
  get isMissing(): boolean {
    return this.status === 404;
  }
}

export function getToken(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage disabled; the session stays unauthenticated */
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { query?: Record<string, string | number | undefined> } = {},
): Promise<T> {
  const { query, ...rest } = init;
  let url = `${BASE}${path}`;
  if (query) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== "") params.set(k, String(v));
    }
    const qs = params.toString();
    if (qs) url += `?${qs}`;
  }
  const headers = new Headers(rest.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (rest.body && !(rest.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let res: Response;
  try {
    res = await fetch(url, { ...rest, headers, cache: "no-store" });
  } catch (err) {
    throw new ApiError(0, `Could not reach the backend at ${url}. Is it running?`);
  }

  if (res.status === 204) return undefined as T;

  const contentType = res.headers.get("content-type") ?? "";
  const isJson = contentType.includes("application/json");
  const payload = isJson ? await res.json().catch(() => null) : await res.text();

  if (!res.ok) {
    const detail =
      (isJson && payload && typeof payload === "object" && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : typeof payload === "string" && payload
          ? payload.slice(0, 400)
          : "") || `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, detail, payload);
  }
  return payload as T;
}

const get = <T>(p: string, query?: Record<string, string | number | undefined>) =>
  request<T>(p, { method: "GET", query });
const post = <T>(p: string, body?: unknown) =>
  request<T>(p, { method: "POST", body: body === undefined ? "{}" : JSON.stringify(body) });
const put = <T>(p: string, body: unknown) =>
  request<T>(p, { method: "PUT", body: JSON.stringify(body) });

// ---------------------------------------------------------------------------
// System + auth
// ---------------------------------------------------------------------------
export const api = {
  health: () => get<{ status: string }>("/system/health"),

  capabilities: () =>
    get<{ capabilities: Capability[]; environment: EnvironmentSummary }>("/system/capabilities"),

  contracts: () =>
    get<{ count: number; contracts: Record<string, unknown> }>("/system/contracts"),

  agents: () =>
    get<{
      agents: {
        name: string;
        purpose: string;
        input_schema: string;
        output_schema: string;
        allowed_tools: string[];
        uses_llm: boolean;
        policy: {
          timeout_seconds: number;
          max_attempts: number;
          retry_on: string[];
          max_cost_usd: number;
          on_exhaustion: string;
        };
      }[];
    }>("/system/agents"),

  me: () =>
    get<{
      user_id: string; email: string; tenant_id: string; role: string;
      development_mode: boolean; note: string;
    }>("/auth/me"),

  login: (email: string, password: string) =>
    post<{ token: string; user: { id: string; email: string; display_name: string } }>(
      "/auth/login", { email, password },
    ),

  register: (email: string, password: string, display_name = "") =>
    post<{ token: string; user: { id: string; email: string; display_name: string } }>(
      "/auth/register", { email, password, display_name },
    ),

  // -------------------------------------------------------------------------
  // Dashboard + projects
  // -------------------------------------------------------------------------
  dashboard: () => get<DashboardData>("/dashboard"),

  listProjects: () => get<{ projects: ProjectSummary[] }>("/projects"),

  createProject: (body: {
    name: string;
    description?: string;
    business_objective?: string;
    desired_kpis?: string[];
    reporting_questions?: string[];
    reporting_period?: string;
    currency?: string;
    units?: string;
    business_definitions?: Record<string, string>;
    meeting_transcript?: string;
  }) => post<{ project: Project }>("/projects", body),

  getProject: (id: string) =>
    get<{
      project: Project;
      files: SourceFile[];
      tasks: AgentTask[];
      progress: number;
      approvals_pending: ApprovalRequest[];
      spend_usd: number;
    }>(`/projects/${id}`),

  deleteProject: (id: string) => request<void>(`/projects/${id}`, { method: "DELETE" }),

  uploadFiles: async (id: string, files: File[]) => {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    return request<{
      accepted: SourceFile[];
      rejected: { filename: string; reason: string }[];
    }>(`/projects/${id}/files`, { method: "POST", body: form });
  },

  addTranscript: async (id: string, text: string, filename = "meeting_transcript.md") => {
    const form = new FormData();
    form.append("text", text);
    form.append("filename", filename);
    return request<{ file: SourceFile }>(`/projects/${id}/transcript`, {
      method: "POST", body: form,
    });
  },

  listFiles: (id: string) => get<{ files: SourceFile[] }>(`/projects/${id}/files`),

  runProfile: (id: string) => post<{ profile: SourceDataProfile }>(`/projects/${id}/profile`),

  getProfile: (id: string) =>
    get<{
      profile: SourceDataProfile;
      issue_counts: Record<string, number>;
      versions: ArtifactVersion[];
    }>(`/projects/${id}/profile`),

  tableRows: (id: string, table: string, limit = 100, offset = 0) =>
    get<{
      table: string; columns: string[]; rows: Record<string, string>[];
      total_rows: number; limit: number; offset: number;
    }>(`/projects/${id}/tables/${encodeURIComponent(table)}/rows`, { limit, offset }),

  audit: (id: string, limit = 200, offset = 0) =>
    get<{ events: AuditEvent[]; total: number }>(`/projects/${id}/audit`, { limit, offset }),

  // -------------------------------------------------------------------------
  // Orchestration
  // -------------------------------------------------------------------------
  tasks: (id: string) =>
    get<{ tasks: AgentTask[]; progress: number; runnable: string[] }>(`/projects/${id}/tasks`),

  runTask: (id: string, kind: string, payload: Record<string, unknown> = {}) =>
    post<{
      succeeded?: boolean; blocked?: boolean; reason?: string;
      approvals?: ApprovalRequest[]; task?: AgentTask; execution?: AgentExecution;
      [k: string]: unknown;
    }>(`/projects/${id}/tasks/${kind}/run`, payload),

  runPipeline: (id: string, opts: { stop_after?: string; auto_approve?: boolean } = {}) =>
    post<{
      steps: { kind: string; succeeded: boolean; reason?: string; [k: string]: unknown }[];
      progress: number;
      pending_approvals: ApprovalRequest[];
    }>(`/projects/${id}/run`, opts),

  approvals: (id: string, state?: string) =>
    get<{ approvals: ApprovalRequest[] }>(`/projects/${id}/approvals`, { state }),

  decideApproval: (id: string, approvalId: string, approve: boolean, note = "") =>
    post<{ approval: ApprovalRequest }>(`/projects/${id}/approvals/${approvalId}`, {
      approve, note,
    }),

  executions: (id: string, limit = 50) =>
    get<{ executions: AgentExecution[] }>(`/projects/${id}/executions`, { limit }),

  // -------------------------------------------------------------------------
  // Artifacts
  // -------------------------------------------------------------------------
  artifactIndex: (id: string) =>
    get<{ artifacts: Record<string, { exists: boolean; versions: ArtifactVersion[] }> }>(
      `/projects/${id}/artifacts`,
    ),

  artifact: <T>(id: string, name: ArtifactName, version?: number) =>
    get<{ artifact: T; versions: ArtifactVersion[] }>(`/projects/${id}/artifacts/${name}`, { version }),

  modelDiagram: (id: string, version?: number) =>
    get<{ diagram: ModelDiagram }>(`/projects/${id}/model/diagram`, { version }),

  editMeasure: (
    id: string,
    name: string,
    body: { expression?: string; description?: string; format_string?: string; force?: boolean },
  ) =>
    put<{
      saved: boolean;
      measure?: MeasureDefinition;
      validation: ValidationResult[];
      reason?: string;
    }>(`/projects/${id}/measures/${encodeURIComponent(name)}`, body),

  evaluateMeasure: (id: string, name: string, filters: Record<string, string> = {}) =>
    post<EvaluationOutcome>(
      `/projects/${id}/measures/${encodeURIComponent(name)}/evaluate`, { filters },
    ),

  decideImprovement: (id: string, improvementId: string, approve: boolean) =>
    put<{ recommendation: ImprovementReport["recommendations"][number]; applied: boolean; note: string }>(
      `/projects/${id}/improvements/${improvementId}`, { approve },
    ),

  // -------------------------------------------------------------------------
  // Deliverables
  // -------------------------------------------------------------------------
  delivery: (id: string) => get<DeliveryBundle>(`/projects/${id}/delivery`),

  exports: (id: string) =>
    get<{ exports: ExportArtifact[]; capabilities: Capability[] }>(`/projects/${id}/exports`),

  downloadHref: (id: string, exportId: string) =>
    `${BASE}/projects/${id}/exports/${exportId}/download`,

  minutesHref: (id: string) => `${BASE}/projects/${id}/minutes.md`,

  minutesMarkdown: (id: string) => request<string>(`/projects/${id}/minutes.md`),
};

export type ArtifactName =
  | "requirements" | "minutes" | "plan" | "execution"
  | "model" | "report" | "validation" | "improvements";

// Narrow helpers so pages do not have to repeat the generic parameter.
export const loadMinutes = (id: string, v?: number) => api.artifact<MeetingMinutes>(id, "minutes", v);
export const loadRequirements = (id: string, v?: number) => api.artifact<RequirementsSpec>(id, "requirements", v);
export const loadPlan = (id: string, v?: number) => api.artifact<TransformationPlan>(id, "plan", v);
export const loadExecution = (id: string, v?: number) => api.artifact<TransformationExecution>(id, "execution", v);
export const loadModel = (id: string, v?: number) => api.artifact<SemanticModel>(id, "model", v);
export const loadReport = (id: string, v?: number) => api.artifact<ReportSpec>(id, "report", v);
export const loadValidation = (id: string, v?: number) => api.artifact<ValidationReport>(id, "validation", v);
export const loadImprovements = (id: string, v?: number) => api.artifact<ImprovementReport>(id, "improvements", v);

/** Resolve a promise to its value, or to null when the artifact simply does
 *  not exist yet. Any other failure still propagates -- a backend error must
 *  not be displayed as "not generated yet". */
export async function optional<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch (err) {
    if (err instanceof ApiError && err.isMissing) return null;
    throw err;
  }
}

export const TASK_KINDS = [
  "profile_sources",
  "extract_requirements",
  "plan_transformations",
  "execute_transformations",
  "propose_semantic_model",
  "generate_measures",
  "design_report",
  "export_pbip",
  "run_qa",
  "suggest_improvements",
] as const;

export const TASK_LABELS: Record<string, string> = {
  profile_sources: "Profile sources",
  extract_requirements: "Requirements + MoM",
  plan_transformations: "Plan transformations",
  execute_transformations: "Execute transformations",
  propose_semantic_model: "Propose semantic model",
  generate_measures: "Generate DAX measures",
  design_report: "Design report",
  export_pbip: "Export PBIP project",
  run_qa: "Run QA",
  suggest_improvements: "Suggest improvements",
};
