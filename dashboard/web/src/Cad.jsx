import React, { useCallback, useEffect, useState } from "react";
import Board3D from "./Board3D.jsx";
import Pane, { Guard } from "./Pane.jsx";
import Browser, { Comments } from "./Browser.jsx";
import CustomParts from "./CustomParts.jsx";
import PartsDb from "./PartsDb.jsx";
import Learning from "./Learning.jsx";
import "./Cad.css";
import "./Fusion.css";

const j = (r) => (r.ok ? r.json() : r.json().then((e) => Promise.reject(new Error(e.detail || r.statusText))));
const SHAPES = [["Rectangle", "rectangle", <rect key="a" x="4" y="8" width="24" height="16" rx="1" />], ["Rounded", "rectangle with rounded corners 4 mm", <rect key="a" x="4" y="8" width="24" height="16" rx="6" />],
  ["Capsule", "capsule", <rect key="a" x="3" y="9" width="26" height="14" rx="7" />], ["Round", "round", <circle key="a" cx="16" cy="16" r="11" />],
  ["Oval", "oval", <ellipse key="a" cx="16" cy="16" rx="13" ry="9" />]];
const slug = (s) => (s || "untitled").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "untitled";

// Fusion-style workspace: document tab (name.nt), ribbon, tabs for Design / Parts Database / Learning.
export default function Cad({ c }) {
  const { runId, state, log, svg, P } = c;
  const real = runId !== "__none__";
  const [tab, setTab] = useState("Design");
  const [notes, setNotes] = useState([]);
  const [rows, setRows] = useState([]);
  const [sel, setSel] = useState(null);
  const [tick, setTick] = useState(0);
  const [note, setNote] = useState("");
  const [custom, setCustom] = useState(false);
  const [spec, setSpec] = useState(null);
  const [size, setSize] = useState({ w: "", h: "", holes: true });
  const [sizeNote, setSizeNote] = useState("");
  const loadNotes = useCallback(() => (real ? fetch(`/api/runs/${runId}/annotations`).then(j).then(setNotes).catch(() => setNotes([])) : setNotes([])), [runId]);
  useEffect(() => { loadNotes(); setSpec(null); if (real) fetch(`/api/runs/${runId}/spec`).then(j).then(setSpec).catch(() => {}); }, [runId]);
  const d = state?.design, ver = state?.design_version || 0;
  useEffect(() => { if (real && d) fetch(`/api/runs/${runId}/bom2`).then(j).then((x) => setRows(x.rows)).catch(() => setRows([])); }, [runId, ver]);
  const row = sel && rows.find((r) => r.refs.split(" ").includes(sel));
  const status = !real ? "" : !state ? "In Progress" : state.done ? (state.done.ok ? "Complete" : "Stopped") : "In Progress";
  const fileName = `${slug(spec?.device || d?.name)}.nt`;

  const addNote = (target, text) => fetch(`/api/runs/${runId}/annotations`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ target, text }) }).then(loadNotes);
  const delNote = (id) => fetch(`/api/runs/${runId}/annotations/${id}`, { method: "DELETE" }).then(loadNotes);
  const reshape = (text) => {
    if (!real) return;
    setNote("Reshaping…");
    fetch(`/api/runs/${runId}/shape`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text }) })
      .then(j).then((sp) => { setTick((t) => t + 1); return fetch(`/api/runs/${runId}/board.json`).then(j).then((b) => { const o = b.outline;
        setNote(o ? `${o.width_mm} × ${o.height_mm} mm${o.fits ? "" : " · " + o.problems.join("; ")}` : sp.note); }); })
      .catch((e) => setNote(`Didn't Work: ${e.message}`));
  };
  const applySize = () => {
    setSizeNote("Resizing…");
    fetch(`/api/runs/${runId}/board_options`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ width_mm: size.w ? Number(size.w) : null, height_mm: size.h ? Number(size.h) : null, holes: size.holes }) })
      .then(j).then((b) => { setTick((t) => t + 1); setSizeNote(`${b.width_mm} × ${b.height_mm} mm${b.size && !b.size.fits ? " · " + b.size.problems.join("; ") : ""}`); })
      .catch((e) => setSizeNote(`Didn't Work: ${e.message}`));
  };
  const waiting = !real
    ? <div className="empty small">Describe a device in Wood and the team will design it.</div>
    : <div className="empty small"><span className="badge prog">In Progress</span><br />The team is working on the first version…</div>;

  return (
    <div className="fusion">
      <header className="fribbon">
        <div className="ftabs">
          <span className="doc">{fileName}{status && <em className={`st ${status === "Complete" ? "ok" : ""}`}>{status}</em>}</span>
          <span className="grow" />
          {["Design", "Parts Database", "Learning"].map((t) => <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t}</button>)}
        </div>
        <div className="fgroups">
          {tab === "Design" && (<>
            <div className="fg"><div className="fb">
              <button onClick={() => document.getElementById("wood-input")?.focus()}><svg viewBox="0 0 32 32"><path d="M8 4h11l6 6v18H8z" /><path d="M19 4v6h6M16 14v8M12 18h8" /></svg>New Design</button>
              <button onClick={() => setCustom(true)}><svg viewBox="0 0 32 32"><rect x="9" y="9" width="14" height="14" /><path d="M13 5v4M19 5v4M13 23v4M19 23v4M5 13h4M5 19h4M23 13h4M23 19h4" /></svg>Your Own Chip</button>
            </div><span className="fl">Create ▾</span></div>
            <div className="fg"><div className="fb">
              {SHAPES.map(([label, text, shape]) => (
                <button key={label} disabled={!real || !d} onClick={() => reshape(text)} title={`Make The Board ${label}`}><svg viewBox="0 0 32 32">{shape}</svg>{label}</button>))}
            </div><span className="fl">Board Shape ▾ {note && <em>{note}</em>}</span></div>
            <div className="fg"><div className="fb size">
              <label>Width<input value={size.w} placeholder="Auto" onChange={(e) => setSize({ ...size, w: e.target.value })} /></label>
              <label>Height<input value={size.h} placeholder="Auto" onChange={(e) => setSize({ ...size, h: e.target.value })} /></label>
              <label className="chk"><input type="checkbox" checked={size.holes} onChange={(e) => setSize({ ...size, holes: e.target.checked })} />Mounting Holes</label>
              <button disabled={!real || !d} onClick={applySize}><svg viewBox="0 0 32 32"><path d="M5 9h22v14H5zM9 9v4M13 9v3M17 9v4M21 9v3" /></svg>Apply Size</button>
            </div><span className="fl">Board Size (mm) ▾ {sizeNote && <em>{sizeNote}</em>}</span></div>
            <div className="fg"><div className="fb">
              <a className={`btn exportbtn ${!d ? "off" : ""}`} href={d ? `/api/runs/${runId}/export.zip` : undefined} download><svg viewBox="0 0 32 32"><path d="M16 5v15M10 14l6 6 6-6M6 24h20" /></svg>Export</a>
            </div><span className="fl">Download All ▾</span></div>
          </>)}
          {tab === "Parts Database" && <div className="fg"><div className="fb"><button onClick={() => setCustom(true)}><svg viewBox="0 0 32 32"><rect x="9" y="9" width="14" height="14" /><path d="M13 5v4M19 5v4M13 23v4M19 23v4M5 13h4M5 19h4M23 13h4M23 19h4" /></svg>Add Custom Part</button></div><span className="fl">Create ▾</span></div>}
          {tab === "Learning" && <div className="fg"><div className="fl pad">How the team improves from one design to the next.</div></div>}
        </div>
      </header>

      {tab === "Design" && (
        <div className="fmain">
          <aside className="fleft">
            <Browser state={state} log={log} rows={rows} sel={sel} setSel={setSel} />
            <Comments notes={notes} sel={sel} selName={row?.name} onAdd={addNote} onDelete={delNote} />
          </aside>
          <div className="split">
            <Pane title="Circuit Diagram" kind="sch" runId={runId} notes={notes} reload={loadNotes}>{d && svg ? <P.Schematic svg={svg} /> : waiting}</Pane>
            <Pane title="3D Board" kind="3d" runId={runId} notes={notes} reload={loadNotes}>
              {d ? <Guard what="The 3D board"><Board3D runId={runId} designVersion={ver * 1000 + tick + (c.shapeTick || 0)} failing={state.failing_refs || []} /></Guard> : waiting}</Pane>
          </div>
        </div>
      )}
      {tab === "Parts Database" && <div className="fpage"><PartsDb reloadKey={custom} /></div>}
      {tab === "Learning" && <div className="fpage"><Learning runId={runId} /></div>}
      {custom && <CustomParts onClose={() => setCustom(false)} />}
    </div>
  );
}
