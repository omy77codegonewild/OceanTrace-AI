import { useState } from "react";
import { api, fmtNum, fmtUtc } from "../lib/api";
import { useStore } from "../lib/store";
import { Badge, Card, Factor, KV } from "./Common";

/** Vessel Attribution Matrix — full-centre panel. Everything shown comes from the persisted attribution run. */
export default function SuspectsPanel() {
  const { attribution, caseData, selectedMmsi, selectMmsi, setPanel, caseId, notify, refresh, hindcast } = useStore();
  const [note, setNote] = useState("");
  const s = attribution?.summary || {};
  const bands = useStore.getState().config?.attribution?.priority_bands || { high: 80, medium: 55, low: 30 };

  if (!attribution) {
    return (
      <div className="fullpanel">
        <h2>Vessel Attribution Matrix</h2>
        <div className="muted small" style={{ marginBottom: 14 }}>Ranks AIS-transmitting vessels by consistency with the estimated origin region and release window. Score S = 100·D·(0.30 P + 0.25 T + 0.10 H + 0.10 L + 0.15 G + 0.10 V) — weights configurable.</div>
        <Card title="Not run yet" amber>
          <div className="small">Steps: {hindcast ? "✓ hindcast available" : "① run a hindcast"} → {caseData?.ais_summary.positions ? `✓ ${caseData.ais_summary.positions} AIS positions loaded` : "② import AIS covering the release window"} → ③ run AIS correlation from the ship panel.</div>
          <button className="btn primary" style={{ marginTop: 10 }} onClick={() => setPanel("map")}>Back to map</button>
        </Card>
      </div>
    );
  }
  const cands = attribution.candidates;
  const sel = cands.find((c) => c.mmsi === selectedMmsi) || cands[0];
  const review = async (status: "investigate" | "dismiss" | "unknown") => {
    if (!caseId || !sel) return;
    try {
      await api.review(caseId, { candidate: { mmsi: sel.mmsi, status, note: note || null } });
      notify("ok", `MMSI ${sel.mmsi} marked ${status}`);
      await refresh();
    } catch (e: any) { notify("error", e.message); }
  };
  const aisModes: string[] = Array.from(new Set((caseData?.ais_imports || []).map((i: any) => i.data_mode)));

  return (
    <div className="fullpanel">
      <div className="row" style={{ marginBottom: 6 }}>
        <div>
          <h2>Vessel Attribution Matrix</h2>
          <div className="small muted">run {attribution.id} · hindcast {attribution.hindcast_run_id} · {fmtUtc(attribution.created_at)}</div>
        </div>
        <div className="right row">
          {aisModes.map((m) => <Badge key={m} kind={m}>AIS {m}</Badge>)}
          {s.dark_source_possible && <Badge kind="warn">▲ dark source possible</Badge>}
          <button className="btn sm" onClick={() => setPanel("map")}>Map</button>
        </div>
      </div>
      <div className="grid3" style={{ marginBottom: 12 }}>
        <div className="card metric"><div className="v">{s.vessels_in_dataset ?? "—"}</div><div className="k">vessels in dataset</div></div>
        <div className="card metric"><div className="v">{s.vessels_in_search_region ?? "—"}</div><div className="k">in space-time corridor</div></div>
        <div className="card metric"><div className="v">{s.vessels_scored ?? "—"}</div><div className="k">scored candidates</div></div>
      </div>
      <div className="small muted" style={{ marginBottom: 10 }}>
        Search window <span className="mono cyan">{fmtUtc(s.search_time_start_utc)} → {fmtUtc(s.search_time_end_utc)}</span> · release window <span className="mono">{fmtUtc(s.release_window?.[0])} → {fmtUtc(s.release_window?.[1])}</span> · origin buffer {s.search_buffer_km} km · expected AIS interval {s.expected_report_interval_min} min
      </div>
      {cands.length === 0 ? (
        <Card title="No candidates" amber><div className="small">{s.vessels_in_time_window} vessel(s) transmitted during the search window but none entered the corridor. Widen AIS coverage (more providers / satellite AIS) or consider a non-transmitting (dark) source. {s.note}</div></Card>
      ) : (
        <div className="grid2" style={{ gridTemplateColumns: "minmax(0, 1.3fr) minmax(320px, 1fr)", alignItems: "start" }}>
          <div className="card tblwrap" style={{ padding: 0 }}>
            <table className="tbl compact" style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr><th>#</th><th>Vessel</th><th>Type</th><th>Score</th><th title="proximity">P</th><th title="temporal overlap">T</th><th title="heading compatibility">H</th><th title="loitering">L</th><th title="AIS gap relevance">G</th><th title="vessel type prior">V</th><th title="data quality multiplier">D</th><th>Review</th></tr></thead>
              <tbody>
                {cands.map((c) => (
                  <tr key={c.mmsi} className={sel?.mmsi === c.mmsi ? "sel" : ""} onClick={() => selectMmsi(c.mmsi)}>
                    <td className="mono">{c.rank}</td>
                    <td><div style={{ fontWeight: 600, whiteSpace: "nowrap" }}>{c.vessel?.name || "unnamed"}</div><div className="small muted mono">{c.mmsi}</div></td>
                    <td className="small">{c.vessel?.type || "unknown"}</td>
                    <td><span className={`score ${c.priority}`}>{c.score.toFixed(1)}</span><div className="xs muted">{c.priority}</div></td>
                    {["proximity", "temporal_overlap", "heading_compatibility", "loitering", "ais_gap_relevance", "vessel_type", "data_quality"].map((k) => <td key={k} className="mono small">{fmtNum(c.factors[k], 2)}</td>)}
                    <td>{c.review_status ? <Badge kind={c.review_status === "dismiss" ? "neutral" : "warn"}>{c.review_status}</Badge> : <span className="muted small">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="small muted" style={{ padding: 10 }}>Bands: High ≥{bands.high} · Medium ≥{bands.medium} · Low ≥{bands.low} · else Insufficient. {s.note}</div>
          </div>
          {sel && (
            <div className="col">
              <Card title={`#${sel.rank} · ${sel.vessel?.name || "unnamed"} · MMSI ${sel.mmsi}`} right={<span className={`score ${sel.priority}`}>{sel.score.toFixed(1)}</span>}>
                <div className="col" style={{ gap: 4 }}>
                  <Factor name="P proximity (space-time)" value={sel.factors.proximity} />
                  <Factor name="T temporal overlap" value={sel.factors.temporal_overlap} />
                  <Factor name="H heading compatibility" value={sel.factors.heading_compatibility} />
                  <Factor name="L loitering" value={sel.factors.loitering} />
                  <Factor name="G AIS gap relevance" value={sel.factors.ais_gap_relevance} />
                  <Factor name="V vessel type prior" value={sel.factors.vessel_type} />
                  <Factor name="D data quality (multiplier)" value={sel.factors.data_quality} amber />
                </div>
                <KV items={[
                  ["vessel type", sel.vessel?.type || "unknown"],
                  ["flag / IMO", sel.vessel?.flag || sel.vessel?.imo ? `${sel.vessel?.flag || "—"}${sel.vessel?.imo ? ` · IMO ${sel.vessel.imo}` : ""}${sel.vessel?.callsign ? ` (${sel.vessel.callsign})` : ""}` : "—"],
                  ["min distance to origin", `${fmtNum(sel.raw?.min_distance_km, 1)} km`],
                  ["space-time min", sel.raw?.spacetime_min_km == null ? "n/a" : `${fmtNum(sel.raw.spacetime_min_km, 1)} km`],
                  ["closest approach", <span className="small">{fmtUtc(sel.raw?.closest_time_utc)}</span>],
                  ["dwell in 90% region", `${fmtNum(sel.raw?.dwell_minutes, 0)} min`],
                  ["slow-speed dwell", `${fmtNum(sel.raw?.loiter_minutes, 0)} min`],
                  ["AIS gaps", `${sel.raw?.gaps?.length ?? 0}`],
                  ["track bearing", sel.raw?.track_bearing_deg == null ? "—" : `${fmtNum(sel.raw.track_bearing_deg, 0)}°`],
                  ["source→slick bearing", `${fmtNum(sel.raw?.source_to_slick_bearing_deg, 0)}°`],
                ]} />
              </Card>
              <Card title="Evidence chain">
                <ol className="small" style={{ margin: 0, paddingLeft: 18 }}>{sel.evidence.map((e, i) => <li key={i}>{e}</li>)}</ol>
              </Card>
              {sel.raw?.gaps?.length > 0 && (
                <Card title="AIS gaps" amber>
                  <table className="tbl small" style={{ width: "100%" }}>
                    <thead><tr><th>start</th><th>end</th><th>min</th><th>overlap</th><th>near origin</th></tr></thead>
                    <tbody>{sel.raw.gaps.map((g: any, i: number) => <tr key={i}><td className="mono">{fmtUtc(g.start_utc)}</td><td className="mono">{fmtUtc(g.end_utc)}</td><td>{g.minutes}</td><td>{g.overlaps_window_minutes}</td><td>{g.near_origin ? "yes" : "no"}</td></tr>)}</tbody>
                  </table>
                </Card>
              )}
              <Card title="Limitations" amber>
                <ul className="small" style={{ margin: 0, paddingLeft: 16 }}>{sel.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul>
              </Card>
              <Card title="Analyst review">
                <input className="input" placeholder="note for the dossier (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
                <div className="row" style={{ marginTop: 8 }}>
                  <button className="btn sm grow" onClick={() => review("investigate")}>Flag for investigation</button>
                  <button className="btn sm grow ghost" onClick={() => review("dismiss")}>Dismiss</button>
                  <button className="btn sm grow ghost" onClick={() => review("unknown")}>Reset</button>
                </div>
                <button className="btn sm block" style={{ marginTop: 8 }} onClick={() => { selectMmsi(sel.mmsi); setPanel("map"); }}>Show track on map</button>
              </Card>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
