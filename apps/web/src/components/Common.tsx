import { ReactNode } from "react";
import { useStore } from "../lib/store";

/** Compact sun/moon toggle for switching between light and dark themes. */
export const ThemeToggle = () => {
  const { theme, toggleTheme } = useStore();
  return (
    <button
      className="theme-toggle"
      onClick={toggleTheme}
      title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
    >
      {theme === "dark" ? (
        /* Sun icon — shown in dark mode (click to go light) */
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="5" />
          <line x1="12" y1="1" x2="12" y2="3" /><line x1="12" y1="21" x2="12" y2="23" />
          <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" /><line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
          <line x1="1" y1="12" x2="3" y2="12" /><line x1="21" y1="12" x2="23" y2="12" />
          <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" /><line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
        </svg>
      ) : (
        /* Moon icon — shown in light mode (click to go dark) */
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
    </button>
  );
};


export const Badge = ({ kind, children, title }: { kind: string; children: ReactNode; title?: string }) => (
  <span className={`badge ${kind}`} title={title}>{children}</span>
);

export const ModeBadge = ({ mode }: { mode?: string | null }) => {
  const m = (mode || "imported").toLowerCase();
  return <Badge kind={m} title="Data provenance mode — never hidden">{m === "real" ? "● REAL DATA" : m === "imported" ? "● IMPORTED" : m === "constant" ? "▲ CONSTANT FIELD" : "▲ " + m.toUpperCase()}</Badge>;
};

export const Card = ({ title, children, right, amber }: { title?: string; children: ReactNode; right?: ReactNode; amber?: boolean }) => (
  <div className="card">
    {title && (
      <div className="row" style={{ marginBottom: 8 }}>
        <h4 className={amber ? "amber" : ""} style={{ margin: 0 }}>{title}</h4>
        <div className="right">{right}</div>
      </div>
    )}
    {children}
  </div>
);

export const KV = ({ items }: { items: [string, ReactNode][] }) => (
  <div className="kv">
    {items.map(([k, v]) => (
      <div className="tile" key={k}>
        <div className="k">{k}</div>
        <div className="v">{v}</div>
      </div>
    ))}
  </div>
);

export const Factor = ({ name, value, amber }: { name: string; value: number; amber?: boolean }) => (
  <div className="factor">
    <span className="muted">{name}</span>
    <div className={`bar ${amber ? "amber" : ""}`}><i style={{ width: `${Math.round(value * 100)}%` }} /></div>
    <span className="mono" style={{ textAlign: "right" }}>{value.toFixed(2)}</span>
  </div>
);

export const Slider = ({ label, value, min, max, step, onChange, fmt }: { label: string; value: number; min: number; max: number; step: number; onChange: (v: number) => void; fmt?: (v: number) => string }) => (
  <div>
    <div className="row small" style={{ justifyContent: "space-between" }}>
      <span className="muted">{label}</span>
      <span className="mono cyan">{fmt ? fmt(value) : value}</span>
    </div>
    <input className="slider" type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} />
  </div>
);

export const Spinner = () => <span className="pulse cyan">●</span>;

export const Icon = ({ name }: { name: string }) => {
  const p: Record<string, ReactNode> = {
    map: <><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18" /></>,
    ship: <><path d="M3 17l2 3h14l2-3M4 17l1-5h14l1 5M7 12V8h10v4M10 8V5h4v3" /></>,
    replay: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    whatif: <><circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="6" r="2.5" /><circle cx="12" cy="18" r="2.5" /><path d="M8 7.5l3 8M16 7.5l-3 8" /></>,
    evidence: <><rect x="3" y="3" width="8" height="8" rx="1.5" /><rect x="13" y="3" width="8" height="8" rx="1.5" /><rect x="3" y="13" width="8" height="8" rx="1.5" /><rect x="13" y="13" width="8" height="8" rx="1.5" /></>,
    dossier: <><path d="M6 3h9l4 4v14H6z" /><path d="M15 3v4h4M9 12h6M9 16h6" /></>,
    play: <path d="M7 5v14l11-7z" fill="currentColor" />,
    pause: <><rect x="6" y="5" width="4" height="14" fill="currentColor" /><rect x="14" y="5" width="4" height="14" fill="currentColor" /></>,
    first: <><path d="M6 5v14M19 5L8 12l11 7z" fill="currentColor" /></>,
    last: <><path d="M18 5v14M5 5l11 7-11 7z" fill="currentColor" /></>,
    upload: <><path d="M12 16V4M6 10l6-6 6 6M4 20h16" /></>,
    sat: <><path d="M4 14l6 6M9 9l6 6M5 13l4-4 6 6-4 4zM13 5l6 6M11 7l2-2 6 6-2 2z" /></>,
  };
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{p[name]}</svg>;
};
