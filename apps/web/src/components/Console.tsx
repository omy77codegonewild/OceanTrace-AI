import { useEffect, useState } from "react";
import { fmtUtc } from "../lib/api";
import { LayerVis, Panel, useStore } from "../lib/store";
import AisPanel from "./AisPanel";
import { Badge, Icon, ModeBadge, Spinner, ThemeToggle } from "./Common";
import DossierPanel from "./DossierPanel";
import EvidencePanel from "./EvidencePanel";
import HindcastPanel from "./HindcastPanel";
import MapView from "./MapView";
import ScenePanel from "./ScenePanel";
import { HindcastCard, SceneCard, SlickCard, TopSuspectCard } from "./SidePanels";
import SuspectsPanel from "./SuspectsPanel";
import WhatIfPanel from "./WhatIfPanel";

type Tool = "none" | "scene" | "drift" | "ais";

const STEPS = [
  { n: 1, title: "Detect & Verify", sub: "SAR → slick polygons", tool: "scene" as Tool },
  { n: 2, title: "Reverse Drift", sub: "origin region + window", tool: "drift" as Tool },
  { n: 3, title: "AIS Correlation", sub: "candidate ranking", tool: "ais" as Tool },
  { n: 4, title: "Legal Dossier", sub: "report + exports", tool: "none" as Tool },
];

/** Domain-mean current & wind at the current replay step (from the stored forcing field). */
function MetOcean() {
  const { hindcast, replayStep } = useStore();
  const ev = hindcast?.detail?.env_vectors;
  if (!ev?.steps?.length) return null;
  const rows: number[][] = ev.steps[Math.min(replayStep, ev.steps.length - 1)];
  const mean = (k: number) => rows.reduce((a, r) => a + r[k], 0) / rows.length;
  const uc = mean(0), vc = mean(1), uw = mean(2), vw = mean(3);
  const dir = (u: number, v: number) => Math.round(((Math.atan2(u, v) * 180) / Math.PI + 360) % 360);
  return (
    <div className="row" style={{ gap: 14 }}>
      <div><div className="xs muted">Current ({ev.provider})</div><div className="mono cyan small">{Math.hypot(uc, vc).toFixed(2)} m/s → {dir(uc, vc)}°</div></div>
      <div><div className="xs muted">Wind 10 m</div><div className="mono cyan small">{(Math.hypot(uw, vw) * 1.944).toFixed(1)} kt → {dir(uw, vw)}°</div></div>
      <Badge kind={ev.data_mode}>{ev.data_mode}</Badge>
    </div>
  );
}

export default function Console() {
  const st = useStore();
  const { caseData, hindcast, attribution, panel, setPanel, tool, setTool, focus, setFocus, layers, toggleLayer, jobs, replayStep, setReplay, playing, setPlaying, health, selectedMmsi, selectedSlick } = st;

  const stepDone = [!!caseData?.slicks.features.length, !!hindcast, !!attribution, !!(attribution && caseData?.slicks.features.some((f) => f.properties.review_status))];
  const activeStep = !stepDone[0] ? 1 : !stepDone[1] ? 2 : !stepDone[2] ? 3 : 4;

  // replay: step through hindcast cloud snapshots
  const cloudN = hindcast?.detail?.cloud?.length || 0;
  const hours: number[] = hindcast?.detail?.cloud_hours || [];
  useEffect(() => {
    if (!playing || !cloudN) return;
    const t = setInterval(() => {
      const s = useStore.getState();
      s.setReplay(s.replayStep + 1 >= cloudN ? 0 : s.replayStep + 1);
    }, 450);
    return () => clearInterval(t);
  }, [playing, cloudN]);
  // first visit of an empty case: open the scene intake automatically; close it once a scene exists
  const loaded = !!caseData;
  const sceneId = caseData?.scene?.id ?? null;
  useEffect(() => {
    if (!loaded) return;
    if (!sceneId) setTool("scene");
    else if (tool === "scene" && caseData!.slicks.features.length) setTool("none");
  }, [loaded, sceneId]);

  const openStep = (i: number) => {
    const s = STEPS[i - 1];
    if (i === 4) { setPanel("dossier"); return; }
    setPanel("map");
    setTool(s.tool === tool ? "none" : s.tool);
  };
  const running = jobs.filter((j) => j.state === "queued" || j.state === "running");
  const rail: { id: Panel; label: string; icon: string }[] = [
    { id: "map", label: "Map", icon: "map" }, { id: "suspects", label: "Suspects", icon: "ship" }, { id: "replay", label: "Replay", icon: "replay" },
    { id: "whatif", label: "What-If", icon: "whatif" }, { id: "evidence", label: "Evidence", icon: "evidence" }, { id: "dossier", label: "Dossier", icon: "dossier" },
  ];
  const layerDefs: { k: keyof LayerVis; label: string; color: string }[] = [
    { k: "scene", label: "SAR scene", color: "#94a3b8" }, { k: "slicks", label: "Slick polygons", color: "#f43f5e" }, { k: "origin", label: "Origin regions 50/70/90%", color: "#f59e0b" },
    { k: "particles", label: "Backtrack particles & paths", color: "#22d3ee" }, { k: "forecast", label: "Forward forecast", color: "#a78bfa" }, { k: "tracks", label: "AIS tracks (all)", color: "#64748b" },
    { k: "candidates", label: "Candidate tracks + CPA", color: "#22c55e" }, { k: "gaps", label: "AIS dark segments", color: "#fb923c" }, { k: "search", label: "Search corridor", color: "#38bdf8" },
    { k: "vectors", label: "Current / wind vectors (met-ocean)", color: "#67e8f9" },
  ];
  const showMap = panel === "map" || panel === "replay";

  return (
    <div className="app">
      <header className="topbar">
        <div className="row" style={{ gap: 10, alignItems: "center" }}>
          <img src="/logo.png" alt="Spill Forensics Logo" style={{ height: "28px", width: "auto", display: "block" }} />
          <span style={{ fontWeight: 800, letterSpacing: "0.12em", color: "var(--cyan)" }}>SPILL FORENSICS</span>
          <span className="muted small">| SAR Oil-Spill Source Attribution</span>
        </div>
        <button className="btn sm ghost" onClick={() => st.setCase(null)}>← cases</button>
        <span style={{ fontWeight: 600 }}>{caseData?.name || "…"}</span>
        <ModeBadge mode={caseData?.data_mode} />
        {caseData?.scene && <span className="small muted">scene {fmtUtc(caseData.scene.acquisition_time)}</span>}
        <div className="right row">
          {running.length > 0 && <span className="small cyan"><Spinner /> {running[0].label} {Math.round((running[0].progress || 0) * 100)}%</span>}
          <ThemeToggle />
          <Badge kind={health ? "ok" : "danger"}>{health ? `API ${health.version}` : "API offline"}</Badge>
          <Badge kind={caseData?.detector.onnx_model_present ? "real" : "neutral"} title={caseData?.detector.onnx_model_path}>{caseData?.detector.active_adapter === "onnx" ? "ONNX model" : "classical detector"}</Badge>
          <Badge kind="neutral">{fmtUtc(new Date().toISOString()).slice(0, 16)} UTC</Badge>
        </div>
      </header>

      <nav className="stepbar">
        {STEPS.map((s, i) => (
          <div key={s.n} className={`step ${stepDone[i] ? "done" : ""} ${activeStep === s.n ? "active" : ""}`} onClick={() => openStep(s.n)}>
            <span className="n">{stepDone[i] ? "✓" : s.n}</span>
            <div><b>{s.title}</b><small>{s.sub}</small></div>
            {i < 3 && <span className="muted" style={{ marginLeft: 6 }}>›</span>}
          </div>
        ))}
      </nav>

      <aside className="rail">
        {rail.map((r) => (
          <button key={r.id} className={panel === r.id ? "active" : ""} onClick={() => setPanel(r.id)} title={r.label}><Icon name={r.icon} />{r.label}</button>
        ))}
        <div style={{ flex: 1 }} />
        <button onClick={() => { setPanel("map"); setTool(tool === "scene" ? "none" : "scene"); }} className={tool === "scene" ? "active" : ""} title="Scene intake"><Icon name="sat" />Scene</button>
        <button onClick={() => { setPanel("map"); setTool(tool === "drift" ? "none" : "drift"); }} className={tool === "drift" ? "active" : ""} title="Drift forcing"><Icon name="upload" />Drift</button>
        <button onClick={() => { setPanel("map"); setTool(tool === "ais" ? "none" : "ais"); }} className={tool === "ais" ? "active" : ""} title="AIS"><Icon name="ship" />AIS</button>
      </aside>

      <main className="center">
        <MapView />
        {showMap && (
          <>
            <div className="overlay map-focus">
              {([["corridor", "Full Corridor"], ["slick", "Slick Poly"], ["origin", "Spill Origin"], ["suspect", "Top Suspect"]] as const).map(([k, l]) => (
                <button key={k} className={`chip ${focus === k ? "active" : ""}`} onClick={() => setFocus(k)} disabled={(k === "origin" && !hindcast) || (k === "suspect" && !attribution?.candidates.length)}>{l}</button>
              ))}
              <button className={`chip ${layers.gaps ? "active" : ""}`} onClick={() => toggleLayer("gaps")} disabled={!attribution}>Dark Segment</button>
            </div>
            <div className="overlay map-layers">
              <div className="xs muted" style={{ marginBottom: 4 }}>Map layers</div>
              {layerDefs.map((l) => (
                <label key={l.k}><input type="checkbox" checked={layers[l.k]} onChange={() => toggleLayer(l.k)} /><i style={{ display: "inline-block", width: 10, height: 10, borderRadius: 2, background: l.color }} />{l.label}</label>
              ))}
              <div className="xs muted" style={{ margin: "6px 0 2px" }}>Basemap</div>
              <div className="row" style={{ gap: 4 }}>
                {(["dark", "ocean", "osm"] as const).map((b) => <button key={b} className={`chip ${st.basemap === b ? "active" : ""}`} style={{ padding: "2px 8px" }} onClick={() => st.setBasemap(b)}>{b}</button>)}
              </div>
            </div>
            <div className="overlay map-legend">
              <div className="xs muted">Legend</div>
              <span><i style={{ background: "#f43f5e" }} />slick (oil) · <i style={{ background: "#f59e0b" }} />uncertain</span>
              <span><i style={{ background: "#f59e0b", opacity: 0.6 }} />origin 50/70/90% · <i style={{ background: "#22d3ee" }} />particles T−{hours[Math.min(replayStep, Math.max(hours.length - 1, 0))] ?? "—"} h</span>
              <span><i style={{ background: "#22c55e" }} />candidate track · <i style={{ background: "#fb923c" }} />AIS gap · <i style={{ background: "#a78bfa" }} />forecast</span>
              <span className="muted">© OpenStreetMap · Open-Meteo/Copernicus Marine · Sentinel-1 via Planetary Computer</span>
            </div>
            {tool === "scene" && <ScenePanel onClose={() => setTool("none")} />}
            {tool === "drift" && <HindcastPanel onClose={() => setTool("none")} />}
            {tool === "ais" && <AisPanel onClose={() => setTool("none")} />}
          </>
        )}
        {panel === "suspects" && <SuspectsPanel />}
        {panel === "whatif" && <WhatIfPanel />}
        {panel === "evidence" && <EvidencePanel />}
        {panel === "dossier" && <DossierPanel />}
      </main>

      <aside className="side">
        <SceneCard />
        <SlickCard />
        <HindcastCard />
        <TopSuspectCard />
        {jobs.length > 0 && (
          <div className="card">
            <h4>Jobs</h4>
            {jobs.slice(0, 6).map((j) => (
              <div key={j.id} className="small" style={{ marginBottom: 6 }}>
                <div className="row"><span className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{j.label}</span><Badge kind={j.state === "failed" ? "danger" : j.state === "completed" ? "ok" : "neutral"}>{j.state}</Badge></div>
                {(j.state === "running" || j.state === "queued") && <><div className="prog"><i style={{ width: `${Math.round((j.progress || 0) * 100)}%` }} /></div><div className="xs muted">{j.message}</div></>}
                {j.state === "failed" && <div className="xs red">{j.error}</div>}
              </div>
            ))}
          </div>
        )}
      </aside>

      <footer className="bottom">
        <div className="row">
          <button className="btn sm ghost" onClick={() => setReplay(0)} disabled={!cloudN} title="T0 (acquisition)"><Icon name="first" /></button>
          <button className="btn sm" onClick={() => setPlaying(!playing)} disabled={!cloudN}><Icon name={playing ? "pause" : "play"} /></button>
          <button className="btn sm ghost" onClick={() => setReplay(cloudN - 1)} disabled={!cloudN} title="window start"><Icon name="last" /></button>
        </div>
        <div className="timeline">
          <input type="range" className="slider" min={0} max={Math.max(cloudN - 1, 0)} value={Math.min(replayStep, Math.max(cloudN - 1, 0))} onChange={(e) => setReplay(Number(e.target.value))} disabled={!cloudN} />
          <div className="marks">
            <span>T0 {caseData?.scene ? fmtUtc(caseData.scene.acquisition_time).slice(5, 16) : "—"} (acquisition)</span>
            <span className="cyan">{cloudN ? `T−${hours[Math.min(replayStep, cloudN - 1)]} h · ${hindcast?.detail?.cloud_steps?.[Math.min(replayStep, cloudN - 1)] ? fmtUtc(hindcast.detail.cloud_steps[Math.min(replayStep, cloudN - 1)]).slice(5, 16) : ""}` : "no hindcast — replay disabled"}</span>
            <span>{hindcast ? `T−${hindcast.config?.hindcast_hours} h` : "—"}</span>
          </div>
        </div>
        <MetOcean />
        <div className="small muted" style={{ minWidth: 200, textAlign: "right" }}>
          {selectedSlick && <span>slick <span className="mono cyan">{selectedSlick}</span></span>}
          {selectedMmsi && <span> · vessel <span className="mono cyan">{selectedMmsi}</span></span>}
          {hindcast && <span> · window <span className="mono">{fmtUtc(hindcast.release_start).slice(5, 16)}→{fmtUtc(hindcast.release_end).slice(5, 16)}</span></span>}
        </div>
      </footer>
    </div>
  );
}
