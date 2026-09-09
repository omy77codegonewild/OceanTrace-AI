import { useEffect, useState } from "react";
import { api, fmtUtc } from "../lib/api";
import { useStore } from "../lib/store";
import { Badge, Card } from "./Common";

/** Step 4 — Legal-style dossier: report preview (server-rendered HTML), exports and the audit trail. */
export default function DossierPanel() {
  const { caseId, caseData, hindcast, attribution, setPanel, notify, refresh } = useStore();
  const [audit, setAudit] = useState<any[]>([]);
  const [notes, setNotes] = useState(caseData?.notes || "");
  const [reload, setReload] = useState(0);
  useEffect(() => { if (caseId) api.audit(caseId).then(setAudit).catch(() => setAudit([])); }, [caseId, reload, caseData?.updated_at]);
  if (!caseId || !caseData) return null;
  const ready = !!caseData.scene && caseData.slicks.features.length > 0;
  const saveNotes = async () => {
    try { await api.review(caseId, { notes }); notify("ok", "Case notes saved"); await refresh(); } catch (e: any) { notify("error", e.message); }
  };
  return (
    <div className="fullpanel">
      <div className="row" style={{ marginBottom: 10 }}>
        <div><h2>Incident Dossier</h2><div className="small muted">Report is rendered server-side from the persisted case state — exports are the same JSON/GeoJSON contracts the API serves.</div></div>
        <div className="right row">
          <Badge kind={caseData.data_mode}>{caseData.data_mode}</Badge>
          <button className="btn sm" onClick={() => setPanel("map")}>Map</button>
        </div>
      </div>
      <div className="grid2" style={{ gridTemplateColumns: "1fr 380px", alignItems: "start" }}>
        <Card title="Report preview" right={<div className="row"><button className="btn sm ghost" onClick={() => setReload((x) => x + 1)}>refresh</button><a className="btn sm primary" href={api.reportUrl(caseId)} target="_blank" rel="noreferrer">Open / print ↗</a></div>}>
          {ready ? (
            <iframe key={reload + (caseData.updated_at || "")} title="report" src={api.reportUrl(caseId)} style={{ width: "100%", height: "70vh", border: "1px solid var(--line)", borderRadius: 8, background: "#fff" }} />
          ) : <div className="small muted">The dossier needs at least a scene and one detection. Steps completed: scene {caseData.scene ? "✓" : "✗"} · detections {caseData.slicks.features.length} · hindcast {hindcast ? "✓" : "✗"} · attribution {attribution ? "✓" : "✗"}.</div>}
        </Card>
        <div className="col">
          <Card title="Exports">
            <div className="col">
              <a className="btn block wrap" href={api.exportUrl(caseId, "json")} download>Case bundle (JSON) · slicks, hindcast, candidates, provenance</a>
              <a className="btn block wrap" href={api.exportUrl(caseId, "geojson")} download>All layers (GeoJSON) · slicks, origin regions, candidate tracks</a>
              <a className="btn block wrap" href={api.reportUrl(caseId)} download={`${caseId}_report.html`}>Report (HTML, self-contained, printable)</a>
            </div>
            <div className="small muted" style={{ marginTop: 8 }}>Every export carries data_mode labels, model versions, config hash and the disclaimer that vessel rankings are investigation priorities, not findings.</div>
          </Card>
          <Card title="Completeness">
            <table className="tbl small" style={{ width: "100%" }}>
              <tbody>
                <tr><td>Scene</td><td>{caseData.scene ? <Badge kind="ok">✓ {fmtUtc(caseData.scene.acquisition_time)}</Badge> : <Badge kind="neutral">missing</Badge>}</td></tr>
                <tr><td>Detections</td><td>{caseData.slicks.features.length ? <Badge kind="ok">{caseData.slicks.features.length} polygons</Badge> : <Badge kind="neutral">none</Badge>}</td></tr>
                <tr><td>Slick review</td><td>{caseData.slicks.features.some((f) => f.properties.review_status) ? <Badge kind="ok">reviewed</Badge> : <Badge kind="warn">pending</Badge>}</td></tr>
                <tr><td>Hindcast</td><td>{hindcast ? <Badge kind="ok">{hindcast.id}</Badge> : <Badge kind="neutral">none</Badge>}</td></tr>
                <tr><td>AIS data</td><td>{caseData.ais_summary.positions ? <Badge kind="ok">{caseData.ais_summary.positions} fixes</Badge> : <Badge kind="neutral">none</Badge>}</td></tr>
                <tr><td>Attribution</td><td>{attribution ? <Badge kind="ok">{attribution.candidates.length} candidates</Badge> : <Badge kind="neutral">none</Badge>}</td></tr>
              </tbody>
            </table>
          </Card>
          <Card title="Analyst notes">
            <textarea className="input" rows={4} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Context, cross-checks, external reports…" />
            <button className="btn sm" style={{ marginTop: 6 }} onClick={saveNotes}>Save to case</button>
          </Card>
          <Card title={`Audit trail (${audit.length})`}>
            <div className="col" style={{ gap: 4, maxHeight: 260, overflowY: "auto" }}>
              {audit.slice().reverse().map((a, i) => (
                <div key={i} className="small" style={{ borderBottom: "1px solid var(--line)", paddingBottom: 4 }}>
                  <span className="mono muted">{fmtUtc(a.ts).slice(0, 19)}</span> <b>{a.action}</b> <span className="muted">by {a.actor}</span>
                </div>
              ))}
              {audit.length === 0 && <div className="small muted">No entries yet.</div>}
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
