import { fmtNum, fmtUtc } from "../lib/api";
import { useStore } from "../lib/store";
import { Badge, Card } from "./Common";

/** Evidence / provenance graph: each node is a real artifact of this case with its provenance; missing nodes are shown dim, never fabricated. */
export default function EvidencePanel() {
  const { caseData, hindcast, attribution, setPanel, selectedSlick } = useStore();
  const scene = caseData?.scene;
  const slick = caseData?.slicks.features.find((f) => f.id === (hindcast?.slick_id || selectedSlick));
  const env = hindcast?.detail?.environment;
  const top = attribution?.candidates?.[0];
  const m = scene?.metadata || {};

  const Node = ({ on, title, lines, kind }: { on: boolean; title: string; lines: (string | null | undefined)[]; kind?: string }) => (
    <div className={`node ${on ? "" : "off"}`}>
      <div className="row"><b>{title}</b>{kind && on && <span className="right"><Badge kind={kind}>{kind}</Badge></span>}</div>
      {on ? lines.filter(Boolean).map((l, i) => <small key={i} style={{ display: "block" }}>{l}</small>) : <small className="muted">not yet produced</small>}
    </div>
  );
  const Arrow = ({ label }: { label: string }) => <div className="col arrow" style={{ alignItems: "center", gap: 0, minWidth: 80, textAlign: "center" }}><span className="xs muted">{label}</span><span className="cyan">⟶</span></div>;

  return (
    <div className="fullpanel">
      <div className="row" style={{ marginBottom: 10 }}>
        <div><h2>Evidence &amp; Provenance Chain</h2><div className="small muted">Causal chain from observation to investigation priority. Every node is traceable to a stored artifact, config hash and audit entry.</div></div>
        <button className="btn sm right" onClick={() => setPanel("map")}>Map</button>
      </div>
      <div className="chain">
        <Node on={!!scene} title="SAR observation" kind={m.source === "planetary_computer" ? "real" : "imported"} lines={[scene && `acq ${fmtUtc(scene.acquisition_time)}`, m.item_id || m.original_name, m.georef_source && `georef: ${m.georef_source}`, Array.isArray(m.pixel_size_m) && `${Math.round(m.pixel_size_m[0])} m px`]} />
        <Arrow label="segmentation" />
        <Node on={!!slick} title="Slick polygon" lines={[slick && `${slick.id} · ${slick.properties.class}`, slick && `conf ${fmtNum(slick.properties.confidence, 2)} · ${fmtNum(slick.properties.area_km2, 2)} km²`, slick && `model ${slick.properties.model_version}`, slick?.properties.review_status && `analyst: ${slick.properties.review_status}`]} />
        <Arrow label="forcing" />
        <Node on={!!env} title="Met-ocean forcing" kind={env?.data_mode} lines={[env?.source, env?.wind_source && `wind: ${env.wind_source}`, env?.grid && `grid ${env.grid}`, env?.time_range && `${fmtUtc(env.time_range[0]).slice(0, 16)} → ${fmtUtc(env.time_range[1]).slice(0, 16)}`]} />
        <Arrow label="backward Lagrangian" />
        <Node on={!!hindcast} title="Origin region + window" lines={[hindcast && `${fmtUtc(hindcast.release_start)} → ${fmtUtc(hindcast.release_end)}`, hindcast && `70%: ${fmtNum(hindcast.metrics?.origin_areas_km2?.["70"], 0)} km² · basis ${hindcast.metrics?.window_basis}`, hindcast && `${hindcast.metrics?.ensemble_members}×${hindcast.metrics?.particle_count} particles`]} />
        <Arrow label="space-time match" />
        <Node on={!!attribution} title="AIS correlation" lines={[attribution && `${attribution.summary?.vessels_scored} scored of ${attribution.summary?.vessels_in_dataset}`, attribution && `corridor buffer ${attribution.summary?.search_buffer_km} km`, attribution?.summary?.dark_source_possible ? "▲ dark source possible" : null]} />
        <Arrow label="ranking" />
        <Node on={!!top} title="Candidate for investigation" kind={top?.priority === "high" ? "danger" : top?.priority === "medium" ? "warn" : "neutral"} lines={[top && `${top.vessel?.name || "unnamed"} (MMSI ${top.mmsi})`, top && `score ${top.score.toFixed(1)} · ${top.priority} priority`, top?.review_status && `analyst: ${top.review_status}`]} />
      </div>
      <div className="grid2" style={{ marginTop: 16, alignItems: "start" }}>
        <Card title="Model & config provenance">
          <table className="tbl small" style={{ width: "100%" }}>
            <tbody>
              <tr><td className="muted">case</td><td className="mono">{caseData?.id} · {caseData?.data_mode}</td></tr>
              <tr><td className="muted">config hash</td><td className="mono">{caseData?.config_hash || "—"}</td></tr>
              <tr><td className="muted">detector</td><td className="mono">{caseData?.detector.active_adapter} {caseData?.detector.onnx_model_present ? "(ONNX model present)" : "(classical fallback — upload ONNX weights to data/models)"}</td></tr>
              <tr><td className="muted">hindcast run</td><td className="mono">{hindcast?.id || "—"} · {hindcast?.config?.integrator} · seed {hindcast?.config?.seed}</td></tr>
              <tr><td className="muted">attribution run</td><td className="mono">{attribution?.id || "—"}</td></tr>
              <tr><td className="muted">weights</td><td className="mono small">{attribution ? Object.entries(attribution.weights).map(([k, v]) => `${k.split("_")[0]}=${(v as number).toFixed(2)}`).join(" ") : "—"}</td></tr>
            </tbody>
          </table>
        </Card>
        <Card title="Stated limitations" amber>
          <ul className="small" style={{ margin: 0, paddingLeft: 16 }}>
            {(slick?.properties.limitations || []).map((l, i) => <li key={"s" + i}>{l}</li>)}
            {(hindcast?.metrics?.limitations || []).map((l: string, i: number) => <li key={"h" + i}>{l}</li>)}
            {(top?.limitations || []).map((l, i) => <li key={"a" + i}>{l}</li>)}
            {!slick && !hindcast && <li>No artifacts yet.</li>}
          </ul>
        </Card>
      </div>
    </div>
  );
}
