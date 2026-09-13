import { useRef, useState } from "react";
import { api, fmtUtc } from "../lib/api";
import { useStore } from "../lib/store";
import { Badge, Slider, InfoTooltip } from "./Common";

/** Step 3 — AIS data + attribution. */
export default function AisPanel({ onClose }: { onClose: () => void }) {
  const { caseId, caseData, hindcast, runJob, notify, health, config } = useStore();
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState("imported");
  const [label, setLabel] = useState("");
  const [liveMin, setLiveMin] = useState(10);
  const inputRef = useRef<HTMLInputElement>(null);
  const defW = config?.attribution?.weights || {};
  const [w, setW] = useState<Record<string, number>>({ ...defW });
  const keys = ["proximity", "temporal_overlap", "heading_compatibility", "loitering", "ais_gap_relevance", "vessel_type"];
  const keyDescs: Record<string, string> = {
    proximity: "Distance between the vessel track and the reverse-drift origin region.",
    temporal_overlap: "Time intersection of the vessel being near the simulated origin.",
    heading_compatibility: "Alignment of the vessel's heading with the geometry of the spill.",
    loitering: "Abnormal speed drops or circling behavior indicative of operational discharge.",
    ais_gap_relevance: "Suspiciously timed transponder deactivations matching the spill time.",
    vessel_type: "Statistical likelihood of this vessel class causing an oil spill (e.g., Tankers vs. Yachts)."
  };
  const sum = keys.reduce((a, k) => a + (w[k] ?? defW[k] ?? 0), 0);

  const importCsv = async () => {
    if (!caseId || !file) return notify("error", "Choose an AIS CSV first");
    const fd = new FormData();
    fd.append("file", file);
    fd.append("source_label", label || file.name);
    fd.append("data_mode", mode);
    await runJob(`Import AIS ${file.name}`, () => api.importAis(caseId, fd));
    setFile(null);
  };
  const record = async () => {
    if (!caseId || !caseData?.scene) return notify("error", "Need a scene bbox to record around");
    const b = caseData.scene.bounds;
    const pad = 0.5;
    await runJob(`Record live AIS ${liveMin} min`, () => api.liveRecord(caseId, [b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad], liveMin));
  };
  const generateSynth = async () => {
    if (!caseId) return;
    if (!hindcast) return notify("error", "Run a hindcast first to define the release window and origin region");
    await runJob("Synthesize AIS corridor traffic", () => api.generateSyntheticAis(caseId));
  };
  const attribute = async () => {
    if (!caseId) return;
    if (!hindcast) return notify("error", "Run a hindcast first — attribution needs an origin region and release window");
    if (!caseData?.ais_summary.positions) return notify("error", "Load AIS data first (CSV import, live recording, or synthetic corridor generator)");
    const norm: Record<string, number> = {};
    keys.forEach((k) => (norm[k] = (w[k] ?? defW[k] ?? 0) / (sum || 1)));
    await runJob("AIS attribution", () => api.attribute(caseId, { hindcast_run_id: hindcast.id, weights: norm }), async () => { useStore.getState().setPanel("suspects"); });
  };

  return (
    <div className="overlay map-panel">
      <div className="row" style={{ marginBottom: 8 }}><b>AIS DATA &amp; ATTRIBUTION</b><button className="btn sm ghost right" onClick={onClose}>✕</button></div>
      <div className="col" style={{ gap: 10 }}>
        <div className="tile">
          <div className="xs cyan">Loaded AIS</div>
          <div className="mono">{caseData?.ais_summary.positions.toLocaleString()} positions · {caseData?.ais_summary.vessels} vessels</div>
          <div className="small muted">{caseData?.ais_summary.time_start_utc ? `${fmtUtc(caseData.ais_summary.time_start_utc)} → ${fmtUtc(caseData.ais_summary.time_end_utc)}` : "none yet"}</div>
          {(caseData?.ais_imports || []).map((im) => (
            <div className="row small" key={im.id} style={{ marginTop: 4 }}>
              <Badge kind={im.data_mode}>{im.data_mode}</Badge>
              <span className="grow" title={im.filename}>{im.source} · {im.rows_valid} ok / {im.rows_quarantined} quarantined</span>
              <button className="btn sm ghost danger" onClick={async () => { await api.deleteAis(caseId!, im.id); useStore.getState().refresh(); }}>✕</button>
            </div>
          ))}
        </div>
        {hindcast && (
          <div className="small muted">Release window <span className="mono cyan">{fmtUtc(hindcast.release_start)} → {fmtUtc(hindcast.release_end)}</span>. Load AIS covering this interval ± {config?.ais?.candidate_search?.time_pad_hours ?? 6} h around the origin region (buffer {config?.ais?.candidate_search?.origin_buffer_km ?? 25} km).</div>
        )}
        <div className="dropzone" onClick={() => inputRef.current?.click()}>
          {file ? <b>{file.name}</b> : "Historical AIS CSV (mmsi, timestamp_utc, longitude, latitude, sog_knots, cog_deg, …). Provider column aliases (MarineCadastre, Spire, Datalastic…) are auto-mapped."}
          <input ref={inputRef} type="file" accept=".csv,.txt" hidden onChange={(e) => setFile(e.target.files?.[0] || null)} />
        </div>
        <div className="row">
          <select className="input" style={{ width: 130 }} value={mode} onChange={(e) => setMode(e.target.value)} title="Declared provenance of this AIS file">
            <option value="real">REAL</option><option value="imported">IMPORTED</option><option value="synthetic">SYNTHETIC</option><option value="demo">DEMO</option>
          </select>
          <input className="input grow" placeholder="source label (provider / archive)" value={label} onChange={(e) => setLabel(e.target.value)} />
          <button className="btn primary" onClick={importCsv} disabled={!file}>Import</button>
        </div>
        <div className="tile">
          <div className="row">
            <span className="xs cyan">Scenario Benchmark Traffic</span>
            <Badge kind="synthetic">SYNTHETIC</Badge>
          </div>
          <div className="small muted" style={{ margin: "4px 0" }}>Generate synthetic corridor traffic matching the estimated release window to test attribution and suspect ranking immediately on this SAR scene.</div>
          <button className="btn sm fill" onClick={generateSynth} disabled={!hindcast}>⚡ Generate Corridor AIS Traffic</button>
        </div>
        <div className="tile">
          <div className="row"><span className="xs cyan">Live AIS recorder (aisstream.io)</span><Badge kind={health?.integrations?.aisstream_key_configured ? "ok" : "neutral"}>{health?.integrations?.aisstream_key_configured ? "key configured" : "set OT_AISSTREAM_API_KEY"}</Badge></div>
          <div className="small muted" style={{ margin: "4px 0" }}>Records real-time position reports for the scene corridor (+0.5°) into this case — builds the historical archive for future incidents.</div>
          <Slider label="Duration" value={liveMin} min={1} max={120} step={1} onChange={setLiveMin} fmt={(v) => `${v} min`} />
          <button className="btn sm block" onClick={record} disabled={!health?.integrations?.aisstream_key_configured || !caseData?.scene}>Start recording</button>
        </div>
        <div className="tile">
          <div className="xs cyan" style={{ marginBottom: 6 }}>Scoring weights (normalised to 1.00 · current sum {sum.toFixed(2)})</div>
          {keys.map((k) => (
            <Slider key={k} label={<InfoTooltip text={keyDescs[k]}>{k.replace(/_/g, " ")}</InfoTooltip>} value={w[k] ?? defW[k] ?? 0} min={0} max={0.6} step={0.05} onChange={(v) => setW({ ...w, [k]: v })} fmt={(v) => v.toFixed(2)} />
          ))}
          <button className="btn sm ghost" onClick={() => setW({ ...defW })}>reset to config defaults</button>
        </div>
        <button className="btn primary block" onClick={attribute} disabled={!hindcast || !caseData?.ais_summary.positions}>Run AIS correlation &amp; ranking →</button>
      </div>
    </div>
  );
}
