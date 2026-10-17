import React, { useEffect, useRef, useState } from "react";

const j = (r) => (r.ok ? r.json() : r.json().then((e) => Promise.reject(new Error(e.detail || r.statusText))));
const usd = (n) => (n == null ? "" : `$${Number(n).toFixed(2)}`);
const SHAPE = /\b(round|circle|circular|oval|ellips\w*|capsule|pill|octagon\w*|hexagon\w*|rounded)\b/i;
const STOCK = /out of stock|unavailable|discontinued|can'?t (get|buy)/i;

// The assistant: a side panel of bubbles. Type a new design, or a change ("make the board round", "U5 is out of stock").
export default function PromptBar({ runId, onStart, onShape, busy }) {
  const real = runId && runId !== "__none__";
  const [text, setText] = useState("");
  const [lines, setLines] = useState([]);
  const [spec, setSpec] = useState(null);
  const [result, setResult] = useState(null);
  const [totals, setTotals] = useState(null);
  const [mine, setMine] = useState([]);            // what you typed and what the assistant answered locally
  const end = useRef(null);

  useEffect(() => {
    setLines([]); setSpec(null); setResult(null); setMine([]);
    if (!real) return;
    const es = new EventSource("/api/narration/stream");
    es.onmessage = (m) => { const l = JSON.parse(m.data); setLines((c) => (l.id === 0 ? [l] : [...c.filter((x) => x.id !== l.id), l])); };
    return () => es.close();
  }, [runId]);
  useEffect(() => {
    if (!real) return;
    let alive = true;
    const tick = () => {
      fetch(`/api/runs/${runId}/spec`).then(j).then((s) => alive && s && setSpec(s)).catch(() => {});
      fetch(`/api/runs/${runId}/result`).then(j).then((r) => alive && r && setResult(r)).catch(() => {});
      fetch(`/api/runs/${runId}/state`).then(j).then((s) => alive && setTotals(s.totals || null)).catch(() => {});
    };
    tick(); const t = setInterval(tick, 4000);
    return () => { alive = false; clearInterval(t); };
  }, [runId]);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [lines, mine, result]);

  const say = (who, t) => setMine((m) => [...m, { who, t, at: Date.now() }]);
  const post = (url, body) => fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then(j);
  const go = async () => {
    const t = text.trim(); if (!t || busy) return;
    setText(""); say("me", t);
    try {
      if (real && STOCK.test(t)) {
        const rows = (await fetch(`/api/runs/${runId}/bom2`).then(j).catch(() => ({ rows: [] }))).rows;
        const ref = (t.match(/\b([A-Z]{1,3}\d+)\b/) || [])[1];
        const row = rows.find((r) => (ref && r.refs.split(" ").includes(ref)) || t.toUpperCase().includes(r.mpn.toUpperCase()));
        if (!row) return say("ai", "I couldn't tell which part you mean. Name it, like “U5 is out of stock”.");
        await post(`/api/runs/${runId}/control`, { type: "out_of_stock", mpn: row.mpn });
        say("ai", `Told the team ${row.name} is out of stock. It will swap it for another part.`);
      } else if (real && SHAPE.test(t)) {
        say("ai", "Reshaping the board…");
        const sp = await post(`/api/runs/${runId}/shape`, { text: t });
        onShape && onShape();
        const b = await fetch(`/api/runs/${runId}/board.json`).then(j).catch(() => null);
        const o = b?.outline;
        say("ai", o ? `The board is now ${sp.kind.replace("_", " ")}, ${o.width_mm} × ${o.height_mm} mm.${o.fits ? "" : " " + o.problems.join("; ") + "."}` : sp.note);
      } else {
        onStart(t);
      }
    } catch (e) { say("ai", `That didn't work: ${e.message}`); }
  };

  const done = result && result.selections;
  const feed = [...lines.map((l) => ({ who: "ai", t: l.text, at: l.ts * 1000, tone: l.tone })), ...mine].sort((a, b) => a.at - b.at);
  return (
    <aside className="ai">
      <div className="ai-h">Assistant</div>
      <div className="ai-body">
        {!real && <div className="b ai">What would you like to design? Describe the device, its budget and any limits.</div>}
        {spec && <div className="b meta">{spec.device} · {usd(spec.cost_usd)} · {spec.power_mw} mW{totals ? ` · now ${usd(totals.cost_usd)}` : ""}{!spec.in_scope && <div className="scope">{spec.scope_note}</div>}</div>}
        {feed.map((m, i) => <div key={i} className={`b ${m.who} ${m.tone || ""}`}>{m.t}</div>)}
        {real && !result && <div className="b ai"><span className="typing" /></div>}
        {done && <div className="b ai done">Done: {result.bom?.length} parts{totals ? `, ${usd(totals.cost_usd)}` : ""}.</div>}
        {result && !done && <div className="b ai warn">The team couldn't finish a design this time.</div>}
        <div ref={end} />
      </div>
      <div className="ai-in"><input id="ai-input" value={text} placeholder={real ? "New design, or a change…" : "Describe a device…"} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && go()} />
        <button className="send" disabled={!text.trim() || busy} onClick={go}>↑</button></div>
    </aside>
  );
}
