import React, { useEffect, useMemo, useState } from "react";
import CustomParts from "./CustomParts.jsx";

const j = (r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText)));
const usd = (n) => (n == null ? "Unknown" : `$${Number(n).toFixed(n < 1 ? 3 : 2)}`);

// The whole parts database: the catalog snapshot the team picks from, plus your own parts (listed first).
export default function PartsDb({ reloadKey }) {  // custom parts are added from the ribbon
  const [rows, setRows] = useState([]);
  const [q, setQ] = useState("");
  const [cat, setCat] = useState("");
  const [adding, setAdding] = useState(false);
  const [tick, setTick] = useState(0);
  useEffect(() => { fetch("/api/parts").then(j).then(setRows).catch(() => setRows([])); }, [tick, reloadKey]);
  const cats = useMemo(() => [...new Set(rows.map((r) => r.category))].sort(), [rows]);
  const shown = rows.filter((r) => (!cat || r.category === cat) && (!q || `${r.name} ${r.mpn} ${r.category} ${r.package}`.toLowerCase().includes(q.toLowerCase())));
  return (
    <div className="pdb">
      <div className="pdb-bar">
        <input placeholder="Search Parts…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={cat} onChange={(e) => setCat(e.target.value)}><option value="">All Categories</option>{cats.map((c) => <option key={c}>{c}</option>)}</select>
        <span className="muted">{shown.length} Of {rows.length} Parts · {rows.filter((r) => r.custom).length} Custom</span>
        <span className="grow" />
      </div>
      <div className="pdb-scroll">
        <table>
          <thead><tr><th>Part</th><th>Category</th><th>Package</th><th className="num">Price</th><th className="num">Stock</th><th className="num">Pins</th><th>Links</th></tr></thead>
          <tbody>{shown.map((r) => (
            <tr key={r.mpn} className={r.custom ? "cust" : ""}>
              <td><b>{r.name}</b>{r.custom && <span className="badge">Custom</span>}<div className="sub">{r.mpn}</div></td>
              <td>{r.category}</td><td>{r.package || "–"}</td><td className="num">{usd(r.price_usd)}</td><td className="num">{r.stock ?? "Unknown"}</td><td className="num">{r.pins || "–"}</td>
              <td className="lk">{Object.entries(r.links || {}).map(([k, v]) => <a key={k} href={v} target="_blank" rel="noreferrer">{k}</a>)}</td>
            </tr>))}</tbody>
        </table>
        <div className="muted pdb-note">Prices and stock come from a dated LCSC / JLCPCB snapshot, not a live quote. Custom parts show the price and stock you entered.</div>
      </div>
    </div>
  );
}
