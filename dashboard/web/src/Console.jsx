import React, { useEffect, useMemo, useRef, useState } from "react";

const sev = (e) => {
  const p = e.payload || {};
  if (e.kind === "agent_status" && p.status === "failed") return "ERROR";
  if (e.kind === "verifier_result") return p.ok ? "INFO" : (p.verifier === "gate" ? "WARNING" : "ERROR");
  if (e.kind === "replan" || e.kind === "constraint_change") return "WARNING";
  if (e.kind === "run_done") return p.ok ? "INFO" : "ERROR";
  return "INFO";
};
const text = (e) => {
  const p = e.payload || {};
  switch (e.kind) {
    case "step": return p.message;
    case "agent_spawned": return `spawned ${p.label || ""}`;
    case "agent_status": return `status → ${p.status}`;
    case "delegate": return `delegate → ${p.to}  ${(p.constraints || [""])[0].slice(0, 90)}`;
    case "report": return `report → ${p.to}: ${(p.options || []).map((o) => o.name).join(" | ").slice(0, 120)}`;
    case "verifier_result": return `[${p.verifier}] ${p.ok ? "PASS" : `${(p.errors || []).length} issue(s): ${(p.errors || []).slice(0, 2).map((x) => x.message).join("; ").slice(0, 150)}`}`;
    case "design_updated": return "design updated";
    case "run_done": return p.summary;
    default: return `${e.kind} ${JSON.stringify(p).slice(0, 120)}`;
  }
};

// EDA-style console: timestamped, severity-tagged, filterable.
export default function Console({ events, t0 }) {
  const [filter, setFilter] = useState("ALL");
  const [q, setQ] = useState("");
  const ref = useRef(null);
  const rows = useMemo(() => events.map((e) => ({ e, s: sev(e), t: text(e) || "" }))
    .filter((r) => (filter === "ALL" || r.s === filter) && (!q || (r.e.agent_id + r.t).toLowerCase().includes(q.toLowerCase()))), [events, filter, q]);
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [rows.length]);
  const n = (s) => events.filter((e) => sev(e) === s).length;
  const fmt = (ts) => { const d = Math.max(0, ts - (t0 || ts)); return `${String(Math.floor(d / 60)).padStart(2, "0")}:${(d % 60).toFixed(1).padStart(4, "0")}`; };
  return (
    <div className="console">
      <div className="console-bar">
        {["ALL", "INFO", "WARNING", "ERROR"].map((f) => (
          <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>{f === "ALL" ? `All ${events.length}` : `${f[0] + f.slice(1).toLowerCase()} ${n(f)}`}</button>
        ))}
        <input placeholder="Filter log…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="console-body" ref={ref}>
        {rows.slice(-600).map((r, i) => (
          <div key={i} className={`cl ${r.s}`}><span className="ct">[{fmt(r.e.ts)}]</span><span className="cs">{r.s}</span><span className="ca">{r.e.agent_id}</span><span className="cm">{r.t}</span></div>
        ))}
      </div>
    </div>
  );
}
