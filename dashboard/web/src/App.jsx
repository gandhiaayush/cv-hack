import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";
import Board3D from "./Board3D.jsx";
import BomPlus from "./BomPlus.jsx";
import Cad from "./Cad.jsx";
import { DesignNote, NegotiationBoard, Overview, clip, shortName, subLabel } from "./views.jsx";

const STATUS_LABEL = { running: "running", waiting: "waiting", done: "done", failed: "failed" };

const TABS = [["overview", "Overview"], ["negotiation", "Negotiation"], ["schematic", "Schematic"], ["board", "3D Board"], ["bom", "BOM & Cost"]];

export default function App({ fixedRunId, shapeTick } = {}) {
  const none = fixedRunId === "__none__";
  const [runs, setRuns] = useState([]);
  const [runId, setRunId] = useState(fixedRunId || null);
  const [follow, setFollow] = useState(!fixedRunId);
  useEffect(() => { if (fixedRunId) setRunId(fixedRunId); }, [fixedRunId]);  // the app finds the latest run after it loads
  const [state, setState] = useState(null);
  const [log, setLog] = useState([]);
  const [svg, setSvg] = useState(null);
  const [bom, setBom] = useState([]);
  const [tab, setTabState] = useState(() => (TABS.some(([t]) => t === location.hash.slice(1)) ? location.hash.slice(1) : "overview"));
  const setTab = (t) => { setTabState(t); history.replaceState(null, "", `#${t}`); };
  const [toast, setToast] = useState(null);
  const refetchTimer = useRef(null);
  const lastDesignKey = useRef("");

  // Run list; in follow mode, jump to the newest run automatically.
  useEffect(() => {
    let alive = true;
    const tick = () =>
      api.runs().then((rs) => {
        if (!alive) return;
        setRuns(rs);
        if (rs.length && (follow || !runId)) setRunId((cur) => (follow ? rs[0].run_id : cur || rs[0].run_id));
      }).catch(() => {});
    tick();
    const t = setInterval(tick, 3000);
    return () => { alive = false; clearInterval(t); };
  }, [follow, runId]);

  const refresh = useCallback((id) => {
    api.state(id).then((s) => {
      setState(s);
      const key = `${s.design_version}:${Object.values(s.verifiers || {}).map((v) => v.ok + ":" + v.ts).join(",")}`;
      if (key !== lastDesignKey.current) {
        lastDesignKey.current = key;
        api.schematic(id).then(setSvg);
        api.bom(id).then(setBom).catch(() => setBom([]));
      }
    }).catch(() => {});
  }, []);

  // Live stream: append to the log and refetch folded state (throttled).
  useEffect(() => {
    if (!runId || none) return;
    setLog([]); setState(null); setSvg(null); lastDesignKey.current = "";
    refresh(runId);
    const es = api.events(runId);
    es.onmessage = (m) => {
      const e = JSON.parse(m.data);
      setLog((l) => (l.length > 400 ? [...l.slice(-300), e] : [...l, e]));
      clearTimeout(refetchTimer.current);
      refetchTimer.current = setTimeout(() => refresh(runId), 150);
    };
    return () => es.close();
  }, [runId, refresh]);

  const flash = (msg) => { setToast(msg); setTimeout(() => setToast(null), 3500); };
  const sendControl = (body) =>
    api.control(runId, body).then(() => flash(`Sent: ${body.type} ${body.mpn || body.value || ""}`))
      .catch((e) => flash(`Failed: ${e.message}`));
  const doExport = () =>
    api.exportRun(runId).then((r) => flash(`Wrote ${r.files.length} files to runs/${runId}/out`))
      .catch((e) => flash(`Export failed: ${e.message}`));

  const running = state && !state.done;
  if (fixedRunId) {
    return <Cad c={{ runId, state, log, svg, bom, shapeTick, sendControl, doExport,
      P: { Schematic } }} />;
  }
  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className={`dot ${running ? "live" : state?.done?.ok ? "ok" : ""}`} />
          <h1>Agent PCB designer</h1>
          <select value={runId || ""} onChange={(e) => { setFollow(false); setRunId(e.target.value); }}>
            {runs.map((r) => <option key={r.run_id} value={r.run_id}>{r.run_id}</option>)}
          </select>
          <label className="follow"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow latest</label>
          {(() => {
            const open = state?.done?.ok && (state?.verifiers?.critic?.errors || []).some((e) => e.severity === "error");
            const cls = running ? "live" : open ? "warn" : state?.done?.ok ? "ok" : state?.done ? "bad" : "";
            const txt = running ? "running" : open ? "accepted · blockers open" : state?.done ? (state.done.ok ? "accepted" : "failed") : "—";
            return <span className={`hpill ${cls}`}>{txt}</span>;
          })()}
        </div>
        <Budgets totals={state?.totals} budgets={state?.budgets} />
        <Controls bom={bom} onSend={sendControl} onExport={doExport} disabled={!runId} />
      </header>

      {!runId ? (
        <div className="empty">
          No runs yet. Start one with <code>python -m cvhack.mocks.fake_run</code> or the real mother agent.
        </div>
      ) : (
        <main className="grid">
          <section className="panel tree">
            <h2>Agents <small>{state?.agents?.length || 0}</small></h2>
            <AgentTree agents={state?.agents || []} />
            <Verifiers verifiers={state?.verifiers || {}} />
            <Inference models={state?.inference || []} />
          </section>

          <section className="panel center">
            <div className="tabs">
              {TABS.map(([t, label]) => (
                <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{label}</button>
              ))}
              <span className="spacer" />
              {state?.design && <span className="muted">{state.design.name} · v{state.design_version}</span>}
            </div>
            <Changes commands={state?.commands || []} />
            {(tab === "schematic" || tab === "board") && <DesignNote state={state} />}
            {tab === "overview" ? <Overview state={state} />
              : tab === "negotiation" ? <NegotiationBoard state={state} />
              : tab === "schematic" ? <Schematic svg={svg} />
              : tab === "board" ? (state?.design ? <Board3D runId={runId} designVersion={state.design_version} failing={state.failing_refs || []} />
                                                 : <div className="empty small">Waiting for the first design…</div>)
              : <BomPlus runId={runId} designVersion={state?.design_version} />}
          </section>

          <section className="panel right">
            {state?.requirements?.length > 0 && (<>
              <h2>Requirements
                <small>{state.requirements.filter((r) => r.status === "pass").length}/{state.requirements.length}</small>
                <a className="dl" href={`/api/runs/${runId}/matrix.csv`}>csv</a>
              </h2>
              <div className="mwrap"><Matrix reqs={state.requirements} /></div>
            </>)}
            {state?.rules?.length > 0 && (
              <>
                <h2>Design rules
                  <small>{state.rules.filter((r) => r.status === "pass").length}/{state.rules.filter((r) => r.status !== "not_applicable").length}</small>
                </h2>
                <div className="mwrap rules"><Rules rules={state.rules} /></div>
              </>
            )}
            <h2>Run log <small>{state?.n_events || 0} events</small></h2>
            <Log events={log} />
          </section>
        </main>
      )}

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function Budgets({ totals, budgets }) {
  if (!totals || !budgets) return <div className="budgets" />;
  const items = [["power_mw", "Power", "mW"], ["cost_usd", "Cost", "$"], ["area_mm2", "Area", "mm²"]];
  return (
    <div className="budgets">
      {items.map(([k, label, unit]) => {
        const pct = budgets[k] ? totals[k] / budgets[k] : 0;
        return (
          <div key={k} className={`meter ${pct > 1 ? "over" : pct > 0.85 ? "warn" : ""}`}>
            <div className="meter-head"><span>{label}</span><b>{fmt(totals[k])}{unit === "$" ? "" : " " + unit} / {fmt(budgets[k])}{unit === "$" ? " $" : ""}</b></div>
            <div className="bar"><div style={{ width: `${Math.min(pct, 1) * 100}%` }} /></div>
          </div>
        );
      })}
    </div>
  );
}

function Controls({ bom, onSend, onExport, disabled }) {
  const [mpn, setMpn] = useState("");
  const ics = bom.filter((r) => !/^[RC]\d/.test(r.refs));
  return (
    <div className="controls">
      <select value={mpn} onChange={(e) => setMpn(e.target.value)} disabled={disabled}>
        <option value="">part…</option>
        {ics.map((r) => <option key={r.mpn} value={r.mpn}>{r.refs.split(" ")[0]} · {r.mpn}</option>)}
      </select>
      <button disabled={disabled || !mpn} onClick={() => onSend({ type: "out_of_stock", mpn })}>Mark out of stock</button>
      <button disabled={disabled} onClick={() => onSend({ type: "device_class", value: "III" })}>Raise to Class III</button>
      <button className="primary" disabled={disabled} onClick={onExport}>Export outputs</button>
    </div>
  );
}

function AgentTree({ agents }) {
  const children = useMemo(() => {
    const m = {};
    agents.forEach((a) => { (m[a.parent || "__root"] ||= []).push(a); });
    return m;
  }, [agents]);
  // Re-delegated agents share a label; number the repeats ("Power · round 2").
  const round = useMemo(() => {
    const seen = {}, out = {};
    agents.forEach((a) => { const k = `${a.parent}|${a.label}`; seen[k] = (seen[k] || 0) + 1; out[a.id] = seen[k]; });
    return out;
  }, [agents]);
  const render = (a, depth) => (
    <li key={a.id}>
      <div className={`agent ${a.status} role-${a.role}`}>
        <span className={`sdot ${a.status}`} title={STATUS_LABEL[a.status] || a.status} />
        <span className="alabel">{a.role === "subsystem" ? subLabel(a.label.replace(/ /g, "_")) : a.label}{round[a.id] > 1 && <span className="muted"> · round {round[a.id]}</span>}</span>
        {a.model && <span className="amodel" title={a.model}>{shortModel(a.model)}</span>}
        {a.steps > 0 && <span className="steps">{a.steps}</span>}
      </div>
      {a.last && <div className="alast" title={a.last}>{clip(a.last, 60)}</div>}
      {children[a.id] && <ul>{children[a.id].map((c) => render(c, depth + 1))}</ul>}
    </li>
  );
  return <ul className="atree">{(children.__root || []).map((a) => render(a, 0))}</ul>;
}

function Verifiers({ verifiers }) {
  const vs = Object.values(verifiers);
  if (!vs.length) return null;
  return (
    <div className="verifiers">
      <h2>Checks</h2>
      {vs.map((v) => (
        <details key={v.verifier} className={`ver ${v.ok ? "ok" : "bad"}`}>
          <summary><span>{v.ok ? "✓" : "✗"}</span> {verifierName(v.verifier)} <small>{v.errors?.length ? `${v.errors.length} issue${v.errors.length > 1 ? "s" : ""}` : "pass"}</small></summary>
          {v.errors?.slice(0, 8).map((e, i) => (
            <div key={i} className={`err ${e.severity}`} title={e.message}><code>{e.code.replace(/^CRITIC_/, "")}</code> {clip(e.message, 120)}</div>
          ))}
        </details>
      ))}
    </div>
  );
}

function Schematic({ svg }) {
  const [zoom, setZoom] = useState(1);
  if (!svg) return <div className="empty small">Waiting for the first schematic block…</div>;
  return (
    <div className="sch-wrap">
      <div className="zoom">
        <button onClick={() => setZoom((z) => Math.max(0.5, z - 0.25))}>−</button>
        <span>{Math.round(zoom * 100)}%</span>
        <button onClick={() => setZoom((z) => Math.min(4, z + 0.25))}>+</button>
      </div>
      <div className="sch" style={{ width: `${zoom * 100}%` }} dangerouslySetInnerHTML={{ __html: svg }} />
    </div>
  );
}

function Bom({ rows, runId }) {
  const total = rows.reduce((s, r) => s + (r.ext_usd ?? 0), 0);
  const unpriced = rows.filter((r) => r.ext_usd == null).length;
  return (
    <div className="bom">
      <table>
        <thead><tr><th>Refs</th><th>Qty</th><th>MPN</th><th>Description</th><th className="num">Unit</th><th className="num">Ext</th><th className="num">Stock</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.mpn} className={r.stock != null && r.stock < r.qty ? "oos" : ""}>
              <td>{r.refs}</td><td>{r.qty}</td><td><code>{r.mpn}</code></td><td>{r.description}</td>
              <td className="num">{money(r.unit_usd, 3)}</td><td className="num">{money(r.ext_usd, 2)}</td><td className="num">{r.stock ?? <span className="muted" title="stock not checked">?</span>}</td>
            </tr>
          ))}
        </tbody>
        <tfoot><tr><td colSpan={5}>Total{unpriced ? <span className="muted"> ({unpriced} line{unpriced > 1 ? "s" : ""} unpriced)</span> : null}</td><td className="num">${total.toFixed(2)}</td><td><a href={`/api/runs/${runId}/bom.csv`}>csv</a></td></tr></tfoot>
      </table>
    </div>
  );
}

function Matrix({ reqs }) {
  if (!reqs.length) return <div className="empty small">No requirements yet</div>;
  return (
    <table className="matrix">
      <tbody>
        {reqs.map((r) => (
          <tr key={r.req_id} className={r.status}>
            <td><span className={`chip ${r.status}`}>{r.status}</span></td>
            <td><b>{r.req_id}</b> {r.text}
              {(r.parts?.length > 0 || r.rule_ids?.length > 0) && (
                <div className="trace">{r.parts?.join(" ")}{r.rule_ids?.length ? ` · ${r.rule_ids.join(" ")}` : ""}</div>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Changes({ commands }) {
  if (!commands.length) return null;
  return (
    <div className="changes">
      {commands.slice(-4).map((c, i) => (
        <span key={i} className={`change ${c.applied ? "applied" : "pending"}`}>
          <b>{c.type === "out_of_stock" ? `${c.mpn} out of stock` : c.type === "device_class" ? `Class ${c.value}` : c.type}</b>
          {c.applied ? " · agents re-planning" : " · sent, waiting for agents"}
        </span>
      ))}
    </div>
  );
}

const shortModel = (m) => (m || "").split("/").pop().replace(/^NVIDIA-/, "").replace(/-A\d+B$/, "");

function Inference({ models }) {
  if (!models.length) return null;
  const calls = models.reduce((s, m) => s + m.calls, 0);
  const tok = models.reduce((s, m) => s + m.prompt_tokens + m.completion_tokens, 0);
  return (
    <div className="inference">
      <h2>Inference <small>{models[0].provider || "Crusoe"} · {calls} calls · {fmtTok(tok)} tokens</small></h2>
      {models.map((m) => (
        <div key={m.model} className="imodel">
          <div className="iname" title={m.model}>{shortModel(m.model)}</div>
          <div className="istats muted small">
            {m.calls} calls · {fmtTok(m.prompt_tokens)} in / {fmtTok(m.completion_tokens)} out · avg {(m.latency_s / Math.max(m.calls, 1)).toFixed(1)}s
          </div>
          <div className="muted small">{m.agents.join(", ")}</div>
        </div>
      ))}
    </div>
  );
}

const fmtTok = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : `${n}`);

const RULE_CHIP = { pass: "pass", fail: "fail", indeterminate: "fail", not_applicable: "pending" };

function Rules({ rules }) {
  return (
    <table className="matrix">
      <tbody>
        {rules.map((r) => (
          <tr key={r.rule_id} className={RULE_CHIP[r.status]}>
            <td><span className={`chip ${RULE_CHIP[r.status]}`} title={r.status}>{r.status === "indeterminate" ? "unknown" : r.status === "not_applicable" ? "n/a" : r.status}</span></td>
            <td><b>{r.rule_id}</b> {r.title}
              <div className="trace">{r.refs.join(" ")}</div>
              {r.messages?.length > 0 && <div className="trace bad">{r.messages.slice(0, 3).join("; ")}</div>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const LOG_KINDS = new Set(["step", "replan", "verifier_result", "constraint_change", "run_done",
  "delegate", "report", "decision"]);

function Log({ events }) {
  const ref = useRef(null);
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [events]);
  return (
    <div className="log" ref={ref}>
      {events.filter((e) => LOG_KINDS.has(e.kind)).map((e, i) => (
        <div key={i} className={`ev k-${e.kind} ${e.kind === "verifier_result" ? (e.payload.ok ? "ok" : "bad") : ""}`}>
          <span className="t">{new Date(e.ts * 1000).toLocaleTimeString([], { hour12: false })}</span>
          <span className="who">{String(e.agent_id).split(":")[0].replace("verifier.", "")}</span>
          <span className="msg">{describe(e)}</span>
        </div>
      ))}
    </div>
  );
}

function describe(e) {
  const p = e.payload;
  switch (e.kind) {
    case "step": return clip(p.message, 110);
    case "agent_spawned": return `spawned ${p.label || ""}`;
    case "replan": return `re-plan: ${clip(p.reason, 80)}`;
    case "verifier_result": return `${verifierName(p.verifier)}: ${p.ok ? "pass" : `${(p.errors || []).length} issue(s)`}`;
    case "constraint_change": return `CONSTRAINT: ${Object.entries(p).filter(([k]) => k !== "ts").map(([k, v]) => `${k}=${v}`).join(" ")}`;
    case "run_done": return `${p.ok ? "done" : "failed"}: ${clip(p.summary, 90)}`;
    case "delegate": return `asked ${subLabel(p.subsystem || p.to)}`;
    case "report": return `reported ${p.options?.length || 0} option(s)`;
    case "decision": return `chose for ${subLabel(String(p.to).split(":")[0])}: ${shortName(p.chosen || p.message || "", 40)}`;
    default: return e.kind;
  }
}

const verifierName = (v) => String(v).startsWith("gate:") ? `gate · ${subLabel(v.slice(5))}` : String(v).replace(/_/g, " ");
const fmtBudget = (b) => (b ? Object.entries(b).map(([k, v]) => `${k}=${v}`).join(" ") : "");
const money = (n, d) => (n == null ? <span className="muted" title="price unknown">?</span> : n.toFixed(d));
const fmt = (n) => (n == null ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 }));
