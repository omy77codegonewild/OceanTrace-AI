import { useState } from "react";
import { api, fmtNum, fmtUtc } from "../lib/api";
import { useStore } from "../lib/store";
import { Badge, Card, Factor, KV } from "./Common";

/** Right-hand column content: Step 1 slick details, Step 2 hindcast result, Step 3 top suspect summary. */
export function SlickCard() {
  const { caseData, selectedSlick, selectSlick, caseId, notify, refresh } = useStore();
  const feats = caseData?.slicks.features || [];
  const f = feats.find((x) => x.id === selectedSlick);
  const [note, setNote] = useState("");
  const review = async (status: "confirmed" | "look_alike" | "uncertain") => {
    if (!caseId || !f) return;
    try {
      await api.review(caseId, { slick: { slick_id: f.id, status, note: note || null } });
      notify("ok", `Slick ${f.id} marked ${status}`);
      await refresh();
    } catch (e: any) { notify("error", e.message); }
  };
  if (!caseData?.scene) return <Card title="Detection"><div className="small muted">No scene yet. Open the scene panel (satellite icon) to upload a GeoTIFF/PNG or fetch a Sentinel-1 subset.</div></Card>;
  return (
    <Card title={`Detections (${feats.length})`} right={<Badge kind="neutral">{caseData.detector.active_adapter}</Badge>}>
      {feats.length === 0 && <div className="small muted">Detector found no dark features above threshold. Lower the probability threshold / min area in the scene panel and re-run.</div>}
      <div className="col" style={{ gap: 4, maxHeight: 180, overflowY: "auto" }}>
        {feats.map((x) => (
          <div key={x.id} className={`row chip ${x.id === selectedSlick ? "active" : ""}`} onClick={() => { selectSlick(x.id); useStore.getState().setFocus("slick"); }}>
            <span className="mono">{x.id}</span>
            <Badge kind={x.properties.class === "oil" ? "danger" : x.properties.class === "uncertain" ? "warn" : "neutral"}>{x.properties.class}</Badge>
            <span className="right mono">{fmtNum(x.properties.area_km2, 2)} km²</span>
          </div>
        ))}
      </div>
      {f && (
        <div className="col" style={{ marginTop: 10 }}>
          <KV items={[
            ["confidence", <>{fmtNum(f.properties.confidence, 2)} <span className="muted small">(p̄ {fmtNum(f.properties.prob_mean, 2)})</span></>],
            ["area", `${fmtNum(f.properties.area_km2, 2)} km²`],
            ["centroid", <span className="small">{f.properties.centroid?.[1]?.toFixed(4)}°N {f.properties.centroid?.[0]?.toFixed(4)}°E</span>],
            ["axis / orientation", `${fmtNum(f.properties.major_axis_km, 1)}×${fmtNum(f.properties.minor_axis_km, 1)} km · ${fmtNum(f.properties.orientation_deg, 0)}°`],
            ["coast distance", f.properties.coast_distance_km == null ? "—" : `${fmtNum(f.properties.coast_distance_km, 1)} km`],
            ["look-alike risk", <Badge kind={f.properties.look_alike_risk === "high" ? "danger" : f.properties.look_alike_risk === "medium" ? "warn" : "ok"}>{f.properties.look_alike_risk}</Badge>],
          ]} />
          <div className="small muted">model <span className="mono">{f.properties.model_version}</span> · adapter {f.properties.adapter} · {f.properties.pixel_count} px</div>
          {f.properties.limitations?.length > 0 && (
            <ul className="small muted" style={{ margin: 0, paddingLeft: 16 }}>{f.properties.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul>
          )}
          <div className="row small"><span className="muted">analyst review:</span>{f.properties.review_status ? <Badge kind={f.properties.review_status === "confirmed" ? "ok" : "warn"}>{f.properties.review_status}</Badge> : <span className="muted">pending</span>}</div>
          <input className="input" placeholder="review note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="row">
            <button className="btn sm grow" onClick={() => review("confirmed")}>Confirm oil</button>
            <button className="btn sm grow" onClick={() => review("look_alike")}>Look-alike</button>
            <button className="btn sm grow ghost" onClick={() => review("uncertain")}>Uncertain</button>
          </div>
        </div>
      )}
    </Card>
  );
}

export function SceneCard() {
  const { caseData } = useStore();
  const s = caseData?.scene;
  if (!s) return null;
  const m = s.metadata || {};
  return (
    <Card title="Scene" right={<Badge kind={m.source === "planetary_computer" ? "real" : "imported"}>{m.source === "planetary_computer" ? "Sentinel-1 GRD · REAL" : "uploaded"}</Badge>}>
      <KV items={[
        ["acquired (UTC)", <span className="small">{fmtUtc(s.acquisition_time)}</span>],
        ["time source", <span className="small">{m.acquisition_time_source || "—"}</span>],
        ["georef", <span className="small">{m.georef_source || "—"}</span>],
        ["pixel", Array.isArray(m.pixel_size_m) ? `${Math.round(m.pixel_size_m[0])} × ${Math.round(m.pixel_size_m[1])} m` : "—"],
        ["size", m.analysis_shape ? `${m.analysis_shape[1]}×${m.analysis_shape[0]}` : "—"],
        ["bounds", <span className="small mono">{s.bounds.map((b) => b.toFixed(3)).join(", ")}</span>],
      ]} />
      {m.warnings?.length > 0 && <div className="small amber" style={{ marginTop: 6 }}>▲ {m.warnings.join(" · ")}</div>}
    </Card>
  );
}

export function HindcastCard() {
  const { hindcast, forecast } = useStore();
  if (!hindcast) return <Card title="Reverse drift"><div className="small muted">No hindcast yet. Select a slick, open the drift panel (⟲) and run a backtrack.</div></Card>;
  const m = hindcast.metrics || {};
  const d = hindcast.detail || {};
  const areas = m.origin_areas_km2 || {};
  return (
    <>
      <Card title="Probable source region" right={<Badge kind={m.environment?.data_mode || "real"}>{(m.environment?.provider || "").replace("_", "-")} · {(m.environment?.data_mode || "").toUpperCase()}</Badge>}>
        <div className="tile" style={{ marginBottom: 8 }}>
          <div className="xs muted">Release window (UTC)</div>
          <div className="mono cyan">{fmtUtc(hindcast.release_start)}</div>
          <div className="mono cyan">→ {fmtUtc(hindcast.release_end)}</div>
          <div className="small muted">T−{m.window_hours_back?.[1]} h … T−{m.window_hours_back?.[0]} h · peak T−{m.peak_hours_back} h · basis <span className="mono">{m.window_basis}</span></div>
        </div>
        <KV items={[
          ["50% region", `${fmtNum(areas["50"], 1)} km²`],
          ["70% region", `${fmtNum(areas["70"], 1)} km²`],
          ["90% region", `${fmtNum(areas["90"], 1)} km²`],
          ["ensemble", `${m.ensemble_members} × ${m.particle_count}`],
          ["stranded", `${fmtNum((m.stranded_fraction || 0) * 100, 1)} %`],
          ["confidence level", `${Math.round((m.confidence_level || 0) * 100)} %`],
        ]} />
        {d.environment && <div className="small muted" style={{ marginTop: 6 }}>{d.environment.source} {d.environment.wind_source ? `· wind: ${d.environment.wind_source}` : ""}</div>}
        {m.limitations?.length > 0 && <ul className="small muted" style={{ margin: "6px 0 0", paddingLeft: 16 }}>{m.limitations.slice(0, 4).map((l: string, i: number) => <li key={i}>{l}</li>)}</ul>}
      </Card>
      {forecast && (
        <Card title="Forward forecast" right={<Badge kind={forecast.coastal_impact_risk ? "danger" : "ok"}>{forecast.coastal_impact_risk ? "coastal impact risk" : "offshore"}</Badge>}>
          <KV items={[["horizon", `${forecast.hours} h`], ["envelope", `${fmtNum(forecast.envelope_area_km2, 0)} km²`], ["stranded", `${fmtNum((forecast.stranded_fraction || 0) * 100, 1)} %`], ["generated", <span className="small">{fmtUtc(forecast.created_at)}</span>]]} />
        </Card>
      )}
    </>
  );
}

export function TopSuspectCard() {
  const { attribution, selectedMmsi, selectMmsi, setPanel } = useStore();
  if (!attribution) return <Card title="AIS correlation"><div className="small muted">No attribution run yet. Import AIS covering the release window and run the correlation (ship icon).</div></Card>;
  const c = attribution.candidates.find((x) => x.mmsi === selectedMmsi) || attribution.candidates[0];
  const s = attribution.summary || {};
  if (!c) return <Card title="AIS correlation" amber><div className="small">No vessel had AIS positions inside the search corridor ({s.vessels_in_dataset} vessels in dataset, {s.vessels_in_time_window} in time window). A dark (non-transmitting) source cannot be excluded.</div></Card>;
  return (
    <Card title={c.rank === 1 ? "Top candidate for investigation" : `Candidate #${c.rank}`} right={<button className="btn sm ghost" onClick={() => setPanel("suspects")}>matrix →</button>}>
      <div className="row">
        <div className="grow">
          <div style={{ fontWeight: 700 }}>{c.vessel?.name || "unnamed vessel"}</div>
          <div className="small muted mono">MMSI {c.mmsi} · {c.vessel?.type || "type unknown"}</div>
        </div>
        <div className={`score ${c.priority}`}>{c.score.toFixed(1)}</div>
      </div>
      <div className="col" style={{ gap: 4, marginTop: 8 }}>
        {Object.entries(c.factors).filter(([k]) => k !== "data_quality").map(([k, v]) => <Factor key={k} name={k.replace(/_/g, " ")} value={v as number} />)}
        <Factor name="data quality D" value={c.factors.data_quality} amber />
      </div>
      <div className="small muted" style={{ marginTop: 6 }}>Min distance {fmtNum(c.raw?.min_distance_km, 1)} km{c.raw?.spacetime_min_km != null ? ` · space-time ${fmtNum(c.raw.spacetime_min_km, 1)} km` : ""} · {c.raw?.positions} fixes</div>
      <div className="row small" style={{ marginTop: 6 }}>
        <Badge kind={c.priority === "high" ? "danger" : c.priority === "medium" ? "warn" : "neutral"}>{c.priority} priority</Badge>
        <span className="muted">{s.vessels_scored} scored / {s.vessels_in_dataset} in dataset</span>
        {c.mmsi !== selectedMmsi && <button className="btn sm ghost right" onClick={() => selectMmsi(c.mmsi)}>focus</button>}
      </div>
      <div className="xs amber" style={{ marginTop: 8 }}>Investigation priority only — not an accusation.</div>
    </Card>
  );
}
