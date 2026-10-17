import React, { useCallback, useEffect, useState } from "react";
import "./BomPlus.css";

const COLORS = ["#58a6ff", "#3fb950", "#d29922", "#bc8cff", "#f778ba", "#79c0ff"];
const usd = (n, d = 2) => (n == null ? "?" : `$${n.toFixed(d)}`);
const j = (r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText)));

// BOM with real part names + buy/datasheet links, a cost spec with volume pricing, and annotations.
export default function BomPlus({ runId, designVersion }) {
  const [data, setData] = useState(null);
  const [notes, setNotes] = useState([]);
  const [open, setOpen] = useState(null);   // target of the note box currently open
  const [draft, setDraft] = useState("");

  const load = useCallback(() => {
    fetch(`/api/runs/${runId}/bom2`).then(j).then(setData).catch(() => setData(null));
    fetch(`/api/runs/${runId}/annotations`).then(j).then(setNotes).catch(() => {});
  }, [runId]);
  useEffect(load, [load, designVersion]);

  const add = (target) => {
    if (!draft.trim()) return;
    fetch(`/api/runs/${runId}/annotations`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ target, text: draft }) }).then(j).then(() => { setDraft(""); setOpen(null); load(); });
  };
  const del = (id) => fetch(`/api/runs/${runId}/annotations/${id}`, { method: "DELETE" }).then(load);

  if (!data) return <div className="empty small">Waiting for the first design…</div>;
  const { rows, cost } = data;
  const subs = Object.entries(cost.by_subsystem);
  const total1 = cost.boards["1"];
  const NoteChips = ({ target }) => notes.filter((n) => n.target === target).map((n) => (
    <span className="note" key={n.id}>{n.text}<button title="delete" onClick={() => del(n.id)}>×</button></span>
  ));
  const NoteBox = ({ target }) => open === target ? (
    <div className="addnote">
      <input autoFocus value={draft} placeholder="Add a note…" onChange={(e) => setDraft(e.target.value)}
             onKeyDown={(e) => e.key === "Enter" && add(target)} />
      <button onClick={() => add(target)}>Add</button>
    </div>
  ) : null;

  return (
    <div className="bomplus">
      <div className="cost">
        <div className={`card ${total1 > cost.budget_usd ? "over" : ""}`}><small>Per board · qty 1 (LCSC)</small><b>{usd(total1)}</b></div>
        <div className="card"><small>Per board · 100 boards</small><b>{usd(cost.boards["100"])}</b></div>
        <div className="card"><small>Per board · 1,000 boards</small><b>{usd(cost.boards["1000"])}</b></div>
        <div className="card"><small>Budget</small><b>{usd(cost.budget_usd)}</b></div>
      </div>
      <div className="legend">
        {subs.map(([s, v], i) => <span key={s}><span style={{ color: COLORS[i % 6] }}>■</span> {s} {usd(v)}</span>)}
      </div>
      <div className="bar">
        {subs.map(([s, v], i) => <i key={s} title={`${s} ${usd(v)}`} style={{ width: `${(v / Math.max(total1, 0.0001)) * 100}%`, background: COLORS[i % 6] }} />)}
      </div>
      {Math.abs(cost.agent_priced_total - total1) > 0.5 && (
        <div className="sub" style={{ marginBottom: 8, color: "#d29922" }}>
          Distributor prices total {usd(total1)}, but the agents priced this design at {usd(cost.agent_priced_total)} using demo-catalogue
          values. Real prices change whether the budget holds.
        </div>
      )}
      {cost.unpriced_lines > 0 && <div className="sub" style={{ marginBottom: 8 }}>{cost.unpriced_lines} line(s) have no price in the data, so totals are a lower bound.</div>}

      <table>
        <thead><tr><th>Part</th><th>Subsystem</th><th>Refs</th><th className="num">Qty</th><th className="num">Unit</th><th className="num">@100</th><th className="num">@1k</th><th>Stock</th><th>Links</th><th /></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.mpn} className={r.stock != null && r.stock < r.qty ? "oos" : ""}>
              <td>
                <div className="pname">{r.name}</div>
                <div className="sub">{r.subtitle}</div>
                <NoteChips target={r.mpn} />
                <NoteBox target={r.mpn} />
              </td>
              <td>{r.subsystem}</td>
              <td>{r.refs}</td>
              <td className="num">{r.qty}</td>
              <td className="num">{usd(r.unit_usd, 3)}</td>
              <td className="num">{usd(r.at_volume["100"], 3)}</td>
              <td className="num">{usd(r.at_volume["1000"], 3)}</td>
              <td title={r.source}>{r.stock_qty ?? r.stock ?? "?"}<div className="sub">{r.checked_at || ""}</div></td>
              <td className="links">
                {Object.entries(r.links || {}).map(([k, v]) => <a key={k} href={v} target="_blank" rel="noreferrer">{k}</a>)}
                {!Object.keys(r.links || {}).length && <span className="sub">no distributor data</span>}
              </td>
              <td><button title="Add note" onClick={() => { setOpen(open === r.mpn ? null : r.mpn); setDraft(""); }}>＋</button></td>
            </tr>
          ))}
        </tbody>
      </table>

      <div style={{ marginTop: 14 }}>
        <div className="sub">Design notes</div>
        <NoteChips target="design" />
        {open === "design" ? <NoteBox target="design" /> : <button onClick={() => { setOpen("design"); setDraft(""); }}>＋ Add design note</button>}
      </div>
      <div className="foot">{data.note} <a href={`/api/runs/${runId}/bom.csv`}>csv</a></div>
    </div>
  );
}
