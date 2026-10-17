import React, { useEffect, useMemo, useState } from "react";

const KW = /\b(module|endmodule|wire|input|output|inout)\b/;

// Verilog-style structural netlist of the current design. Click a reference designator to select the part.
export default function Netlist({ runId, version, sel, onSel }) {
  const [text, setText] = useState("");
  useEffect(() => { fetch(`/api/runs/${runId}/netlist.v`).then((r) => r.text()).then(setText).catch(() => setText("// unavailable")); }, [runId, version]);
  const lines = useMemo(() => text.split("\n"), [text]);
  // lines belonging to the selected instance: from "<MPN> REF (" through ");"
  const hl = useMemo(() => {
    const s = new Set(); if (!sel) return s;
    const start = lines.findIndex((l) => new RegExp(`^\\s+\\w+ ${sel} \\(`).test(l));
    if (start < 0) return s;
    for (let i = start - 1; i < lines.length; i++) { s.add(i); if (/^\s+\);/.test(lines[i]) || (i === start && /\(\);/.test(lines[i]))) break; }
    return s;
  }, [lines, sel]);
  const render = (l) => {
    if (/^\s*\/\//.test(l)) return <span className="v-c">{l}</span>;
    const [code, ...rest] = l.split(/(\/\*.*?\*\/)/);
    const parts = code.split(/(\b[A-Z]{1,3}\d+\b|\b(?:module|endmodule|wire|input|output|inout)\b|\.\w+)/);
    return <>{parts.map((p, i) =>
      /^[A-Z]{1,3}\d+$/.test(p) ? <a key={i} className="v-ref" onClick={() => onSel(p)}>{p}</a>
      : KW.test(p) && p.length < 10 ? <span key={i} className="v-k">{p}</span>
      : /^\.\w+$/.test(p) ? <span key={i} className="v-p">{p}</span> : <React.Fragment key={i}>{p}</React.Fragment>)}
      {rest.map((r, i) => <span key={"c" + i} className={/UNCONNECTED/.test(r) ? "v-bad" : "v-c"}>{r}</span>)}</>;
  };
  return (
    <pre className="netlist">
      {lines.map((l, i) => (
        <div key={i} className={`vl ${hl.has(i) ? "sel" : ""}`}><span className="ln">{i + 1}</span>{render(l)}</div>
      ))}
    </pre>
  );
}
