import React, { useCallback, useEffect, useMemo, useState } from "react";
import Board3D from "./Board3D.jsx";
import BomPlus from "./BomPlus.jsx";
import Netlist from "./Netlist.jsx";
import Proof from "./Proof.jsx";
import Pane, { Guard } from "./Pane.jsx";
import Console from "./Console.jsx";
import CustomParts from "./CustomParts.jsx";
import "./Cad.css";

const j = (r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText)));
const usd = (n, d = 3) => (n == null ? "?" : `$${Number(n).toFixed(d)}`);
const VIEWS = [["split", "Schematic + 3D"], ["schematic", "Schematic"], ["board", "3D Board"], ["bom", "BOM & Cost"], ["netlist", "Netlist"], ["negotiation", "Negotiation"], ["proof", "Proof"]];

// Fusion/EDA-style workspace: ribbon, browser tree, viewport with document tabs, inspector, console, status bar.
export default function Cad({ c }) {
  const { runId, state, log, svg, sendControl, doExport, bom, P } = c;
  const [tab, setTab] = useState("split");
  const [shapeText, setShapeText] = useState("");
  const [shapeTick, setShapeTick] = useState(0);
  const [shapeNote, setShapeNote] = useState("");
  const [con, setCon] = useState(true);
  const [ribbon, setRibbon] = useState("DESIGN");
  const [sel, setSel] = useState(null);
  const [rows, setRows] = useState([]);
  const [showCustom, setShowCustom] = useState(false);
  const [custom, setCustom] = useState([]);
  const [insp, setInsp] = useState("Properties");
  const [notes, setNotes] = useState([]);
  const [draft, setDraft] = useState("");
  const ver = state?.design_version || 0;

  useEffect(() => { if (state?.design) fetch(`/api/runs/${runId}/bom2`).then(j).then((d) => setRows(d.rows)).catch(() => setRows([])); }, [runId, ver]);
  useEffect(() => { fetch("/api/custom_parts").then(j).then((d) => setCustom(d.parts)).catch(() => {}); }, [showCustom]);
  const loadNotes = useCallback(() => fetch(`/api/runs/${runId}/annotations`).then(j).then(setNotes).catch(() => {}), [runId]);
  useEffect(() => { loadNotes(); }, [loadNotes]);

  const byRef = useMemo(() => { const m = {}; rows.forEach((r) => r.refs.split(" ").forEach((x) => (m[x] = r))); return m; }, [rows]);
  const row = sel ? byRef[sel] : null;
  const d = state?.design;
  const status = !state ? "connecting" : state.done ? (state.done.ok ? "complete" : "stopped") : "running";
  const t0 = log.length ? log[0].ts : null;
  const addNote = () => { if (!draft.trim()) return; fetch(`/api/runs/${runId}/annotations`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ target: sel || "design", text: draft }) }).then(() => { setDraft(""); loadNotes(); }); };

  const applyShape = () => {
    if (!shapeText.trim()) return;
    setShapeNote("Board-shape agent working…");
    fetch(`/api/runs/${runId}/shape`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: shapeText }) })
      .then(j).then((sp) => { setShapeTick((t) => t + 1); return fetch(`/api/runs/${runId}/board.json`).then(j).then((b) => {
        const o = b.outline; setShapeNote(o ? `${sp.kind.replace("_", " ")}: board ${o.width_mm} × ${o.height_mm} mm${o.fits ? "" : " (" + o.problems.join("; ") + ")"}` : sp.note); }); })
      .catch((e) => setShapeNote(`Shape failed: ${e.message}`));
  };
  const board3d = d ? <Guard what="The 3D board"><Board3D runId={runId} designVersion={ver * 1000 + shapeTick} failing={state.failing_refs || []} /></Guard> : <div className="empty small">Waiting for the first design…</div>;
  const split = (
    <div className="split">
      <Pane title="Schematic" kind="sch" runId={runId} notes={notes} reload={loadNotes}><P.Schematic svg={svg} /></Pane>
      <Pane title="3D board" kind="3d" runId={runId} notes={notes} reload={loadNotes}>{board3d}</Pane>
    </div>
  );
  const view = tab === "split" ? split : tab === "schematic" ? <P.Schematic svg={svg} />
    : tab === "board" ? board3d
    : tab === "bom" ? <BomPlus runId={runId} designVersion={ver} />
    : tab === "proof" ? <Proof runId={runId} />
    : tab === "netlist" ? <Netlist runId={runId} version={ver} sel={sel} onSel={setSel} />
    : <P.Negotiation entries={state?.negotiation || []} agents={state?.agents || []} replans={state?.replans || []} />;

  return (
    <div className="cad">
      <header className="ribbon">
        <div className="rtop">
          <span className={`pill ${status}`}>{status}</span>
          <b className="doc">{d?.name || "Untitled design"}</b>
          <span className="muted">v{ver} · {runId}</span>
          <nav className="rtabs">{["DESIGN", "VALIDATE", "OUTPUT"].map((t) => <button key={t} className={ribbon === t ? "on" : ""} onClick={() => setRibbon(t)}>{t}</button>)}</nav>
        </div>
        <div className="rbody">
          {ribbon === "DESIGN" && (<>
            <div className="rgroup"><span className="gl">VIEW</span><div>{VIEWS.map(([k, l]) => <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{l}</button>)}</div></div>
            <div className="rgroup"><span className="gl">BOARD SHAPE (agent)</span><div>
              <input className="shape-in" value={shapeText} placeholder="e.g. round 38 mm, capsule wristband pod" onChange={(e) => setShapeText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && applyShape()} />
              <button onClick={applyShape}>Apply</button>{shapeNote && <span className="muted shape-note">{shapeNote}</span>}</div></div>
            <div className="rgroup"><span className="gl">PARTS</span><div><button onClick={() => setShowCustom(true)}>＋ Custom MCU / chip</button></div></div>
          </>)}
          {ribbon === "VALIDATE" && (<>
            <div className="rgroup"><span className="gl">CONSTRAINT CHANGE</span><P.Controls bom={bom} onSend={sendControl} onExport={doExport} disabled={!runId} /></div>
            <div className="rgroup"><span className="gl">CHECKS</span><div><button onClick={() => setInsp("Checks")}>Open results</button></div></div>
          </>)}
          {ribbon === "OUTPUT" && (
            <div className="rgroup"><span className="gl">EXPORT</span><div>
              <button className="primary" onClick={doExport}>Export outputs</button>
              <a className="btn" href={`/api/runs/${runId}/bom.csv`}>BOM (csv)</a>
              <a className="btn" href={`/api/runs/${runId}/netlist.v`} target="_blank" rel="noreferrer">Netlist (.v)</a>
              <a className="btn" href={`/api/runs/${runId}/schematic.svg`} target="_blank" rel="noreferrer">Schematic (svg)</a>
            </div></div>)}
        </div>
      </header>

      <div className="cad-main">
        <aside className="cbrowser">
          <div className="ph">BROWSER</div>
          <details open><summary>Design {d && <small>{d.blocks.reduce((n, b) => n + b.parts.length, 0)} parts</small>}</summary>
            {!d && <div className="muted pad">Waiting for the first design…</div>}
            {d?.blocks.map((b) => (
              <details key={b.subsystem} open className="sub"><summary>{b.subsystem} <small>{b.parts.length}</small></summary>
                {b.parts.map((p) => (
                  <div key={p.ref} className={`node ${sel === p.ref ? "on" : ""} ${(state.failing_refs || []).includes(p.ref) ? "bad" : ""}`} onClick={() => { setSel(p.ref); setInsp("Properties"); }}>
                    <code>{p.ref}</code><span>{byRef[p.ref]?.name || p.mpn}</span>
                  </div>))}
              </details>))}
          </details>
          <details><summary>Agents <small>{state?.agents?.length || 0}</small></summary><div className="pad"><P.AgentTree agents={state?.agents || []} /></div></details>
          <details><summary>Custom parts <small>{custom.length}</small></summary>
            <div className="pad">{custom.map((p) => <div key={p.mpn} className="node"><code>{p.category}</code><span>{p.mpn}</span></div>)}
              <button onClick={() => setShowCustom(true)}>＋ Add</button></div></details>
        </aside>

        <section className="cview">
          <div className="doctabs">{VIEWS.map(([k, l]) => <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{l}{k === "negotiation" && state?.negotiation?.length ? ` (${state.negotiation.length})` : ""}</button>)}</div>
          <div className="vp">{view}</div>
        </section>

        <aside className="cinspect">
          <div className="ph">INSPECTOR</div>
          <div className="itabs">{["Properties", "Comments", "Checks", "Requirements"].map((t) => <button key={t} className={insp === t ? "on" : ""} onClick={() => setInsp(t)}>{t}</button>)}</div>
          <div className="ibody">
            {insp === "Properties" && (row ? (
              <div>
                <h4>{row.name}</h4><div className="muted">{row.subtitle}</div>
                <table className="kv"><tbody>
                  <tr><td>Reference</td><td>{sel}{row.qty > 1 ? ` (+${row.qty - 1} like it)` : ""}</td></tr>
                  <tr><td>Part number</td><td><code>{row.mpn}</code></td></tr>
                  <tr><td>Subsystem</td><td>{row.subsystem}</td></tr>
                  <tr><td>Unit price</td><td>{usd(row.unit_usd)}{row.price_known ? "" : " (unknown)"}</td></tr>
                  <tr><td>@100 / @1k boards</td><td>{usd(row.at_volume?.["100"])} / {usd(row.at_volume?.["1000"])}</td></tr>
                  <tr><td>Stock</td><td>{row.stock_qty ?? row.stock ?? "?"} <span className="muted">{row.checked_at || ""}</span></td></tr>
                  <tr><td>Source</td><td className="muted">{row.source}</td></tr>
                </tbody></table>
                <div className="links">{Object.entries(row.links || {}).map(([k, v]) => <a key={k} href={v} target="_blank" rel="noreferrer">{k}</a>)}</div>
              </div>
            ) : (
              <div>
                <h4>{d?.name || "Design"}</h4>
                <table className="kv"><tbody>
                  <tr><td>Version</td><td>{ver}</td></tr>
                  <tr><td>Parts</td><td>{d ? d.blocks.reduce((n, b) => n + b.parts.length, 0) : 0}</td></tr>
                  <tr><td>Nets</td><td>{d ? new Set(d.blocks.flatMap((b) => b.nets.map((n) => n.name))).size : 0}</td></tr>
                  <tr><td>Open pins</td><td>{d?.no_connects?.length ?? 0} marked no-connect</td></tr>
                </tbody></table>
                <div className="muted pad">Select a part in the browser or click a reference in the netlist.</div>
              </div>))}
            {insp === "Properties" && (
              <div className="notes"><h5>Notes {sel ? `on ${sel}` : "on design"}</h5>
                {notes.filter((n) => n.target === (sel || "design")).map((n) => <div key={n.id} className="note">{n.text}</div>)}
                <div className="addnote"><input value={draft} placeholder="Add a note…" onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addNote()} /><button onClick={addNote}>Add</button></div>
              </div>)}
            {insp === "Comments" && (<div>
              {notes.length === 0 && <div className="muted">No comments yet. Use 💬 Comment on a pane and click to drop a pin.</div>}
              {notes.map((n) => <div key={n.id} className="note"><b>{n.target.includes("@") ? `${n.target.startsWith("sch") ? "Schematic" : "3D"} pin` : n.target}</b> · {n.text}</div>)}</div>)}
            {insp === "Checks" && (<><P.Verifiers verifiers={state?.verifiers || {}} />{state?.rules?.length > 0 && <div className="mwrap rules"><P.Rules rules={state.rules} /></div>}</>)}
            {insp === "Requirements" && <P.Matrix reqs={state?.requirements || []} />}
          </div>
        </aside>
      </div>

      <section className={`cconsole ${con ? "" : "closed"}`}><Console events={log} t0={t0} /></section>
      <footer className="cstatus">
        <span>{status === "running" ? "● Running" : status === "complete" ? "✓ Complete" : status}</span>
        <span>{state?.agents?.length || 0} agents</span>
        <span>{state?.n_events || 0} events</span>
        <span className="grow"><P.Budgets totals={state?.totals} budgets={state?.budgets} /></span>
        <span className="tog" onClick={() => setCon(!con)}>{con ? "Hide console" : "Show console"}</span>
        <span>{sel ? `Selected ${sel}` : "Nothing selected"}</span>
      </footer>
      {showCustom && <CustomParts onClose={() => setShowCustom(false)} />}
    </div>
  );
}
