import { useEffect, useState } from "react";
import { api, fmtUtc } from "../lib/api";
import { useStore } from "../lib/store";
import { Badge, Slider } from "./Common";

/** Step 2 — Lagrangian drift forcing: all parameters go to the API and are validated against config limits. */
export default function HindcastPanel({ onClose }: { onClose: () => void }) {
  const { caseId, caseData, selectedSlick, runJob, config, hindcast, notify } = useStore();
  const d = config?.hindcast?.defaults || {};
  const lim = config?.hindcast?.limits || {};
  const [hours, setHours] = useState<number>(d.hindcast_hours ?? 24);
  const [dt, setDt] = useState<number>(d.time_step_minutes ?? 15);
  const [particles, setParticles] = useState<number>(d.particle_count ?? 600);
  const [members, setMembers] = useState<number>(d.ensemble_members ?? 20);
  const [wmin, setWmin] = useState<number>((d.windage_range || [0.02, 0.04])[0] * 100);
  const [wmax, setWmax] = useState<number>((d.windage_range || [0.02, 0.04])[1] * 100);
  const [cscale, setCscale] = useState<number>(1.0);
  const [cjit, setCjit] = useState<number>(d.current_direction_jitter_deg ?? 15);
  const [diff, setDiff] = useState<number>(d.diffusion_m2_s ?? 1);
  const [mass, setMass] = useState<number>((d.probability_mass ?? 0.7) * 100);
  const [integ, setInteg] = useState<string>(d.integrator ?? "rk4");
  const [env, setEnv] = useState<string>("open_meteo");
  const [ncId, setNcId] = useState<string | null>(null);
  const [fcHours, setFcHours] = useState<number>(config?.hindcast?.forecast?.hours ?? 24);

  useEffect(() => { if (config) { setHours(d.hindcast_hours); setDt(d.time_step_minutes); setParticles(d.particle_count); setMembers(d.ensemble_members); } }, [config]);

  const slick = caseData?.slicks.features.find((f) => f.id === selectedSlick);
  const body = () => ({
    hindcast_hours: hours, time_step_minutes: dt, particle_count: particles, ensemble_members: members,
    windage_range: [wmin / 100, wmax / 100], current_scale_range: [Math.max(0.3, cscale - 0.2), Math.min(2.0, cscale + 0.2)],
    current_direction_jitter_deg: cjit, diffusion_m2_s: diff, probability_mass: mass / 100, integrator: integ, environment_source: env, netcdf_env_id: ncId,
  });

  const run = async () => {
    if (!caseId || !slick) return notify("error", "Select a slick polygon first");
    if (wmin > wmax) return notify("error", "windage min must be ≤ max");
    await runJob(`Hindcast ${hours}h × ${members} members`, () => api.hindcast(caseId, slick.id, body()), async () => { useStore.getState().setFocus("origin"); });
  };
  const runForecast = async () => {
    if (!caseId || !slick) return notify("error", "Select a slick polygon first");
    await runJob(`Forecast T+${fcHours}h`, () => api.forecast(caseId, slick.id, { hours: fcHours, particle_count: Math.min(particles, 400), ensemble_members: Math.min(members, 10), windage_range: [wmin / 100, wmax / 100], environment_source: env === "netcdf_upload" ? "open_meteo" : env }));
  };
  const uploadNc = async (f: File | null) => {
    if (!f || !caseId) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      const r = await api.upload<any>(`/api/v1/cases/${caseId}/environment/netcdf`, fd);
      setNcId(r.netcdf_env_id);
      setEnv("netcdf_upload");
      notify("ok", `NetCDF ${f.name} staged (IMPORTED)`);
    } catch (e: any) { notify("error", e.message); }
  };

  const r = (k: string, i: number, fb: number) => (lim[k] ? lim[k][i] : fb);
  return (
    <div className="overlay map-panel">
      <div className="row" style={{ marginBottom: 8 }}><b>LAGRANGIAN DRIFT FORCING</b><button className="btn sm ghost right" onClick={onClose}>✕</button></div>
      <div className="small muted" style={{ marginBottom: 8 }}>
        Seeds particles across <b>{slick ? `${slick.id} (${slick.properties.area_km2} km²)` : "— select a slick —"}</b> at {fmtUtc(caseData?.scene?.acquisition_time)} and integrates backward. Every value is validated server-side against <span className="mono">configs/default.yaml</span> limits.
      </div>
      <div className="col" style={{ gap: 10 }}>
        <Slider label="Backtrack duration" value={hours} min={r("hindcast_hours", 0, 1)} max={r("hindcast_hours", 1, 96)} step={1} onChange={setHours} fmt={(v) => `${v} h`} />
        <Slider label="Time step" value={dt} min={r("time_step_minutes", 0, 5)} max={r("time_step_minutes", 1, 60)} step={5} onChange={setDt} fmt={(v) => `${v} min`} />
        <Slider label="Particles per member" value={particles} min={r("particle_count", 0, 50)} max={Math.min(3000, r("particle_count", 1, 5000))} step={50} onChange={setParticles} />
        <Slider label="Ensemble members" value={members} min={r("ensemble_members", 0, 1)} max={Math.min(60, r("ensemble_members", 1, 100))} step={1} onChange={setMembers} />
        <div className="row">
          <div className="grow"><Slider label="Windage min" value={wmin} min={0} max={r("windage", 1, 0.08) * 100} step={0.25} onChange={setWmin} fmt={(v) => `${v.toFixed(2)} %`} /></div>
          <div className="grow"><Slider label="Windage max" value={wmax} min={0} max={r("windage", 1, 0.08) * 100} step={0.25} onChange={setWmax} fmt={(v) => `${v.toFixed(2)} %`} /></div>
        </div>
        <Slider label="Current scale (±0.2 sampled)" value={cscale} min={r("current_scale", 0, 0.3) + 0.2} max={r("current_scale", 1, 2) - 0.2} step={0.05} onChange={setCscale} fmt={(v) => `${v.toFixed(2)}×`} />
        <Slider label="Current direction jitter σ" value={cjit} min={0} max={60} step={1} onChange={setCjit} fmt={(v) => `${v}°`} />
        <Slider label="Eddy diffusivity" value={diff} min={0} max={r("diffusion_m2_s", 1, 50)} step={0.5} onChange={setDiff} fmt={(v) => `${v} m²/s`} />
        <Slider label="Origin region probability mass" value={mass} min={30} max={95} step={5} onChange={setMass} fmt={(v) => `${v} %`} />
        <div className="row">
          <div className="grow"><label className="lbl">Integrator</label><select className="input" value={integ} onChange={(e) => setInteg(e.target.value)}><option value="rk4">RK4</option><option value="euler">Euler</option></select></div>
          <div className="grow"><label className="lbl">Environment source</label>
            <select className="input" value={env} onChange={(e) => setEnv(e.target.value)}>
              <option value="open_meteo">Open-Meteo (Copernicus SMOC + ERA5/GFS) — REAL</option>
              <option value="netcdf_upload" disabled={!ncId}>Uploaded NetCDF — IMPORTED</option>
              <option value="constant">Constant test field — DIAGNOSTIC</option>
            </select>
          </div>
        </div>
        <label className="dropzone small" style={{ padding: 8 }}>
          {ncId ? <span className="green">NetCDF staged ({ncId})</span> : "Upload CF NetCDF (u_current, v_current, u10_wind, v10_wind)"}
          <input type="file" accept=".nc,.nc4" hidden onChange={(e) => uploadNc(e.target.files?.[0] || null)} />
        </label>
        {env === "constant" && <Badge kind="warn">▲ constant field is NOT real ocean data — result will be labelled CONSTANT</Badge>}
        <div className="row">
          <button className="btn primary grow" onClick={run} disabled={!slick}>Run backtrack ({integ.toUpperCase()})</button>
          <button className="btn grow" onClick={runForecast} disabled={!slick}>Run forward T+{fcHours}h</button>
        </div>
        <Slider label="Forecast horizon" value={fcHours} min={6} max={72} step={6} onChange={setFcHours} fmt={(v) => `${v} h`} />
        {hindcast && (
          <div className="tile small">
            <div className="xs muted">Last run · {hindcast.id}</div>
            <div className="mono">{fmtUtc(hindcast.release_start)} → {fmtUtc(hindcast.release_end)}</div>
            <div className="muted">basis: {hindcast.metrics?.window_basis} · env: {hindcast.metrics?.environment?.provider} <Badge kind={hindcast.metrics?.environment?.data_mode}>{hindcast.metrics?.environment?.data_mode}</Badge></div>
          </div>
        )}
        {caseData?.hindcast_runs && caseData.hindcast_runs.length > 1 && (
          <div>
            <label className="lbl">Compare scenarios (previous runs are retained)</label>
            <select className="input small" value={hindcast?.id || ""} onChange={(e) => useStore.getState().loadHindcast(e.target.value)}>
              {caseData.hindcast_runs.filter((r) => r.status === "completed").map((r) => (
                <option key={r.id} value={r.id}>{r.id} · {r.hindcast_hours}h · {r.ensemble_members} members · windage {JSON.stringify(r.windage_range)} · {fmtUtc(r.release_start)}→{fmtUtc(r.release_end)}</option>
              ))}
            </select>
          </div>
        )}
      </div>
    </div>
  );
}
