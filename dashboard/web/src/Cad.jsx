import React, { useCallback, useEffect, useState } from "react";
import Board3D from "./Board3D.jsx";
import Pane, { Guard } from "./Pane.jsx";
import Browser, { Comments } from "./Browser.jsx";
import CustomParts from "./CustomParts.jsx";
import "./Cad.css";
import "./Fusion.css";

const j = (r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText)));
const SHAPES = [["Rectangle", "rectangle", <rect key="a" x="4" y="8" width="24" height="16" rx="1" />], ["Rounded", "rectangle with rounded corners 4 mm", <rect key="a" x="4" y="8" width="24" height="16" rx="6" />],
  ["Capsule", "capsule", <rect key="a" x="3" y="9" width="26" height="14" rx="7" />], ["Round", "round", <circle key="a" cx="16" cy="16" r="11" />],
  ["Oval", "oval", <ellipse key="a" cx="16" cy="16" rx="13" ry="9" />]];

// Fusion-style workspace: ribbon, browser tree (with comments), circuit diagram and 3D board side by side.
export default function Cad({ c }) {
  const { runId, state, log, svg, P } = c;
  const [notes, setNotes] = useState([]);
  const [rows, setRows] = useState([]);
  const [sel, setSel] = useState(null);
  const [tick, setTick] = useState(0);
  const [note, setNote] = useState("");
  const [custom, setCustom] = useState(false);
  const real = runId !== "__none__";
  const loadNotes = useCallback(() => (real ? fetch(`/api/runs/${runId}/annotations`).then(j).then(setNotes).catch(() => setNotes([])) : setNotes([])), [runId]);
  useEffect(() => { loadNotes(); }, [loadNotes]);
  const d = state?.design, ver = state?.design_version || 0;
  useEffect(() => { if (real && d) fetch(`/api/runs/${runId}/bom2`).then(j).then((x) => setRows(x.rows)).catch(() => setRows([])); }, [runId, ver]);
  const row = sel && rows.find((r) => r.refs.split(" ").includes(sel));

  const addNote = (target, text) => fetch(`/api/runs/${runId}/annotations`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ target, text }) }).then(loadNotes);
  const delNote = (id) => fetch(`/api/runs/${runId}/annotations/${id}`, { method: "DELETE" }).then(loadNotes);
  const reshape = (text) => {
    if (!real) return;
    setNote("Reshaping the board…");
    fetch(`/api/runs/${runId}/shape`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text }) })
      .then(j).then((sp) => { setTick((t) => t + 1); return fetch(`/api/runs/${runId}/board.json`).then(j).then((b) => { const o = b.outline;
        setNote(o ? `${o.width_mm} × ${o.height_mm} mm${o.fits ? "" : " · " + o.problems.join("; ")}` : sp.note); }); })
      .catch((e) => setNote(`Didn't work: ${e.message}`));
  };
  const waiting = <div className="empty small">{!real ? "Describe a device in the assistant and the team will design it." : "The team is working on the first version…"}</div>;
  return (
    <div className="fusion">
      <header className="fribbon">
        <div className="ftabs"><span className="on">DESIGN</span></div>
        <div className="fgroups">
          <div className="fg"><div className="fb">
            <button onClick={() => document.getElementById("ai-input")?.focus()}><svg viewBox="0 0 32 32"><path d="M8 4h11l6 6v18H8z" /><path d="M19 4v6h6M16 14v8M12 18h8" /></svg>New design</button>
            <button onClick={() => setCustom(true)}><svg viewBox="0 0 32 32"><rect x="9" y="9" width="14" height="14" /><path d="M13 5v4M19 5v4M13 23v4M19 23v4M5 13h4M5 19h4M23 13h4M23 19h4" /></svg>Your own chip</button>
          </div><span className="fl">CREATE ▾</span></div>
          <div className="fg"><div className="fb">
            {SHAPES.map(([label, text, shape]) => (
              <button key={label} disabled={!real || !d} onClick={() => reshape(text)} title={`Make the board ${label.toLowerCase()}`}><svg viewBox="0 0 32 32">{shape}</svg>{label}</button>))}
          </div><span className="fl">BOARD SHAPE ▾ {note && <em>{note}</em>}</span></div>
          <div className="fg"><div className="fb">
            <a className={`btn ${!d ? "off" : ""}`} href={d ? `/api/runs/${runId}/bom.csv` : undefined}><svg viewBox="0 0 32 32"><path d="M6 6h20v20H6zM6 12h20M6 18h20M13 6v20" /></svg>Parts list</a>
            <a className={`btn ${!d ? "off" : ""}`} href={d ? `/api/runs/${runId}/schematic.svg` : undefined} target="_blank" rel="noreferrer"><svg viewBox="0 0 32 32"><path d="M6 24l7-12 5 8 3-5 5 9z" /></svg>Diagram</a>
          </div><span className="fl">EXPORT ▾</span></div>
        </div>
      </header>
      <div className="fmain">
        <aside className="fleft">
          <Browser state={state} log={log} rows={rows} sel={sel} setSel={setSel} />
          <Comments notes={notes} sel={sel} selName={row?.name} onAdd={addNote} onDelete={delNote} />
        </aside>
        <div className="split">
          <Pane title="Circuit diagram" kind="sch" runId={runId} notes={notes} reload={loadNotes}>{d && svg ? <P.Schematic svg={svg} /> : waiting}</Pane>
          <Pane title="3D board" kind="3d" runId={runId} notes={notes} reload={loadNotes}>
            {d ? <Guard what="The 3D board"><Board3D runId={runId} designVersion={ver * 1000 + tick + (c.shapeTick || 0)} failing={state.failing_refs || []} /></Guard> : waiting}</Pane>
        </div>
      </div>
      {custom && <CustomParts onClose={() => setCustom(false)} />}
    </div>
  );
}
