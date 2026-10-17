import React, { useEffect, useRef, useState } from "react";

const j = (r) => (r.ok ? r.json() : r.json().then((e) => Promise.reject(new Error(e.detail || r.statusText))));
const usd = (n) => (n == null ? "" : `$${Number(n).toFixed(2)}`);
const title = (s) => (s || "").replace(/\b\w/g, (c) => c.toUpperCase());
const SHAPE = /\b(round|circle|circular|oval|ellips\w*|capsule|pill|octagon\w*|hexagon\w*|rounded)\b/i;
const STOCK = /out of stock|unavailable|discontinued|can'?t (get|buy)/i;

// A log seen end-on: bark, growth rings, heartwood.
export const WoodLogo = ({ size = 22 }) => (
  <svg width={size} height={size} viewBox="0 0 32 32" aria-label="Wood">
    <circle cx="16" cy="16" r="14" fill="#8a5a2b" /><circle cx="16" cy="16" r="11.5" fill="#d9a766" />
    <circle cx="16" cy="16" r="8.6" fill="none" stroke="#a9733a" strokeWidth="1.1" /><circle cx="16" cy="16" r="5.6" fill="none" stroke="#a9733a" strokeWidth="1.1" />
    <circle cx="16" cy="16" r="2.4" fill="#8a5a2b" /><path d="M16 16 L26 11" stroke="#8a5a2b" strokeWidth=".8" opacity=".5" />
  </svg>
);

// Wood: the assistant. A collapsible side panel. Type a new design, or a change ("make the board round",
// "U5 is out of stock"). It never forces the scroll position once you scroll up to read.
export default function Wood({ runId, open, setOpen, onStart, onShape, busy }) {
  const real = runId && runId !== "__none__";
  const [text, setText] = useState("");
  const [rules, setRules] = useState("");
  const [showRules, setShowRules] = useState(false);
  const [lines, setLines] = useState([]);
  const [spec, setSpec] = useState(null);
  const [result, setResult] = useState(null);
  const [totals, setTotals] = useState(null);
  const [mine, setMine] = useState([]);
  const body = useRef(null);
  const stick = useRef(true);

  useEffect(() => {
    setLines([]); setSpec(null); setResult(null); setMine([]); stick.current = true;
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
  // Follow new messages only while you are at the bottom; scrolling up stops it until you scroll back down.
  const onScroll = () => { const el = body.current; if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; };
  useEffect(() => { const el = body.current; if (el && stick.current) el.scrollTop = el.scrollHeight; }, [lines, mine, result, open]);

  const say = (who, t) => { stick.current = true; setMine((m) => [...m, { who, t, at: Date.now() }]); };
  const post = (url, b) => fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(b) }).then(j);
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
        onStart(t, rules.trim());
      }
    } catch (e) { say("ai", `That didn't work: ${e.message}`); }
  };

  if (!open) {
    return (
      <aside className="wood closed" onClick={() => setOpen(true)} title="Open Wood">
        <WoodLogo size={26} /><span className="vt">Wood</span><span className="chev">‹</span>
      </aside>
    );
  }
  const done = result && result.selections;
  const feed = [...lines.map((l) => ({ who: "ai", t: l.text, at: l.ts * 1000, tone: l.tone })), ...mine].sort((a, b) => a.at - b.at);
  return (
    <aside className="wood">
      <div className="wood-h"><WoodLogo /><b>Wood</b><span className="grow" /><button className="icon" onClick={() => setOpen(false)} title="Collapse Wood">›</button></div>
      <div className="wood-body" ref={body} onScroll={onScroll}>
        {!real && <div className="b ai">What would you like to design? Describe the device, its budget and any limits.</div>}
        {spec && <div className="b meta">{title(spec.device)} · {usd(spec.cost_usd)} · {spec.power_mw} mW{totals ? ` · Now ${usd(totals.cost_usd)}` : ""}{!spec.in_scope && <div className="scope">{spec.scope_note}</div>}</div>}
        {feed.map((m, i) => <div key={i} className={`b ${m.who} ${m.tone || ""}`}>{m.t}</div>)}
        {real && !result && <div className="b ai"><span className="typing" /></div>}
        {done && <div className="b ai done">Done: {result.bom?.length} parts{totals ? `, ${usd(totals.cost_usd)}` : ""}.</div>}
        {result && !done && <div className="b ai warn">The team couldn't finish a design this time.</div>}
      </div>
      {showRules && <textarea className="rules" value={rules} placeholder="Your own rules for the team. For example: “Prefer Nordic chips. Keep the board under 30 mm wide.” They apply to your next new design." onChange={(e) => setRules(e.target.value)} />}
      <div className="wood-in">
        <button className={`icon ${rules ? "on" : ""}`} title="Your Own Instructions For The Team" onClick={() => setShowRules(!showRules)}>✎</button>
        <input id="wood-input" value={text} placeholder={real ? "New Design, Or A Change…" : "Describe A Device…"} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && go()} />
        <button className="send" disabled={!text.trim() || busy} onClick={go}>↑</button>
      </div>
    </aside>
  );
}
