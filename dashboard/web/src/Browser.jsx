import React, { useMemo, useState } from "react";

// Plain names for the team and what they did. Raw agent ids and tool names never reach the screen.
const ROLE = { mother: "Lead designer", critic: "Independent reviewer", regulatory: "Regulatory check", sourcing: "Supply check", board_shape: "Board shape",
  power: "Power", analog_fe: "Sensor circuit", mcu_comms: "Microcontroller and radio", protection: "Protection" };
const HOW = { lowest_cost: "lowest cost", robust_sourcing: "easy to buy", lowest_power: "lowest power", medical_conservative: "extra careful" };
const DID = { search_parts: "Browsed parts", find_alternates: "Looked for alternatives", get_subsystem_context: "Read the design rules",
  get_regulation_section: "Read an FDA rule", search_regulations: "Searched FDA rules", classify_device: "Looked up the FDA class",
  run_python: "Ran a calculation", check_supply: "Checked stock", submit_report: "Handed in its options",
  submit_findings: "Handed in its findings", finalize: "Proposed a design", list_subsystems: "Listed the parts of the board",
  delegate_batch: "Handed out the work", delegate: "Handed out the work" };

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
      <div className="bh">BROWSER</div>
      <div className="btree">
        <div className="bgroup">Board{d && <small>{d.blocks.reduce((n, b) => n + b.parts.length, 0)} parts</small>}</div>
        {!d && <div className="bn leaf" style={{ paddingLeft: 26 }}>Nothing yet</div>}
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
        <div className="bgroup">Team{agents.length > 0 && <small>{agents.filter((a) => a.status === "running").length} working</small>}</div>
        {agents.length === 0 && <div className="bn leaf" style={{ paddingLeft: 26 }}>Not started</div>}
        {roots.map((a) => node(a, 0))}
      </div>
    </div>
  );
}

// Comments panel at the foot of the browser, like Fusion: everything people wrote, plus a box for the selected part.
export function Comments({ notes, sel, selName, onAdd, onDelete }) {
  const [t, setT] = useState("");
  const label = (n) => n.target.startsWith("sch@") ? "Circuit diagram" : n.target.startsWith("3d@") ? "Board" : n.target === "design" ? "Whole design" : n.target;
  return (
    <div className="comments">
      <div className="bh">COMMENTS<small>{notes.length}</small></div>
      <div className="clist">
        {notes.length === 0 && <div className="muted">None yet. Use 💬 on a view to pin one, or write below.</div>}
        {notes.map((n) => <div key={n.id} className="cmt"><b>{label(n)}</b> {n.text}<button title="Delete" onClick={() => onDelete(n.id)}>×</button></div>)}
      </div>
      <div className="cadd">
        <input value={t} placeholder={sel ? `Comment on ${selName || sel}…` : "Comment on the design…"} onChange={(e) => setT(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && t.trim()) { onAdd(sel || "design", t); setT(""); } }} />
      </div>
    </div>
  );
}
