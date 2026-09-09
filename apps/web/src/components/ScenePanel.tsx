import { useRef, useState } from "react";
import { api, fmtUtc } from "../lib/api";
import { useStore } from "../lib/store";
import { Badge, Card, Icon, KV } from "./Common";

/** Step 1 — scene intake: upload a GeoTIFF/PNG or pull a real Sentinel-1 subset from the catalog; run detection. */
export default function ScenePanel({ onClose }: { onClose: () => void }) {
  const { caseId, caseData, runJob, notify, config } = useStore();
  const [tab, setTab] = useState<"upload" | "catalog">("upload");
  const [file, setFile] = useState<File | null>(null);
  const [bounds, setBounds] = useState("");
  const [acq, setAcq] = useState("");
  const [thr, setThr] = useState<number>(config?.detection_postprocess?.prob_threshold ?? 0.5);
  const [minArea, setMinArea] = useState<number>(config?.detection_postprocess?.min_area_km2 ?? 0.15);
  const [over, setOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  // catalog
  const [bbox, setBbox] = useState("");
  const [start, setStart] = useState(() => new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10));
  const [end, setEnd] = useState(() => new Date().toISOString().slice(0, 10));
  const [items, setItems] = useState<any[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [pol, setPol] = useState("vv");

  const isGeo = file && /\.tiff?$/i.test(file.name);
  const detectOpts = JSON.stringify({ prob_threshold: thr, min_area_km2: minArea });

  const upload = async () => {
    if (!caseId || !file) return notify("error", "Choose a file first");
    const fd = new FormData();
    fd.append("file", file);
    fd.append("run_detection", "true");
    fd.append("detect_options", detectOpts);
    if (bounds.trim()) fd.append("bounds", bounds.trim());
    if (acq.trim()) fd.append("acquisition_time_utc", acq.trim());
    const j = await runJob(`Ingest + detect ${file.name}`, () => api.uploadScene(caseId, fd));
    if (j?.state === "completed") { onClose(); useStore.getState().setFocus("slick"); }
  };

  const search = async () => {
    const bb = bbox.split(",").map(Number);
    if (bb.length !== 4 || bb.some(isNaN)) return notify("error", "bbox must be min_lon,min_lat,max_lon,max_lat");
    setSearching(true);
    try {
      const r = await api.catalogSearch(bb, `${start}T00:00:00Z`, `${end}T23:59:59Z`, 25);
      setItems(r.items);
      if (!r.items.length) notify("info", "No Sentinel-1 GRD scenes for that area/date range");
    } catch (e: any) {
      notify("error", e.message);
    } finally {
      setSearching(false);
    }
  };

  const fetchItem = async (id: string) => {
    if (!caseId) return;
    const bb = bbox.split(",").map(Number);
    const j = await runJob(`Fetch Sentinel-1 ${id.slice(0, 32)}… + detect`, () => api.catalogFetch(caseId, { item_id: id, bbox: bb, polarization: pol, run_detection: true, detect_options: { prob_threshold: thr, min_area_km2: minArea } }));
    if (j?.state === "completed") { onClose(); useStore.getState().setFocus("slick"); }
  };

  const rerun = async () => {
    if (!caseId) return;
    await runJob("Re-run detection", () => api.analyze(caseId, { prob_threshold: thr, min_area_km2: minArea }));
  };

  return (
    <div className="overlay map-panel">
      <div className="row" style={{ marginBottom: 10 }}>
        <b>SCENE INTAKE</b>
        <button className="btn sm ghost right" onClick={onClose}>✕</button>
      </div>
      <div className="row" style={{ marginBottom: 10 }}>
        <button className={`chip ${tab === "upload" ? "active" : ""}`} onClick={() => setTab("upload")}>Upload image</button>
        <button className={`chip ${tab === "catalog" ? "active" : ""}`} onClick={() => setTab("catalog")}>Sentinel-1 catalog (live)</button>
      </div>

      {tab === "upload" && (
        <div className="col">
          <div className={`dropzone ${over ? "over" : ""}`} onClick={() => inputRef.current?.click()} onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
            onDrop={(e) => { e.preventDefault(); setOver(false); const f = e.dataTransfer.files?.[0]; if (f) setFile(f); }}>
            <div style={{ width: 22, margin: "0 auto 4px", color: "var(--cyan)" }}><Icon name="upload" /></div>
            {file ? <b>{file.name} <span className="muted">({(file.size / 1e6).toFixed(1)} MB)</span></b> : <span>Drop a Sentinel-1 GeoTIFF/COG here, or a PNG/JPG chip with manual bounds</span>}
            <input ref={inputRef} type="file" accept=".tif,.tiff,.png,.jpg,.jpeg" hidden onChange={(e) => setFile(e.target.files?.[0] || null)} />
          </div>
          {file && !isGeo && <div className="small amber">PNG/JPG carries no georeference — bounds and acquisition time are required and will be labelled <b>manual</b>.</div>}
          <div>
            <label className="lbl">Bounds (WGS84) — required for PNG/JPG, ignored for georeferenced TIFF</label>
            <input className="input mono" placeholder="min_lon,min_lat,max_lon,max_lat" value={bounds} onChange={(e) => setBounds(e.target.value)} />
          </div>
          <div>
            <label className="lbl">Acquisition time UTC — optional if in TIFF tags / Sentinel-1 filename</label>
            <input className="input mono" placeholder="2026-09-06T01:02:53Z" value={acq} onChange={(e) => setAcq(e.target.value)} />
          </div>
          <DetectOpts thr={thr} setThr={setThr} minArea={minArea} setMinArea={setMinArea} />
          <button className="btn primary block" onClick={upload} disabled={!file}>Ingest scene &amp; run detection →</button>
        </div>
      )}

      {tab === "catalog" && (
        <div className="col">
          <div className="small muted">Searches ESA Copernicus Sentinel-1 GRD via Microsoft Planetary Computer (free, no key). Only the requested bbox is pulled through HTTP range reads.</div>
          <div><label className="lbl">Area bbox (WGS84)</label><input className="input mono" placeholder="72.3,18.5,72.9,19.1" value={bbox} onChange={(e) => setBbox(e.target.value)} /></div>
          <div className="row">
            <div className="grow"><label className="lbl">From</label><input className="input mono" type="date" value={start} onChange={(e) => setStart(e.target.value)} /></div>
            <div className="grow"><label className="lbl">To</label><input className="input mono" type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></div>
            <div style={{ width: 80 }}><label className="lbl">Pol.</label><select className="input" value={pol} onChange={(e) => setPol(e.target.value)}><option value="vv">VV</option><option value="vh">VH</option></select></div>
          </div>
          <button className="btn block" onClick={search} disabled={searching}>{searching ? "Searching…" : "Search catalog"}</button>
          <DetectOpts thr={thr} setThr={setThr} minArea={minArea} setMinArea={setMinArea} />
          {items && (
            <div className="col" style={{ maxHeight: 260, overflowY: "auto" }}>
              {items.map((it) => (
                <div className="tile row" key={it.id}>
                  <div className="grow">
                    <div className="mono small">{it.id}</div>
                    <div className="small muted">{fmtUtc(it.datetime)} · {it.platform} · {it.orbit_state} · {(it.polarizations || []).join("+")}</div>
                  </div>
                  <button className="btn sm primary" onClick={() => fetchItem(it.id)}>Fetch</button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {caseData?.scene && (
        <div style={{ marginTop: 12 }}>
          <Card title="Current scene" right={<Badge kind={caseData.scene.metadata?.source_type === "stac" ? "real" : "imported"}>{caseData.scene.metadata?.source_type === "stac" ? "CATALOG" : "UPLOAD"}</Badge>}>
            <KV items={[
              ["Acquired (UTC)", fmtUtc(caseData.scene.acquisition_time)],
              ["Time source", caseData.scene.metadata?.acquisition_time_source],
              ["Sensor / pol.", `${caseData.scene.metadata?.sensor || "?"} / ${caseData.scene.metadata?.polarization || "?"}`],
              ["Georeference", caseData.scene.metadata?.georef_source],
              ["Pixel (m)", (caseData.scene.metadata?.pixel_size_m || []).map((v: number) => Math.round(v)).join(" × ")],
              ["Detector", `${caseData.scene.metadata?.detection_provenance?.adapter || "—"}`],
            ]} />
            {(caseData.scene.metadata?.warnings || []).map((w: string) => <div className="small amber" key={w} style={{ marginTop: 6 }}>⚠ {w}</div>)}
            <button className="btn sm block" style={{ marginTop: 8 }} onClick={rerun}>Re-run detection with current thresholds</button>
          </Card>
        </div>
      )}
    </div>
  );
}

function DetectOpts({ thr, setThr, minArea, setMinArea }: any) {
  return (
    <div className="row">
      <div className="grow"><label className="lbl">Oil prob. threshold</label><input className="input mono" type="number" step={0.05} min={0.05} max={0.95} value={thr} onChange={(e) => setThr(Number(e.target.value))} /></div>
      <div className="grow"><label className="lbl">Min area km²</label><input className="input mono" type="number" step={0.05} min={0.001} value={minArea} onChange={(e) => setMinArea(Number(e.target.value))} /></div>
    </div>
  );
}
