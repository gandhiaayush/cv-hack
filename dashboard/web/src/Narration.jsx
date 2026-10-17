import React, { useEffect, useRef, useState } from "react";

const TONE = { info: "var(--muted, #8a93a6)", good: "#3fb950", warn: "#d29922" };

// Plain-English live commentary, streamed from /api/narration/stream.
export default function Narration() {
  const [lines, setLines] = useState([]);
  const end = useRef(null);
  useEffect(() => {
    const es = new EventSource("/api/narration/stream");
    es.onmessage = (m) => {
      const l = JSON.parse(m.data);
      setLines((cur) => (l.id === 0 ? [l] : [...cur.filter((x) => x.id !== l.id), l].slice(-40)));
    };
    return () => es.close();
  }, []);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [lines]);
  return (
    <div className="narration" style={{ maxHeight: 260, overflow: "auto", marginBottom: 10 }}>
      {lines.length === 0 && <div className="muted" style={{ fontSize: 12 }}>Commentary appears when a run starts…</div>}
      {lines.map((l) => (
        <p key={l.id} style={{ margin: "0 0 8px", fontSize: 13, lineHeight: 1.4, paddingLeft: 8, borderLeft: `3px solid ${TONE[l.tone] || TONE.info}` }}>
          {l.text}
        </p>
      ))}
      <div ref={end} />
    </div>
  );
}
