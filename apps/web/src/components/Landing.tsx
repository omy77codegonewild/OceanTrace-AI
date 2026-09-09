import { useEffect, useState } from "react";
import { api, DataMode, fmtUtc } from "../lib/api";
import { useStore } from "../lib/store";
import { Badge, ModeBadge, ThemeToggle } from "./Common";

export default function Landing() {
  const { setCase, health, notify } = useStore();
  const [cases, setCases] = useState<any[]>([]);
  const [name, setName] = useState("");
  const [mode, setMode] = useState<DataMode>("real");
  const [busy, setBusy] = useState(false);

  const load = () => api.cases().then(setCases).catch((e) => notify("error", e.message));
  useEffect(() => { load(); }, []);

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

  return (
    <div className="landing">
      <div className="topbar" style={{ position: "sticky", top: 0, zIndex: 2 }}>
        <div className="brand"><div className="logo"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--logo-stroke)" strokeWidth="3"><circle cx="12" cy="12" r="6" /></svg></div><div><b>OCEANTRACE AI</b><small>MARITIME FORENSICS DSS · SIH 2026 PS 26143</small></div></div>
        <div className="right row">
          <ThemeToggle />
          <Badge kind={health ? "ok" : "danger"}>{health ? `API v${health.version} · cfg ${health.config_version}` : "API OFFLINE"}</Badge>
          {health && <Badge kind={health.detector.onnx_model_present ? "ok" : "warn"} title={health.detector.onnx_model_path}>{health.detector.onnx_model_present ? "ONNX MODEL LOADED" : "CLASSICAL DETECTOR (NO ONNX WEIGHTS)"}</Badge>}
          <Badge kind="neutral">OPEN-METEO / COPERNICUS · {health?.integrations?.aisstream_key_configured ? "AISSTREAM LIVE" : "AIS: CSV IMPORT"}</Badge>
        </div>
      </div>
      <div className="hero">
        <div>
          <Badge kind="imported">★ AI FOR A SECURE &amp; CLEAN BHARAT</Badge>
          <h1>Detect Slicks.<br />Reconstruct Drift.<br /><span>Protect India's Oceans.</span></h1>
          <p className="lead">
            Upload a Sentinel-1 SAR scene (or pull one from the live Copernicus catalog). OceanTrace detects candidate slicks, hindcasts them with real
            currents and winds using an ensemble Lagrangian model, correlates historical AIS traffic, and produces an explainable, uncertainty-aware
            shortlist of <b>candidate vessels for investigation</b> — never a verdict.
          </p>
          <div className="card" style={{ marginTop: 22, maxWidth: 620 }}>
            <h4>New investigation case</h4>
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
              ["1 · DETECT", "SAR → probability mask → CRS-aware polygons with area, orientation, confidence, look-alike risk"],
              ["2 · HINDCAST", "RK4 ensemble backtrack on Copernicus currents + ERA5/GFS winds → 50/70/90 % origin regions + release window"],
              ["3 · ATTRIBUTE", "AIS space-time correlation → 0–100 investigation priority with per-factor evidence and audit trail"],
            ].map(([t, d]) => (
              <div className="tile" key={t}><div className="xs cyan">{t}</div><div className="small" style={{ marginTop: 4 }}>{d}</div></div>
            ))}
          </div>
        </div>
        <div>
          <div className="card">
            <div className="row" style={{ marginBottom: 10 }}><h4 style={{ margin: 0 }}>Open a case</h4><button className="btn sm ghost right" onClick={load}>↻ refresh</button></div>
            <div className="caselist">
              {cases.length === 0 && <div className="muted small">No cases yet. Create one on the left.</div>}
              {cases.map((c) => (
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
              <div className="tile"><div className="k">Detector</div><div className="v small">{health?.detector?.active_adapter || "—"}</div></div>
              <div className="tile"><div className="k">Environment</div><div className="v small">Open-Meteo · SMOC / ERA5</div></div>
              <div className="tile"><div className="k">Catalog</div><div className="v small">Planetary Computer S1-GRD</div></div>
              <div className="tile"><div className="k">Live AIS</div><div className="v small">{health?.integrations?.aisstream_key_configured ? "aisstream.io configured" : "not configured (.env)"}</div></div>
            </div>
            <div className="small muted" style={{ marginTop: 8 }}>
              To connect your trained segmentation model, drop an ONNX file at <span className="mono">{health?.detector?.onnx_model_path || "data/models/oilspill_seg.onnx"}</span> and restart the API. Until then the classical dark-spot detector runs and is labelled as such.
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
