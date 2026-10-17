import React, { useEffect, useState } from "react";

const j = (r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText)));
const usd = (n) => (n == null ? "–" : `$${Number(n).toFixed(2)}`);
const mins = (s) => (s == null ? "–" : s < 90 ? `${Math.round(s)} Sec` : `${(s / 60).toFixed(1)} Min`);

// How the team improves over time: every finished design, what the checks and reviewer caught, and the lessons carried forward.
export default function Learning({ runId }) {
  const [d, setD] = useState(null);
  const [L, setL] = useState(null);
  const load = () => fetch("/api/learning").then(j).then(setD).catch(() => {});
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, []);
  useEffect(() => { if (runId && runId !== "__none__") fetch(`/api/runs/${runId}/ledger`).then(j).then(setL).catch(() => {}); }, [runId]);
  const setStatus = (id, status) => fetch(`/api/lessons/${id}/status`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ status }) }).then(load);
  if (!d) return <div className="empty small">Loading…</div>;
  const runs = d.runs, max = Math.max(1, ...runs.map((r) => Math.max(r.blockers, r.refused)));
  const b = L?.baseline, sc = L?.system?.completeness;
  return (
    <div className="learn">
      <h3>Single Answer Vs The Team</h3>
      {b?.completeness && sc ? (
        <table className="cmp"><thead><tr><th /><th>One Answer From The Same Model</th><th>The Team</th></tr></thead><tbody>
          <tr><td>Checks Passed (Buildable Board)</td><td className={b.completeness.passed < sc.passed ? "r" : ""}>{b.completeness.passed} / {b.completeness.total}</td><td className={sc.passed >= b.completeness.passed ? "g" : ""}>{sc.passed} / {sc.total}</td></tr>
          <tr><td>Time</td><td>{mins(b.seconds)}</td><td>{mins(L.system.seconds)}</td></tr>
          <tr><td>Independent Review</td><td>None</td><td>{L.system.objections} Objections, {L.system.fixed} Fixed</td></tr>
        </tbody></table>
      ) : <div className="muted">Finish a design to see how one answer compares with the team. The single answer gets the full parts catalog and is graded by the same checks.</div>}
      <p className="fine">The team is slower. The single answer skips the checks, so it misses the parts a real board needs.</p>

      <h3>Results Over Time</h3>
      {runs.length === 0 && <div className="muted">No finished designs yet.</div>}
      {runs.length > 0 && (
        <div className="chart">{runs.map((r, i) => (
          <div key={r.run_id} className="col" title={`${r.device}: ${r.blockers} serious objections, ${r.refused} options refused`}>
            <div className="bars"><i className="b1" style={{ height: `${(r.blockers / max) * 100}%` }} /><i className="b2" style={{ height: `${(r.refused / max) * 100}%` }} /></div>
            <small>Design {i + 1}</small></div>))}
          <div className="legend"><span><i className="b1" /> Serious Reviewer Objections</span><span><i className="b2" /> Options Refused By Checks</span></div></div>
      )}
      {runs.length > 0 && (
        <table className="cmp tight"><thead><tr><th>Design</th><th>Time</th><th>Objections Fixed / Rebutted</th><th>Lessons Applied</th><th>Checks</th><th>Cost</th></tr></thead><tbody>
          {runs.map((r, i) => <tr key={r.run_id}><td>{i + 1}. {r.device}</td><td>{mins(r.seconds)}</td><td>{r.fixed} / {r.rebutted}</td><td>{r.lessons_used}</td><td>{r.complete ?? "–"} / {r.of ?? "–"}</td><td>{usd(r.cost_usd)}</td></tr>)}</tbody></table>
      )}
      {runs.length < 2 && runs.length > 0 && <p className="fine">Run more designs to see the trend. Lessons from one design are used in the next.</p>}

      <h3>Lessons The Team Carries Forward</h3>
      {d.lessons.length === 0 && <div className="muted">None yet.</div>}
      <ul className="lessons">{d.lessons.map((l) => (
        <li key={l.id} className={l.status}>
          <div>{l.text}</div>
          <small>{l.applies_to === "system" ? "Whole Board" : l.applies_to.replace("_", " ")} · {l.status === "auto" || l.status === "approved" ? "Applied To Future Designs" : l.status === "rejected" ? "Rejected" : "Waiting For Your Approval"} · Seen In {l.runs.length} Design{l.runs.length > 1 ? "s" : ""}
            {l.status !== "approved" && <button onClick={() => setStatus(l.id, "approved")}>Approve</button>}
            {l.status !== "rejected" && <button onClick={() => setStatus(l.id, "rejected")}>Reject</button>}</small>
        </li>))}</ul>
    </div>
  );
}
