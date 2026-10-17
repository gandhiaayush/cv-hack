import React, { useEffect, useRef, useState } from "react";
import { BRAND } from "./brand.js";
import { Composer } from "./Home.jsx";
import Lessons from "./Lessons.jsx";

const j = (r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText)));
const usd = (n) => (n == null ? "?" : `$${Number(n).toFixed(2)}`);

// Left column: your prompt, the run brief, the live commentary as it streams, then the final answer.
export default function Chat({ runId, prompt, onNew, busy }) {
  const [lines, setLines] = useState([]);
  const [spec, setSpec] = useState(null);
  const [result, setResult] = useState(null);
  const end = useRef(null);

  useEffect(() => {
    setLines([]); setSpec(null); setResult(null);
    const es = new EventSource("/api/narration/stream");
    es.onmessage = (m) => {
      const l = JSON.parse(m.data);
      setLines((cur) => (l.id === 0 ? [l] : [...cur.filter((x) => x.id !== l.id), l]));
    };
    return () => es.close();
  }, [runId]);

  useEffect(() => {
    let alive = true;
    const tick = () => {
      fetch(`/api/runs/${runId}/spec`).then(j).then((s) => alive && s && setSpec(s)).catch(() => {});
      fetch(`/api/runs/${runId}/result`).then(j).then((r) => { if (alive && r) setResult(r); }).catch(() => {});
    };
    tick();
    const t = setInterval(() => { if (!result) tick(); }, 3000);
    return () => { alive = false; clearInterval(t); };
  }, [runId, result]);

  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [lines, spec, result]);

  const running = !result;
  return (
    <aside className="chat">
      <div className="chat-head">
        <span className="name">{BRAND}</span>
        <button onClick={onNew}>＋ New design</button>
      </div>
      <div className="chat-body">
        <div className="bubble-u">{prompt || spec?.request}</div>
        {spec && (
          <div className="brief">
            <b>Brief</b> · {spec.device} · budget {usd(spec.cost_usd)} / {spec.power_mw} mW
            {!spec.in_scope && <div className="scope">{spec.scope_note}</div>}
          </div>
        )}
        {lines.length === 0 && running && <div className="msg-a">Getting the team started<span className="typing" /></div>}
        {lines.map((l) => <div key={l.id} className={`msg-a ${l.tone === "warn" ? "warn" : l.tone === "good" ? "good" : ""}`}>{l.text}</div>)}
        {running && lines.length > 0 && <div className="msg-a"><span className="typing" /></div>}
        {result && <Final result={result} />}
        {result && <Lessons runId={runId} result={result} />}
        <div ref={end} />
      </div>
      <div className="chat-foot">
        <Composer onSubmit={onNew && ((t) => onNew(t))} busy={busy} placeholder="Describe another design…" />
      </div>
    </aside>
  );
}

function Final({ result }) {
  if (!result.selections) {
    return <div className="final"><h3>No accepted design</h3>
      <div className="flag">The run ended without a design that passed the checks ({result.status || "unknown"}). The log shows where it stopped.</div></div>;
  }
  const label = { accepted: "Design accepted", accepted_with_contested_blockers: "Design accepted, with contested objections",
    accepted_with_unresolved_objections: "Design accepted, with unresolved objections", accepted_UNREVIEWED_critic_failed: "Design produced, not independently reviewed" }[result.status] || result.status;
  return (
    <div className="final">
      <h3>{label}</h3>
      <ul>{Object.entries(result.selections).map(([s, o]) => <li key={s}><b>{s.replace("_", " ")}</b>: {o}</li>)}</ul>
      <div>{usd(result.totals?.cost_usd)} · {result.totals?.power_mw} mW · {result.bom?.length} parts · {result.critic_rounds} review round(s)</div>
      {result.contested_blockers?.length > 0 && (
        <div className="flag">{result.contested_blockers.length} serious reviewer objection(s) were rebutted by the design agent, not fixed. Treat as open.</div>
      )}
    </div>
  );
}
