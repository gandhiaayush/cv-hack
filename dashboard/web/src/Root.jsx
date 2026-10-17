import React, { useCallback, useEffect, useState } from "react";
import App from "./App.jsx";
import Wood from "./Wood.jsx";

// One screen: the workspace with Wood (the assistant) collapsible on the right. ?run=<id> keeps your place; otherwise the newest
// real run (one started from a prompt) is shown. Scripted sample runs never appear.
export default function Root() {
  const [runId, setRunId] = useState(new URLSearchParams(location.search).get("run"));
  const [busy, setBusy] = useState(false);
  const [shapeTick, setShapeTick] = useState(0);
  const [open, setOpenState] = useState(() => { try { return localStorage.getItem("wood-open") !== "0"; } catch { return true; } });
  const setOpen = (v) => { setOpenState(v); try { localStorage.setItem("wood-open", v ? "1" : "0"); } catch {} };
  useEffect(() => {
    if (runId) return;
    fetch("/api/real_runs").then((r) => r.json()).then((rs) => { if (rs.length) setRunId(rs[0].run_id); }).catch(() => {});
  }, [runId]);
  const start = useCallback(async (text, instructions) => {
    setBusy(true);
    try {
      const r = await fetch("/api/start", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ prompt: text, instructions: instructions || "" }) });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      const { run_id } = await r.json();
      history.replaceState(null, "", `?run=${run_id}`);
      setRunId(run_id);
    } catch (e) { alert(e.message); }
    setBusy(false);
  }, []);
  return (
    <div className="shell3" style={{ gridTemplateColumns: `minmax(0, 1fr) ${open ? "340px" : "46px"}` }}>
      <div className="work"><App fixedRunId={runId || "__none__"} shapeTick={shapeTick} /></div>
      <Wood runId={runId} open={open} setOpen={setOpen} busy={busy} onStart={start} onShape={() => setShapeTick((t) => t + 1)} />
    </div>
  );
}
