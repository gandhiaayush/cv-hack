import React, { useEffect, useState } from "react";

const j = (r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText)));
const usd = (n) => (n == null ? "–" : `$${Number(n).toFixed(2)}`);
const secs = (s) => (s == null ? "–" : s < 90 ? `${Math.round(s)} s` : `${(s / 60).toFixed(1)} min`);

// The skeptic's view: the same model answering in one shot (with the whole parts catalog in context) vs this
// system, both graded by the same plain-code checks. Shows where the system is better and where it is not.
export default function Proof({ runId }) {
  const [d, setD] = useState(null);
  useEffect(() => {
    let alive = true;
    const tick = () => fetch(`/api/runs/${runId}/ledger`).then(j).then((x) => alive && setD(x)).catch(() => {});
    tick(); const t = setInterval(tick, 4000);
    return () => { alive = false; clearInterval(t); };
  }, [runId]);
  if (!d) return <div className="empty small">No data yet…</div>;
  const b = d.baseline, s = d.system, n = d.counts;
  const bc = b?.completeness, sc = s.completeness;
  const cell = (v, good) => <td className={good === true ? "g" : good === false ? "r" : ""}>{v}</td>;
  const bw = bc && sc ? (sc.passed > bc.passed ? "sys" : sc.passed < bc.passed ? "base" : "tie") : null;
  return (
    <div className="proof">
      <h3>Why not just ask a chatbot?</h3>
      <p className="lede">We gave the <b>same model</b> the same request in a single reply, with the <b>entire parts catalog in its context</b>, the best case for a chatbot with the right integrations. Both answers are graded by the same code checks, not by a model.</p>
      <table className="cmp">
        <thead><tr><th /><th>Single shot</th><th>This system</th></tr></thead>
        <tbody>
          <tr><td>Time to an answer</td><td>{secs(b?.seconds)}</td><td>{s.done ? secs(s.seconds) : `${secs(s.seconds)} so far…`}</td></tr>
          <tr><td>Buildable-board checks passed <small>(battery, USB input, pull-ups, decoupling…)</small></td>
            {cell(bc ? `${bc.passed} / ${bc.total}` : "…", bw === "base" ? true : bw === "sys" ? false : null)}
            {cell(sc ? `${sc.passed} / ${sc.total}` : s.done ? "no design" : "…", bw === "sys" ? true : bw === "base" ? false : null)}</tr>
          <tr><td>Part, rating or category violations</td><td>{b?.violations ?? "…"}</td><td>{s.done ? "0 (refused before acceptance)" : "…"}</td></tr>
          <tr><td>Parts not in the verified catalog</td><td>{b?.unknown_parts ?? "…"}</td><td>0 (checked per option)</td></tr>
          <tr><td>Independent review</td><td>none</td><td>{s.objections} objections ({s.blockers} serious): {s.fixed} fixed, {s.rebutted} rebutted</td></tr>
          <tr><td>Estimated BOM / power</td><td>{usd(b?.cost_usd)} · {b?.power_mw ?? "–"} mW</td><td>{usd(s.totals?.cost_usd)} · {s.totals?.power_mw ?? "–"} mW</td></tr>
          <tr><td>Remembers lessons next time</td><td>no</td><td>{s.lessons_used} applied this run</td></tr>
        </tbody>
      </table>
      {bc?.missing?.length > 0 && <p className="miss"><b>Single shot is missing:</b> {bc.missing.join("; ")}</p>}
      {sc?.missing?.length > 0 && <p className="miss"><b>This system is still missing:</b> {sc.missing.join("; ")}</p>}
      {bw === "tie" && <p className="honest">On the completeness checks, the system and the single shot are tied for this run.</p>}
      {bw === "base" && <p className="honest">On this run the single shot scored higher than the system. Speed and effort favour the single shot too.</p>}

      <h3>What the checks caught while the agents worked</h3>
      <p className="lede">{n.agents} agents · {n.tool_calls} tool calls · {n.gate_checks} gate checks · {n.options_refused} options refused by code before the lead agent saw them.</p>
      {d.caught.length === 0 && <div className="muted">Nothing refused yet.</div>}
      <ul className="caught">{d.caught.map((c, i) => (
        <li key={i}><b>{c.agent.replace("_", " ")}</b> proposed “{c.option.replace(/^\[[^\]]+\]\s*/, "").slice(0, 70)}” → refused: {c.codes.join(", ").toLowerCase().replace(/_/g, " ")}{c.parts.length ? ` (${c.parts.slice(0, 3).join(", ")})` : ""}</li>))}</ul>
      <p className="fine">Checks are plain code over a dated LCSC/JLCPCB snapshot and prototype rules. They are not regulatory advice.</p>
    </div>
  );
}
