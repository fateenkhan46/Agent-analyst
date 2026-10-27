// Types mirroring the backend contracts in backend/app/schemas.
// Deliberately hand-written rather than generated: they cover only what the
// UI actually reads, so a backend field the UI ignores cannot drift into the
// bundle unnoticed.

export type Severity = "critical" | "high" | "medium" | "low" | "info";
export type Confidence = "high" | "medium" | "low";
export type Origin = "deterministic" | "llm" | "heuristic" | "user" | "imported";
export type CapabilityStatus =
  | "implemented"
  | "not_configured"
  | "unavailable"
  | "not_implemented";

export interface Capability {
  key: string;
  title: string;
  status: CapabilityStatus;
  detail: string;
  requires: string[];
  tested: boolean;
}

export interface EnvironmentSummary {
  probes: Record<string, boolean>;
  database: string;
  storage_backend: string;
  llm_provider_active: string;
  llm_is_development_mode: boolean;
  powerbi_desktop_available: boolean;
  powerbi_desktop_note: string;
  counts: Record<string, number>;
}

export interface Issue {
  code: string;
  severity: Severity;
  message: string;
  location: string;
  evidence: Record<string, unknown>;
  recommended_fix: string;
}

export interface Provenance {
  origin: Origin;
  produced_by: string;
  confidence: Confidence;
  rationale: string;
  evidence: string[];
}

/** One stored version of an artifact. The backend never updates an artifact in
 *  place: saving writes version N+1 and moves the `is_current` flag, so the
 *  newest entry is first and the older ones stay readable. */
export interface ArtifactVersion {
  id: string;
  version_row_id: string;
  version: number;
  is_current: boolean;
  created_at: string | null;
}

export interface Project {
  id: string;
  name: string;
  description: string;
  business_objective: string;
  desired_kpis: string[];
  reporting_questions: string[];
  reporting_period: string;
  currency: string;
  units: string;
  status: string;
  created_at: string;
  updated_at: string;
  budget_usd: number;
  spend: { amount_usd: number; input_tokens: number; output_tokens: number };
  profile_id: string;
  minutes_id: string;
  requirements_id: string;
  model_id: string;
  report_id: string;
}

export interface ProjectSummary extends Project {
  file_count: number;
  progress: number;
  spend_usd: number;
}

export interface SourceFile {
  id: string;
  original_filename: string;
  kind: string;
  size_bytes: number;
  sha256: string;
  status: string;
  rejection_reason: string;
  uploaded_at: string;
}

export interface FormatVariant {
  pattern: string;
  description: string;
  count: number;
  examples: string[];
}

export interface ColumnProfile {
  id: string;
  name: string;
  inferred_type: string;
  type_confidence: Confidence;
  row_count: number;
  null_count: number;
  blank_count: number;
  unique_count: number;
  duplicate_count: number;
  sample_values: string[];
  top_values: { value: string; count: number; share: number }[];
  format_variants: FormatVariant[];
  numeric: { min: number | null; max: number | null; mean: number | null } | null;
  temporal: { min_date: string | null; max_date: string | null; granularity: string } | null;
  is_unique: boolean;
  is_complete: boolean;
  primary_key_score: number;
  detected_currency_symbols: string[];
  issues: Issue[];
}

export interface SourceTable {
  id: string;
  table_name: string;
  display_name: string;
  sheet_name: string;
  row_count: number;
  column_count: number;
  header_row_index: number;
  naming_convention: string;
  duplicate_row_count: number;
  candidate_primary_keys: string[][];
  preview_rows: Record<string, string>[];
  columns: ColumnProfile[];
  issues: Issue[];
}

export interface KeyOverlap {
  left: string;
  right: string;
  left_distinct: number;
  right_distinct: number;
  matched_distinct: number;
  left_match_share: number;
  right_match_share: number;
  left_is_unique: boolean;
  right_is_unique: boolean;
  name_similarity: number;
}

export interface SourceDataProfile {
  id: string;
  tables: SourceTable[];
  key_overlaps: KeyOverlap[];
  cross_table_issues: Issue[];
  provenance: Provenance;
  generated_at: string;
}

export interface Attendee {
  name: string;
  role: string;
  speaking_turns: number;
  inferred: boolean;
}

export interface Decision {
  id: string;
  statement: string;
  decided_by: string;
  affects: string[];
  confidence: Confidence;
  spans: { start_line: number; end_line: number; speaker: string; quote: string }[];
}

export interface ActionItem {
  id: string;
  description: string;
  owner: string;
  due: string;
  status: string;
}

export interface OpenQuestion {
  id: string;
  question: string;
  why_it_matters: string;
  blocks: string[];
  severity: Severity;
  answer: string;
}

export interface MeetingMinutes {
  id: string;
  title: string;
  meeting_date: string;
  transcript_line_count: number;
  attendees: Attendee[];
  agenda: string[];
  executive_summary: string;
  discussion_points: { topic: string; summary: string; participants: string[] }[];
  decisions: Decision[];
  action_items: ActionItem[];
  open_questions: OpenQuestion[];
  risks: string[];
  provenance: Provenance;
}

export interface KpiRequirement {
  id: string;
  name: string;
  business_question: string;
  definition: string;
  aggregation: string;
  unit: string;
  format_string: string;
  time_intelligence: string[];
  priority: number;
  confidence: Confidence;
  needs_confirmation: boolean;
}

export interface RequirementsSpec {
  id: string;
  business_objective: string;
  objectives: string[];
  kpis: KpiRequirement[];
  dimensions: { name: string; purpose: string; as_slicer: boolean }[];
  definitions: { term: string; definition: string; agreed: boolean }[];
  reporting_period_start: string;
  reporting_period_end: string;
  currency: string;
  assumptions: { id: string; statement: string; impact_if_wrong: string; severity: Severity; confirmed: boolean | null }[];
  open_questions: OpenQuestion[];
  provenance: Provenance;
  approved: boolean;
}

export interface ColumnMapping {
  id: string;
  source_table: string;
  source_column: string;
  canonical_name: string;
  canonical_type: string;
  confidence: Confidence;
  reason: string;
  evidence: string[];
  accepted: boolean | null;
  unit_conversion: string;
}

export interface TransformationStep {
  id: string;
  ordinal: number;
  op: string;
  target_table: string;
  params: Record<string, unknown>;
  title: string;
  reason: string;
  expected_effect: string;
  risk: "safe" | "review" | "destructive";
  requires_approval: boolean;
  approved: boolean | null;
  evidence: string[];
  affects_columns: string[];
  confidence: Confidence;
}

export interface JoinProposal {
  id: string;
  left_table: string;
  left_keys: string[];
  right_table: string;
  right_keys: string[];
  implied_cardinality: string;
  match_share: number;
  unmatched_left: number;
  fan_out_risk: boolean;
  recommended: boolean;
  recommendation_reason: string;
  confidence: Confidence;
  evidence: string[];
}

export interface TransformationPlan {
  id: string;
  summary: string;
  mappings: ColumnMapping[];
  steps: TransformationStep[];
  join_proposals: JoinProposal[];
  duplicate_findings: { table: string; keys: string[]; duplicate_group_count: number; assessment: string }[];
  output_datasets: string[];
  provenance: Provenance;
  approved: boolean;
}

export interface TransformationExecution {
  id: string;
  succeeded: boolean;
  skipped_steps: string[];
  error: string;
  row_count_deltas: { step_id: string; op: string; table: string; rows_before: number; rows_after: number }[];
  lineage: { target: string; sources: string[]; via_steps: string[]; transform_summary: string }[];
  datasets: { id: string; name: string; row_count: number; column_count: number; role_hint: string; columns: { name: string; canonical_type: string }[] }[];
  quality_before: QualityReport | null;
  quality_after: QualityReport | null;
  m_queries: Record<string, string>;
  log: string[];
}

export interface QualityReport {
  phase: string;
  table_row_counts: Record<string, number>;
  null_shares: Record<string, number>;
  type_conformance: Record<string, number>;
  duplicate_row_counts: Record<string, number>;
  orphan_key_counts: Record<string, number>;
  issues: Issue[];
}

export interface ColumnDefinition {
  name: string;
  data_type: string;
  is_key: boolean;
  is_hidden: boolean;
  format_string: string;
  description: string;
}

export interface TableDefinition {
  name: string;
  role: "fact" | "dimension" | "date" | "bridge" | "aggregate" | "helper";
  dataset_name: string;
  description: string;
  grain: string;
  rationale: string;
  row_count: number;
  primary_key: string[];
  columns: ColumnDefinition[];
}

export interface RelationshipDefinition {
  id: string;
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
  cardinality: string;
  cross_filter: string;
  is_active: boolean;
  match_share: number;
  orphan_key_count: number;
  to_side_is_unique: boolean;
  rationale: string;
  evidence: string[];
  confidence: Confidence;
  requires_approval: boolean;
  approved: boolean | null;
}

export interface MeasureValidationCase {
  id: string;
  name: string;
  filters: Record<string, string>;
  expected_source: string;
  comparison: string;
}

export interface MeasureDefinition {
  id: string;
  name: string;
  expression: string;
  home_table: string;
  kpi_id: string;
  description: string;
  format_string: string;
  display_folder: string;
  unit: string;
  assumptions: string[];
  aggregation_behaviour: string;
  depends_on_measures: string[];
  depends_on_columns: string[];
  requires_date_table: boolean;
  validation_cases: MeasureValidationCase[];
  provenance: Provenance | null;
  confidence: Confidence;
}

export interface SemanticModel {
  id: string;
  name: string;
  version: number;
  tables: TableDefinition[];
  relationships: RelationshipDefinition[];
  measures: MeasureDefinition[];
  schema_shape: string;
  design_rationale: string;
  issues: Issue[];
  provenance: Provenance | null;
  approved: boolean;
}

export interface DiagramNode {
  id: string;
  label: string;
  role: TableDefinition["role"];
  columns: string[];
  row_count: number;
  x: number;
  y: number;
}

export interface DiagramEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  cardinality: string;
  cross_filter: string;
  is_active: boolean;
  warning: string;
}

export interface ModelDiagram {
  nodes: DiagramNode[];
  edges: DiagramEdge[];
  mermaid: string;
}

export interface FieldBinding {
  role: string;
  table: string;
  field: string;
  is_measure: boolean;
  display_name: string;
}

export interface VisualDefinition {
  id: string;
  name: string;
  visual_type: string;
  title: string;
  subtitle: string;
  layout: { x: number; y: number; width: number; height: number };
  bindings: FieldBinding[];
  business_question: string;
  design_rationale: string;
}

export interface ReportPage {
  id: string;
  name: string;
  display_name: string;
  ordinal: number;
  purpose: string;
  audience: string;
  width: number;
  height: number;
  visuals: VisualDefinition[];
  is_drill_through: boolean;
  drill_through_field: string;
  is_hidden: boolean;
}

export interface ReportSpec {
  id: string;
  name: string;
  pages: ReportPage[];
  theme: { name: string; data_colors: string[]; table_accent: string };
  design_notes: string;
  unmapped_kpis: string[];
  issues: Issue[];
  provenance: Provenance | null;
}

export type TestStatus = "pass" | "fail" | "warn" | "skipped" | "not_supported" | "error";

export interface ValidationResult {
  id: string;
  test_name: string;
  category: string;
  target: string;
  expected: string;
  actual: string;
  status: TestStatus;
  engine: string;
  severity: Severity;
  evidence: Record<string, unknown>;
  recommended_fix: string;
  responsible_agent: string;
  skipped_reason: string;
}

export interface ValidationReport {
  id: string;
  generated_at: string;
  results: ValidationResult[];
  rework_requests: { id: string; target_agent: string; reason: string; instructions: string[]; severity: Severity }[];
  validation_scope_note: string;
  provenance: Provenance | null;
}

export interface ImprovementRecommendation {
  id: string;
  area: string;
  title: string;
  rationale: string;
  evidence: string[];
  expected_benefit: string;
  quantified_benefit: string;
  trade_offs: string[];
  effort: string;
  severity: Severity;
  affected_objects: string[];
  state: string;
}

export interface ImprovementReport {
  id: string;
  recommendations: ImprovementRecommendation[];
  summary: string;
}

export interface AgentTask {
  id: string;
  kind: string;
  owner: string;
  status: string;
  depends_on: string[];
  attempt: number;
  error: string;
  rework_reason: string;
  updated_at: string;
}

export interface AgentExecution {
  id: string;
  task_id: string;
  agent: string;
  status: string;
  started_at: string;
  duration_ms: number;
  cost: { amount_usd: number; input_tokens: number; output_tokens: number };
  llm_calls: { provider: string; model: string; purpose: string; cost: { amount_usd: number } }[];
  tool_calls: { name: string; succeeded: boolean; duration_ms: number }[];
  log: string[];
  error: string;
}

export interface ApprovalRequest {
  id: string;
  kind: string;
  subject_id: string;
  subject_label: string;
  question: string;
  detail: string;
  severity: Severity;
  state: string;
  decided_by: string;
  decision_note: string;
  created_at: string;
}

export interface AuditEvent {
  id: string;
  at: string;
  actor: string;
  action: string;
  summary: string;
  target: string;
  severity: Severity;
  detail: Record<string, unknown>;
}

export interface ExportArtifact {
  id: string;
  kind: string;
  filename: string;
  size_bytes: number;
  sha256: string;
  file_count: number;
  manifest: string[];
  remaining_manual_steps: string[];
  opens_in: string;
  created_at: string;
  verified_at: string | null;
  file_present: boolean;
  download_href: string;
  note: string;
}

export interface EvaluationOutcome {
  measure: string;
  supported: boolean;
  value: number | null;
  is_blank: boolean;
  engine: string;
  sql_executed: string[];
  unsupported_construct: string;
  reason: string;
  duration_ms: number;
  notes: string[];
}

export interface DeliverySection {
  available: boolean;
  reason: string;
  href: string;
  summary: Record<string, unknown>;
}

export interface DeliveryBundle {
  project: Project;
  capabilities: Capability[];
  sections: Record<string, DeliverySection>;
  exports: ExportArtifact[];
  unresolved_questions: (OpenQuestion & { source: string })[];
  unconfirmed_assumptions: { id: string; statement: string; impact_if_wrong: string; severity: Severity }[];
}

export interface DashboardData {
  projects: {
    id: string;
    name: string;
    status: string;
    updated_at: string;
    progress: number;
    file_count: number;
    spend_usd: number;
    qa_passed: boolean | null;
  }[];
  project_count: number;
  recent_executions: {
    id: string;
    project_id: string;
    agent: string;
    status: string;
    duration_ms: number;
    cost_usd: number;
    started_at: string;
    error: string;
  }[];
  validation_failures: {
    project_id: string;
    project_name: string;
    test: string;
    target: string;
    actual: string;
    severity: Severity;
    fix: string;
  }[];
  spend: { total_usd: number; note: string | false };
  failed_executions: number;
  environment: EnvironmentSummary;
}
