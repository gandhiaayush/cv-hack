import React, { useEffect, useState } from "react";

const j = (r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText)));

// What the team has learned across runs, with the effect on reviewer blockers and approve/reject controls.
export default function Lessons({ runId, result }) {
  const [data, setData] = useState(null);
  const load = () => fetch("/api/lessons").then(j).then(setData).catch(() => {});
  useEffect(() => { load(); }, [runId, result]);
  if (!data) return null;
  const mine = data.lessons.filter((l) => l.runs.includes(runId));
  const hist = data.history.filter((h) => h.run !== runId).slice(-4);
  const set = (id, status) => fetch(`/api/lessons/${id}/status`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ status }) }).then(load);
  const used = result?.lessons_used?.length || 0;
  return (
    <div className="final" style={{ marginTop: 10 }}>
      <h3>What the team learned</h3>
      <div style={{ fontSize: 13 }}>
        Reviewer blockers this run: <b>{result?.blockers_first_round ?? "?"}</b>
        {hist.length > 0 && <> · earlier runs: {hist.map((h) => h.blockers_first_round).join(", ")}</>}
        {" · "}lessons applied: <b>{used}</b>
      </div>
      {mine.length === 0 && <div className="sub" style={{ color: "var(--muted)", fontSize: 12, marginTop: 6 }}>No new lessons from this run.</div>}
      <ul style={{ listStyle: "none", margin: "8px 0 0", padding: 0 }}>
        {mine.map((l) => (
          <li key={l.id} style={{ margin: "0 0 8px", fontSize: 13 }}>
            {l.text}
            <div style={{ fontSize: 11.5, color: "var(--muted)" }}>
              {l.applies_to} · {l.status === "auto" ? "applied to future runs (the design changed because of it)" : l.status === "candidate" ? "contested: not applied until approved" : l.status}
              {" · "}from {l.source.objection}
              {l.status !== "approved" && <button style={{ marginLeft: 8, fontSize: 11 }} onClick={() => set(l.id, "approved")}>Approve</button>}
              {l.status !== "rejected" && <button style={{ marginLeft: 4, fontSize: 11 }} onClick={() => set(l.id, "rejected")}>Reject</button>}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
