import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";

const STATUS_LABEL = { running: "running", waiting: "waiting", done: "done", failed: "failed" };

export default function App() {
  const [runs, setRuns] = useState([]);
  const [runId, setRunId] = useState(null);
  const [follow, setFollow] = useState(true);
  const [state, setState] = useState(null);
  const [log, setLog] = useState([]);
  const [svg, setSvg] = useState(null);
  const [bom, setBom] = useState([]);
  const [tab, setTab] = useState("schematic");
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
    if (!runId) return;
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
  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className={`dot ${running ? "live" : state?.done?.ok ? "ok" : ""}`} />
          <h1>Agent PCB run</h1>
          <select value={runId || ""} onChange={(e) => { setFollow(false); setRunId(e.target.value); }}>
            {runs.map((r) => <option key={r.run_id} value={r.run_id}>{r.run_id}</option>)}
          </select>
          <label className="follow"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow latest</label>
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
          </section>

          <section className="panel center">
            <div className="tabs">
              {["schematic", "bom"].map((t) => (
                <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t === "bom" ? "BOM" : "Schematic"}</button>
              ))}
              <span className="spacer" />
              {state?.design && <span className="muted">{state.design.name} · v{state.design_version}</span>}
            </div>
            {tab === "schematic" ? <Schematic svg={svg} /> : <Bom rows={bom} runId={runId} />}
          </section>

          <section className="panel right">
            <h2>Requirements
              <small>{(state?.requirements || []).filter((r) => r.status === "pass").length}/{state?.requirements?.length || 0}</small>
              <a className="dl" href={`/api/runs/${runId}/matrix.csv`}>csv</a>
            </h2>
            <div className="mwrap"><Matrix reqs={state?.requirements || []} /></div>
            <h2>Run log <small>{state?.n_events || 0} events</small></h2>
            <Log events={log} />
          </section>
        </main>
      )}
      {state?.done && (
        <div className={`banner ${state.done.ok ? "ok" : "bad"}`}>{state.done.ok ? "✓" : "✗"} {state.done.summary}</div>
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
  const render = (a, depth) => (
    <li key={a.id}>
      <div className={`agent ${a.status} role-${a.role}`}>
        <span className={`sdot ${a.status}`} title={STATUS_LABEL[a.status] || a.status} />
        <span className="alabel">{a.label}</span>
        {a.steps > 0 && <span className="steps">{a.steps}</span>}
      </div>
      {a.last && <div className="alast">{a.last}</div>}
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
      <h2>Gate</h2>
      {vs.map((v) => (
        <details key={v.verifier} className={`ver ${v.ok ? "ok" : "bad"}`} open={!v.ok}>
          <summary><span>{v.ok ? "✓" : "✗"}</span> {v.verifier} <small>{v.errors?.length ? `${v.errors.length} issue(s)` : "pass"}</small></summary>
          {v.errors?.slice(0, 12).map((e, i) => (
            <div key={i} className={`err ${e.severity}`}><code>{e.code}</code> {e.message}</div>
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

const LOG_KINDS = new Set(["step", "replan", "verifier_result", "constraint_change", "run_done",
  "delegate", "report", "decision", "agent_spawned"]);

function Log({ events }) {
  const ref = useRef(null);
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [events]);
  return (
    <div className="log" ref={ref}>
      {events.filter((e) => LOG_KINDS.has(e.kind)).map((e, i) => (
        <div key={i} className={`ev k-${e.kind} ${e.kind === "verifier_result" ? (e.payload.ok ? "ok" : "bad") : ""}`}>
          <span className="t">{new Date(e.ts * 1000).toLocaleTimeString([], { hour12: false })}</span>
          <span className="who">{e.agent_id}</span>
          <span className="msg">{describe(e)}</span>
        </div>
      ))}
    </div>
  );
}

function describe(e) {
  const p = e.payload;
  switch (e.kind) {
    case "step": return p.message;
    case "agent_spawned": return `spawned ${p.label || ""}`;
    case "replan": return `REPLAN: ${p.reason}${p.changes?.length ? " → " + p.changes.join("; ") : ""}`;
    case "verifier_result": return p.ok ? "PASS" : `FAIL: ${[...new Set((p.errors || []).map((x) => x.code))].join(", ")}`;
    case "constraint_change": return `CONSTRAINT: ${Object.entries(p).filter(([k]) => k !== "ts").map(([k, v]) => `${k}=${v}`).join(" ")}`;
    case "run_done": return `DONE: ${p.summary}`;
    case "delegate": return `→ ${p.to}: delegate ${fmtBudget(p.budget)}`;
    case "report": return `→ ${p.to}: ${p.options?.length || 0} option(s)${p.conflicts?.length ? ", conflicts: " + p.conflicts.join("; ") : ""}${p.needs?.length ? ", needs: " + p.needs.join("; ") : ""}`;
    case "decision": return `→ ${p.to}: ${p.chosen || p.message || "decision"}${p.spawn?.length ? " (spawn " + p.spawn.join(", ") + ")" : ""}`;
    default: return e.kind;
  }
}

const fmtBudget = (b) => (b ? Object.entries(b).map(([k, v]) => `${k}=${v}`).join(" ") : "");
const money = (n, d) => (n == null ? <span className="muted" title="price unknown">?</span> : n.toFixed(d));
const fmt = (n) => (n == null ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 }));
