import React, { useCallback, useEffect, useState } from "react";
import App from "./App.jsx";
import Home from "./Home.jsx";
import Chat from "./Chat.jsx";

// ?run=<id> keeps a workspace across refreshes; #/dashboard is the original follow-latest dashboard.
export default function Root() {
  const q = new URLSearchParams(location.search);
  const [runId, setRunId] = useState(q.get("run"));
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const start = useCallback(async (text) => {
    setBusy(true); setError("");
    try {
      const r = await fetch("/api/start", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ prompt: text }) });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      const { run_id } = await r.json();
      history.replaceState(null, "", `?run=${run_id}`);
      setPrompt(text); setRunId(run_id);
    } catch (e) { setError(String(e.message || e)); }
    setBusy(false);
  }, []);
  const home = () => { history.replaceState(null, "", location.pathname); setRunId(null); setPrompt(""); };

  if (location.hash.startsWith("#/dashboard")) return <App />;
  if (!runId) return <Home onStart={start} error={error} busy={busy} />;
  return (
    <div className="shell">
      <Chat runId={runId} prompt={prompt} busy={busy} onNew={(t) => (typeof t === "string" ? start(t) : home())} />
      <div className="work"><App fixedRunId={runId} /></div>
    </div>
  );
}
