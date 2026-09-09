/** Typed API client. All URLs are relative (/api/...) — the dev server proxies to FastAPI. */
export type DataMode = "real" | "imported" | "synthetic" | "demo";

export interface Job {
  id: string;
  case_id: string | null;
  kind: string;
  state: "queued" | "running" | "partial" | "completed" | "failed";
  progress: number;
  message: string | null;
  result: any;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface Feature<G = any, P = any> {
  type: "Feature";
  id: string;
  geometry: G;
  properties: P;
}
export interface FeatureCollection<F = Feature> {
  type: "FeatureCollection";
  features: F[];
  [k: string]: any;
}

export interface SlickProps {
  class: "oil" | "look_alike" | "uncertain";
  confidence: number;
  area_km2: number;
  perimeter_km: number;
  centroid: [number, number];
  major_axis_km: number;
  minor_axis_km: number;
  elongation: number | null;
  orientation_deg: number;
  model_version: string;
  adapter: string;
  rank: number;
  prob_mean: number;
  prob_max: number;
  pixel_count: number;
  coast_distance_km: number | null;
  look_alike_risk: "low" | "medium" | "high";
  limitations: string[];
  review_status: string | null;
  review_note: string | null;
  scene_id: string;
}

export interface Scene {
  id: string;
  case_id: string;
  acquisition_time: string;
  bounds: [number, number, number, number];
  asset_uri: string;
  preview_url: string;
  metadata: any;
}

export interface HindcastRun {
  id: string;
  slick_id: string;
  env_field_id: string;
  origin_geometry: any;
  release_start: string;
  release_end: string;
  metrics: any;
  config: any;
  status: string;
  created_at: string;
  detail?: any;
}

export interface Candidate {
  id: string;
  mmsi: number;
  rank: number;
  score: number;
  priority: "high" | "medium" | "low" | "insufficient";
  factors: Record<string, number>;
  raw: any;
  evidence: string[];
  limitations: string[];
  vessel: { name: string | null; type: string | null; type_normalized?: string; length_m: number | null; source: string | null };
  track: Feature;
  review_status: string | null;
  review_note: string | null;
}

export interface Attribution {
  id: string;
  hindcast_run_id: string;
  weights: Record<string, number>;
  summary: any;
  candidates: Candidate[];
  created_at: string;
}

export interface CaseDetail {
  id: string;
  name: string;
  status: string;
  data_mode: DataMode;
  created_at: string;
  updated_at: string;
  notes: string | null;
  config_hash: string | null;
  scene: Scene | null;
  slicks: FeatureCollection<Feature<any, SlickProps>>;
  environment: any;
  hindcast: HindcastRun | null;
  hindcast_runs: any[];
  attribution: Attribution | null;
  attribution_runs: any[];
  ais_imports: any[];
  ais_summary: { positions: number; vessels: number; time_start_utc: string | null; time_end_utc: string | null };
  detector: { configured_adapter: string; onnx_model_present: boolean; active_adapter: string; onnx_model_path: string; classical_version: string };
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, msg: string) {
    super(msg);
    this.status = status;
  }
}

async function handle<T>(r: Response): Promise<T> {
  if (r.status === 204) return undefined as T;
  const text = await r.text();
  let body: any = text;
  try {
    body = JSON.parse(text);
  } catch {
    /* plain text */
  }
  if (!r.ok) {
    const detail = typeof body === "object" && body?.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : text;
    throw new ApiError(r.status, detail || r.statusText);
  }
  return body as T;
}

export const api = {
  get: <T>(url: string) => fetch(url).then((r) => handle<T>(r)),
  post: <T>(url: string, body?: any) =>
    fetch(url, { method: "POST", headers: body !== undefined ? { "Content-Type": "application/json" } : undefined, body: body !== undefined ? JSON.stringify(body) : undefined }).then((r) => handle<T>(r)),
  patch: <T>(url: string, body: any) => fetch(url, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => handle<T>(r)),
  del: <T>(url: string) => fetch(url, { method: "DELETE" }).then((r) => handle<T>(r)),
  upload: <T>(url: string, form: FormData) => fetch(url, { method: "POST", body: form }).then((r) => handle<T>(r)),

  health: () => api.get<any>("/api/v1/health"),
  config: () => api.get<any>("/api/v1/config"),
  cases: () => api.get<any[]>("/api/v1/cases"),
  createCase: (name: string, data_mode: DataMode, notes?: string) => api.post<CaseDetail>("/api/v1/cases", { name, data_mode, notes }),
  getCase: (id: string) => api.get<CaseDetail>(`/api/v1/cases/${id}`),
  deleteCase: (id: string) => api.del<void>(`/api/v1/cases/${id}`),
  job: (id: string) => api.get<Job>(`/api/v1/jobs/${id}`),
  jobs: (caseId: string) => api.get<Job[]>(`/api/v1/cases/${caseId}/jobs`),
  uploadScene: (caseId: string, form: FormData) => api.upload<{ job_id: string }>(`/api/v1/cases/${caseId}/scene`, form),
  catalogSearch: (bbox: number[], start: string, end: string, limit = 20) => api.post<any>("/api/v1/catalog/search", { bbox, start, end, limit }),
  catalogFetch: (caseId: string, body: any) => api.post<{ job_id: string }>(`/api/v1/cases/${caseId}/scene/catalog`, body),
  analyze: (caseId: string, body: any) => api.post<{ job_id: string }>(`/api/v1/cases/${caseId}/analyze`, body),
  hindcast: (caseId: string, slickId: string, body: any) => api.post<{ job_id: string }>(`/api/v1/cases/${caseId}/slicks/${slickId}/hindcast`, body),
  forecast: (caseId: string, slickId: string, body: any) => api.post<{ job_id: string }>(`/api/v1/cases/${caseId}/slicks/${slickId}/forecast`, body),
  getForecast: (caseId: string, slickId: string) => api.get<any>(`/api/v1/cases/${caseId}/slicks/${slickId}/forecast`),
  hindcastLayer: (caseId: string, runId?: string) => api.get<HindcastRun>(`/api/v1/cases/${caseId}/layers/hindcast${runId ? `?run_id=${runId}` : ""}`),
  attributionLayer: (caseId: string, runId?: string) => api.get<Attribution>(`/api/v1/cases/${caseId}/layers/attribution${runId ? `?run_id=${runId}` : ""}`),
  aisTracks: (caseId: string, q = "") => api.get<FeatureCollection>(`/api/v1/cases/${caseId}/layers/ais_tracks${q}`),
  importAis: (caseId: string, form: FormData) => api.upload<{ job_id: string }>(`/api/v1/cases/${caseId}/ais/import`, form),
  deleteAis: (caseId: string, importId: string) => api.del<void>(`/api/v1/cases/${caseId}/ais/${importId}`),
  liveRecord: (caseId: string, bbox: number[], minutes: number) => api.post<{ job_id: string }>(`/api/v1/cases/${caseId}/ais/live/record`, { bbox, minutes }),
  attribute: (caseId: string, body: any) => api.post<{ job_id: string }>(`/api/v1/cases/${caseId}/attribute`, body),
  review: (caseId: string, body: any) => api.patch<CaseDetail>(`/api/v1/cases/${caseId}/review`, body),
  audit: (caseId: string) => api.get<any[]>(`/api/v1/cases/${caseId}/audit`),
  exportUrl: (caseId: string, fmt: "json" | "geojson") => `/api/v1/cases/${caseId}/export?format=${fmt}`,
  reportUrl: (caseId: string) => `/api/v1/cases/${caseId}/report.html`,
};

/** Poll a job until terminal. onTick receives every snapshot. */
export async function waitJob(jobId: string, onTick?: (j: Job) => void, intervalMs = 900): Promise<Job> {
  for (;;) {
    const j = await api.job(jobId);
    onTick?.(j);
    if (j.state === "completed" || j.state === "failed") return j;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

export const fmtUtc = (s: string | null | undefined) => (s ? s.replace("T", " ").replace(/(\.\d+)?Z$/, " UTC") : "—");
export const fmtNum = (x: any, nd = 2) => (typeof x === "number" && isFinite(x) ? x.toFixed(nd) : "—");
