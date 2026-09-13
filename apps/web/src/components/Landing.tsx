import { useEffect, useState } from "react";
import { api, DataMode, fmtUtc, waitJob } from "../lib/api";
import { useStore } from "../lib/store";
import { Badge, ModeBadge, ThemeToggle } from "./Common";

export default function Landing() {
  const { setCase, health, notify } = useStore();
  const [cases, setCases] = useState<any[]>([]);
  const [name, setName] = useState("");
  const [mode, setMode] = useState<DataMode>("real");
  const [busy, setBusy] = useState(false);
  const [filterMode, setFilterMode] = useState<string>("real");

  const load = () => api.cases().then(setCases).catch((e) => notify("error", e.message));
  useEffect(() => {
    load();
    const onFocus = () => load();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, []);

  const create = async () => {
    if (!name.trim()) return notify("error", "Enter a case name");
    setBusy(true);
    try {
      const c = await api.createCase(name.trim(), mode);
      await setCase(c.id);
    } catch (e: any) {
      notify("error", e.message);
    } finally {
      setBusy(false);
    }
  };

  const del = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm("Delete this case and all its artifacts?")) return;
    await api.deleteCase(id);
    load();
  };

  const createSynthetic = async () => {
    setBusy(true);
    try {
      notify("info", "Initializing synthetic smoke test fixture...");
      const res = await api.createSyntheticDemo("Synthetic Smoke Test · Arabian Sea");
      const job = await waitJob(res.job_id);
      if (job.state === "completed") {
        notify("ok", "Synthetic test case loaded successfully!");
        await setCase(job.result.case_id);
      } else {
        notify("error", job.error || "Failed to initialize test case");
      }
    } catch (e: any) {
      notify("error", e.message);
    } finally {
      setBusy(false);
    }
  };

  const isModelActive = health?.detector?.active_adapter === "pytorch_unet" || health?.detector?.active_adapter === "onnx";
  const modelLabel = health?.detector?.active_adapter === "pytorch_unet"
    ? `PYTORCH U-NET (${health.detector.model_version || "ACTIVE"})`
    : health?.detector?.active_adapter === "onnx"
    ? `ONNX MODEL (${health.detector.model_version || "ACTIVE"})`
    : "CLASSICAL DETECTOR (FALLBACK)";

  const filteredCases = cases.filter((c) => c.data_mode === filterMode);

  return (
    <div className="landing">
      <div className="topbar" style={{ position: "sticky", top: 0, zIndex: 2 }}>
        <div className="brand" style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <img src="/logo.png" alt="Spill Forensics Logo" style={{ height: "34px", width: "auto", display: "block" }} />
          <div>
            <b style={{ letterSpacing: "0.08em" }}>SPILL FORENSICS</b>
            <small>MARITIME FORENSICS DSS · SIH 2026 PS 26143</small>
          </div>
        </div>
        <div className="right row">
          <ThemeToggle />
          <Badge kind={health ? "ok" : "danger"}>{health ? `API v${health.version} · cfg ${health.config_version}` : "API OFFLINE"}</Badge>
          {health && (
            <Badge kind={isModelActive ? "ok" : "warn"} title={health.detector?.pytorch_model_path || health.detector?.onnx_model_path}>
              ★ {modelLabel}
            </Badge>
          )}
          <Badge kind="neutral">OPEN-METEO / COPERNICUS · {health?.integrations?.aisstream_key_configured ? "AISSTREAM LIVE" : "AIS: CSV IMPORT"}</Badge>
        </div>
      </div>
      <div className="hero">
        <div>
          <Badge kind="imported">★ AI FOR A SECURE &amp; CLEAN BHARAT</Badge>
          <h1>Detect Slicks.<br />Reconstruct Drift.<br /><span>Protect India's Oceans.</span></h1>
          <p className="lead">
            Upload a Sentinel-1 SAR scene (or pull one from the live Copernicus catalog). Spill Forensics detects candidate slicks using your trained U-Net model,
            hindcasts them with real currents and winds using an ensemble Lagrangian model, correlates historical AIS traffic, and produces an explainable, uncertainty-aware
            shortlist of <b>candidate vessels for investigation</b> — never a verdict.
          </p>
          <div className="card" style={{ marginTop: 22, maxWidth: 620 }}>
            <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
              <h4 style={{ margin: 0 }}>New investigation case</h4>
              <button className="btn sm cyan ghost" onClick={createSynthetic} disabled={busy} title="Load pre-built synthetic SAR + AIS test scenario instantly">
                ⚡ 1-Click Synthetic Smoke Test
              </button>
            </div>
            <div className="row" style={{ gap: 10 }}>
              <input className="input" placeholder="Case name, e.g. Arabian Sea anomaly 2026-09-06" value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} />
              <select className="input" style={{ width: 170 }} value={mode} onChange={(e) => setMode(e.target.value as DataMode)} title="Declared provenance of the data you will load">
                <option value="real">REAL data</option>
                <option value="imported">IMPORTED data</option>
                <option value="synthetic">SYNTHETIC test</option>
                <option value="demo">DEMO fixture</option>
              </select>
              <button className="btn primary" onClick={create} disabled={busy}>Create case →</button>
            </div>
            <div className="small muted" style={{ marginTop: 8 }}>The declared mode is stamped on every card, export and log entry. Synthetic or demo data is never presented as real.</div>
          </div>
          <div className="grid3" style={{ marginTop: 22, maxWidth: 720 }}>
            {[
              ["1 · DETECT (TRAINED U-NET)", "SAR → probability mask → CRS-aware polygons with area, orientation, confidence, look-alike risk"],
              ["2 · HINDCAST (COPERNICUS)", "RK4 ensemble backtrack on Copernicus currents + ERA5/GFS winds → 50/70/90 % origin regions + release window"],
              ["3 · ATTRIBUTE (AIS FORENSICS)", "AIS space-time correlation → 0–100 investigation priority with per-factor evidence and audit trail"],
            ].map(([t, d]) => (
              <div className="tile" key={t}><div className="xs cyan">{t}</div><div className="small" style={{ marginTop: 4 }}>{d}</div></div>
            ))}
          </div>
        </div>
        <div>
          <div className="card">
            <div className="row" style={{ marginBottom: 10 }}><h4 style={{ margin: 0 }}>Open a case</h4><button className="btn sm ghost right" onClick={load}>↻ refresh</button></div>
            <div className="row" style={{ gap: 4, padding: "4px", background: "var(--bg-elevated)", borderRadius: "6px", marginBottom: "12px" }}>
              {["real", "synthetic", "imported"].map((m) => (
                <button 
                  key={m}
                  className={`btn sm ${filterMode === m ? "primary" : "ghost"}`}
                  style={{ flex: 1, textTransform: "capitalize" }}
                  onClick={() => setFilterMode(m)}
                >
                  {m}
                </button>
              ))}
            </div>
            <div className="caselist">
              {filteredCases.length === 0 && <div className="muted small">No cases found for this filter.</div>}
              {filteredCases.map((c) => (
                <div className="item" key={c.id} onClick={() => setCase(c.id)}>
                  <div className="grow">
                    <div className="row"><b>{c.name}</b><ModeBadge mode={c.data_mode} /></div>
                    <div className="small muted mono">{c.id} · {c.status} · {c.n_slicks} slick(s) · {c.acquisition_time_utc ? fmtUtc(c.acquisition_time_utc) : "no scene"}</div>
                  </div>
                  <button className="btn sm danger ghost" onClick={(e) => del(c.id, e)}>✕</button>
                </div>
              ))}
            </div>
          </div>
          <div className="card" style={{ marginTop: 12 }}>
            <h4>System status</h4>
            <div className="kv">
              <div className="tile"><div className="k">Active Detector</div><div className="v small">{health?.detector?.active_adapter || "—"}</div></div>
              <div className="tile"><div className="k">Model Version</div><div className="v small">{health?.detector?.model_version || "v1.0"}</div></div>
              <div className="tile"><div className="k">Environment</div><div className="v small">Open-Meteo · SMOC / ERA5</div></div>
              <div className="tile"><div className="k">Live AIS</div><div className="v small">{health?.integrations?.aisstream_key_configured ? "aisstream.io configured" : "CSV Import / Fixtures"}</div></div>
            </div>
            <div className="small muted" style={{ marginTop: 8 }}>
              {isModelActive ? (
                <span style={{ color: "var(--green)" }}>✔ Trained model weights loaded from <span className="mono">{health?.detector?.pytorch_model_path || health?.detector?.onnx_model_path}</span>. Deep learning segmentation is active.</span>
              ) : (
                <span>Classical adaptive dark-spot detector active as fallback.</span>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
