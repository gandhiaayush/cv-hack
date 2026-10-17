import React, { useMemo, useState } from "react";

// Plain names for the team and what they did. Raw agent ids and tool names never reach the screen.
const ROLE = { mother: "Lead Designer", critic: "Independent Reviewer", regulatory: "Regulatory Check", sourcing: "Supply Check", board_shape: "Board Shape",
  power: "Power", analog_fe: "Sensor Circuit", mcu_comms: "Microcontroller And Radio", protection: "Protection" };
const HOW = { lowest_cost: "Lowest Cost", robust_sourcing: "Easy To Buy", lowest_power: "Lowest Power", medical_conservative: "Extra Careful" };
const DID = { search_parts: "Browsed Parts", find_alternates: "Looked For Alternatives", get_subsystem_context: "Read The Design Rules",
  get_regulation_section: "Read An FDA Rule", search_regulations: "Searched FDA Rules", classify_device: "Looked Up The FDA Class",
  run_python: "Ran A Calculation", check_supply: "Checked Stock", submit_report: "Handed In Its Options",
  submit_findings: "Handed In Its Findings", finalize: "Proposed A Design", list_subsystems: "Listed The Parts Of The Board",
  delegate_batch: "Handed Out The Work", delegate: "Handed Out The Work" };

export const name = (a) => `${ROLE[a.role_key] || ROLE[a.label?.replace(" ", "_")] || a.label}${a.strategy ? ` · ${HOW[a.strategy] || a.strategy}` : ""}`;
const toolOf = (msg) => (/^(\w+)\s*(\(|→)/.exec(msg || "") || [])[1];

const Icon = ({ s }) => s === "running" ? <span className="spin" /> : s === "failed" ? <span className="x">✕</span> : <span className="ok">✓</span>;

// Browser tree: the design by part group, then the team and what each member did, in plain words.
export default function Browser({ state, log, rows, sel, setSel }) {
  const [open, setOpen] = useState({});
  const byRef = useMemo(() => { const m = {}; rows.forEach((r) => r.refs.split(" ").forEach((x) => (m[x] = r))); return m; }, [rows]);
  const steps = useMemo(() => {
    const m = {};
    log.filter((e) => e.kind === "step").forEach((e) => {
      const t = toolOf(e.payload.message); if (!t || !DID[t]) return;
      const list = (m[e.agent_id] = m[e.agent_id] || []); const last = list[list.length - 1];
      if (last && last.t === t && !/→/.test(e.payload.message)) last.n += 1; else if (!/→/.test(e.payload.message)) list.push({ t, n: 1 });
    });
    return m;
  }, [log]);
  const d = state?.design;
  // internal bookkeeping actors (the one-shot comparison, bare ids without a run suffix) are not part of the team
  const agents = (state?.agents || []).filter((a) => a.id === "critic" || (a.id.includes(":") && a.id.split(":")[0] !== "baseline"));
  const roots = agents.filter((a) => !a.parent || !agents.some((x) => x.id === a.parent));
  const kids = (id) => agents.filter((a) => a.parent === id);
  const node = (a, depth) => {
    const k = kids(a.id), st = steps[a.id] || [], isOpen = open[a.id] ?? (a.status === "running" && depth > 0 && st.length > 0);
    return (
      <div key={a.id}>
        <div className="bn" style={{ paddingLeft: 10 + depth * 16 }} onClick={() => setOpen({ ...open, [a.id]: !isOpen })}>
          <span className="car">{k.length || st.length ? (isOpen ? "▾" : "▸") : ""}</span><Icon s={a.status} />
          <span className="lbl">{ROLE[a.id.split(":")[0]] || a.label}{a.strategy ? <small> · {HOW[a.strategy] || a.strategy}</small> : null}</span>
        </div>
        {isOpen && st.map((x, i) => <div key={i} className="bn leaf" style={{ paddingLeft: 38 + depth * 16 }}>{DID[x.t]}{x.n > 1 ? ` ×${x.n}` : ""}</div>)}
        {isOpen && k.map((c) => node(c, depth + 1))}
      </div>
    );
  };
  return (
    <div className="browser">
      <div className="bh">Browser</div>
      <div className="btree">
        <div className="bgroup">Board{d && <small>{d.blocks.reduce((n, b) => n + b.parts.length, 0)} Parts</small>}</div>
        {!d && <div className="bn leaf" style={{ paddingLeft: 26 }}>Nothing Yet</div>}
        {d?.blocks.map((b) => (
          <div key={b.subsystem}>
            <div className="bn" style={{ paddingLeft: 12 }} onClick={() => setOpen({ ...open, ["b" + b.subsystem]: !open["b" + b.subsystem] })}>
              <span className="car">{open["b" + b.subsystem] ? "▾" : "▸"}</span><span className="lbl">{ROLE[b.subsystem] || b.subsystem}<small> · {b.parts.length}</small></span>
            </div>
            {open["b" + b.subsystem] && b.parts.map((p) => (
              <div key={p.ref} className={`bn leaf part ${sel === p.ref ? "on" : ""}`} style={{ paddingLeft: 40 }} onClick={() => setSel(sel === p.ref ? null : p.ref)}>
                <code>{p.ref}</code> {byRef[p.ref]?.name?.replace(/^\S+ /, "") || p.mpn}
              </div>))}
          </div>))}
        <div className="bgroup">Team{agents.length > 0 && <small>{agents.filter((a) => a.status === "running").length} Working</small>}</div>
        {agents.length === 0 && <div className="bn leaf" style={{ paddingLeft: 26 }}>Not Started</div>}
        {roots.map((a) => node(a, 0))}
      </div>
    </div>
  );
}

// Comments panel at the foot of the browser, like Fusion: everything people wrote, plus a box for the selected part.
export function Comments({ notes, sel, selName, onAdd, onDelete }) {
  const [t, setT] = useState("");
  const label = (n) => n.target.startsWith("sch@") ? "Circuit Diagram" : n.target.startsWith("3d@") ? "Board" : n.target === "design" ? "Whole Design" : n.target;
  return (
    <div className="comments">
      <div className="bh">Comments<small>{notes.length}</small></div>
      <div className="clist">
        {notes.length === 0 && <div className="muted">None Yet. Use 💬 On A View To Pin One, Or Write Below.</div>}
        {notes.map((n) => <div key={n.id} className="cmt"><b>{label(n)}</b> {n.text}<button title="Delete" onClick={() => onDelete(n.id)}>×</button></div>)}
      </div>
      <div className="cadd">
        <input value={t} placeholder={sel ? `Comment On ${selName || sel}…` : "Comment On The Design…"} onChange={(e) => setT(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && t.trim()) { onAdd(sel || "design", t); setT(""); } }} />
      </div>
    </div>
  );
}
