import React, { useEffect, useState } from "react";

const j = (r) => (r.ok ? r.json() : r.json().then((e) => Promise.reject(new Error(e.detail || r.statusText))));
const EXAMPLE = `// Declare the chip as a Verilog module: ports become pins.
module MY_MCU (
  input  VDD, input GND,
  inout  SDA, inout SCL,
  input  INT, input CHG_STAT,
  inout  SWDIO, input SWDCLK, input NRESET,
  output [1:0] GPO
);`;

// Add a custom MCU or chip: pinout from a Verilog module header or a pin list, plus what you know about it.
export default function CustomParts({ onClose }) {
  const [list, setList] = useState({ categories: [], parts: [] });
  const [f, setF] = useState({ mpn: "", category: "mcu", description: "", price_usd: "", stock: "", power_mw: "", max_voltage_v: "", footprint: "" });
  const [mode, setMode] = useState("verilog");
  const [src, setSrc] = useState("");
  const [pre, setPre] = useState(null);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");
  const load = () => fetch("/api/custom_parts").then(j).then(setList).catch(() => {});
  useEffect(() => { load(); }, []);

  const body = () => {
    const n = (k) => (f[k] === "" ? null : Number(f[k]));
    return { ...f, price_usd: n("price_usd"), stock: n("stock"), power_mw: n("power_mw"), max_voltage_v: n("max_voltage_v"),
      verilog: mode === "verilog" ? src : "", pins: mode === "pins" ? src : "" };
  };
  const post = (url) => fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body()) }).then(j);
  const preview = () => { setErr(""); setOk(""); post("/api/custom_parts/preview").then(setPre).catch((e) => { setPre(null); setErr(e.message); }); };
  const save = () => { setErr(""); post("/api/custom_parts").then((r) => { setOk(`Saved ${r.mpn} (${r.pins} pins). Agents can now use it.`); setPre(null); load(); }).catch((e) => setErr(e.message)); };
  const del = (m) => fetch(`/api/custom_parts/${encodeURIComponent(m)}`, { method: "DELETE" }).then(load);
  const inp = (k, label, ph, w) => (
    <label className="cp-f" style={{ flex: w || 1 }}>{label}
      <input value={f[k]} placeholder={ph} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></label>
  );
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-h"><h3>Custom parts</h3><button onClick={onClose}>✕</button></div>
        <div className="cp-row">
          {inp("mpn", "Part number", "MY-MCU-1", 2)}
          <label className="cp-f" style={{ flex: 1 }}>Category
            <select value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })}>
              {list.categories.map((c) => <option key={c}>{c}</option>)}</select></label>
        </div>
        {inp("description", "Description", "What it is")}
        <div className="cp-row">
          {inp("price_usd", "Price (USD)", "unknown")}{inp("stock", "Stock", "unknown")}
          {inp("power_mw", "Active power (mW)", "0")}{inp("max_voltage_v", "Max supply (V)", "unknown")}
        </div>
        <div className="cp-tabs">
          <button className={mode === "verilog" ? "on" : ""} onClick={() => setMode("verilog")}>Verilog module</button>
          <button className={mode === "pins" ? "on" : ""} onClick={() => setMode("pins")}>Pin list</button>
          {mode === "verilog" && <button onClick={() => setSrc(EXAMPLE)}>Insert example</button>}
        </div>
        <textarea className="cp-src" spellCheck={false} value={src} onChange={(e) => setSrc(e.target.value)}
          placeholder={mode === "verilog" ? "module MY_MCU ( input VDD, input GND, inout SDA, ... );" : "One pin per line: name[,input|output|inout|power]\nVDD,power\nGND,power\nSDA,inout"} />
        <div className="cp-hint">Name pins by function (VDD, GND, SDA, SCL, SWDIO, NRESET…) and the board builder can wire the part in automatically. Unknown fields stay unknown. Footprint: {f.footprint || "a standard KiCad package is chosen by pin count"}.</div>
        {pre && <div className="cp-pre"><b>{pre.pins.length} pins</b> · {pre.footprint}{pre.footprint_assumed ? " (assumed)" : ""}
          <div>{pre.pins.map((p) => <span key={p.num} className={`pin ${p.type}`}>{p.num}:{p.name}</span>)}</div></div>}
        {err && <div className="err">{err}</div>}
        {ok && <div className="cp-ok">{ok}</div>}
        <div className="cp-actions"><button onClick={preview} disabled={!src.trim()}>Preview pins</button>
          <button className="primary" onClick={save} disabled={!f.mpn || !src.trim()}>Add part</button></div>
        {list.parts.length > 0 && (<>
          <h4>Your parts</h4>
          <table className="cp-list"><tbody>{list.parts.map((p) => (
            <tr key={p.mpn}><td><b>{p.mpn}</b></td><td>{p.category}</td><td>{p.pins} pins</td>
              <td>{p.price_usd == null ? "price unknown" : `$${p.price_usd}`}</td>
              <td><button onClick={() => del(p.mpn)}>Remove</button></td></tr>))}</tbody></table></>)}
      </div>
    </div>
  );
}
