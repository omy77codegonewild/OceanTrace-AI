import { create } from "zustand";
import { api, Attribution, CaseDetail, HindcastRun, Job, waitJob } from "./api";

export type Panel = "map" | "suspects" | "replay" | "whatif" | "evidence" | "dossier";
export type Focus = "corridor" | "slick" | "origin" | "suspect";

export interface LayerVis {
  scene: boolean;
  slicks: boolean;
  origin: boolean;
  particles: boolean;
  tracks: boolean;
  candidates: boolean;
  gaps: boolean;
  forecast: boolean;
  search: boolean;
  vectors: boolean;
}

export interface JobEntry extends Partial<Job> {
  id: string;
  label: string;
}

export type Theme = "dark" | "light";

interface State {
  caseId: string | null;
  caseData: CaseDetail | null;
  hindcast: HindcastRun | null;
  attribution: Attribution | null;
  forecast: any | null;
  tracks: any | null;
  selectedSlick: string | null;
  selectedMmsi: number | null;
  panel: Panel;
  focus: Focus;
  focusNonce: number;
  layers: LayerVis;
  basemap: "dark" | "ocean" | "osm";
  replayStep: number;
  playing: boolean;
  jobs: JobEntry[];
  toast: { kind: "info" | "error" | "ok"; text: string } | null;
  busy: Record<string, boolean>;
  theme: Theme;
  health: any;
  config: any;

  setCase: (id: string | null) => Promise<void>;
  refresh: () => Promise<void>;
  loadHindcast: (runId?: string) => Promise<void>;
  loadAttribution: (runId?: string) => Promise<void>;
  loadTracks: () => Promise<void>;
  selectSlick: (id: string | null) => void;
  selectMmsi: (m: number | null) => void;
  setPanel: (p: Panel) => void;
  setFocus: (f: Focus) => void;
  toggleLayer: (k: keyof LayerVis) => void;
  setBasemap: (b: "dark" | "ocean" | "osm") => void;
  setReplay: (s: number) => void;
  setPlaying: (p: boolean) => void;
  runJob: (label: string, start: () => Promise<{ job_id: string }>, after?: (j: Job) => Promise<void> | void) => Promise<Job | null>;
  notify: (kind: "info" | "error" | "ok", text: string) => void;
  setBusy: (k: string, v: boolean) => void;
  toggleTheme: () => void;
  init: () => Promise<void>;
}

export const useStore = create<State>((set, get) => ({
  caseId: null,
  caseData: null,
  hindcast: null,
  attribution: null,
  forecast: null,
  tracks: null,
  selectedSlick: null,
  selectedMmsi: null,
  panel: "map",
  focus: "corridor",
  focusNonce: 0,
  layers: { scene: true, slicks: true, origin: true, particles: true, tracks: true, candidates: true, gaps: true, forecast: true, search: false, vectors: true },
  basemap: "dark",
  replayStep: 0,
  playing: false,
  jobs: [],
  toast: null,
  busy: {},
  theme: (localStorage.getItem("ot.theme") as Theme) || "dark",
  health: null,
  config: null,

  init: async () => {
    // Apply persisted theme on startup
    const savedTheme = (localStorage.getItem("ot.theme") as Theme) || "dark";
    document.documentElement.setAttribute("data-theme", savedTheme);
    set({ theme: savedTheme });
    try {
      const [health, config] = await Promise.all([api.health(), api.config()]);
      set({ health, config });
    } catch (e: any) {
      get().notify("error", `API unreachable: ${e.message}`);
    }
    const saved = localStorage.getItem("ot.caseId");
    if (saved) await get().setCase(saved);
  },

  setCase: async (id) => {
    set({ caseId: id, caseData: null, hindcast: null, attribution: null, forecast: null, tracks: null, selectedSlick: null, selectedMmsi: null, replayStep: 0, playing: false, focus: "corridor" });
    if (id) {
      localStorage.setItem("ot.caseId", id);
      await get().refresh();
    } else localStorage.removeItem("ot.caseId");
  },

  refresh: async () => {
    const id = get().caseId;
    if (!id) return;
    try {
      const data = await api.getCase(id);
      const sel = get().selectedSlick;
      const firstOil = data.slicks.features.find((f) => f.properties.class === "oil") || data.slicks.features[0];
      set({ caseData: data, selectedSlick: sel && data.slicks.features.some((f) => f.id === sel) ? sel : firstOil?.id || null });
      if (data.hindcast) await get().loadHindcast();
      else set({ hindcast: null });
      if (data.attribution) await get().loadAttribution();
      else set({ attribution: null });
      if (data.ais_summary.positions > 0) await get().loadTracks();
      else set({ tracks: null });
      if (data.hindcast?.slick_id) {
        try {
          set({ forecast: (await api.getForecast(id, data.hindcast.slick_id)).forecast ?? null });
        } catch {
          set({ forecast: null });
        }
      }
    } catch (e: any) {
      if (e.status === 404) {
        get().notify("error", "Case not found; it may have been deleted.");
        await get().setCase(null);
      } else get().notify("error", e.message);
    }
  },

  loadHindcast: async (runId) => {
    const id = get().caseId;
    if (!id) return;
    const hc = await api.hindcastLayer(id, runId);
    set({ hindcast: hc && hc.id ? hc : null, replayStep: 0 });
  },
  loadAttribution: async (runId) => {
    const id = get().caseId;
    if (!id) return;
    const at = await api.attributionLayer(id, runId);
    set({ attribution: at && at.id ? at : null });
  },
  loadTracks: async () => {
    const id = get().caseId;
    if (!id) return;
    const st = get();
    // limit to hindcast search window when available so the map stays readable
    let q = "";
    const s = st.attribution?.summary;
    if (s?.search_time_start_utc) q = `?t0=${encodeURIComponent(s.search_time_start_utc)}&t1=${encodeURIComponent(s.search_time_end_utc)}`;
    set({ tracks: await api.aisTracks(id, q) });
  },

  selectSlick: (id) => set({ selectedSlick: id }),
  selectMmsi: (m) => set({ selectedMmsi: m, focus: m ? "suspect" : get().focus, focusNonce: get().focusNonce + 1 }),
  setPanel: (p) => set({ panel: p }),
  setFocus: (f) => {
    // "Top Suspect" with nothing selected -> select the rank-1 candidate so the camera has a target
    const top = get().attribution?.candidates?.[0];
    const sel = f === "suspect" && !get().selectedMmsi && top ? top.mmsi : get().selectedMmsi;
    set({ focus: f, focusNonce: get().focusNonce + 1, selectedMmsi: sel });
  },
  toggleLayer: (k) => set({ layers: { ...get().layers, [k]: !get().layers[k] } }),
  setBasemap: (b) => set({ basemap: b }),
  setReplay: (s) => set({ replayStep: s }),
  setPlaying: (p) => set({ playing: p }),
  notify: (kind, text) => {
    set({ toast: { kind, text } });
    setTimeout(() => {
      if (get().toast?.text === text) set({ toast: null });
    }, kind === "error" ? 9000 : 4500);
  },
  setBusy: (k, v) => set({ busy: { ...get().busy, [k]: v } }),
  toggleTheme: () => {
    const next = get().theme === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("ot.theme", next);
    set({ theme: next });
  },

  runJob: async (label, start, after) => {
    try {
      const { job_id } = await start();
      const entry: JobEntry = { id: job_id, label, state: "queued", progress: 0 };
      set({ jobs: [entry, ...get().jobs].slice(0, 12) });
      const final = await waitJob(job_id, (j) => set({ jobs: get().jobs.map((e) => (e.id === job_id ? { ...e, ...j, label } : e)) }));
      if (final.state === "failed") get().notify("error", `${label} failed: ${final.error}`);
      else {
        get().notify("ok", `${label} completed`);
        await get().refresh();
        await after?.(final);
      }
      return final;
    } catch (e: any) {
      get().notify("error", `${label}: ${e.message}`);
      return null;
    }
  },
}));
