import React, { useMemo } from "react";
import Narration from "./Narration.jsx";

// ---------- concise text helpers ----------
export const clip = (s, n = 90) => {
  const t = String(s ?? "").replace(/\s+/g, " ").trim();
  return t.length > n ? t.slice(0, n - 1) + "…" : t;
};
// "Option A – Keep current power subsystem (core parts only)" -> "Keep current power subsystem"
export const shortName = (s, n = 46) =>
  clip(String(s ?? "").replace(/^\s*option\s*[\w\d]+\s*[-–—:.)]\s*/i, "").replace(/\s*\([^)]*\)\s*$/, ""), n);
export const roleOf = (id) => String(id ?? "").split(":")[0];
const SUB_LABEL = { power: "Power", analog_fe: "Analog front-end", mcu_comms: "MCU / comms", protection: "Protection" };
export const subLabel = (s) => SUB_LABEL[s] || String(s).replace(/_/g, " ");
const money = (v) => (v == null ? null : `$${Number(v).toFixed(2)}`);
const mw = (v) => (v == null ? null : `${Number(v).toFixed(v < 10 ? 1 : 0)} mW`);

// ---------- negotiation model (works for the fake run and A's real agents) ----------
function useNegotiation(state) {
  return useMemo(() => {
    const subs = new Map();
    const get = (s) => {
      if (!subs.has(s)) subs.set(s, { sub: s, rounds: 0, ask: null, budget: null, report: null, chosen: null, objections: [], gate: null });
      return subs.get(s);
    };
    for (const e of state?.negotiation || []) {
      if (e.kind === "delegate") {
        const r = get(e.subsystem ? roleOf(e.subsystem) : roleOf(e.to));
        r.rounds += 1;
        r.ask = (e.constraints || []).join(" · ");
        r.budget = e.budget && Object.keys(e.budget).length ? e.budget : r.budget;
      } else if (e.kind === "report") {
        get(roleOf(e.from)).report = e;
      } else if (e.kind === "decision") {
        const r = get(roleOf(e.to));
        if (e.chosen) r.chosen = e.chosen;
        r.why = e.message;
      }
    }
    const vs = state?.verifiers || {};
    for (const [k, v] of Object.entries(vs)) {
      if (k.startsWith("gate:")) get(k.slice(5)).gate = v;
    }
    const critic = vs.critic;
    for (const err of critic?.errors || []) {
      const m = /\[([^\]]+)\]/.exec(err.message || "");
      if (m && subs.has(m[1])) subs.get(m[1]).objections.push(err);
    }
    return { rows: [...subs.values()], critic };
  }, [state]);
}

function optionGate(o, gate) {
  if (/GATE FAILED/.test(o.notes || "")) return false;
  const passed = gate?.details?.passed;
  if (Array.isArray(passed)) return passed.includes(o.name);
  return null;
}

// ---------- Negotiation tab ----------
export function NegotiationBoard({ state }) {
  const { rows, critic } = useNegotiation(state);
  if (!rows.length) return <div className="empty small">No delegations yet</div>;
  return (
    <div className="nb">
      <div className="nb-legend muted small">
        The mother <b>asks</b> each subsystem agent for options, they <b>report</b> options that pass the gate, the
        mother <b>chooses</b>, and the critic <b>objects</b> if something is wrong.
      </div>
      <table className="nb-table">
        <thead><tr><th>Subsystem</th><th>Asked</th><th>Options reported</th><th>Chosen</th><th>Critic</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.sub}>
              <td className="nb-sub">
                <b>{subLabel(r.sub)}</b>
                {r.rounds > 1 && <div className="pill warn">round {r.rounds}</div>}
              </td>
              <td className="nb-ask" title={r.ask || ""}>
                {r.budget && <div className="small">{Object.entries(r.budget).map(([k, v]) => `${k.replace("_usd", " $").replace("_mw", " mW")} ${v}`).join(" · ")}</div>}
                <div className="muted small">{clip(r.ask, 110) || "—"}</div>
              </td>
              <td>
                <div className="nb-opts">
                  {(r.report?.options || []).map((o, i) => {
                    const ok = optionGate(o, r.gate);
                    const chosen = r.chosen && o.name === r.chosen;
                    return (
                      <div key={i} className={`nb-opt ${chosen ? "chosen" : ""} ${ok === false ? "failed" : ""}`} title={`${o.name}\n${o.notes || ""}`}>
                        <div className="nb-opt-name">{shortName(o.name)}</div>
                        <div className="nb-opt-meta">
                          {money(o.cost) && <span>{money(o.cost)}</span>}
                          {mw(o.power_mw) && <span>{mw(o.power_mw)}</span>}
                          {ok === true && <span className="ok">gate ✓</span>}
                          {ok === false && <span className="bad">gate ✗</span>}
                        </div>
                      </div>
                    );
                  })}
                  {!r.report && <span className="muted small">working…</span>}
                </div>
                {r.report?.needs?.length > 0 && <div className="muted small">needs: {clip(r.report.needs.join("; "), 100)}</div>}
              </td>
              <td className="nb-chosen" title={r.why || ""}>
                {r.chosen ? <><b>{shortName(r.chosen, 40)}</b>{r.why && <div className="muted small">{clip(r.why, 90)}</div>}</> : <span className="muted">—</span>}
              </td>
              <td className="nb-critic">
                {r.objections.length ? r.objections.map((o, i) => (
                  <div key={i} className={`obj ${o.severity === "error" ? "blocker" : "concern"}`} title={o.message}>
                    <span className="sev">{o.severity === "error" ? "blocker" : "concern"}</span> {clip(o.message.replace(/^\S+\s+\[[^\]]+\]\s*/, ""), 80)}
                  </div>
                )) : <span className="muted small">{critic ? "no objections" : "—"}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------- Overview tab ----------
export function Overview({ state }) {
  const { rows, critic } = useNegotiation(state);
  const agents = state?.agents || [];
  const done = state?.done;
  const subs = rows.length;
  const reported = rows.filter((r) => r.report).length;
  const gates = Object.entries(state?.verifiers || {}).filter(([k]) => k.startsWith("gate"));
  const gatesOk = gates.filter(([, v]) => v.ok).length;
  const decided = rows.filter((r) => r.chosen).length;
  const steps = [
    ["Delegate", subs > 0, `${subs} subsystems`],
    ["Reports", subs > 0 && reported === subs, `${reported}/${subs || "?"}`],
    ["Gate", gates.length > 0 && gatesOk === gates.length, gates.length ? `${gatesOk}/${gates.length} pass` : "—"],
    ["Decision", decided > 0, decided ? `${decided} chosen` : "—"],
    ["Critic", critic ? critic.ok : false, critic ? (critic.ok ? "approved" : `${critic.errors.length} objections`) : "—"],
    ["Result", !!done, done ? (done.ok ? "accepted" : "failed") : "running"],
  ];
  const current = steps.findIndex(([, ok]) => !ok);
  const running = agents.filter((a) => a.status === "running").map((a) => a.label);
  const calls = (state?.inference || []).reduce((s, m) => s + m.calls, 0);
  const elapsed = state?.started && state?.updated ? Math.round(state.updated - state.started) : 0;
  const blockers = (critic?.errors || []).filter((e) => e.severity === "error");
  const openBlockers = done?.ok && blockers.length > 0;
  if (openBlockers) { steps[5][1] = false; steps[5][2] = `accepted, ${blockers.length} blockers open`; }
  return (
    <div className="ov">
      <div className={`ov-status ${done ? (openBlockers ? "warn" : done.ok ? "ok" : "bad") : "live"}`}>
        {statusLine(done, openBlockers, blockers.length, running)}
      </div>
      <div className="stepper">
        {steps.map(([name, ok, sub], i) => (
          <div key={name} className={`step ${ok ? "done" : i === current ? (done ? "warn" : "now") : ""}`}>
            <div className="step-dot">{ok ? "✓" : i + 1}</div>
            <div className="step-name">{name}</div>
            <div className="step-sub">{sub}</div>
          </div>
        ))}
      </div>
      <div className="ov-stats">
        <Stat label="Elapsed" value={elapsed ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s` : "—"} />
        <Stat label="Agents" value={agents.length} />
        <Stat label="Crusoe calls" value={calls || "—"} />
        <Stat label="Re-plans" value={(state?.replans || []).length + rows.filter((r) => r.rounds > 1).length} />
        <Stat label="Cost / power" value={state?.totals ? `$${state.totals.cost_usd.toFixed(2)} · ${state.totals.power_mw.toFixed(1)} mW` : "—"} />
      </div>
      {blockers.length > 0 && (
        <div className="ov-card">
          <h3>Critic blockers <small>{blockers.length}</small></h3>
          {blockers.slice(0, 5).map((o, i) => <div key={i} className="obj blocker" title={o.message}><span className="sev">blocker</span> {clip(o.message.replace(/^\S+\s+/, ""), 140)}</div>)}
        </div>
      )}
      {rows.some((r) => r.chosen) && (
        <div className="ov-card">
          <h3>Choices</h3>
          {rows.filter((r) => r.chosen).map((r) => (
            <div key={r.sub} className="ov-choice"><span className="muted">{subLabel(r.sub)}</span><b>{shortName(r.chosen, 60)}</b></div>
          ))}
        </div>
      )}
      <div className="ov-card">
        <h3>Live commentary</h3>
        <Narration />
      </div>
    </div>
  );
}

function statusLine(done, openBlockers, nBlockers, running) {
  if (!done) return running.length ? `● Working: ${running.slice(0, 4).join(", ")}${running.length > 4 ? ` +${running.length - 4} more` : ""}` : "● Starting…";
  const r = done.result || {};
  const t = r.totals || {};
  const bits = [t.cost_usd != null && `$${Number(t.cost_usd).toFixed(2)}`, t.power_mw != null && `${t.power_mw} mW`,
    r.critic_rounds != null && `${r.critic_rounds} critic round${r.critic_rounds === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
  if (openBlockers) return `⚠ Accepted with ${nBlockers} critic blocker${nBlockers > 1 ? "s" : ""} still open${bits ? " · " + bits : ""}`;
  return `${done.ok ? "✓ Accepted" : "✗ Failed"}${bits ? " · " + bits : ""}${!r.totals ? " · " + clip(done.summary, 90) : ""}`;
}

function Stat({ label, value }) {
  return <div className="stat"><div className="stat-v">{value}</div><div className="stat-l">{label}</div></div>;
}

// ---------- what the schematic / board is showing ----------
export function DesignNote({ state }) {
  const src = state?.design_source;
  if (!src) return null;
  const n = state.design_notes || {};
  const title = { reference: "Reference board, before the agents' changes", selection: "Draft from the mother's selection",
    final: "Final selection applied to the reference board" }[src] || src;
  return (
    <div className="design-note">
      <b>{title}</b>
      {src !== "reference" && (
        <span className="muted">
          {" "}· kept {n.kept?.length || 0} · added {n.added?.length || 0} · removed {n.removed?.length || 0}
          {n.unwired?.length ? <span className="warn" title={n.unwired.join("\n")}> · {n.unwired.length} pins still need wiring</span> : null}
        </span>
      )}
    </div>
  );
}
