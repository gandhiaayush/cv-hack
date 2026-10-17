import React, { useRef, useState } from "react";
import { BRAND, TAGLINE, EXAMPLES } from "./brand.js";
import CustomParts from "./CustomParts.jsx";

// ChatGPT-style landing: one big prompt box.
export function Composer({ onSubmit, busy, placeholder, autoFocus }) {
  const [text, setText] = useState("");
  const ta = useRef(null);
  const go = () => { const t = text.trim(); if (t && !busy) { onSubmit(t); setText(""); } };
  const grow = (el) => { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 200) + "px"; };
  return (
    <div className="composer">
      <textarea ref={ta} rows={1} autoFocus={autoFocus} value={text} placeholder={placeholder}
        onChange={(e) => { setText(e.target.value); grow(e.target); }}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); } }} />
      <button className="send" disabled={!text.trim() || busy} onClick={go} title="Send">↑</button>
    </div>
  );
}

export default function Home({ onStart, error, busy }) {
  const [showCustom, setShowCustom] = useState(false);
  return (
    <div className="home">
      <div className="mast"><div className="name">{BRAND}</div><div className="rule" /></div>
      <div className="home-body">
        <h1>What would you like to design?</h1>
        <p className="tag">{TAGLINE}</p>
        <Composer onSubmit={onStart} busy={busy} autoFocus placeholder="Describe the device, its budget and constraints…" />
        <div className="chips">
          {EXAMPLES.map((e) => <button key={e} className="chip2" disabled={busy} onClick={() => onStart(e)}>{e}</button>)}
        </div>
        {error && <div className="err">{error}</div>}
        <div className="chips"><button className="chip2" onClick={() => setShowCustom(true)}>＋ Add a custom MCU or chip</button></div>
        <div className="fine">Designs are demonstrations built from a public parts catalog and prototype rules. They are not regulatory advice.</div>
      </div>
      {showCustom && <CustomParts onClose={() => setShowCustom(false)} />}
    </div>
  );
}
