"""Case report renderer (HTML; print-to-PDF from the browser). Every number in
the report comes from the exported case bundle — nothing is templated in."""
from __future__ import annotations

import html
from typing import Any


def _h(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _fmt(x: Any, nd: int = 2) -> str:
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def render_report(bundle: dict[str, Any]) -> str:
    case = bundle["case"]
    scene = bundle.get("scene") or {}
    meta = scene.get("metadata") or {}
    slicks = (bundle.get("slicks") or {}).get("features", [])
    hc = bundle.get("hindcast")
    at = bundle.get("attribution")
    env = bundle.get("environment") or {}
    det = bundle.get("detector") or {}
    mode = str(case.get("data_mode", "imported")).upper()

    rows = []
    for f in slicks:
        p = f["properties"]
        rows.append(f"<tr><td>{_h(f['id'])}</td><td>{_h(p.get('class'))}</td><td>{_fmt(p.get('confidence'))}</td><td>{_fmt(p.get('area_km2'))}</td><td>{_fmt(p.get('orientation_deg'), 0)}°</td><td>{_h(p.get('centroid'))}</td><td>{_h(p.get('review_status') or 'unreviewed')}</td></tr>")
    slick_table = "".join(rows) or "<tr><td colspan=7>No candidate slicks detected.</td></tr>"

    hc_html = "<p>No hindcast run.</p>"
    if hc:
        m = hc.get("metrics") or {}
        areas = m.get("origin_areas_km2") or {}
        lim = "".join(f"<li>{_h(x)}</li>" for x in m.get("limitations", []))
        hc_html = f"""
        <p><b>Estimated release window (under model assumptions):</b> {_h(hc.get('release_start'))} → {_h(hc.get('release_end'))} UTC</p>
        <p><b>Origin probability regions:</b> 50 %: {_fmt(areas.get('50'))} km² · 70 %: {_fmt(areas.get('70'))} km² · 90 %: {_fmt(areas.get('90'))} km²</p>
        <p><b>Window basis:</b> {_h(m.get('window_basis'))} · peak convergence at T−{_fmt(m.get('peak_hours_back'), 1)} h · ensemble {_h(m.get('ensemble_members'))} × {_h(m.get('particle_count'))} particles</p>
        <p><b>Environmental data:</b> {_h((m.get('environment') or {}).get('source'))} <span class="badge">{_h((m.get('environment') or {}).get('data_mode', '')).upper()}</span></p>
        <p><b>Parameters:</b> <code>{_h({k: v for k, v in (hc.get('config') or {}).items()})}</code></p>
        <ul class="lim">{lim}</ul>"""

    at_html = "<p>No attribution run.</p>"
    if at:
        s = at.get("summary") or {}
        crow = []
        for c in at.get("candidates", []):
            v = c.get("vessel") or {}
            fac = c.get("factors") or {}
            ev = "".join(f"<li>{_h(e)}</li>" for e in c.get("evidence", []))
            crow.append(f"""<tr><td>#{_h(c.get('rank'))}</td><td>{_h(v.get('name') or '—')}<br><small>MMSI {_h(c.get('mmsi'))} · {_h(v.get('type') or 'type unknown')}</small></td>
            <td><b>{_fmt(c.get('score'), 1)}</b><br><small>{_h(c.get('priority'))}</small></td>
            <td><small>P {_fmt(fac.get('proximity'))} · T {_fmt(fac.get('temporal_overlap'))} · H {_fmt(fac.get('heading_compatibility'))} · L {_fmt(fac.get('loitering'))} · G {_fmt(fac.get('ais_gap_relevance'))} · V {_fmt(fac.get('vessel_type'))} · D {_fmt(fac.get('data_quality'))}</small></td>
            <td><ul class="ev">{ev}</ul></td><td>{_h(c.get('review_status') or 'unreviewed')}</td></tr>""")
        at_html = f"""
        <p>AIS sources: {''.join(f"<span class='badge'>{_h(i.get('source') or i.get('filename'))} · {_h(str(i.get('data_mode', '')).upper())} · {_h(i.get('rows_valid'))} fixes</span> " for i in (bundle.get('ais_imports') or [])) or '<span class="badge">none recorded</span>'}
        {"<span class='badge' style='background:#fde68a'>SYNTHETIC AIS — test data, not real vessel traffic</span>" if any(str(i.get('data_mode')) == 'synthetic' for i in (bundle.get('ais_imports') or [])) or 'synthetic' in (s.get('ais_data_modes') or []) else ''}</p>
        <p>Vessels in dataset: {_h(s.get('vessels_in_dataset'))} · in time window: {_h(s.get('vessels_in_time_window'))} · in search region: {_h(s.get('vessels_in_search_region'))} · scored: {_h(s.get('vessels_scored'))}</p>
        <p>Search: {_h(s.get('search_buffer_km'))} km buffer around 90 % origin region, {_h(s.get('search_time_start_utc'))} → {_h(s.get('search_time_end_utc'))} UTC · weights {_h(at.get('weights'))}</p>
        {"<p class='warn'><b>Dark-source scenario possible:</b> no candidate reaches medium priority; a vessel without AIS may be responsible. Do not infer identity.</p>" if s.get('dark_source_possible') else ""}
        <table><thead><tr><th>Rank</th><th>Candidate vessel</th><th>Score</th><th>Factors</th><th>Evidence</th><th>Analyst</th></tr></thead><tbody>{''.join(crow) or '<tr><td colspan=6>No candidates in search window.</td></tr>'}</tbody></table>"""

    audit = "".join(f"<tr><td>{_h(a.get('ts'))}</td><td>{_h(a.get('action'))}</td><td><small>{_h(a.get('detail'))[:220]}</small></td></tr>" for a in bundle.get("audit_log", []))

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>OceanTrace Incident Dossier — {_h(case['name'])} ({_h(case['id'])})</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;margin:36px;color:#0f172a;line-height:1.5;background:#f8fafc}}
.container{{max-width:980px;margin:0 auto;background:#fff;padding:36px 44px;border-radius:10px;box-shadow:0 4px 20px rgba(0,0,0,0.06);border:1px solid #e2e8f0}}
.no-print{{margin-bottom:20px;display:flex;gap:10px;justify-content:flex-end}}
.btn{{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;background:#0284c7;color:#fff;text-decoration:none;border-radius:6px;font-size:13px;font-weight:600;border:none;cursor:pointer}}
.btn:hover{{background:#0369a1}}
.btn-ghost{{background:#f1f5f9;color:#334155;border:1px solid #cbd5e1}}
.btn-ghost:hover{{background:#e2e8f0}}
h1{{margin:0 0 6px;color:#0f172a;font-size:24px;font-weight:800;letter-spacing:-0.02em}}
h2{{margin-top:28px;margin-bottom:12px;border-bottom:2px solid #0284c7;padding-bottom:6px;color:#0f172a;font-size:16px;font-weight:700;letter-spacing:-0.01em}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}}
th,td{{border:1px solid #cbd5e1;padding:8px 10px;vertical-align:top;text-align:left}}
th{{background:#f1f5f9;font-weight:700;color:#334155}}
.badge{{display:inline-block;padding:3px 9px;border-radius:12px;background:#0284c7;color:#fff;font-size:11px;font-weight:700}}
.mode{{background:#f59e0b}}
.warn{{background:#fffbeb;border-left:4px solid #f59e0b;padding:10px;border-radius:4px;color:#92400e;margin:12px 0}}
.lim li,.ev li{{font-size:12px;margin-bottom:3px}}
.disc{{background:#f8fafc;border:1px solid #cbd5e1;padding:12px;font-size:12px;border-radius:6px;color:#475569;margin:14px 0}}
code{{font-size:11px;word-break:break-all;background:#f1f5f9;padding:2px 4px;border-radius:4px}}
@media print{{
  body{{margin:0;background:#fff}}
  .container{{box-shadow:none;border:none;padding:0;max-width:100%}}
  .no-print{{display:none !important}}
}}
</style></head><body>
<div class="container">
<div class="no-print">
  <button class="btn" onclick="window.print()">🖨️ Print / Save as PDF</button>
  <a class="btn btn-ghost" href="/api/v1/cases/{_h(case['id'])}/export?format=json" download>📥 Case JSON</a>
  <a class="btn btn-ghost" href="/api/v1/cases/{_h(case['id'])}/export?format=geojson" download>🗺️ GeoJSON Layers</a>
</div>
<h1>Spill Forensics — Investigation Incident Dossier</h1>
<p><b>{_h(case['name'])}</b> · Case ID: <code>{_h(case['id'])}</code> · Status: <b>{_h(case['status'])}</b> · <span class="badge mode">DATA MODE: {_h(mode)}</span> · Exported: {_h(bundle.get('exported_at_utc'))}</p>
<div class="disc">{_h(bundle.get('disclaimer'))}</div>

<h2>1. Scene</h2>
<p>File: {_h(meta.get('original_filename'))} · Sensor: {_h(meta.get('sensor'))} · Polarisation: {_h(meta.get('polarization') or 'n/a')} · Acquisition: <b>{_h(scene.get('acquisition_time'))} UTC</b> (source: {_h(meta.get('acquisition_time_source'))})</p>
<p>CRS: {_h(meta.get('crs'))} (original {_h(meta.get('crs_original') or 'none — manual bounds')}) · Bounds: {_h(scene.get('bounds'))} · Pixel ≈ {_h(meta.get('pixel_size_m'))} m · Georeference: {_h(meta.get('georef_source'))}</p>
<p>Detector: {_h(det.get('active_adapter'))} · {_h((meta.get('detection_provenance') or {}).get('model_version'))} · threshold {_h((meta.get('detection_provenance') or {}).get('threshold'))}</p>

<h2>2. Candidate slicks</h2>
<table><thead><tr><th>ID</th><th>Class</th><th>Confidence</th><th>Area km²</th><th>Orientation</th><th>Centroid</th><th>Analyst</th></tr></thead><tbody>{slick_table}</tbody></table>

<h2>3. Hindcast — estimated origin</h2>{hc_html}

<h2>4. AIS correlation — candidate vessels (investigation priority)</h2>{at_html}

<h2>5. Environmental data</h2>
<p>{_h(env.get('provider'))} <span class="badge">{_h(str(env.get('data_mode', '')).upper())}</span> · {_h((env.get('metadata') or {}).get('source'))} · coverage adequate: {_h((env.get('coverage') or {}).get('adequate'))}</p>

<h2>6. Audit log</h2>
<table><thead><tr><th>Time (UTC)</th><th>Action</th><th>Detail</th></tr></thead><tbody>{audit}</tbody></table>
<p><small>Config snapshot hash: {_h(case.get('config_hash'))}. Generated by Spill Forensics v0.3 — output is investigation support, not a legal determination.</small></p>
</div>
</body></html>"""
