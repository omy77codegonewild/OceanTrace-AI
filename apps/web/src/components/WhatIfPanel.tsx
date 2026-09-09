import { useMemo, useState } from "react";
import { api, fmtNum, fmtUtc } from "../lib/api";
import { useStore } from "../lib/store";
import { Badge, Card, Slider } from "./Common";

/**
 * What-If / sensitivity. Two real analyses:
 *  1) Scenario table of every retained hindcast run (params → window/areas) + a real parameter sweep (launches N jobs).
 *  2) Weight sensitivity: re-scores the persisted per-vessel factors client-side with S = 100·D·Σ w_i·f_i (same formula as the backend).
 */
export default function WhatIfPanel() {
  const { caseData, hindcast, attribution, runJob, caseId, setPanel, config, loadHindcast } = useStore();
  const runs = (caseData?.hindcast_runs || []).filter((r: any) => r.status === "completed");
  const defW: Record<string, number> = config?.attribution?.weights || {};
  const keys = ["proximity", "temporal_overlap", "heading_compatibility", "loitering", "ais_gap_relevance", "vessel_type"];
  const [w, setW] = useState<Record<string, number>>({ ...defW });
  const [sweepN, setSweepN] = useState(3);
  const [sweepWmax, setSweepWmax] = useState(6);
  const [sweepHours, setSweepHours] = useState<number>(hindcast?.config?.hindcast_hours ?? config?.hindcast?.defaults?.hindcast_hours ?? 24);

  const rescored = useMemo(() => {
    if (!attribution) return [];
    const sum = keys.reduce((a, k) => a + (w[k] ?? 0), 0) || 1;
    const bands = config?.attribution?.priority_bands || { high: 80, medium: 55, low: 30 };
    return attribution.candidates
      .map((c) => {
        const S = 100 * c.factors.data_quality * keys.reduce((a, k) => a + ((w[k] ?? 0) / sum) * (c.factors[k] ?? 0), 0);
        const pr = S >= bands.high ? "high" : S >= bands.medium ? "medium" : S >= bands.low ? "low" : "insufficient";
        return { ...c, newScore: S, newPriority: pr };
      })
      .sort((a, b) => b.newScore - a.newScore)
      .map((c, i) => ({ ...c, newRank: i + 1 }));
  }, [attribution, w, config]);

  const sweep = async () => {
    if (!caseId || !hindcast) return;
    const base = hindcast.config || {};
    const lows = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0].slice(0, sweepN);
    for (const lo of lows) {
      const hi = Math.min(sweepWmax, lo + 1.5);
      await runJob(`Sweep windage ${lo}–${hi}%`, () => api.hindcast(caseId, hindcast.slick_id, { ...base, hindcast_hours: sweepHours, windage_range: [lo / 100, hi / 100], ensemble_members: Math.min(base.ensemble_members || 10, 10), particle_count: Math.min(base.particle_count || 300, 300), environment_source: hindcast.metrics?.environment?.provider === "constant" ? "constant" : "open_meteo" }));
    }
  };

  return (
    <div className="fullpanel">
      <div className="row" style={{ marginBottom: 10 }}>
        <div><h2>What-If &amp; Sensitivity</h2><div className="small muted">Every row is a real run persisted for this case — nothing is simulated in the browser except the weight re-scoring below.</div></div>
        <button className="btn sm right" onClick={() => setPanel("map")}>Map</button>
      </div>
      <div className="grid2" style={{ alignItems: "start" }}>
        <div className="col">
          <Card title={`Hindcast scenarios (${runs.length})`}>
            {runs.length === 0 && <div className="small muted">Run a hindcast first.</div>}
            {runs.length > 0 && (
              <table className="tbl small" style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead><tr><th>run</th><th>h</th><th>E×N</th><th>windage</th><th>cur×</th><th>window (UTC)</th><th>70% km²</th><th>env</th></tr></thead>
                <tbody>
                  {runs.map((r: any) => (
                    <tr key={r.id} className={hindcast?.id === r.id ? "sel" : ""} onClick={() => loadHindcast(r.id)}>
                      <td className="mono">{r.id.slice(-6)}</td><td>{r.hindcast_hours}</td><td>{r.ensemble_members}×{r.particle_count}</td>
                      <td className="mono">{(r.windage_range || []).map((x: number) => (x * 100).toFixed(1)).join("–")}%</td>
                      <td className="mono">{(r.current_scale_range || []).map((x: number) => x.toFixed(1)).join("–")}</td>
                      <td className="mono">{fmtUtc(r.release_start).slice(5, 16)} → {fmtUtc(r.release_end).slice(5, 16)}</td>
                      <td className="mono">{fmtNum(r.origin_areas_km2?.["70"], 0)}</td>
                      <td><Badge kind={r.environment?.data_mode || "real"}>{r.environment?.provider}</Badge></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <div className="small muted" style={{ marginTop: 6 }}>Click a row to load that scenario on the map and use it for attribution.</div>
          </Card>
          <Card title="Parameter sweep (launches real jobs)">
            <Slider label="Scenarios" value={sweepN} min={2} max={6} step={1} onChange={setSweepN} />
            <Slider label="Windage upper bound" value={sweepWmax} min={2} max={8} step={0.5} onChange={setSweepWmax} fmt={(v) => `${v}%`} />
            <Slider label="Backtrack duration" value={sweepHours} min={6} max={72} step={6} onChange={setSweepHours} fmt={(v) => `${v} h`} />
            <button className="btn primary block" style={{ marginTop: 8 }} disabled={!hindcast} onClick={sweep}>Run windage sweep on {hindcast?.slick_id || "—"}</button>
            <div className="small muted" style={{ marginTop: 6 }}>Each scenario re-uses the current run's parameters, capped at 10 members × 300 particles for speed. Results appear in the table above and in the drift panel's scenario selector.</div>
          </Card>
        </div>
        <div className="col">
          <Card title="Weight sensitivity (re-score persisted factors)" right={<button className="btn sm ghost" onClick={() => setW({ ...defW })}>reset</button>}>
            {keys.map((k) => <Slider key={k} label={k.replace(/_/g, " ")} value={w[k] ?? 0} min={0} max={0.6} step={0.05} onChange={(v) => setW({ ...w, [k]: v })} fmt={(v) => v.toFixed(2)} />)}
            {!attribution && <div className="small muted" style={{ marginTop: 8 }}>Run an attribution to see how the ranking responds to weight changes.</div>}
            {attribution && (
              <table className="tbl small" style={{ width: "100%", marginTop: 10, borderCollapse: "collapse" }}>
                <thead><tr><th>new #</th><th>was</th><th>vessel</th><th>score → new</th><th>Δ</th></tr></thead>
                <tbody>
                  {rescored.map((c) => (
                    <tr key={c.mmsi}>
                      <td className="mono">{c.newRank}</td><td className="mono muted">{c.rank}</td>
                      <td>{c.vessel?.name || c.mmsi}</td>
                      <td><span className={`score ${c.priority}`} style={{ fontSize: 13 }}>{c.score.toFixed(1)}</span> → <span className={`score ${c.newPriority}`} style={{ fontSize: 13 }}>{c.newScore.toFixed(1)}</span></td>
                      <td className={`mono ${c.newRank < c.rank ? "green" : c.newRank > c.rank ? "red" : "muted"}`}>{c.newRank === c.rank ? "=" : c.newRank < c.rank ? `▲${c.rank - c.newRank}` : `▼${c.newRank - c.rank}`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <div className="small muted" style={{ marginTop: 6 }}>Client-side preview only. To persist a re-weighted ranking, run attribution again with these weights from the AIS panel.</div>
          </Card>
        </div>
      </div>
    </div>
  );
}
