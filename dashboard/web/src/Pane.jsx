import React, { useRef, useState } from "react";

// One failing view (e.g. no WebGL for the 3D board) must not blank the whole workspace.
export class Guard extends React.Component {
  state = { err: null };
  static getDerivedStateFromError(err) { return { err }; }
  render() {
    return this.state.err
      ? <div className="empty small" style={{ padding: 20 }}>{this.props.what || "This view"} could not start in this browser ({String(this.state.err.message || this.state.err).slice(0, 120)}). Try Chrome with hardware acceleration on.</div>
      : this.props.children;
  }
}

// A viewport pane with pin-drop comments. Pins are stored as annotations with target "<kind>@x,y" (fractions of the
// pane). In comment mode a click drops a pin; otherwise the pane behaves normally (e.g. orbiting the 3D board).
export default function Pane({ title, kind, runId, notes, reload, children, right }) {
  const [on, setOn] = useState(false);
  const [draft, setDraft] = useState(null);   // {x, y}
  const [text, setText] = useState("");
  const [open, setOpen] = useState(null);
  const box = useRef(null);
  const mine = notes.filter((n) => n.target.startsWith(kind + "@"));
  const pos = (n) => n.target.slice(kind.length + 1).split(",").map(Number);
  const click = (e) => {
    const r = box.current.getBoundingClientRect();
    setDraft({ x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height }); setText(""); setOpen(null);
  };
  const save = () => {
    if (!text.trim() || !draft) return;
    fetch(`/api/runs/${runId}/annotations`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ target: `${kind}@${draft.x.toFixed(3)},${draft.y.toFixed(3)}`, text }) })
      .then(() => { setDraft(null); setText(""); reload(); });
  };
  const del = (id) => fetch(`/api/runs/${runId}/annotations/${id}`, { method: "DELETE" }).then(() => { setOpen(null); reload(); });
  return (
    <div className="pane">
      <div className="pane-h"><b>{title}</b><span className="grow" />{right}
        <button className={on ? "on" : ""} onClick={() => { setOn(!on); setDraft(null); }}>💬 Comment{mine.length ? ` (${mine.length})` : ""}</button></div>
      <div className="pane-b" ref={box}>
        {children}
        {on && <div className="catcher" onClick={click} />}
        {mine.map((n, i) => { const [x, y] = pos(n); return (
          <div key={n.id} className="pin" style={{ left: `${x * 100}%`, top: `${y * 100}%` }} onClick={(e) => { e.stopPropagation(); setOpen(open === n.id ? null : n.id); }}>
            <span>{i + 1}</span>
            {open === n.id && <div className="pin-pop" onClick={(e) => e.stopPropagation()}>{n.text}<button onClick={() => del(n.id)}>Delete</button></div>}
          </div>); })}
        {draft && (
          <div className="pin draft" style={{ left: `${draft.x * 100}%`, top: `${draft.y * 100}%` }}>
            <span>+</span>
            <div className="pin-pop" onClick={(e) => e.stopPropagation()}>
              <input autoFocus value={text} placeholder="Comment…" onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") setDraft(null); }} />
              <button onClick={save}>Post</button>
            </div>
          </div>)}
      </div>
    </div>
  );
}
