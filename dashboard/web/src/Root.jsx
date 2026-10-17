import React, { useCallback, useEffect, useState } from "react";
import App from "./App.jsx";
import PromptBar from "./PromptBar.jsx";

// One screen: Fusion-style workspace with the assistant on the side. ?run=<id> keeps your place; otherwise the newest
// real run (one started from a prompt) is shown. Scripted sample runs never appear.
export default function Root() {
  const [runId, setRunId] = useState(new URLSearchParams(location.search).get("run"));
  const [busy, setBusy] = useState(false);
  const [shapeTick, setShapeTick] = useState(0);
  useEffect(() => {
    if (runId) return;
    fetch("/api/real_runs").then((r) => r.json()).then((rs) => { if (rs.length) setRunId(rs[0].run_id); }).catch(() => {});
  }, [runId]);
  const start = useCallback(async (text) => {
    setBusy(true);
    try {
      const r = await fetch("/api/start", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ prompt: text }) });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      const { run_id } = await r.json();
      history.replaceState(null, "", `?run=${run_id}`);
      setRunId(run_id);
    } catch (e) { alert(e.message); }
    setBusy(false);
  }, []);
  return (
    <div className="shell3">
      <div className="work"><App fixedRunId={runId || "__none__"} shapeTick={shapeTick} /></div>
      <PromptBar runId={runId} busy={busy} onStart={start} onShape={() => setShapeTick((t) => t + 1)} />
    </div>
  );
}
