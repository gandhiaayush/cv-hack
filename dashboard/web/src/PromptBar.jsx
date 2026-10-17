import React, { useEffect, useRef, useState } from "react";
import { BRAND } from "./brand.js";
import CustomParts from "./CustomParts.jsx";

const j = (r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText)));
const usd = (n) => (n == null ? "?" : `$${Number(n).toFixed(2)}`);

// The AI is a text bubble at the top, like starting a new file: type a design, see the latest thing the team did.
export default function PromptBar({ runId, onStart, busy }) {
  const [text, setText] = useState("");
  const [lines, setLines] = useState([]);
  const [spec, setSpec] = useState(null);
  const [result, setResult] = useState(null);
  const [more, setMore] = useState(false);
  const [custom, setCustom] = useState(false);
  const hist = useRef(null);

  useEffect(() => {
    setLines([]); setSpec(null); setResult(null);
    const es = new EventSource("/api/narration/stream");
    es.onmessage = (m) => { const l = JSON.parse(m.data); setLines((c) => (l.id === 0 ? [l] : [...c.filter((x) => x.id !== l.id), l])); };
    return () => es.close();
  }, [runId]);
  useEffect(() => {
    let alive = true;
    const tick = () => {
      fetch(`/api/runs/${runId}/spec`).then(j).then((s) => alive && s && setSpec(s)).catch(() => {});
      fetch(`/api/runs/${runId}/result`).then(j).then((r) => alive && r && setResult(r)).catch(() => {});
    };
    tick(); const t = setInterval(() => { if (!result) tick(); }, 3000);
    return () => { alive = false; clearInterval(t); };
  }, [runId, result]);
  useEffect(() => { if (more && hist.current) hist.current.scrollTop = hist.current.scrollHeight; }, [lines, more]);

  const go = () => { const t = text.trim(); if (t && !busy) { onStart(t); setText(""); } };
  const last = lines[lines.length - 1];
  const done = result && result.selections;
  return (
    <div className="pbar">
      <div className="pbar-row">
        <span className="brand">{BRAND}</span>
        <div className="pill-input">
          <button className="newfile" title="New design" onClick={() => document.getElementById("pbar-input")?.focus()}>＋</button>
          <input id="pbar-input" value={text} placeholder="New design: describe the device, budget and constraints…" onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && go()} />
          <button className="send" disabled={!text.trim() || busy} onClick={go}>↑</button>
        </div>
        <button onClick={() => setCustom(true)}>＋ Custom MCU / chip</button>
      </div>
      <div className="bubble">
        <div className="bubble-main">
          {spec && <span className="chip3">{spec.device} · {usd(spec.cost_usd)} · {spec.power_mw} mW</span>}
          <span className="msg">{done ? `Design ${result.status.replace(/_/g, " ")}: ${usd(result.totals?.cost_usd)}, ${result.bom?.length} parts, ${result.critic_rounds} review round(s).`
            : result ? `Run ended without an accepted design (${result.status}).` : last ? last.text : "Getting the team started…"}
            {!result && <span className="typing" />}</span>
          {lines.length > 1 && <button className="lnk" onClick={() => setMore(!more)}>{more ? "Hide" : `History (${lines.length})`}</button>}
        </div>
        {more && <div className="bubble-hist" ref={hist}>{lines.map((l) => <p key={l.id} className={l.tone}>{l.text}</p>)}</div>}
        {spec && !spec.in_scope && <div className="scope">{spec.scope_note}</div>}
      </div>
      {custom && <CustomParts onClose={() => setCustom(false)} />}
    </div>
  );
}
