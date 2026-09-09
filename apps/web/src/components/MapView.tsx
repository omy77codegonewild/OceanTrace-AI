import maplibregl, { LngLatBoundsLike, Map as MLMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useMemo, useRef } from "react";
import { useStore } from "../lib/store";

/** Basemaps (no API key, no watermark):
 *  - dark  : OpenFreeMap vector tiles (OpenMapTiles schema) recoloured to a navy ocean palette
 *  - ocean : Esri World Ocean Base raster
 *  - osm   : OpenStreetMap raster
 * Swap/add providers here; nothing else in the app depends on the basemap. */
const RASTER: Record<string, { tiles: string[]; attribution: string; paint?: any }> = {
  ocean: { tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}"], attribution: "Esri, GEBCO, NOAA, Garmin", paint: { "raster-brightness-max": 0.7, "raster-saturation": -0.3 } },
  osm: { tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], attribution: "© OpenStreetMap contributors", paint: { "raster-opacity": 0.85, "raster-saturation": -0.55, "raster-brightness-max": 0.75 } },
};
const OFM_STYLE = "https://tiles.openfreemap.org/styles/dark";
const PALETTE: Record<string, string> = { background: "#141d2b", water: "#0a1526", waterway: "#0a1526", landcover_wood: "#152233", landuse_park: "#152233", landuse_residential: "#18212e", building: "#1c2634" };

function rasterLayers(visible: string): any[] {
  return Object.entries(RASTER).map(([k, v]) => ({ id: `base-${k}`, type: "raster", source: k, layout: { visibility: k === visible ? "visible" : "none" }, paint: v.paint || {} }));
}
function rasterSources(): any {
  return Object.fromEntries(Object.entries(RASTER).map(([k, v]) => [k, { type: "raster", tiles: v.tiles, tileSize: 256, attribution: v.attribution, maxzoom: 18 }]));
}
/** Fallback style if the vector style cannot be fetched (offline / blocked): Esri ocean raster. */
const FALLBACK_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
  sources: rasterSources(),
  layers: [{ id: "background", type: "background", paint: { "background-color": "#0a1a2b" } }, ...rasterLayers("ocean")],
};
async function buildStyle(): Promise<{ style: maplibregl.StyleSpecification; vector: boolean }> {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 6000);
    const r = await fetch(OFM_STYLE, { signal: ctrl.signal });
    clearTimeout(t);
    if (!r.ok) throw new Error(String(r.status));
    const st = (await r.json()) as maplibregl.StyleSpecification;
    st.layers = st.layers.map((l: any) => {
      if (PALETTE[l.id] && l.paint) {
        const key = l.type === "background" ? "background-color" : l.type === "fill" ? "fill-color" : l.type === "line" ? "line-color" : null;
        if (key) l.paint = { ...l.paint, [key]: PALETTE[l.id] };
      }
      if (l.type === "symbol" && l.paint) l.paint = { ...l.paint, "text-color": "#8fa3bd", "text-halo-color": "#0a1526" };
      if (l.id.startsWith("highway") || l.id.startsWith("railway") || l.id.startsWith("road")) l.paint = { ...(l.paint || {}), ...(l.type === "line" ? { "line-opacity": 0.35 } : {}) };
      return l;
    });
    st.sources = { ...st.sources, ...rasterSources() };
    // raster basemaps sit right above the background, hidden until selected
    st.layers.splice(1, 0, ...rasterLayers("none"));
    return { style: st, vector: true };
  } catch {
    return { style: FALLBACK_STYLE, vector: false };
  }
}
/** Arrow glyph used for met-ocean vectors (data URI, no network). */
function arrowImage(color: string): { width: number; height: number; data: Uint8ClampedArray } {
  const w = 24, h = 24, cv = document.createElement("canvas"); cv.width = w; cv.height = h;
  const c = cv.getContext("2d")!;
  c.strokeStyle = color; c.fillStyle = color; c.lineWidth = 2; c.lineCap = "round";
  c.beginPath(); c.moveTo(12, 21); c.lineTo(12, 5); c.stroke();
  c.beginPath(); c.moveTo(12, 2); c.lineTo(7, 9); c.lineTo(17, 9); c.closePath(); c.fill();
  return { width: w, height: h, data: c.getImageData(0, 0, w, h).data };
}

const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

function boundsOf(fc: GeoJSON.FeatureCollection | GeoJSON.Geometry | null | undefined): LngLatBoundsLike | null {
  if (!fc) return null;
  let minx = 180, miny = 90, maxx = -180, maxy = -90;
  const eat = (c: any) => {
    if (typeof c[0] === "number") {
      minx = Math.min(minx, c[0]); maxx = Math.max(maxx, c[0]); miny = Math.min(miny, c[1]); maxy = Math.max(maxy, c[1]);
    } else c.forEach(eat);
  };
  const geoms: any[] = (fc as any).type === "FeatureCollection" ? (fc as any).features.map((f: any) => f.geometry) : [fc];
  geoms.forEach((g) => g && g.coordinates && eat(g.coordinates));
  if (minx > maxx) return null;
  return [[minx, miny], [maxx, maxy]];
}

export default function MapView() {
  const ref = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const readyRef = useRef(false);
  const vectorRef = useRef(false);
  const st = useStore();
  const { caseData, hindcast, attribution, tracks, forecast, selectedSlick, selectedMmsi, layers, replayStep, focus, focusNonce, basemap } = st;

  // ---- derived GeoJSON --------------------------------------------------
  const slicksFC = useMemo<GeoJSON.FeatureCollection>(() => {
    if (!caseData) return EMPTY;
    return { type: "FeatureCollection", features: caseData.slicks.features.map((f) => ({ ...f, properties: { ...f.properties, id: f.id, selected: f.id === selectedSlick } })) as any };
  }, [caseData, selectedSlick]);

  const originFC = useMemo<GeoJSON.FeatureCollection>(() => {
    if (!hindcast) return EMPTY;
    const regs = hindcast.detail?.origin_regions || {};
    const feats: any[] = [];
    for (const k of ["90", "70", "50"]) if (regs[k]) feats.push({ type: "Feature", properties: { level: Number(k) }, geometry: regs[k].geometry });
    if (!feats.length && hindcast.origin_geometry) feats.push({ type: "Feature", properties: { level: 70 }, geometry: hindcast.origin_geometry });
    return { type: "FeatureCollection", features: feats };
  }, [hindcast]);

  const cloudFC = useMemo<GeoJSON.FeatureCollection>(() => {
    const cloud = hindcast?.detail?.cloud;
    if (!cloud?.length) return EMPTY;
    const idx = Math.min(replayStep, cloud.length - 1);
    return { type: "FeatureCollection", features: cloud[idx].map((c: number[]) => ({ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: c } })) };
  }, [hindcast, replayStep]);

  const pathsFC = useMemo<GeoJSON.FeatureCollection>(() => {
    const paths = hindcast?.detail?.paths;
    if (!paths?.length) return EMPTY;
    const idx = Math.min(replayStep + 1, paths[0].length);
    return { type: "FeatureCollection", features: paths.map((p: number[][]) => ({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: p.slice(0, Math.max(2, idx)) } })) };
  }, [hindcast, replayStep]);

  const centroidLineFC = useMemo<GeoJSON.FeatureCollection>(() => {
    const steps = hindcast?.detail?.steps;
    if (!steps?.length) return EMPTY;
    return { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: steps.map((s: any) => s.centroid) } }] };
  }, [hindcast]);

  const stepRegionFC = useMemo<GeoJSON.FeatureCollection>(() => {
    const steps = hindcast?.detail?.steps;
    const hours = hindcast?.detail?.cloud_hours;
    if (!steps?.length || !hours?.length) return EMPTY;
    const h = hours[Math.min(replayStep, hours.length - 1)];
    // nearest step that carries a region polygon
    let best: any = null, bd = 1e9;
    for (const s of steps) if (s.region && Math.abs(s.hours - h) < bd) { bd = Math.abs(s.hours - h); best = s; }
    return best ? { type: "FeatureCollection", features: [{ type: "Feature", properties: { hours: best.hours }, geometry: best.region }] } : EMPTY;
  }, [hindcast, replayStep]);

  const tracksFC = useMemo<GeoJSON.FeatureCollection>(() => {
    if (!tracks) return EMPTY;
    const scored = new Set((attribution?.candidates || []).map((c) => c.mmsi));
    return { type: "FeatureCollection", features: tracks.features.filter((f: any) => !scored.has(f.properties.mmsi)) };
  }, [tracks, attribution]);

  const candFC = useMemo<GeoJSON.FeatureCollection>(() => {
    if (!attribution) return EMPTY;
    return { type: "FeatureCollection", features: attribution.candidates.map((c) => ({ ...c.track, properties: { ...c.track.properties, mmsi: c.mmsi, rank: c.rank, score: c.score, priority: c.priority, name: c.vessel.name || `MMSI ${c.mmsi}`, selected: c.mmsi === selectedMmsi } })) as any };
  }, [attribution, selectedMmsi]);

  const gapsFC = useMemo<GeoJSON.FeatureCollection>(() => {
    if (!attribution) return EMPTY;
    const feats: any[] = [];
    for (const c of attribution.candidates) for (const g of c.raw?.gaps || []) feats.push({ type: "Feature", properties: { mmsi: c.mmsi, minutes: g.minutes, overlap: g.overlaps_window_minutes }, geometry: { type: "LineString", coordinates: [g.from, g.to] } });
    return { type: "FeatureCollection", features: feats };
  }, [attribution]);

  const cpaFC = useMemo<GeoJSON.FeatureCollection>(() => {
    if (!attribution) return EMPTY;
    return { type: "FeatureCollection", features: attribution.candidates.filter((c) => c.raw?.closest_point).map((c) => ({ type: "Feature", properties: { mmsi: c.mmsi, rank: c.rank, label: `#${c.rank} ${c.vessel.name || c.mmsi}`, selected: c.mmsi === selectedMmsi }, geometry: { type: "Point", coordinates: c.raw.closest_point } })) };
  }, [attribution, selectedMmsi]);

  const searchFC = useMemo<GeoJSON.FeatureCollection>(() => {
    const g = attribution?.summary?.search_geometry;
    return g ? { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: g }] } : EMPTY;
  }, [attribution]);

  const envFC = useMemo<GeoJSON.FeatureCollection>(() => {
    const ev = hindcast?.detail?.env_vectors;
    if (!ev?.grid?.length || !ev.steps?.length) return EMPTY;
    const idx = Math.min(replayStep, ev.steps.length - 1);
    const rows: number[][] = ev.steps[idx];
    const toDeg = (u: number, v: number) => (Math.atan2(u, v) * 180) / Math.PI;
    return {
      type: "FeatureCollection",
      features: ev.grid.map((xy: number[], i: number) => {
        const [uc, vc, uw, vw] = rows[i];
        return { type: "Feature", properties: { cur_speed_ms: +Math.hypot(uc, vc).toFixed(3), cur_dir_to_deg: +((toDeg(uc, vc) + 360) % 360).toFixed(1), wind_speed_ms: +Math.hypot(uw, vw).toFixed(2), wind_dir_to_deg: +((toDeg(uw, vw) + 360) % 360).toFixed(1) }, geometry: { type: "Point", coordinates: xy } };
      }),
    };
  }, [hindcast, replayStep]);

  const forecastFC = useMemo<GeoJSON.FeatureCollection>(() => {
    if (!forecast?.steps?.length) return EMPTY;
    const feats: any[] = forecast.steps.filter((_: any, i: number) => i % 3 === 0 || i === forecast.steps.length - 1).map((s: any) => ({ type: "Feature", properties: { hours: s.hours }, geometry: s.region }));
    if (forecast.envelope) feats.unshift({ type: "Feature", properties: { envelope: true }, geometry: forecast.envelope });
    return { type: "FeatureCollection", features: feats };
  }, [forecast]);

  // ---- map init ---------------------------------------------------------
  useEffect(() => {
    if (!ref.current || mapRef.current) return;
    let cancelled = false;
    let created: MLMap | null = null;
    buildStyle().then(({ style, vector }) => {
    if (cancelled || !ref.current) return;
    vectorRef.current = vector;
    const map = new maplibregl.Map({ container: ref.current, style, center: [72.6, 18.9], zoom: 6.5, attributionControl: false, maxZoom: 16 });
    created = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
    map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: "Spill Forensics · Open-Meteo · Copernicus" }), "bottom-right");
    map.on("styleimagemissing", (e: any) => { if (!map.hasImage(e.id)) map.addImage(e.id, { width: 1, height: 1, data: new Uint8ClampedArray(4) }); });
    map.on("load", () => {
      const add = (id: string, data: GeoJSON.FeatureCollection = EMPTY) => map.addSource(id, { type: "geojson", data });
      ["footprint", "slicks", "origin", "steporigin", "cloud", "paths", "centroid", "tracks", "cands", "gaps", "cpa", "search", "forecast", "env"].forEach((s) => add(s));
      map.addImage("arrow-cur", arrowImage("#22d3ee"), { sdf: false });
      map.addImage("arrow-wind", arrowImage("#f8fafc"), { sdf: false });

      map.addLayer({ id: "search-line", type: "line", source: "search", paint: { "line-color": "#38bdf8", "line-width": 1.2, "line-dasharray": [3, 3], "line-opacity": 0.8 } });
      map.addLayer({ id: "footprint-line", type: "line", source: "footprint", paint: { "line-color": "#67e8f9", "line-width": 1, "line-dasharray": [2, 2], "line-opacity": 0.7 } });
      map.addLayer({ id: "forecast-fill", type: "fill", source: "forecast", filter: ["!", ["has", "envelope"]], paint: { "fill-color": "#a78bfa", "fill-opacity": 0.12 } });
      map.addLayer({ id: "forecast-line", type: "line", source: "forecast", paint: { "line-color": "#a78bfa", "line-width": ["case", ["has", "envelope"], 1.6, 0.8], "line-dasharray": [2, 2], "line-opacity": 0.9 } });
      map.addLayer({ id: "origin-fill", type: "fill", source: "origin", paint: { "fill-color": ["match", ["get", "level"], 50, "#fbbf24", 70, "#f59e0b", "#d97706"], "fill-opacity": ["match", ["get", "level"], 50, 0.35, 70, 0.22, 0.12] } });
      // MapLibre does not allow data-driven line-dasharray -> two layers (solid 50/70, dashed 90)
      map.addLayer({ id: "origin-line", type: "line", source: "origin", filter: ["!=", ["get", "level"], 90], paint: { "line-color": "#fbbf24", "line-width": ["match", ["get", "level"], 70, 2, 1] } });
      map.addLayer({ id: "origin-line90", type: "line", source: "origin", filter: ["==", ["get", "level"], 90], paint: { "line-color": "#fbbf24", "line-width": 1, "line-dasharray": [3, 2] } });
      // met-ocean arrows sit above the scene raster + probability fills, below particles/tracks
      map.addLayer({ id: "env-cur", type: "symbol", source: "env", layout: { "icon-image": "arrow-cur", "icon-size": ["interpolate", ["linear"], ["get", "cur_speed_ms"], 0, 0.35, 0.5, 0.8, 1.5, 1.2], "icon-rotate": ["get", "cur_dir_to_deg"], "icon-rotation-alignment": "map", "icon-allow-overlap": true, "icon-ignore-placement": true }, paint: { "icon-opacity": 0.8 } });
      map.addLayer({ id: "env-wind", type: "symbol", source: "env", layout: { "icon-image": "arrow-wind", "icon-size": ["interpolate", ["linear"], ["get", "wind_speed_ms"], 0, 0.3, 5, 0.6, 15, 1.0], "icon-rotate": ["get", "wind_dir_to_deg"], "icon-rotation-alignment": "map", "icon-allow-overlap": true, "icon-ignore-placement": true, "icon-offset": [14, 0] }, paint: { "icon-opacity": 0.5 } });
      map.addLayer({ id: "steporigin-line", type: "line", source: "steporigin", paint: { "line-color": "#22d3ee", "line-width": 1.5, "line-dasharray": [4, 2], "line-opacity": 0.9 } });
      map.addLayer({ id: "paths-line", type: "line", source: "paths", paint: { "line-color": "#22d3ee", "line-width": 0.8, "line-opacity": 0.35 } });
      map.addLayer({ id: "centroid-line", type: "line", source: "centroid", paint: { "line-color": "#67e8f9", "line-width": 2.2, "line-opacity": 0.95 } });
      map.addLayer({ id: "cloud-pts", type: "circle", source: "cloud", paint: { "circle-radius": 2.2, "circle-color": "#67e8f9", "circle-opacity": 0.85 } });
      map.addLayer({ id: "tracks-line", type: "line", source: "tracks", paint: { "line-color": "#94a3b8", "line-width": 1, "line-opacity": 0.55 } });
      map.addLayer({ id: "cands-line", type: "line", source: "cands", paint: { "line-color": ["match", ["get", "priority"], "high", "#22c55e", "medium", "#f59e0b", "low", "#fb923c", "#94a3b8"], "line-width": ["case", ["get", "selected"], 4, 2.2], "line-opacity": 0.95 } });
      map.addLayer({ id: "gaps-line", type: "line", source: "gaps", paint: { "line-color": "#ef4444", "line-width": 3, "line-dasharray": [1.5, 1.5], "line-opacity": 0.95 } });
      map.addLayer({ id: "slicks-fill", type: "fill", source: "slicks", paint: { "fill-color": ["match", ["get", "class"], "oil", "#ef4444", "look_alike", "#9ca3af", "#f97316"], "fill-opacity": ["case", ["get", "selected"], 0.55, 0.3] } });
      map.addLayer({ id: "slicks-line", type: "line", source: "slicks", paint: { "line-color": ["match", ["get", "class"], "oil", "#fca5a5", "look_alike", "#d1d5db", "#fdba74"], "line-width": ["case", ["get", "selected"], 2.5, 1.2] } });
      map.addLayer({ id: "cpa-pts", type: "circle", source: "cpa", paint: { "circle-radius": ["case", ["get", "selected"], 8, 6], "circle-color": "#f59e0b", "circle-stroke-color": "#0b1830", "circle-stroke-width": 2 } });
      map.addLayer({ id: "cpa-label", type: "symbol", source: "cpa", layout: { "text-field": ["get", "label"], "text-size": 11, "text-offset": [0, 1.3], "text-anchor": "top", "text-font": ["Noto Sans Regular"] }, paint: { "text-color": "#fde68a", "text-halo-color": "#0b1830", "text-halo-width": 1.5 } });

      const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
      map.on("click", "slicks-fill", (e) => { const f = e.features?.[0]; if (f) useStore.getState().selectSlick(String(f.properties.id)); });
      map.on("click", "cands-line", (e) => { const f = e.features?.[0]; if (f) useStore.getState().selectMmsi(Number(f.properties.mmsi)); });
      map.on("click", "cpa-pts", (e) => { const f = e.features?.[0]; if (f) useStore.getState().selectMmsi(Number(f.properties.mmsi)); });
      const hover = (layer: string, html: (p: any) => string) => {
        map.on("mousemove", layer, (e) => { map.getCanvas().style.cursor = "pointer"; const f = e.features?.[0]; if (f) popup.setLngLat(e.lngLat).setHTML(html(f.properties)).addTo(map); });
        map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; popup.remove(); });
      };
      hover("slicks-fill", (p) => `<b>${p.class}</b> · conf ${Number(p.confidence).toFixed(2)}<br/>${Number(p.area_km2).toFixed(2)} km² · ${p.model_version}`);
      hover("cands-line", (p) => `<b>#${p.rank} ${p.name}</b><br/>MMSI ${p.mmsi} · score ${p.score}`);
      hover("tracks-line", (p) => `MMSI ${p.mmsi}${p.vessel_name ? " · " + p.vessel_name : ""}<br/>${p.positions} fixes · ${p.vessel_type || "type unknown"}`);
      hover("gaps-line", (p) => `<b>AIS gap</b> ${p.minutes} min<br/>overlap with window ${p.overlap} min`);
      hover("origin-fill", (p) => `${p.level}% origin probability region`);
      hover("forecast-fill", (p) => `forecast T+${p.hours} h (70% region)`);
      hover("env-cur", (p) => `<b>current</b> ${p.cur_speed_ms} m/s → ${p.cur_dir_to_deg}°<br/><b>wind</b> ${p.wind_speed_ms} m/s → ${p.wind_dir_to_deg}°`);
      readyRef.current = true;
      mapRef.current = map;
      (window as any).__otMap = map; // debugging / e2e hook
      // force a data push once ready
      useStore.setState({});
    });
    });
    return () => { cancelled = true; created?.remove(); mapRef.current = null; readyRef.current = false; };
  }, []);

  // ---- scene raster overlay ---------------------------------------------
  const sceneKey = caseData?.scene?.id;
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    if (map.getLayer("scene-img")) map.removeLayer("scene-img");
    if (map.getSource("scene-img")) map.removeSource("scene-img");
    const sc = caseData?.scene;
    if (!sc) { (map.getSource("footprint") as any)?.setData(EMPTY); return; }
    const [w, s, e, n] = sc.bounds;
    map.addSource("scene-img", { type: "image", url: sc.preview_url, coordinates: [[w, n], [e, n], [e, s], [w, s]] });
    map.addLayer({ id: "scene-img", type: "raster", source: "scene-img", paint: { "raster-opacity": 0.9, "raster-fade-duration": 0 } }, "search-line");
    (map.getSource("footprint") as any)?.setData({ type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] } }] });
    map.fitBounds([[w, s], [e, n]], { padding: 40, duration: 800 });
  }, [sceneKey, readyRef.current]);

  // ---- data pushes ------------------------------------------------------
  const push = (id: string, data: GeoJSON.FeatureCollection) => { const m = mapRef.current; if (m && readyRef.current) (m.getSource(id) as any)?.setData(data); };
  useEffect(() => push("slicks", slicksFC), [slicksFC, readyRef.current]);
  useEffect(() => push("origin", originFC), [originFC, readyRef.current]);
  useEffect(() => push("steporigin", stepRegionFC), [stepRegionFC, readyRef.current]);
  useEffect(() => push("cloud", cloudFC), [cloudFC, readyRef.current]);
  useEffect(() => push("paths", pathsFC), [pathsFC, readyRef.current]);
  useEffect(() => push("centroid", centroidLineFC), [centroidLineFC, readyRef.current]);
  useEffect(() => push("tracks", tracksFC), [tracksFC, readyRef.current]);
  useEffect(() => push("cands", candFC), [candFC, readyRef.current]);
  useEffect(() => push("gaps", gapsFC), [gapsFC, readyRef.current]);
  useEffect(() => push("cpa", cpaFC), [cpaFC, readyRef.current]);
  useEffect(() => push("search", searchFC), [searchFC, readyRef.current]);
  useEffect(() => push("forecast", forecastFC), [forecastFC, readyRef.current]);
  useEffect(() => push("env", envFC), [envFC, readyRef.current]);

  // basemap switch: "dark" = vector layers visible, rasters hidden; otherwise the selected raster only
  useEffect(() => {
    const m = mapRef.current;
    if (!m || !readyRef.current) return;
    const eff = basemap === "dark" && !vectorRef.current ? "ocean" : basemap;
    Object.keys(RASTER).forEach((k) => m.getLayer(`base-${k}`) && m.setLayoutProperty(`base-${k}`, "visibility", k === eff ? "visible" : "none"));
    if (vectorRef.current) {
      const ours = new Set(["background", ...Object.keys(RASTER).map((k) => `base-${k}`)]);
      m.getStyle().layers.forEach((l: any) => {
        if (ours.has(l.id) || l.source === undefined || !(l.source === "openmaptiles" || l.source === "ne2_shaded")) return;
        m.setLayoutProperty(l.id, "visibility", eff === "dark" ? "visible" : "none");
      });
    }
  }, [basemap, readyRef.current]);

  // ---- visibility -------------------------------------------------------
  useEffect(() => {
    const m = mapRef.current;
    if (!m || !readyRef.current) return;
    const vis = (ids: string[], on: boolean) => ids.forEach((id) => m.getLayer(id) && m.setLayoutProperty(id, "visibility", on ? "visible" : "none"));
    vis(["scene-img"], layers.scene);
    vis(["slicks-fill", "slicks-line"], layers.slicks);
    vis(["origin-fill", "origin-line", "origin-line90"], layers.origin);
    vis(["cloud-pts", "paths-line", "centroid-line", "steporigin-line"], layers.particles);
    vis(["tracks-line"], layers.tracks);
    vis(["cands-line", "cpa-pts", "cpa-label"], layers.candidates);
    vis(["gaps-line"], layers.gaps);
    vis(["forecast-fill", "forecast-line"], layers.forecast);
    vis(["search-line"], layers.search);
    vis(["env-cur", "env-wind"], layers.vectors);
  }, [layers, readyRef.current, sceneKey]);

  // ---- focus / camera ---------------------------------------------------
  useEffect(() => {
    const m = mapRef.current;
    if (!m || !readyRef.current) return;
    let b: LngLatBoundsLike | null = null;
    if (focus === "slick") { const f = slicksFC.features.find((x: any) => x.properties.selected) || slicksFC.features[0]; b = f ? boundsOf(f.geometry as any) : null; }
    else if (focus === "origin") b = boundsOf(originFC);
    else if (focus === "suspect") { const f = candFC.features.find((x: any) => x.properties.selected); b = f ? boundsOf(f.geometry as any) : boundsOf(candFC); }
    else { const all: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [...slicksFC.features, ...originFC.features, ...candFC.features] }; b = boundsOf(all) || (caseData?.scene ? [[caseData.scene.bounds[0], caseData.scene.bounds[1]], [caseData.scene.bounds[2], caseData.scene.bounds[3]]] : null); }
    if (b) m.fitBounds(b, { padding: 70, duration: 700, maxZoom: 13 });
  }, [focus, focusNonce, selectedMmsi, hindcast?.id, attribution?.id]);

  return <div ref={ref} style={{ position: "absolute", inset: 0 }} />;
}
